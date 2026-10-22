"""A song playing from an inbox that sortify's copy of that inbox does not
hold: either autoplay took over, or the song was added in Spotify since the
copy was read (2026-10-22, "Splitting An Atom" in [FILTER Y]). One snapshot
read tells them apart; a changed snapshot re-reads the list and the card
sees the song as a member. Once per song and inbox, never on a member.

Zero real Spotify calls: every client method is faked."""

import pytest

from sortify import app as appmod
from sortify.spotify import SpotifyError

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-h1", "image": None, "description": ""},
    {"id": "fy", "name": "[FILTER Y]", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-old", "image": None, "description": ""},
]
A = [{"id": "ar1", "name": "Ar"}]


def tr(u):
    return {"uri": f"spotify:track:{u}", "id": u, "name": u.upper(), "is_local": False,
            "type": "track", "artists": A, "duration_ms": 200_000,
            "added_at": "2026-01-01T00:00:00Z"}


@pytest.fixture
def env(monkeypatch):
    appmod.store.save_config({"input_ids": [], "home_ids": ["h1"],
                              "input_name_pattern": r"^\[.+\]$"})
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 1.0, "items": [dict(p) for p in LISTING]}
    cache["playlists"] = {"h1": {"snapshot_id": "s-h1", "tracks": [tr("a")], "fetched_at": 1.0},
                          "fy": {"snapshot_id": "s-old", "tracks": [tr("z")], "fetched_at": 1.0}}
    appmod.store.save_cache(cache)
    calls = []
    remote = {"fy": ("s-new", [tr("z"), tr("atom")])}

    def snapshot(pid):
        calls.append(("snapshot", pid))
        return remote[pid][0]

    def playlist_tracks(pid, *a, **k):
        calls.append(("tracks", pid))
        return remote[pid][1]

    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    monkeypatch.setattr(appmod.sp, "snapshot", snapshot)
    monkeypatch.setattr(appmod.sp, "playlist_tracks", playlist_tracks)
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    appmod._context_rechecked.clear()
    appmod._ensure_profiles(force=True)
    calls.clear()

    def payload(uri):
        t = {**tr(uri), "album": {"name": "Alb", "images": [{"url": "http://img/1"}]}}
        return appmod._suggestion_payload({"track": t, "context_playlist_id": "fy"})

    yield type("E", (), {"calls": calls, "remote": remote, "payload": staticmethod(payload)})
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    appmod._context_rechecked.clear()


def chip(p, pid="fy"):
    return next(l for l in p["inputs"] if l["id"] == pid)


def test_a_song_added_in_spotify_since_is_found_by_one_check(env):
    p = env.payload("atom")

    assert chip(p)["has_track"] is True
    assert env.calls == [("snapshot", "fy"), ("tracks", "fy")]
    cache = appmod.store.cache()
    assert cache["playlists"]["fy"]["snapshot_id"] == "s-new"
    assert next(x for x in cache["playlist_list"]["items"] if x["id"] == "fy")["snapshot_id"] == "s-new"


def test_an_unchanged_inbox_means_autoplay_and_reads_nothing_more(env):
    env.remote["fy"] = ("s-old", [tr("z")])

    p = env.payload("atom")

    assert chip(p)["has_track"] is False
    assert env.calls == [("snapshot", "fy")]


def test_it_checks_once_per_song(env):
    env.remote["fy"] = ("s-old", [tr("z")])
    env.payload("atom")
    env.payload("atom")
    env.payload("atom")

    assert env.calls == [("snapshot", "fy")]


def test_a_member_costs_nothing(env):
    p = env.payload("z")

    assert chip(p)["has_track"] is True and env.calls == []


def test_a_context_that_is_not_an_inbox_is_not_checked(env):
    t = {**tr("atom"), "album": {"name": "Alb", "images": [{"url": "http://img/1"}]}}
    appmod._suggestion_payload({"track": t, "context_playlist_id": "h1"})
    appmod._suggestion_payload({"track": t, "context_playlist_id": None})

    assert env.calls == []


def test_a_failed_check_is_quiet_and_not_retried_every_poll(env, monkeypatch):
    def boom(pid):
        env.calls.append(("snapshot", pid))
        raise SpotifyError(429, "rate limited")
    monkeypatch.setattr(appmod.sp, "snapshot", boom)

    p = env.payload("atom")
    env.payload("atom")

    assert chip(p)["has_track"] is False
    assert env.calls == [("snapshot", "fy")]
