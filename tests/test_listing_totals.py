"""The track count a playlist row shows must follow the songs you file.

The shipped bug: a home created inside sortify still read "0 tracks" after a
song had been filed into it, and stayed that way. Filing mirrors the move
into the track cache (`_cache_move`) and the snapshot (`_apply_snapshot`),
but nothing touched `playlist_list`'s `total` — which is the number every
row renders. On a 200-track home a stale count is an off-by-one nobody
sees; on a playlist made a minute ago it is a conspicuous 0, and it sticks
until the next full Refresh re-reads the listing from Spotify.

Zero Spotify calls: both writes are trapped.
"""

import time

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    # Fresh, like one created from the Lists view moments ago.
    {"id": "new1", "name": "DARK SOAR", "owner": "me", "editable": True,
     "total": 0, "snapshot_id": "s-new", "image": None, "description": ""},
    {"id": "inp", "name": "[Buffer]", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-in", "image": None, "description": ""},
]

TRACK = {"uri": "spotify:track:z", "id": "z", "name": "Z", "is_local": False,
         "type": "track", "duration_ms": 210_000,
         "artists": [{"id": "ar1", "name": "Ar One"}],
         "added_at": "2026-02-02T00:00:00Z"}


@pytest.fixture
def client(monkeypatch):
    store = appmod.store
    original_cache, original_config = store.cache(), store.config()

    cache = store.cache()
    cache["playlist_list"] = {"fetched_at": 0.0, "items": [dict(p) for p in LISTING]}
    cache["playlists"] = {
        "new1": {"snapshot_id": "s-new", "tracks": [], "fetched_at": time.time()},
        "inp": {"snapshot_id": "s-in", "tracks": [TRACK], "fetched_at": time.time()},
    }
    store.save_cache(cache)
    store.save_config({**original_config, "home_ids": ["new1"], "input_ids": ["inp"],
                       "subset_ids": [], "input_name_pattern": r"^\[.+\]$"})

    # NOT stubbed to a constant: the real my_playlists serves the cached
    # listing, which is the structure under test — a stub returning a fixed
    # list would answer 0 no matter what the fix did.
    monkeypatch.setattr(appmod.sp, "_fetch_my_playlists",
                        lambda *a, **k: pytest.fail("no listing fetch in this test"))
    monkeypatch.setattr(appmod.sp, "add_to_playlist", lambda pid, uri: f"s-{pid}-new")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist", lambda pid, uri: f"s-{pid}-out")
    monkeypatch.setattr(appmod.sp, "save_to_liked", lambda uri: None)
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    appmod.undo_stack.clear()
    try:
        yield TestClient(appmod.app, raise_server_exceptions=False)
    finally:
        store.save_cache(original_cache)
        store.save_config(original_config)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod.undo_stack.clear()


def _total(pid):
    items = (appmod.store.cache().get("playlist_list") or {}).get("items") or []
    return next(p["total"] for p in items if p["id"] == pid)


def _file(client, **over):
    body = {"action": "move", "uri": TRACK["uri"], "from_id": "inp", "to_id": "new1"}
    res = client.post("/api/act", json={**body, **over})
    assert res.status_code == 200, res.text
    return res


def test_filing_a_song_into_a_playlist_counts_it(client):
    _file(client)
    assert _total("new1") == 1


def test_filing_a_song_out_of_an_input_stops_counting_it(client):
    _file(client)
    assert _total("inp") == 2


def test_adding_without_removing_leaves_the_source_alone(client):
    # A subset add: the song stays where it was, so only the destination moves.
    _file(client, from_id=None)
    assert _total("new1") == 1
    assert _total("inp") == 3


def test_undo_puts_both_counts_back(client):
    _file(client)
    assert client.post("/api/undo", json={}).status_code == 200
    assert _total("new1") == 0
    assert _total("inp") == 3


def test_a_count_never_goes_below_zero(client):
    # An input whose cached count is already 0 — a listing read before the
    # last few adds, say. Removing from it must not print "-1 tracks".
    cache = appmod.store.cache()
    for p in cache["playlist_list"]["items"]:
        if p["id"] == "inp":
            p["total"] = 0
    appmod.store.save_cache(cache)
    _file(client)
    assert _total("inp") == 0


def test_the_playlists_view_shows_the_count_it_just_earned(client):
    """The number the row renders is the listing's, so this is the end of the
    chain the bug was reported from."""
    _file(client)
    rows = client.get("/api/playlists").json()["playlists"]
    assert next(p["total"] for p in rows if p["id"] == "new1") == 1
