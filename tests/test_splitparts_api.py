"""Split into parts: the draft, the homes list, Suggest, and (later tasks)
moves, parts and Done. Zero Spotify calls: every client method is faked."""

import threading

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from sortify import splitparts_api as spa

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

HOME = {"id": "H1", "name": "Big Home", "owner": "me", "editable": True,
        "total": 24, "snapshot_id": "snap-1", "image": None, "description": ""}
SMALL = {"id": "H2", "name": "Tiny", "owner": "me", "editable": True,
         "total": 5, "snapshot_id": "snap-2", "image": None, "description": ""}
OTHER = {"id": "X1", "name": "[Inbox]", "owner": "me", "editable": True,
         "total": 40, "snapshot_id": "snap-x", "image": None, "description": ""}


def tracks_for(groups):
    tracks, tags, i = [], {}, 0
    for tag, count in groups.items():
        for _ in range(count):
            aid = f"a{i}"
            tracks.append({"uri": f"spotify:track:t{i}", "name": f"Song {i}",
                           "artists": [{"id": aid, "name": f"Artist {i}"}],
                           "is_local": False})
            tags[aid] = {"name": f"Artist {i}", "tags": [{"name": tag, "count": 100}], "miss": False}
            i += 1
    return tracks, tags


TRACKS, TAGS = tracks_for({"rock": 14, "jazz": 10})


@pytest.fixture
def client(monkeypatch):
    appmod.store.save_config({"client_id": "c" * 32, "input_ids": ["X1"],
                              "home_ids": ["H1", "H2"], "sticky_home_ids": []})
    appmod.store.save_cache({"playlists": {"H1": {"snapshot_id": "snap-1", "tracks": list(TRACKS),
                                                  "fetched_at": 1.0}},
                             "artists": {}, "me": {"id": "me"},
                             "playlist_list": {"fetched_at": 1.0, "items": [HOME, SMALL, OTHER]}})
    appmod.store.save_tag_artists(dict(TAGS))
    appmod.store.save_splitparts({"version": 1, "drafts": {}})
    calls = []
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: [HOME, SMALL, OTHER])
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: calls.append(("snapshot", pid)) or "snap-1")
    monkeypatch.setattr(appmod.sp, "playlist_tracks",
                        lambda pid: calls.append(("tracks", pid)) or list(TRACKS))
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)
    # Suggest runs on a thread in production; tests run it inline.
    monkeypatch.setattr(appmod, "_splitparts_threaded", False)
    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.calls = calls
    yield c
    appmod._splitparts_job.clear()


def test_homes_lists_only_homes_with_splittability(client):
    res = client.get("/api/splitparts/homes").json()["homes"]

    assert [h["id"] for h in res] == ["H1", "H2"]
    big, tiny = res
    assert big["splittable"] is True and big["reason"] is None
    assert tiny["splittable"] is False and "fewer than 10" in tiny["reason"]
    assert client.calls == []


def test_suggest_builds_and_saves_a_draft(client):
    res = client.post("/api/splitparts/H1/suggest", json={"n": 2})
    assert res.status_code == 200

    prog = client.get("/api/splitparts/H1/progress").json()
    assert prog["state"] == "done"
    draft = client.get("/api/splitparts/H1").json()
    assert draft["n"] == 2
    assert [len(p["uris"]) for p in draft["parts"]] == [14, 10]
    assert [p["key"] for p in draft["parts"]] == ["p1", "p2"]
    assert draft["songs"]["spotify:track:t0"] == {"name": "Song 0", "artists": "Artist 0", "local": False}
    assert appmod.store.splitparts()["drafts"]["H1"]["snapshot_id"] == "snap-1"


def test_suggest_reads_only_the_snapshot_when_unchanged(client):
    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert client.calls == [("snapshot", "H1")]


def test_suggest_rereads_tracks_when_the_snapshot_moved(client, monkeypatch):
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: client.calls.append(("snapshot", pid)) or "snap-9")

    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert client.calls == [("snapshot", "H1"), ("tracks", "H1")]
    assert client.get("/api/splitparts/H1").json()["snapshot_id"] == "snap-9"


def test_suggest_refuses_a_non_home_and_a_bad_count(client):
    assert client.post("/api/splitparts/X1/suggest", json={"n": 2}).status_code == 404
    assert client.post("/api/splitparts/H1/suggest", json={"n": 1}).status_code == 400
    assert client.post("/api/splitparts/H1/suggest", json={"n": 7}).status_code == 400


def test_a_split_error_lands_in_progress_not_a_draft(client):
    appmod.store.save_tag_artists({})   # no Last.fm data at all

    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    prog = client.get("/api/splitparts/H1/progress").json()
    assert prog["state"] == "error"
    assert "Last.fm" in prog["message"]
    assert client.get("/api/splitparts/H1").status_code == 404


def test_second_suggest_while_running_is_refused(client, monkeypatch):
    appmod._splitparts_job.update({"pid": "H1", "state": "running"})

    res = client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert res.status_code == 409
    assert "already running" in res.json()["detail"]


def test_discarding_a_draft(client):
    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert client.delete("/api/splitparts/H1").json() == {"ok": True}
    assert client.get("/api/splitparts/H1").status_code == 404


def test_missing_lastfm_lists_what_to_fetch():
    tracks = [{"uri": "u1", "name": "One", "artists": [{"id": "a1", "name": "A"}]},
              {"uri": "u2", "name": "Two", "artists": [{"id": "a2", "name": "B"}]}]
    tags = {"a1": {"name": "A", "tags": [], "miss": True}}
    track_map = {"a\x1fone": {"similar": [], "tags": [], "miss": True}}
    artist_map = {"a2": {"name": "B", "similar": [], "miss": False}}

    need_tags, need_tracks, need_similar = spa.missing_lastfm(tracks, tags, track_map, artist_map)

    assert need_tags == {"a2": "B"}
    assert need_tracks == [("B", "Two")]
    assert need_similar == [("a1", "A")]
