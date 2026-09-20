"""Taking a song back out of a home it was misfiled into.

The card offers this on the rows it marks "already there". It is deliberately
NOT the input Remove wearing a second hat: that verb sweeps every inbox,
because rejecting a song is a decision. Pulling it out of a home is the
opposite — the decision was wrong and the song goes back to being undecided —
so the inboxes must be left exactly as they were.

Zero Spotify calls: every write is trapped.
"""

import time

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
     "total": 2, "snapshot_id": "s-h1", "image": None, "description": ""},
    {"id": "h2", "name": "Home Two", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-h2", "image": None, "description": ""},
    {"id": "inA", "name": "[A]", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-inA", "image": None, "description": ""},
    {"id": "inB", "name": "[B]", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-inB", "image": None, "description": ""},
]

TRACK = {"uri": "spotify:track:z", "id": "z", "name": "Z", "is_local": False,
         "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
         "added_at": "2026-02-02T00:00:00Z"}
OTHER = {"uri": "spotify:track:q", "id": "q", "name": "Q", "is_local": False,
         "type": "track", "artists": [{"id": "ar9", "name": "Ar Nine"}],
         "added_at": "2026-01-01T00:00:00Z"}


@pytest.fixture
def misfiled(monkeypatch):
    """Track z sits in home h1 AND in both inboxes — the misfile the card
    offers to undo, with the song still waiting to be decided."""
    store = appmod.store
    original_cache, original_config = store.cache(), store.config()

    cache = store.cache()
    cache["playlist_list"] = {"fetched_at": time.time(), "items": LISTING}
    cache["playlists"] = {
        "h1": {"snapshot_id": "s-h1", "tracks": [TRACK, OTHER], "fetched_at": time.time()},
        "h2": {"snapshot_id": "s-h2", "tracks": [OTHER], "fetched_at": time.time()},
        "inA": {"snapshot_id": "s-inA", "tracks": [TRACK], "fetched_at": time.time()},
        "inB": {"snapshot_id": "s-inB", "tracks": [TRACK], "fetched_at": time.time()},
    }
    store.save_cache(cache)
    store.save_config({**original_config,
                       "home_ids": ["h1", "h2"], "input_ids": ["inA", "inB"],
                       "subset_ids": [], "input_name_pattern": r"^\[.+\]$"})

    removed, added = [], []
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri: added.append(pid) or f"s-{pid}-new")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist",
                        lambda pid, uri: removed.append(pid) or f"s-{pid}-new")
    monkeypatch.setattr(appmod.sp, "save_to_liked", lambda uri: None)
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)

    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    appmod.undo_stack.clear()
    appmod._ensure_profiles(force=True)

    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.removed, c.added = removed, added
    try:
        yield c
    finally:
        store.save_cache(original_cache)
        store.save_config(original_config)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod.undo_stack.clear()


def _inputs_holding(uri):
    return {l["id"] for l in appmod._profile_state["inputs"] if uri in l["uris"]}


def _homes_holding(uri):
    return {pid for pid, prof in appmod._profile_state["profiles"].items()
            if uri in prof["uris"]}


def _unfile(client, home_id="h1"):
    return client.post("/api/act", json={
        "action": "remove", "uri": TRACK["uri"], "from_id": home_id})


def test_removing_from_a_home_spends_one_call_on_that_home_alone(misfiled):
    assert _unfile(misfiled).status_code == 200
    assert misfiled.removed == ["h1"]


def test_the_inboxes_are_left_alone_so_the_song_is_undecided_again(misfiled):
    _unfile(misfiled)
    # No sweep. The song has to come round again — that is the point of
    # taking it out of the home.
    assert _inputs_holding(TRACK["uri"]) == {"inA", "inB"}


def test_the_home_profile_forgets_it_without_a_rebuild(misfiled):
    assert _homes_holding(TRACK["uri"]) == {"h1"}
    _unfile(misfiled)
    # `already` on the card's suggestion row reads this set, so a stale one
    # keeps offering "already there" for up to PROFILE_TTL.
    assert _homes_holding(TRACK["uri"]) == set()


def test_the_track_cache_forgets_it_too(misfiled):
    _unfile(misfiled)
    uris = [t["uri"] for t in appmod.store.cache()["playlists"]["h1"]["tracks"]]
    assert uris == [OTHER["uri"]]


def test_undo_puts_it_back_in_the_home_and_nowhere_else(misfiled):
    _unfile(misfiled)
    assert misfiled.post("/api/undo", json={}).status_code == 200
    assert misfiled.added == ["h1"]
    assert _homes_holding(TRACK["uri"]) == {"h1"}
    assert _inputs_holding(TRACK["uri"]) == {"inA", "inB"}


def test_removing_from_a_home_never_touches_another_home(misfiled):
    _unfile(misfiled)
    assert "h2" not in misfiled.removed
    uris = [t["uri"] for t in appmod.store.cache()["playlists"]["h2"]["tracks"]]
    assert uris == [OTHER["uri"]]
