"""Bounded on-demand Last.fm tag fetch for unknown now-playing artists.

`_fetch_missing_now_tags` is the only place `/api/now` may ever call Last.fm,
and it may only run from the `?force=1` branch — explicit user action
(opening or refocusing the view), never the passive poll. This mirrors
test_now_polling.py's route-level style (a trapped Spotify client, an
isolated cache/config snapshot) but adds a trapped Last.fm client instead of
counting Spotify calls.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from sortify.app import NOW_FETCH_MAX_ARTISTS, _fetch_missing_now_tags, _merge_save_tag_artists
from sortify.store import Store
from sortify.tags import ArtistTags, LastFmError

TRACK_MS = 210_000


def track_with_artists(artist_ids):
    return {
        "uri": "spotify:track:x", "id": "x", "name": "X", "type": "track",
        "is_local": False, "duration_ms": TRACK_MS,
        "artists": [{"id": aid, "name": f"Artist {aid}"} for aid in artist_ids],
        "album": "Alb", "image": None,
    }


RAW_CURRENTLY_PLAYING = {
    "item": {
        "uri": "spotify:track:x", "id": "x", "name": "X", "type": "track",
        "is_local": False, "duration_ms": TRACK_MS,
        "artists": [{"id": "a1", "name": "A"}, {"id": "a2", "name": "B"},
                    {"id": "a3", "name": "C"}, {"id": "a4", "name": "D"}],
        "album": {"name": "Alb", "images": [{"url": "http://img/1"}]},
    },
    "is_playing": True,
    "progress_ms": 1_000,
    "context": {"type": "playlist", "uri": "spotify:playlist:CTX1"},
}

NOW_LISTING = [{"id": "CTX1", "name": "Some playlist", "owner": "me", "editable": False,
                "total": 3, "snapshot_id": "snap-ctx1", "image": None}]


@pytest.fixture(autouse=True)
def _isolate_tags_json():
    """tags.json is shared across the whole test session (see conftest.py's
    module-wide data dir) — every test in this file writes to it, so restore
    whatever was there before regardless of which test ran."""
    original = Store().tags()
    yield
    Store().save_tags(original)


class RaisingFm:
    """Stands in for a Last.fm client that must never be touched."""

    def top_tags(self, name):
        raise AssertionError(f"Last.fm must not be called for the passive poll (got {name!r})")


class FakeFm:
    """Records every artist name it was asked about."""

    def __init__(self, tags_by_name=None, error_by_name=None):
        self.tags_by_name = tags_by_name or {}
        self.error_by_name = error_by_name or {}
        self.calls = []

    def top_tags(self, name):
        self.calls.append(name)
        if name in self.error_by_name:
            raise self.error_by_name[name]
        got = self.tags_by_name.get(name)
        if got is None:
            return None
        from sortify.tags import ArtistTags
        return ArtistTags(matched_name=name, tags=got)


@pytest.fixture
def isolated_now(monkeypatch):
    """Same isolation as test_now_polling.py's route_client: a clean cache /
    config / splits / tags snapshot restored after the test, and the shared
    profile cache reset so it always rebuilds off this test's fixtures."""
    s = Store()
    original_cache, original_config = s.cache(), s.config()
    original_splits, original_tags = s.splits(), s.tags()

    cache = s.cache()
    cache["playlist_list"] = {"fetched_at": 0.0, "items": NOW_LISTING}
    s.save_cache(cache)
    s.save_config({**original_config, "input_ids": [], "home_ids": [],
                   "input_name_pattern": r"^\[.+\]$"})
    s.save_tag_artists({})
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0

    calls = []

    def trap(method, path, background=False, **kwargs):
        calls.append((method, path))
        return RAW_CURRENTLY_PLAYING

    for name in ("currently_playing", "my_playlists", "playlist_tracks"):
        if name in vars(appmod.sp):
            monkeypatch.delattr(appmod.sp, name)
    monkeypatch.setattr(appmod.sp, "request", trap)
    appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)

    c = TestClient(appmod.app)
    c.spotify_calls = calls
    try:
        yield c
    finally:
        s = Store()
        s.save_cache(original_cache)
        s.save_config(original_config)
        s.save_splits(original_splits)
        s.save_tags(original_tags)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)


