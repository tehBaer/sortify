"""The input switcher counts only songs that can still be played.

Reported 2026-10-20: [Bonum] showed 7 where the Spotify app showed 2. The
read was right, Spotify does list 7, but some of them can no longer be
played (pulled from the catalogue, which the dev-mode API returns with no
title) and the app hides those. The count is what there is to work through,
so it leaves them out. They stay members for everything else.

The read asks Spotify for `is_playable` by naming a market. That can return
a relinked recording, so the playlist's own uri (linked_from) must win, or
removals would aim at a song that is not in the playlist. And because the
market was never probed in dev mode, a 400 drops back to exactly the old
request, once per process.

Zero-Spotify-call throughout: fake request functions and listings only.
"""

import pytest

from sortify import app as appmod
from sortify.spotify import (ITEM_FIELDS, ITEM_FIELDS_PLAYABLE, ITEM_MARKET,
                             Spotify, SpotifyError)
from sortify.store import Store

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)


def _client(monkeypatch, pages, reject_market=False):
    sp = Spotify(Store())
    calls = []

    def fake_request(method, path, background=False, bulk=False,
                     spend_reserve=False, **kw):
        params = kw.get("params") or {}
        calls.append(params)
        if reject_market and "market" in params:
            raise SpotifyError(400, "Invalid market")
        return {"items": pages}

    monkeypatch.setattr(sp, "request", fake_request)
    sp.calls = calls
    return sp


def _entry(uri, name="Song", **extra):
    return {"added_at": "2026-01-01T00:00:00Z", "is_local": False,
            "item": {"uri": uri, "id": uri.rsplit(":", 1)[-1], "name": name,
                     "artists": [], **extra}}


def test_the_read_asks_for_playability(monkeypatch):
    sp = _client(monkeypatch, [_entry("spotify:track:a", is_playable=False),
                               _entry("spotify:track:b", is_playable=True)])
    tracks = sp.playlist_tracks("P1")
    assert sp.calls == [{"limit": 100, "fields": ITEM_FIELDS_PLAYABLE,
                         "market": ITEM_MARKET}]
    assert [t["playable"] for t in tracks] == [False, True]


def test_a_relinked_song_keeps_the_playlists_own_uri(monkeypatch):
    sp = _client(monkeypatch, [_entry(
        "spotify:track:standin", is_playable=True,
        linked_from={"uri": "spotify:track:orig", "id": "orig"})])
    (t,) = sp.playlist_tracks("P1")
    assert (t["uri"], t["id"]) == ("spotify:track:orig", "orig")


def test_a_rejected_market_falls_back_to_the_old_read_once(monkeypatch):
    sp = _client(monkeypatch, [_entry("spotify:track:a")], reject_market=True)
    tracks = sp.playlist_tracks("P1")
    assert [t["uri"] for t in tracks] == ["spotify:track:a"]
    assert tracks[0]["playable"] is None
    assert sp.calls[1:] == [{"limit": 100, "fields": ITEM_FIELDS}]
    sp.calls.clear()
    sp.playlist_tracks("P1")
    assert sp.calls == [{"limit": 100, "fields": ITEM_FIELDS}]


def test_other_errors_are_not_swallowed(monkeypatch):
    sp = Spotify(Store())

    def boom(*a, **kw):
        raise SpotifyError(500, "down")

    monkeypatch.setattr(sp, "request", boom)
    with pytest.raises(SpotifyError):
        sp.playlist_tracks("P1")
    assert sp._items_market is True


@pytest.mark.parametrize("track, unplayable", [
    ({"name": "A", "playable": True}, False),
    ({"name": "A", "playable": None}, False),
    ({"name": "A"}, False),
    ({"name": "A", "playable": False}, True),
    ({"name": "", "playable": None}, True),
    ({"name": None}, True),
])
def test_what_counts_as_unplayable(track, unplayable):
    assert appmod._unplayable(track) is unplayable


# ---- the switcher's count --------------------------------------------------

LISTING = [
    {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-h1", "image": None, "description": ""},
    {"id": "buf", "name": "[Bonum]", "owner": "me", "editable": True,
     "total": 4, "snapshot_id": "s-buf", "image": None, "description": ""},
]


def _t(uri, name="Song", playable=None):
    return {"uri": uri, "id": uri.rsplit(":", 1)[-1], "name": name,
            "playable": playable, "is_local": False, "type": "track",
            "artists": [{"id": "ar1", "name": "Ar One"}],
            "added_at": "2026-01-01T00:00:00Z"}


TRACKS = {
    "h1": [_t("spotify:track:h")],
    "buf": [_t("spotify:track:z"), _t("spotify:track:y"),
            _t("spotify:track:gone", name=""),
            _t("spotify:track:grey", playable=False)],
}

PLAYING = {"uri": "spotify:track:z", "id": "z", "name": "Z", "type": "track",
           "is_local": False, "duration_ms": 210_000,
           "artists": [{"id": "ar1", "name": "Ar One"}],
           "album": {"name": "Alb", "images": [{"url": "http://img/1"}]}}


@pytest.fixture
def restore():
    original_cache, original_config = appmod.store.cache(), appmod.store.config()
    appmod.store.save_config({"input_ids": [], "home_ids": ["h1"],
                              "input_name_pattern": r"^\[.+\]$"})
    yield
    appmod.store.save_cache(original_cache)
    appmod.store.save_config(original_config)
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0


def test_the_playing_switcher_counts_only_playable_songs(monkeypatch, restore):
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    monkeypatch.setattr(appmod, "_cached_tracks", lambda pid, snap: TRACKS.get(pid, []))
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    appmod._ensure_profiles(force=True)
    payload = appmod._suggestion_payload({"track": PLAYING, "context_playlist_id": "buf"})
    inputs = {l["id"]: l for l in payload["inputs"]}
    assert inputs["buf"]["total"] == 2
    # Still members: a song that greyed out is still in the playlist.
    state_buf = next(l for l in appmod._profile_state["inputs"] if l["id"] == "buf")
    assert "spotify:track:grey" in state_buf["uris"]


def test_the_idle_switcher_takes_off_what_the_cache_knows(restore):
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 0, "items": LISTING}
    cache["playlists"] = {"buf": {"snapshot_id": "s-buf", "tracks": TRACKS["buf"],
                                  "fetched_at": 0}}
    appmod.store.save_cache(cache)
    inputs = {l["id"]: l for l in appmod._idle_inputs_payload()}
    assert inputs["buf"]["total"] == 2


def test_the_idle_switcher_without_a_cached_list_keeps_the_listing_total(restore):
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 0, "items": LISTING}
    cache["playlists"] = {}
    appmod.store.save_cache(cache)
    inputs = {l["id"]: l for l in appmod._idle_inputs_payload()}
    assert inputs["buf"]["total"] == 4
