"""Three consistency bugs behind the [FILTER Y] drift card (2026-10-22).

1. cache.json is one file read-modified-written by many paths; two writers
   at once lost one write (the listing sync of a re-read inbox vanished).
   Every writer now holds the one cache lock across its read and write.
2. Our own writes moved a playlist's track-cache snapshot but not the
   listing's, so the next profile rebuild saw a mismatch, re-read the
   playlist and labelled the fresh copy with the listing's STALE snapshot.
   `_apply_snapshot` now moves both.
3. The first profile build after a restart: a concurrent caller was handed
   the empty state and failed with KeyError: 'playlists'. It now waits.

Zero real Spotify calls."""
import threading
import time

import pytest

from sortify import app as appmod
from sortify import spotify as spmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)


@pytest.fixture
def cache_saved():
    original = appmod.store.cache()
    yield
    appmod.store.save_cache(original)
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0


def seed(listing_snap="s-old", entry_snap="s-old"):
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 1.0, "items": [
        {"id": "fy", "name": "[FILTER Y]", "owner": "me", "editable": True,
         "total": 1, "snapshot_id": listing_snap, "image": None, "description": ""}]}
    cache["playlists"] = {"fy": {"snapshot_id": entry_snap, "fetched_at": 1.0,
                                 "tracks": [{"uri": "spotify:track:z", "name": "Z"}]}}
    appmod.store.save_cache(cache)


def test_our_own_write_moves_the_listing_snapshot_too(cache_saved):
    seed()

    appmod._apply_snapshot("fy", "s-new")

    cache = appmod.store.cache()
    assert cache["playlists"]["fy"]["snapshot_id"] == "s-new"
    assert cache["playlist_list"]["items"][0]["snapshot_id"] == "s-new"


def test_a_writer_holding_the_cache_lock_is_never_overwritten(cache_saved, monkeypatch):
    # A slow writer (the listing sync) holds the lock; a track-cache write
    # that started at the same time must wait and then build on its result,
    # not on the copy it would have read before.
    seed()
    started = threading.Event()

    def slow_sync():
        with spmod._LIST_LOCK:
            cache = appmod.store.cache()
            started.set()
            time.sleep(0.2)
            cache["playlist_list"]["items"][0]["snapshot_id"] = "s-synced"
            appmod.store.save_cache(cache)

    t = threading.Thread(target=slow_sync)
    t.start()
    started.wait(2)
    monkeypatch.setattr(appmod.sp, "playlist_tracks", lambda pid, *a, **k: [{"uri": "spotify:track:n", "name": "N"}])
    appmod._cached_tracks("other", "s-o")
    t.join()

    cache = appmod.store.cache()
    assert cache["playlist_list"]["items"][0]["snapshot_id"] == "s-synced"
    assert cache["playlists"]["other"]["snapshot_id"] == "s-o"


def test_the_cache_lock_can_be_taken_again_by_its_holder():
    # app.py's writers call each other (a move syncs the listing); a plain
    # Lock would deadlock there.
    with spmod._LIST_LOCK:
        with spmod._LIST_LOCK:
            pass


def test_a_caller_during_the_first_build_waits_for_it(cache_saved, monkeypatch):
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    building = threading.Event()

    def slow_build():
        building.set()
        time.sleep(0.3)
        return {"built_at": time.time(), "profiles": {"h": {}}, "homes": [], "inputs": [],
                "playlists": [], "input_ids": set(), "playlist_artists": {}, "last_added": {}}

    monkeypatch.setattr(appmod, "_build_profiles", slow_build)
    first = threading.Thread(target=appmod._ensure_profiles)
    first.start()
    building.wait(2)

    state = appmod._ensure_profiles()

    first.join()
    assert "playlists" in state and state["profiles"] == {"h": {}}


def test_a_failed_first_build_does_not_leave_waiters_hanging(cache_saved, monkeypatch):
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    building = threading.Event()

    def broken_build():
        building.set()
        time.sleep(0.2)
        raise RuntimeError("listing unavailable")

    monkeypatch.setattr(appmod, "_build_profiles", broken_build)
    first = threading.Thread(target=lambda: pytest.raises(RuntimeError, appmod._ensure_profiles))
    first.start()
    building.wait(2)

    t0 = time.time()
    with pytest.raises(appmod.HTTPException) as exc:
        appmod._ensure_profiles()
    first.join()
    assert exc.value.status_code == 503 and time.time() - t0 < 5
