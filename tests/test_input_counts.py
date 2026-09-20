"""The input switcher says how many tracks each buffer list holds.

The number is `len(l["uris"])` off the in-memory input membership — the same
set `_sync_membership` keeps current on every filing — so it costs zero
Spotify calls and drops the instant a song is filed out, without waiting for
a profile rebuild. That liveness is the whole point of the feature and is
what these tests pin.

The idle branch ("start an input…", nothing playing) has no membership sets
to count, so it reads `total` off the cached listing instead. Different
source, deliberately: the idle payload must never trigger a profile build.
"""

import time

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-h1", "image": None, "description": ""},
    {"id": "inA", "name": "[A]", "owner": "me", "editable": True,
     "total": 2, "snapshot_id": "s-inA", "image": None, "description": ""},
    {"id": "inB", "name": "[B]", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-inB", "image": None, "description": ""},
]

T1 = {"uri": "spotify:track:z", "id": "z", "name": "Z", "is_local": False,
      "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
      "added_at": "2026-02-02T00:00:00Z"}
T2 = {"uri": "spotify:track:q", "id": "q", "name": "Q", "is_local": False,
      "type": "track", "artists": [{"id": "ar9", "name": "Ar Nine"}],
      "added_at": "2026-01-01T00:00:00Z"}


@pytest.fixture
def counted(monkeypatch):
    """[A] holds two tracks, [B] one, with every Spotify write trapped."""
    store = appmod.store
    original_cache, original_config = store.cache(), store.config()

    cache = store.cache()
    cache["playlist_list"] = {"fetched_at": time.time(), "items": LISTING}
    cache["playlists"] = {
        "h1": {"snapshot_id": "s-h1", "tracks": [T2], "fetched_at": time.time()},
        "inA": {"snapshot_id": "s-inA", "tracks": [T1, T2], "fetched_at": time.time()},
        "inB": {"snapshot_id": "s-inB", "tracks": [T1], "fetched_at": time.time()},
    }
    store.save_cache(cache)
    store.save_config({**original_config, "home_ids": ["h1"],
                       "input_ids": ["inA", "inB"], "subset_ids": [],
                       "input_name_pattern": r"^\[.+\]$"})

    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", lambda pid, uri: f"s-{pid}-new")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist", lambda pid, uri: f"s-{pid}-new")
    monkeypatch.setattr(appmod.sp, "save_to_liked", lambda uri: None)
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)

    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    appmod.undo_stack.clear()
    appmod._ensure_profiles(force=True)

    c = TestClient(appmod.app, raise_server_exceptions=False)
    try:
        yield c
    finally:
        store.save_cache(original_cache)
        store.save_config(original_config)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod.undo_stack.clear()


def _totals(np):
    return {l["id"]: l["total"] for l in appmod._suggestion_payload(np)["inputs"]}


def _np(track, context):
    return {"track": track, "context_playlist_id": context}


def test_every_input_carries_its_track_count(counted):
    assert _totals(_np(T1, "inA")) == {"inA": 2, "inB": 1}


def test_the_count_drops_the_moment_a_song_is_filed_out(counted):
    res = counted.post("/api/act", json={
        "action": "move", "uri": T1["uri"], "from_id": "inA", "to_id": "h1",
        "sweep_inputs": True})
    assert res.status_code == 200
    # Both inboxes held it; the sweep emptied both, so both counts fall — with
    # no profile rebuild in between.
    assert _totals(_np(T2, "inA")) == {"inA": 1, "inB": 0}


def test_the_count_rises_again_on_undo(counted):
    counted.post("/api/act", json={
        "action": "move", "uri": T1["uri"], "from_id": "inA", "to_id": "h1",
        "sweep_inputs": True})
    counted.post("/api/undo", json={})
    assert _totals(_np(T2, "inA")) == {"inA": 2, "inB": 1}


def test_the_idle_payload_counts_from_the_cached_listing(counted):
    # Nothing playing: no membership sets to count, and building them here is
    # exactly what this branch must never do. The listing's own `total` stands
    # in — same number, different (and staler) source.
    assert {l["id"]: l["total"] for l in appmod._idle_inputs_payload()} == {
        "inA": 2, "inB": 1}
