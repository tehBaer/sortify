"""Split into parts: the draft, the homes list, Suggest, and (later tasks)
moves, parts and Done. Zero Spotify calls: every client method is faked."""

import threading

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from sortify import splitparts_api as spa
from sortify.tags import LastFmError

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


# ---- fix round 1 ------------------------------------------------------


def test_suggest_syncs_the_listing_snapshot(client, monkeypatch):
    """The profile rebuild keys off playlist_list's own snapshot_id, not the
    per-playlist track cache — Suggest's fresher `sp.snapshot` read must land
    there too, or the next read mismatches and re-fetches for no reason."""
    monkeypatch.setattr(appmod.sp, "snapshot",
                        lambda pid: client.calls.append(("snapshot", pid)) or "snap-9")

    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    listing = appmod.store.cache()["playlist_list"]["items"]
    home = next(p for p in listing if p["id"] == "H1")
    assert home["snapshot_id"] == "snap-9"
    assert home["total"] == 24

    client.calls.clear()
    appmod._cached_tracks("H1", home["snapshot_id"])
    assert client.calls == []


def test_fill_circuit_breaker_stops_after_consecutive_failures(client, monkeypatch):
    """Every track-record call fails (a Last.fm outage): after
    FILL_FAILURE_LIMIT consecutive failures the fill gives up rather than
    paying a timeout per remaining song, and never reaches artist-similar."""

    class FailingTrackFm:
        def __init__(self):
            self.top_tags_calls = 0
            self.track_calls = 0
            self.track_tags_calls = 0
            self.similar_calls = 0

        def top_tags(self, name):
            self.top_tags_calls += 1
            return None

        def track_similar(self, artist, title):
            self.track_calls += 1
            raise LastFmError("Last.fm is down")

        def track_top_tags(self, artist, title):
            self.track_tags_calls += 1
            return None

        def artist_similar(self, name):
            self.similar_calls += 1
            return []

    fake = FailingTrackFm()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fake)

    res = client.post("/api/splitparts/H1/suggest", json={"n": 2})
    assert res.status_code == 200

    prog = client.get("/api/splitparts/H1/progress").json()
    assert prog["state"] == "done"
    assert fake.track_calls == appmod.FILL_FAILURE_LIMIT
    assert fake.track_tags_calls == 0        # track_similar raised first every time
    assert fake.similar_calls == 0           # the breaker tripped before this phase
    assert fake.top_tags_calls == 0          # tags were already fully cached


def test_fill_skips_remaining_phases_when_tags_fail(client, monkeypatch):
    """When the tag fetch itself raises LastFmError, Last.fm is failing
    outright — the track and artist-similar phases are skipped entirely
    rather than paying for 47 more doomed round trips."""
    partial_tags = dict(TAGS)
    del partial_tags["a0"]   # one artist still needs tags; the rest already have them
    appmod.store.save_tag_artists(partial_tags)

    class FailingTagsFm:
        def __init__(self):
            self.top_tags_calls = 0
            self.track_calls = 0
            self.similar_calls = 0

        def top_tags(self, name):
            self.top_tags_calls += 1
            raise LastFmError("Last.fm tag service is down")

        def track_similar(self, artist, title):
            self.track_calls += 1
            return []

        def track_top_tags(self, artist, title):
            return []

        def artist_similar(self, name):
            self.similar_calls += 1
            return []

    fake = FailingTagsFm()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fake)

    res = client.post("/api/splitparts/H1/suggest", json={"n": 2})
    assert res.status_code == 200

    prog = client.get("/api/splitparts/H1/progress").json()
    assert prog["state"] == "done"
    assert fake.top_tags_calls == 1
    assert fake.track_calls == 0
    assert fake.similar_calls == 0


def test_suggest_refuses_when_done_has_already_started(client):
    draft = {"playlist_id": "H1", "name": "Big Home", "snapshot_id": "snap-1", "n": 2,
             "parts": [{"key": "p1", "name": "N1", "uris": []}],
             "unknown": [], "hint": None, "scores": {}, "songs": {}, "tracks": [],
             "done": {"created": {"p1": "N1"}}}
    spa.put_draft(appmod.store, draft)

    res = client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert res.status_code == 409
    assert "Done has already started" in res.json()["detail"]
    assert appmod.store.splitparts()["drafts"]["H1"] == draft


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


# ---- moves and parts -------------------------------------------------------


@pytest.fixture
def drafted(client):
    client.post("/api/splitparts/H1/suggest", json={"n": 2})
    client.calls.clear()
    return client


def part_of(draft, uri):
    return next(p["key"] for p in draft["parts"] if uri in p["uris"])


def test_move_puts_the_song_at_the_end_of_its_new_part(drafted):
    d = drafted.get("/api/splitparts/H1").json()
    uri = d["parts"][0]["uris"][0]

    d2 = drafted.post("/api/splitparts/H1/move", json={"uri": uri, "to": "p2"}).json()

    assert part_of(d2, uri) == "p2"
    assert d2["parts"][1]["uris"][-1] == uri
    assert sum(len(p["uris"]) for p in d2["parts"]) == 24
    assert appmod.store.splitparts()["drafts"]["H1"]["parts"] == d2["parts"]
    assert drafted.calls == []


def test_move_refuses_unknown_song_or_part(drafted):
    d = drafted.get("/api/splitparts/H1").json()
    uri = d["parts"][0]["uris"][0]

    assert drafted.post("/api/splitparts/H1/move", json={"uri": "nope", "to": "p2"}).status_code == 400
    assert drafted.post("/api/splitparts/H1/move", json={"uri": uri, "to": "p9"}).status_code == 400


def test_add_part_makes_an_empty_new_part(drafted):
    d = drafted.post("/api/splitparts/H1/parts").json()

    assert d["parts"][-1] == {"key": "p3", "name": "New part", "uris": []}


def test_add_part_stops_at_six(drafted):
    for _ in range(4):
        assert drafted.post("/api/splitparts/H1/parts").status_code == 200
    assert drafted.post("/api/splitparts/H1/parts").status_code == 409


def test_remove_part_dry_run_says_where_songs_go_and_saves_nothing(drafted):
    drafted.post("/api/splitparts/H1/parts")
    before = drafted.get("/api/splitparts/H1").json()

    res = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": True}).json()

    assert res == {"moves": {"p1": 10}}   # the empty p3 never wins
    assert drafted.get("/api/splitparts/H1").json() == before


def test_remove_part_moves_each_song_to_its_best_part(drafted):
    drafted.post("/api/splitparts/H1/parts")
    d = drafted.get("/api/splitparts/H1").json()
    moved = list(d["parts"][1]["uris"])

    d2 = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": False}).json()

    assert [p["key"] for p in d2["parts"]] == ["p1", "p3"]
    assert all(part_of(d2, u) == "p1" for u in moved)


def test_at_least_two_parts_remain(drafted):
    res = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": False})

    assert res.status_code == 409
    assert "at least 2" in res.json()["detail"]
