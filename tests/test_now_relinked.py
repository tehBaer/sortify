"""Spotify can play a relinked stand-in of a playlist's song: same name,
artists, album and length, another track id. /me/player reports the stand-in,
the playlist holds the original, so the card thought the song had dropped out
of [FILTER Y] while it was playing from it (2026-10-22, "Bakelit" by M.RUX:
playing 1zCtr…, the playlist holds 3IwQ…). The now answer is matched back to
the context playlist's own copy — from the cached track list, zero calls."""
import pytest

import sortify.app as appmod
from sortify.app import NOW_TTL_IDLE, _currently_playing_shared

M = [{"id": "0dZt", "name": "M.RUX"}]
OWN = {"uri": "spotify:track:own", "id": "own", "name": "Bakelit", "artists": M,
       "duration_ms": 208064, "type": "track", "is_local": False}


def answer(**track):
    t = {"uri": "spotify:track:standin", "id": "standin", "name": "Bakelit",
         "artists": M, "duration_ms": 208064, "type": "track", "is_local": False}
    t.update(track)
    return {"track": t, "is_playing": True, "progress_ms": 1000,
            "context_playlist_id": "FY"}


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    appmod._now_cache.update(at=0.0, value=None, ttl=NOW_TTL_IDLE)
    cache = appmod.store.cache()
    cache.setdefault("playlists", {})["FY"] = {"snapshot_id": "s", "fetched_at": 1.0,
                                               "tracks": [OWN, {**OWN, "uri": "spotify:track:o2",
                                                                "id": "o2", "name": "Other"}]}
    appmod.store.save_cache(cache)
    yield
    appmod._now_cache.update(at=0.0, value=None, ttl=NOW_TTL_IDLE)


def test_a_stand_in_is_read_as_the_playlists_own_copy(monkeypatch):
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: answer())

    value, _ = _currently_playing_shared()

    assert value["track"]["uri"] == "spotify:track:own" and value["track"]["id"] == "own"
    assert value["track"]["played_uri"] == "spotify:track:standin"


def test_the_playlists_own_copy_is_left_alone(monkeypatch):
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: answer(uri="spotify:track:own", id="own"))

    value, _ = _currently_playing_shared()

    assert value["track"]["uri"] == "spotify:track:own" and "played_uri" not in value["track"]


@pytest.mark.parametrize("diff", [
    {"name": "Bakelit (Live)"},
    {"artists": [{"id": "someone", "name": "Else"}]},
    {"duration_ms": 208064 + 5000},
])
def test_a_different_recording_is_not_matched(monkeypatch, diff):
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: answer(**diff))

    value, _ = _currently_playing_shared()

    assert value["track"]["uri"] == "spotify:track:standin"


def test_two_candidates_match_nothing(monkeypatch):
    cache = appmod.store.cache()
    cache["playlists"]["FY"]["tracks"].append({**OWN, "uri": "spotify:track:own2", "id": "own2"})
    appmod.store.save_cache(cache)
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: answer())

    value, _ = _currently_playing_shared()

    assert value["track"]["uri"] == "spotify:track:standin"


def test_no_playlist_context_or_no_cached_list_matches_nothing(monkeypatch):
    monkeypatch.setattr(appmod.sp, "currently_playing",
                        lambda: {**answer(), "context_playlist_id": None})
    assert _currently_playing_shared()[0]["track"]["uri"] == "spotify:track:standin"

    appmod._now_cache.update(at=0.0)
    monkeypatch.setattr(appmod.sp, "currently_playing",
                        lambda: {**answer(), "context_playlist_id": "UNCACHED"})
    assert _currently_playing_shared()[0]["track"]["uri"] == "spotify:track:standin"


def test_matching_costs_no_call(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: (calls.append(1), answer())[1])
    for name in ("playlist_tracks", "snapshot", "my_playlists"):
        monkeypatch.setattr(appmod.sp, name, lambda *a, **k: calls.append(name), raising=False)

    _currently_playing_shared()

    assert calls == [1]