# ---- the unit: _fetch_missing_now_tags -------------------------------------


def test_no_key_means_no_client_and_no_fetch(monkeypatch):
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)

    def boom():
        raise AssertionError("LastFm must not be constructed without a key")

    monkeypatch.setattr(appmod, "LastFm", boom)
    n = _fetch_missing_now_tags(track_with_artists(["a1"]))
    assert n == 0


def test_fetches_at_most_three_unknown_artists(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(tags_by_name={f"Artist {i}": [{"name": "rock", "count": 50}]
                              for i in ("a1", "a2", "a3", "a4")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1", "a2", "a3", "a4"]))

    assert n == NOW_FETCH_MAX_ARTISTS == 3
    assert len(fm.calls) == 3
    saved = Store().tag_artists()
    assert len(saved) == 3
    for aid in list(saved):
        assert saved[aid]["tags"] == [{"name": "rock", "count": 50}]
        assert saved[aid]["miss"] is False


def test_known_artists_including_misses_are_not_refetched(monkeypatch):
    Store().save_tag_artists({
        "a1": {"name": "Artist a1", "lastfm_name": None, "tags": [],
               "fetched_at": "2026-08-17T00:00:00Z", "miss": True},
        "a2": {"name": "Artist a2", "lastfm_name": "Artist a2",
               "tags": [{"name": "jazz", "count": 20}],
               "fetched_at": "2026-08-17T00:00:00Z", "miss": False},
    })
    fm = FakeFm()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1", "a2"]))

    assert n == 0
    assert fm.calls == []


def test_non_code_6_error_leaves_the_artist_absent(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(error_by_name={"Artist a1": LastFmError("Last.fm error 29: rate limited")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert n == 0
    assert "a1" not in Store().tag_artists()


def test_code_6_not_found_is_recorded_as_a_permanent_miss(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(tags_by_name={"Artist a1": None})  # None -> "not found"
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert n == 1
    saved = Store().tag_artists()
    assert saved["a1"]["miss"] is True


def test_a_partial_batch_persists_what_succeeded_before_the_error(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(
        tags_by_name={"Artist a1": [{"name": "rock", "count": 50}]},
        error_by_name={"Artist a2": LastFmError("Last.fm error 8: service offline")},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1", "a2"]))

    saved = Store().tag_artists()
    assert "a1" in saved
    assert "a2" not in saved
    assert n == 1


# ---- the route: only ?force=1 may ever fetch --------------------------------


def test_passive_poll_never_touches_last_fm(isolated_now, monkeypatch):
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: RaisingFm())
    r = isolated_now.get("/api/now")
    assert r.status_code == 200
    assert r.json()["playing"] is True


def test_forced_poll_fetches_and_persists_unknown_artists(isolated_now, monkeypatch):
    fm = FakeFm(tags_by_name={n: [{"name": "rock", "count": 50}]
                              for n in ("A", "B", "C", "D")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    r = isolated_now.get("/api/now?force=1")

    assert r.status_code == 200
    assert sorted(fm.calls) == ["A", "B", "C"]  # capped at 3, first-seen order
    saved = Store().tag_artists()
    assert {"a1", "a2", "a3"} <= set(saved)
    assert "a4" not in saved


def test_forced_poll_with_no_key_returns_200_with_no_fetch(isolated_now, monkeypatch):
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)
    r = isolated_now.get("/api/now?force=1")
    assert r.status_code == 200
    assert Store().tag_artists() == {}


def test_forced_poll_survives_a_last_fm_failure(isolated_now, monkeypatch):
    fm = FakeFm(error_by_name={
        n: LastFmError("Last.fm error 29: rate limited") for n in ("A", "B", "C", "D")
    })
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    r = isolated_now.get("/api/now?force=1")

    assert r.status_code == 200
    assert Store().tag_artists() == {}


def test_a_fetched_artist_is_visible_to_the_very_next_request(isolated_now, monkeypatch):
    """Task 1's ruling: suggestions read tag_artists guard-on-read, not off a
    profile-build-time snapshot — a fetch must be visible without a restart
    and without waiting out PROFILE_TTL."""
    fm = FakeFm(tags_by_name={n: [{"name": "rock", "count": 50}]
                              for n in ("A", "B", "C", "D")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    isolated_now.get("/api/now?force=1")
    fm.calls.clear()
    # Next request, passive: must see the freshly-written tags without
    # calling Last.fm again (write-once) and without a process restart.
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: RaisingFm())
    r = isolated_now.get("/api/now")

    assert r.status_code == 200
    assert Store().tag_artists()["a1"]["tags"] == [{"name": "rock", "count": 50}]


# ---- concurrency: fix round 1, Important 1 ---------------------------------
#
# FastAPI's sync routes run in a threadpool, so two overlapping `?force=1`
# requests genuinely execute concurrently — not a hypothetical. Real threads,
# a gated fake client, and a non-blocking `threading.Lock` around the whole
# fetch: the second overlapping caller must see the lock held and back off
# immediately (fetching nothing this round) rather than either blocking or
# racing the first caller's Last.fm call for the same artist.


def test_concurrent_force_fetches_never_double_call_last_fm(monkeypatch):
    Store().save_tag_artists({})
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class GatedFm:
        def top_tags(self, name):
            calls.append(name)
            entered.set()
            # Held open long enough for the second, overlapping call to run
            # its own (non-blocking) lock attempt while this one is in flight.
            release.wait(timeout=2)
            return ArtistTags(matched_name=name, tags=[{"name": "rock", "count": 50}])

    monkeypatch.setattr(appmod, "_lastfm_client", lambda: GatedFm())

    results: list[int] = []

    def run():
        results.append(_fetch_missing_now_tags(track_with_artists(["a1"])))

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)

    t1.start()
    assert entered.wait(timeout=2), "first fetch never reached Last.fm"
    t2.start()
    t2.join(timeout=2)  # non-blocking lock: must return promptly, fetching nothing
    release.set()
    t1.join(timeout=2)

    assert not t1.is_alive() and not t2.is_alive()
    assert calls == ["Artist a1"]  # never called twice for the same artist
    assert sorted(results) == [0, 1]  # one fetched, one backed off
    assert Store().tag_artists()["a1"]["tags"] == [{"name": "rock", "count": 50}]


# ---- lost-update guard: fix round 1, Important 2 ---------------------------


def test_merge_save_does_not_lose_a_concurrent_writers_work():
    """The fetch path (and the split flow's enrich walk) reads tags.json,
    does slow network work, then saves — a classic read-modify-write race.
    A writer landing in between must not be clobbered by the other's save."""
    Store().save_tag_artists({})

    # This call's own work, computed against a since-gone-stale snapshot...
    stale_view = Store().tag_artists()
    assert stale_view == {}

    # ...while another writer (e.g. a split's enrich walk) saves fresh work
    # in the meantime.
    Store().save_tag_artists({
        "other": {"name": "Other", "lastfm_name": None, "tags": [],
                   "fetched_at": "t0", "miss": False},
    })

    _merge_save_tag_artists({
        "a1": {"name": "Artist a1", "lastfm_name": "Artist a1",
               "tags": [{"name": "rock", "count": 50}], "fetched_at": "t1", "miss": False},
    })

    saved = Store().tag_artists()
    assert set(saved) == {"other", "a1"}  # neither writer's work was dropped


def test_merge_save_never_overwrites_an_existing_entry():
    """Write-once: an entry already on disk always wins, hit or miss alike —
    same rule `enrich` itself follows."""
    Store().save_tag_artists({
        "a1": {"name": "Real Name", "lastfm_name": "Real Name",
               "tags": [{"name": "jazz", "count": 20}], "fetched_at": "t0", "miss": False},
    })

    _merge_save_tag_artists({
        "a1": {"name": "Stale Overwrite", "lastfm_name": None, "tags": [],
               "fetched_at": "t1", "miss": True},
    })

    saved = Store().tag_artists()["a1"]
    assert saved["name"] == "Real Name"
    assert saved["miss"] is False
    assert saved["tags"] == [{"name": "jazz", "count": 20}]
