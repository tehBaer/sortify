"""The Now card's one-tap destinations: Star, Explore artist, Add to rotation.

A quick add is an ordinary subset add with the picker skipped — same
`/api/act` call, same undo stack, same rule that it never spends the song's
filing decision. What is new is the configured list of buttons, the
membership badge that costs nothing (the target's tracks are already
cached), and the explore button's ability to create its playlist the first
time it is pressed.

Zero Spotify calls: the create is faked, and everything else reads local
state.
"""

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from sortify import quickadds

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "star1", "name": "🐾 sjangersprengt topp", "owner": "me", "editable": True,
     "total": 344, "snapshot_id": "s-star", "image": None, "description": ""},
    {"id": "rot1", "name": "{project 17}", "owner": "me", "editable": True,
     "total": 12, "snapshot_id": "s-rot", "image": None, "description": ""},
]

QUICK = [
    {"key": "star", "label": "Star", "playlist_id": "star1"},
    {"key": "explore", "label": "Explore artist", "playlist_id": None,
     "create_name": "Utforsk"},
    {"key": "rotation", "label": "Add to rotation", "playlist_id": "rot1"},
]

TRACKS = {"star1": [{"uri": "spotify:track:a"}], "rot1": []}


def _payload(uri="spotify:track:a", quick=None, listing=None):
    return quickadds.payload(
        {"quick_adds": quick if quick is not None else QUICK},
        listing if listing is not None else LISTING,
        lambda pid: TRACKS.get(pid),
        uri,
    )


# ---- the button list ------------------------------------------------------

def test_the_buttons_keep_the_order_they_are_configured_in():
    assert [e["key"] for e in _payload()] == ["star", "explore", "rotation"]


def test_a_button_carries_its_label_and_its_target():
    star = _payload()[0]
    assert star["label"] == "Star"
    assert star["playlist_id"] == "star1"
    assert star["name"] == "🐾 sjangersprengt topp"
    assert star["total"] == 344


def test_a_target_that_does_not_exist_yet_still_gets_a_button():
    # The explore playlist is created on first press; until then there is
    # nothing in the listing to name, and the button must still be offered.
    explore = _payload()[1]
    assert explore["playlist_id"] is None
    assert explore["name"] is None


def test_an_entry_with_neither_a_target_nor_a_name_to_create_is_dropped():
    assert _payload(quick=[{"key": "x", "label": "X"}]) == []


def test_membership_is_reported_from_the_cache_costing_nothing():
    got = {e["key"]: e["has_track"] for e in _payload()}
    assert got["star"] is True        # the cached track list holds this uri
    assert got["rotation"] is False   # cached, and it does not


def test_an_uncached_target_reports_unknown_rather_than_absent():
    # False would draw "not in it yet" on a playlist nobody has read.
    got = _payload(quick=[{"key": "k", "label": "K", "playlist_id": "nocache"}],
                   listing=LISTING + [{"id": "nocache", "name": "N", "total": 3}])
    assert got[0]["has_track"] is None


def test_no_quick_adds_configured_is_simply_no_buttons():
    assert quickadds.payload({}, LISTING, lambda pid: None, "spotify:track:a") == []


# ---- the explore button's first press -------------------------------------

@pytest.fixture
def client(monkeypatch):
    appmod.store.save_config({
        "client_id": "x", "input_ids": [], "home_ids": [],
        "subset_ids": ["star1", "rot1"],
        "input_name_pattern": r"^\[.+\]$",
        "quick_adds": [dict(e) for e in QUICK],
    })
    appmod.store.save_cache({
        "playlists": {}, "artists": {}, "me": {"id": "me"},
        "playlist_list": {"fetched_at": 1.0, "items": list(LISTING)},
    })
    appmod.store.save_explore({})
    calls = {"create": 0}

    def fake_full(name, description="", bulk=False, spend_reserve=False, public=False):
        calls["create"] += 1
        return "utf1", "snap-utf"

    monkeypatch.setattr(appmod.sp, "create_playlist_full", fake_full)
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: list(LISTING))
    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.calls = calls
    try:
        yield c
    finally:
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0


BODY = {"uri": "spotify:track:z", "title": "Zug", "artist": "Ar One", "artist_id": "ar1"}


def test_the_first_press_creates_the_playlist_and_says_so(client):
    res = client.post("/api/explore", json=BODY)
    assert res.status_code == 200
    body = res.json()
    assert body["created"] is True
    assert body["playlist_id"] == "utf1"
    assert body["name"] == "Utforsk"
    assert client.calls["create"] == 1


def test_the_new_playlist_is_a_subset_so_it_never_becomes_a_filing_home(client):
    client.post("/api/explore", json=BODY)
    cfg = appmod.store.config()
    assert "utf1" in cfg["subset_ids"]
    assert "utf1" not in (cfg.get("home_ids") or [])
    assert "utf1" not in (cfg.get("sticky_home_ids") or [])


def test_the_target_is_remembered_so_the_next_press_creates_nothing(client):
    client.post("/api/explore", json=BODY)
    res = client.post("/api/explore", json={**BODY, "uri": "spotify:track:y"})
    assert res.status_code == 200
    assert res.json()["created"] is False
    assert res.json()["playlist_id"] == "utf1"
    assert client.calls["create"] == 1
    entry = next(e for e in appmod.store.config()["quick_adds"] if e["key"] == "explore")
    assert entry["playlist_id"] == "utf1"


def test_the_artist_is_recorded_for_the_exploring_still_to_come(client):
    client.post("/api/explore", json=BODY)
    logged = appmod.store.explore()["artists"]["ar1"]
    assert logged["name"] == "Ar One"
    assert logged["count"] == 1
    assert logged["tracks"] == ["spotify:track:z"]
    assert logged["first_at"] and logged["last_at"]


def test_a_second_song_by_the_same_artist_counts_up_rather_than_duplicating(client):
    client.post("/api/explore", json=BODY)
    client.post("/api/explore", json={**BODY, "uri": "spotify:track:y", "title": "Yy"})
    logged = appmod.store.explore()["artists"]
    assert list(logged) == ["ar1"]
    assert logged["ar1"]["count"] == 2
    assert logged["ar1"]["tracks"] == ["spotify:track:z", "spotify:track:y"]


def test_the_same_song_twice_does_not_count_twice(client):
    client.post("/api/explore", json=BODY)
    client.post("/api/explore", json=BODY)
    assert appmod.store.explore()["artists"]["ar1"]["count"] == 1


def test_an_artist_with_no_id_is_recorded_under_its_name(client):
    client.post("/api/explore", json={**BODY, "artist_id": None})
    assert "Ar One" in appmod.store.explore()["artists"]


def test_no_explore_button_configured_is_refused_before_creating_anything(client):
    appmod.store.update_config(quick_adds=[e for e in QUICK if e["key"] != "explore"])
    res = client.post("/api/explore", json=BODY)
    assert res.status_code == 400
    assert client.calls["create"] == 0


def test_the_playing_card_offers_the_buttons(client):
    # The one place the client reads them from; without this the card has
    # nothing to render and the feature is invisible.
    got = appmod._quick_adds_payload({"playlists": LISTING}, "spotify:track:a")
    assert [e["key"] for e in got] == ["star", "explore", "rotation"]
