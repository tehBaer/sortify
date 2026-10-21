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


def test_homes_with_a_split_in_progress_come_first(client):
    appmod.store.save_splitparts({"version": 1, "drafts": {"H2": {"playlist_id": "H2"}}})

    res = client.get("/api/splitparts/homes").json()["homes"]

    assert [(h["id"], h["draft"]) for h in res] == [("H2", True), ("H1", False)]   # then by name


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


def test_remove_part_can_send_every_song_to_one_chosen_part(drafted):
    drafted.post("/api/splitparts/H1/parts")
    d = drafted.get("/api/splitparts/H1").json()
    moved = list(d["parts"][1]["uris"])

    d2 = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": False, "to": "p3"}).json()

    assert [p["key"] for p in d2["parts"]] == ["p1", "p3"]
    assert d2["parts"][1]["uris"] == moved          # all of them, in their order
    assert d2["edited"] is True
    assert appmod.store.splitparts()["drafts"]["H1"]["parts"] == d2["parts"]
    assert drafted.calls == []


def test_remove_part_refuses_a_chosen_part_that_is_not_another_part(drafted):
    drafted.post("/api/splitparts/H1/parts")
    before = drafted.get("/api/splitparts/H1").json()

    for to in ("p2", "p9"):   # itself, and no such part
        res = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": False, "to": to})
        assert res.status_code == 400
    assert drafted.get("/api/splitparts/H1").json() == before


def test_at_least_two_parts_remain(drafted):
    res = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": False})

    assert res.status_code == 409
    assert "at least 2" in res.json()["detail"]


# ---- fix round 1 -------------------------------------------------------


def test_move_refuses_when_done_has_started(drafted):
    d = drafted.get("/api/splitparts/H1").json()
    uri = d["parts"][0]["uris"][0]
    draft = spa.get_draft(appmod.store, "H1")
    draft["done"] = {"created": {"p1": "N1"}}
    spa.put_draft(appmod.store, draft)
    before = drafted.get("/api/splitparts/H1").json()

    res = drafted.post("/api/splitparts/H1/move", json={"uri": uri, "to": "p2"})

    assert res.status_code == 409
    assert "Done has already started" in res.json()["detail"]
    assert drafted.get("/api/splitparts/H1").json() == before


def test_add_part_refuses_when_done_has_started(drafted):
    draft = spa.get_draft(appmod.store, "H1")
    draft["done"] = {"created": {"p1": "N1"}}
    spa.put_draft(appmod.store, draft)
    before = drafted.get("/api/splitparts/H1").json()

    res = drafted.post("/api/splitparts/H1/parts")

    assert res.status_code == 409
    assert "Done has already started" in res.json()["detail"]
    assert drafted.get("/api/splitparts/H1").json() == before


def test_remove_part_refuses_when_done_has_started(drafted):
    draft = spa.get_draft(appmod.store, "H1")
    draft["done"] = {"created": {"p1": "N1"}}
    spa.put_draft(appmod.store, draft)
    before = drafted.get("/api/splitparts/H1").json()

    res = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": False})

    assert res.status_code == 409
    assert "Done has already started" in res.json()["detail"]
    assert drafted.get("/api/splitparts/H1").json() == before


def test_remove_part_dry_run_refuses_when_done_has_started(drafted):
    draft = spa.get_draft(appmod.store, "H1")
    draft["done"] = {"created": {"p1": "N1"}}
    spa.put_draft(appmod.store, draft)
    before = drafted.get("/api/splitparts/H1").json()

    res = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": True})

    assert res.status_code == 409
    assert "Done has already started" in res.json()["detail"]
    assert drafted.get("/api/splitparts/H1").json() == before


def test_move_error_message_format(drafted):
    d = drafted.get("/api/splitparts/H1").json()
    uri = d["parts"][0]["uris"][0]

    res1 = drafted.post("/api/splitparts/H1/move", json={"uri": "nope", "to": "p2"})
    assert res1.json()["detail"] == "song 'nope' is not in this split"

    res2 = drafted.post("/api/splitparts/H1/move", json={"uri": uri, "to": "p9"})
    assert res2.json()["detail"] == "no part 'p9'"


# ---- Done ------------------------------------------------------------------

from sortify.spotify import SpotifyError


@pytest.fixture
def env(drafted, monkeypatch):
    appmod.store.update_config(input_name_pattern=r"^\[.+\]$",
                               home_ids=["H1", "H2", "H3"],
                               home_folder_prefixes=["ROOT"], home_folder_exclude=[])
    # "ROOT / Mix" is offered (unblocked); "ROOT / Deep" is blocked because
    # "ROOT / Deep / Sub" also exists — the client's folder search for "Deep"
    # would also hit that subfolder (foldermove._check_leaf_unique).
    appmod.store.save_folders({"H2": {"path": "ROOT / Mix"}, "H3": {"path": "ROOT / Deep"},
                               "X9": {"path": "ROOT / Deep / Sub"}})
    log = []
    ids = iter(["N1", "N2", "N3", "N4", "N5"])

    def create(name, description="", bulk=False, spend_reserve=False, public=False):
        new = next(ids)
        log.append(("create", name, new))
        return new, f"snap-{new}"

    def add(pid, uris, bulk=False, spend_reserve=False):
        log.append(("add", pid, list(uris)))
        return f"snap-add-{pid}"

    def remove_many(pid, uris):
        log.append(("remove", pid, list(uris)))
        return "snap-removed"

    def rename(pid, name):
        log.append(("rename", pid, name))

    monkeypatch.setattr(appmod.sp, "create_playlist_full", create)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", add)
    monkeypatch.setattr(appmod.sp, "remove_many", remove_many)
    monkeypatch.setattr(appmod.sp, "rename_playlist", rename)
    filed = []
    monkeypatch.setattr(appmod.filing, "start",
                        lambda pid, name, dest, **kw: filed.append((pid, name, dest)))
    drafted.log = log
    drafted.filed = filed
    return drafted


def progress(c):
    return c.get("/api/splitparts/H1/progress").json()


def edit_draft(fn):
    env_ = appmod.store.splitparts()
    fn(env_["drafts"]["H1"])
    appmod.store.save_splitparts(env_)


def test_plan_prices_keep_as_subset(env):
    plan = env.get("/api/splitparts/H1/plan", params={"original": "subset"}).json()

    # snapshot + (create + 1 add) x 2 + rename
    assert plan["calls"] == 6
    assert plan["creates"] == 2
    assert plan["removals"] == 0 and plan["empty"] == [] and plan["local"] == []


def test_plan_prices_original_becomes_a_part(env):
    plan = env.get("/api/splitparts/H1/plan", params={"original": "part", "part": "p1"}).json()

    # snapshot + (create + 1 add) + 1 removal batch
    assert plan["calls"] == 4
    assert plan["creates"] == 1
    assert plan["removals"] == 10


def test_done_keep_as_subset_creates_all_parts_then_renames_the_original(env):
    res = env.post("/api/splitparts/H1/done", json={
        "names": {"p1": "Rocky", "p2": "Jazzy"}, "folder": "ROOT / Mix", "original": "subset"})
    assert res.status_code == 200

    p = progress(env)
    assert p["state"] == "done"
    assert [e[0] for e in env.log] == ["create", "add", "create", "add", "rename"]
    assert env.log[0][1] == "Rocky" and env.log[2][1] == "Jazzy"
    assert env.log[-1] == ("rename", "H1", "🐾 Big Home")
    cfg = appmod.store.config()
    assert {"N1", "N2"} <= set(cfg["home_ids"]) and {"N1", "N2"} <= set(cfg["sticky_home_ids"])
    assert "H1" not in cfg["home_ids"]
    assert env.filed == [("N1", "Rocky", "ROOT / Mix"), ("N2", "Jazzy", "ROOT / Mix")]
    assert p["summary"]["created"] == [{"id": "N1", "name": "Rocky", "count": 14},
                                       {"id": "N2", "name": "Jazzy", "count": 10}]
    assert env.get("/api/splitparts/H1").status_code == 404
    assert env.calls == [("snapshot", "H1")]


def test_done_original_becomes_a_part_removes_the_rest_from_it(env):
    d = env.get("/api/splitparts/H1").json()
    others = d["parts"][1]["uris"]

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None,
                                               "original": "part", "part": "p1"})

    assert progress(env)["state"] == "done"
    assert [e[0] for e in env.log] == ["create", "add", "remove"]
    assert env.log[-1] == ("remove", "H1", others)
    assert "H1" in appmod.store.config()["home_ids"]
    assert env.filed == []


def test_done_part_syncs_the_listing_snapshot(env):
    """After a "part" Done, the cached LISTING item for H1 must carry the
    snapshot the removal actually produced — same reasoning as Suggest's
    own _sync_listing_snapshot call: the profile rebuild keys off the
    listing's snapshot_id, not the per-playlist track cache."""
    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None,
                                               "original": "part", "part": "p1"})

    listing = appmod.store.cache()["playlist_list"]["items"]
    home = next(p for p in listing if p["id"] == "H1")
    assert home["snapshot_id"] == "snap-removed"


def test_part_of_only_local_files_is_not_created(env):
    env.post("/api/splitparts/H1/parts")
    d = env.get("/api/splitparts/H1").json()
    two = d["parts"][0]["uris"][:2]
    for u in two:
        env.post("/api/splitparts/H1/move", json={"uri": u, "to": "p3"})
    edit_draft(lambda dr: [dr["songs"][u].update(local=True) for u in two])

    plan = env.get("/api/splitparts/H1/plan", params={"original": "subset"}).json()
    assert plan["creates"] == 2
    assert plan["empty"] == ["New part"]
    assert sorted(plan["local"]) == sorted(d["songs"][u]["name"] for u in two)

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})
    assert [e[0] for e in env.log].count("create") == 2


def test_done_adds_each_song_once(env):
    edit_draft(lambda dr: dr["parts"][0]["uris"].append(dr["parts"][0]["uris"][0]))

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    first_add = next(e for e in env.log if e[0] == "add")
    assert len(first_add[2]) == len(set(first_add[2])) == 14


def test_done_stops_when_the_playlist_changed(env, monkeypatch):
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: "snap-2")

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    p = progress(env)
    assert p["state"] == "changed"
    assert env.log == []


def test_done_resumes_after_a_failure_without_duplicates(env, monkeypatch):
    real_add = appmod.sp.add_to_playlist
    failed = []

    def flaky(pid, uris, bulk=False, spend_reserve=False):
        if pid == "N2" and not failed:
            failed.append(1)
            raise SpotifyError(429, "rate limited")
        return real_add(pid, uris)

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)
    body = {"names": {}, "folder": None, "original": "subset"}

    env.post("/api/splitparts/H1/done", json=body)
    assert progress(env)["state"] == "error"
    assert "press Done again" in progress(env)["message"]
    assert not any(e[0] == "rename" for e in env.log)

    env.post("/api/splitparts/H1/done", json=body)
    assert progress(env)["state"] == "done"
    assert [e[0] for e in env.log].count("create") == 2
    assert [e[0] for e in env.log].count("rename") == 1


def test_second_done_while_running_is_refused(env):
    appmod._splitparts_job.update({"pid": "H1", "state": "running"})

    res = env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert res.status_code == 409
    assert env.log == []


def test_an_input_shaped_part_name_is_refused_before_any_call(env):
    res = env.post("/api/splitparts/H1/done", json={
        "names": {"p1": "[Oops]"}, "folder": None, "original": "subset"})

    assert res.status_code == 400
    assert env.log == []


def test_a_bad_original_choice_is_refused(env):
    assert env.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": None, "original": "part", "part": "p9"}).status_code == 400


def test_reread_keeps_moves_and_places_new_songs(env, monkeypatch):
    d = env.get("/api/splitparts/H1").json()
    moved = d["parts"][0]["uris"][3]
    env.post("/api/splitparts/H1/move", json={"uri": moved, "to": "p2"})
    gone = d["parts"][0]["uris"][0]
    new = {"uri": "spotify:track:new", "name": "Fresh", "is_local": False,
           "artists": [{"id": "a0", "name": "Artist 0"}]}
    changed = [t for t in TRACKS if t["uri"] != gone] + [new]
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: "snap-2")
    monkeypatch.setattr(appmod.sp, "playlist_tracks", lambda pid: list(changed))

    d2 = env.post("/api/splitparts/H1/reread").json()

    assert d2["snapshot_id"] == "snap-2"
    assert part_of(d2, moved) == "p2"
    assert all(gone not in p["uris"] for p in d2["parts"])
    assert part_of(d2, "spotify:track:new") == "p1"   # same artist as rock song 0
    assert "spotify:track:new" in d2["unknown"]
    assert d2["songs"]["spotify:track:new"]["name"] == "Fresh"


def test_reread_refuses_when_done_has_already_started(env):
    draft = spa.get_draft(appmod.store, "H1")
    draft["done"] = {"created": {"p1": "N1"}}
    spa.put_draft(appmod.store, draft)

    res = env.post("/api/splitparts/H1/reread")

    assert res.status_code == 409
    assert "Done has already started" in res.json()["detail"]


# ---- fix round 1 -------------------------------------------------------


def test_done_resumes_mid_batch_without_duplicating_added_songs(env, monkeypatch):
    """p1 grows past 100 songs (two add batches). The second add call (the
    second batch of p1) fails; resuming must continue from the batch that
    failed, not redo the first batch, or every uri of the big part would be
    added twice."""
    def grow(dr):
        p1 = dr["parts"][0]
        p1["uris"] = p1["uris"] + [f"spotify:track:extra{i}" for i in range(140)]
    edit_draft(grow)
    big_uris = list(spa.get_draft(appmod.store, "H1")["parts"][0]["uris"])
    assert len(big_uris) == 154

    real_add = appmod.sp.add_to_playlist
    calls = {"n": 0}

    def flaky(pid, uris, bulk=False, spend_reserve=False):
        calls["n"] += 1
        if calls["n"] == 2:
            raise SpotifyError(429, "rate limited")
        return real_add(pid, uris)

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)
    body = {"names": {}, "folder": None, "original": "subset"}

    env.post("/api/splitparts/H1/done", json=body)
    assert progress(env)["state"] == "error"

    env.post("/api/splitparts/H1/done", json=body)
    assert progress(env)["state"] == "done"

    adds = [e for e in env.log if e[0] == "add"]
    added_to_p1 = [u for e in adds for u in e[2] if e[1] == "N1"]
    assert sorted(added_to_p1) == sorted(big_uris)
    assert len(added_to_p1) == len(set(added_to_p1)) == 154
    all_added = [u for e in adds for u in e[2]]
    assert len(all_added) == len(set(all_added))


def test_done_refuses_an_unknown_folder_before_any_call(env):
    res = env.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": "ROOT / Nowhere", "original": "subset"})

    assert res.status_code == 400
    assert env.log == [] and env.filed == []


def test_done_refuses_a_blocked_folder_before_any_call(env):
    res = env.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": "ROOT / Deep", "original": "subset"})

    assert res.status_code == 400
    assert env.log == [] and env.filed == []


def test_discard_refuses_when_done_has_already_started(env):
    draft = spa.get_draft(appmod.store, "H1")
    draft["done"] = {"created": {"p1": "N1"}}
    spa.put_draft(appmod.store, draft)

    res = env.delete("/api/splitparts/H1")

    assert res.status_code == 409
    assert "Done has already started" in res.json()["detail"]
    assert env.get("/api/splitparts/H1").status_code == 200


def test_filing_uses_the_cached_listing_not_my_playlists(env, monkeypatch):
    def boom(refresh=False):
        raise AssertionError("filing must not pay for sp.my_playlists()")
    monkeypatch.setattr(appmod.sp, "my_playlists", boom)

    res = env.post("/api/splitparts/H1/done", json={
        "names": {"p1": "Rocky", "p2": "Jazzy"}, "folder": "ROOT / Mix", "original": "subset"})

    assert res.status_code == 200
    assert progress(env)["state"] == "done"
    assert [f[1] for f in env.filed] == ["Rocky", "Jazzy"]


def test_created_id_is_saved_before_home_ids_are_updated(env, monkeypatch):
    """A crash right after creating a part's playlist must not leave it an
    unrecorded orphan: the draft needs to know about it before the
    config write that marks it a home, not after. And the resumed run must
    still end up marking it a home — the id being pre-recorded must not
    make the home-marking write get skipped for good."""
    real_update_config = appmod.store.update_config
    calls = {"n": 0}

    def flaky_update_config(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_update_config(**kwargs)

    monkeypatch.setattr(appmod.store, "update_config", flaky_update_config)
    body = {"names": {}, "folder": None, "original": "subset"}

    env.post("/api/splitparts/H1/done", json=body)
    assert progress(env)["state"] == "error"

    draft = appmod.store.splitparts()["drafts"]["H1"]
    assert draft["done"]["created"].get("p1") is not None

    env.post("/api/splitparts/H1/done", json=body)
    assert progress(env)["state"] == "done"
    assert [e[0] for e in env.log].count("create") == 2   # p1 was not created twice
    cfg = appmod.store.config()
    assert {"N1", "N2"} <= set(cfg["home_ids"])
    assert {"N1", "N2"} <= set(cfg["sticky_home_ids"])


# ---- fix round 2 (Task 10 frontend review) ----------------------------


def test_progress_carries_kind_for_a_suggest_job(client):
    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert progress(client)["kind"] == "suggest"


def test_progress_kind_is_none_when_idle(client):
    assert progress(client)["kind"] is None


def test_progress_carries_kind_for_a_done_job(env):
    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert progress(env)["kind"] == "done"


def test_a_resumed_done_validates_against_the_stored_opts_not_the_retry_body(env, monkeypatch):
    """The first attempt chose "part"/"p1" with no folder and crashed mid-way
    (one playlist created, its add not yet done). A retry's body says
    "subset" and names the BLOCKED folder "ROOT / Deep" — a body like that
    alone would be refused outright at the folder check. Validation must
    check the stored opts (what `_splitparts_run_done` actually uses on a
    resume — it already ignores the body), not the retry's body, or a
    crash-and-retry could get stuck 400ing on a body the resumed run was
    never going to honour anyway. And the run itself must still follow the
    stored choice, not the retry body's "subset"."""
    real_add = appmod.sp.add_to_playlist
    failed = []

    def flaky(pid, uris, bulk=False, spend_reserve=False):
        if not failed:
            failed.append(1)
            raise SpotifyError(429, "rate limited")
        return real_add(pid, uris)

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)
    env.post("/api/splitparts/H1/done",
             json={"names": {}, "folder": None, "original": "part", "part": "p1"})
    assert progress(env)["state"] == "error"

    res = env.post("/api/splitparts/H1/done",
                   json={"names": {}, "folder": "ROOT / Deep", "original": "subset"})

    assert res.status_code == 200
    assert progress(env)["state"] == "done"
    assert not any(e[0] == "rename" for e in env.log)      # the stored "part" choice ran
    assert any(e[0] == "remove" for e in env.log)
    assert env.filed == []                                  # the stored folder (None) ran, not "ROOT / Deep"


# ---- fix round 2 (Task 10 re-review) -----------------------------------


def test_a_resumed_done_with_an_unoffered_folder_still_runs(env, monkeypatch):
    """The first attempt used "ROOT / Mix" (offered at the time) and crashed
    mid-way. The folder tree changes before the retry — "ROOT / Mix" is no
    longer offered at all. Every other escape (moves, a fresh suggest,
    discard) already 409s once Done has started, so refusing the retry too
    would strand the split with literally nothing left to press. Filing is
    post-hoc and best-effort, so the resume must still run (and still try
    to file into the now-unoffered folder, exactly as stored)."""
    real_add = appmod.sp.add_to_playlist
    failed = []

    def flaky(pid, uris, bulk=False, spend_reserve=False):
        if not failed:
            failed.append(1)
            raise SpotifyError(429, "rate limited")
        return real_add(pid, uris)

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)
    env.post("/api/splitparts/H1/done",
             json={"names": {"p1": "Rocky", "p2": "Jazzy"}, "folder": "ROOT / Mix", "original": "subset"})
    assert progress(env)["state"] == "error"

    appmod.store.save_folders({})   # the tree changed: "ROOT / Mix" is gone

    res = env.post("/api/splitparts/H1/done",
                   json={"names": {}, "folder": "ROOT / Mix", "original": "subset"})

    assert res.status_code == 200
    assert progress(env)["state"] == "done"
    assert [f[2] for f in env.filed] == ["ROOT / Mix", "ROOT / Mix"]


def test_a_first_attempt_still_refuses_an_unoffered_folder(env):
    """The folder check is skipped only for a RESUME — a first attempt
    (draft["done"] is still null) must keep refusing an unoffered folder
    before any call, same as before this round."""
    res = env.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": "ROOT / Nowhere", "original": "subset"})

    assert res.status_code == 400
    assert env.log == [] and env.filed == []


# ---- final review fixes ------------------------------------------------------


INPUT_SETS = [{"key": "buffer", "label": "buffer", "pattern": r"^\[.+\]$"},
              {"key": "other", "label": "other", "pattern": r"^<.*>$"},
              {"key": "the-bomb", "label": "the bomb", "path_segment": "THE BOMB"}]


@pytest.fixture
def sets_env(env):
    """Input sets configured, and no `input_name_pattern` at all — the shape
    the live config has."""
    appmod.store.update_config(input_name_pattern=None, input_sets=INPUT_SETS)
    appmod.store.save_folders({"H2": {"path": "ROOT / Mix"}, "H3": {"path": "ROOT / THE BOMB"}})
    return env


@pytest.mark.parametrize("name", ["[Oops]", "<Oops>"])
def test_a_part_name_an_input_set_would_claim_is_refused(sets_env, name):
    res = sets_env.post("/api/splitparts/H1/done", json={
        "names": {"p1": name}, "folder": None, "original": "subset"})

    assert res.status_code == 400
    assert "input" in res.json()["detail"]
    assert sets_env.log == [] and sets_env.calls == []


def test_a_folder_that_defines_an_input_set_is_refused(sets_env):
    res = sets_env.post("/api/splitparts/H1/done", json={
        "names": {"p1": "Rocky", "p2": "Jazzy"}, "folder": "ROOT / THE BOMB", "original": "subset"})

    assert res.status_code == 400
    assert "input set" in res.json()["detail"]
    assert sets_env.log == [] and sets_env.filed == []


def test_the_plan_marks_an_input_set_folder_blocked(sets_env):
    folders = sets_env.get("/api/splitparts/H1/plan", params={"original": "subset"}).json()["folders"]

    by_path = {f["path"]: f["blocked"] for f in folders}
    assert by_path["ROOT / Mix"] is None
    assert "input set" in by_path["ROOT / THE BOMB"]


def test_ordinary_names_and_folders_still_pass_with_input_sets(sets_env):
    res = sets_env.post("/api/splitparts/H1/done", json={
        "names": {"p1": "Rocky", "p2": "Jazzy"}, "folder": "ROOT / Mix", "original": "subset"})

    assert res.status_code == 200
    assert progress(sets_env)["state"] == "done"


# finding 1: the draft says whether it holds manual edits


def test_a_fresh_suggestion_is_not_edited(drafted):
    assert drafted.get("/api/splitparts/H1").json()["edited"] is False


def test_moves_and_part_changes_mark_the_draft_edited(drafted):
    d = drafted.get("/api/splitparts/H1").json()
    uri = d["parts"][0]["uris"][0]

    assert drafted.post("/api/splitparts/H1/move", json={"uri": uri, "to": "p2"}).json()["edited"] is True
    drafted.post("/api/splitparts/H1/suggest", json={"n": 2})
    assert drafted.get("/api/splitparts/H1").json()["edited"] is False

    assert drafted.post("/api/splitparts/H1/parts").json()["edited"] is True
    drafted.post("/api/splitparts/H1/suggest", json={"n": 3})
    dry = drafted.post("/api/splitparts/H1/parts/p3/remove", json={"dry": True})
    assert dry.status_code == 200
    assert drafted.get("/api/splitparts/H1").json()["edited"] is False   # a dry run edits nothing
    assert drafted.post("/api/splitparts/H1/parts/p3/remove", json={"dry": False}).json()["edited"] is True


# finding 2: names filing would refuse are refused at Done


@pytest.mark.parametrize("names", [{"p1": "Same", "p2": " same "}, {"p1": "tiny "}, {"p2": "[INBOX]"}])
def test_twin_part_names_are_refused_before_any_call(env, names):
    res = env.post("/api/splitparts/H1/done", json={"names": names, "folder": None, "original": "subset"})

    assert res.status_code == 400
    assert "called" in res.json()["detail"] or "input" in res.json()["detail"]
    assert env.log == [] and env.calls == []


def test_a_part_named_like_the_original_is_a_twin_only_when_the_original_keeps_its_name(env):
    res = env.post("/api/splitparts/H1/done", json={
        "names": {"p2": "big home"}, "folder": None, "original": "part", "part": "p1"})
    assert res.status_code == 400 and env.log == []

    # Kept as a subset, the original is renamed with the subset mark first.
    res = env.post("/api/splitparts/H1/done", json={
        "names": {"p2": "big home"}, "folder": None, "original": "subset"})
    assert res.status_code == 200 and progress(env)["state"] == "done"


# finding 3: which job the slot holds, without knowing its pid


def test_pidless_progress_is_idle_with_an_empty_slot(client):
    assert client.get("/api/splitparts/progress").json() == {
        "pid": None, "kind": None, "state": "idle", "summary": None}


def test_pidless_progress_names_the_last_job(env):
    p = env.get("/api/splitparts/progress").json()
    assert p["pid"] == "H1" and p["kind"] == "suggest"      # the fixture's own Suggest

    env.post("/api/splitparts/H1/done", json={"names": {"p1": "Rocky", "p2": "Jazzy"},
                                               "folder": None, "original": "subset"})

    p = env.get("/api/splitparts/progress").json()
    assert p["pid"] == "H1" and p["kind"] == "done" and p["state"] == "done"
    assert [c["name"] for c in p["summary"]["created"]] == ["Rocky", "Jazzy"]


# finding 4: a started Done can be abandoned, keeping what was made


def fail_second_create(monkeypatch, env):
    real = appmod.sp.create_playlist_full
    n = {"i": 0}

    def flaky(name, **kw):
        n["i"] += 1
        if n["i"] == 2:
            raise SpotifyError(404, "gone")
        return real(name, **kw)

    monkeypatch.setattr(appmod.sp, "create_playlist_full", flaky)


def test_abandon_drops_a_started_done_and_says_what_exists(env, monkeypatch):
    fail_second_create(monkeypatch, env)
    env.post("/api/splitparts/H1/done", json={"names": {"p1": "Rocky"}, "folder": None,
                                               "original": "subset"})
    assert progress(env)["state"] == "error"
    env.log.clear()
    env.calls.clear()

    res = env.post("/api/splitparts/H1/abandon")

    assert res.status_code == 200
    assert res.json() == {"created": [{"id": "N1", "name": "Rocky"}], "original_done": False}
    assert env.get("/api/splitparts/H1").status_code == 404
    assert env.log == [] and env.calls == []          # zero Spotify calls
    assert "N1" in appmod.store.config()["home_ids"]   # what was made stays a home


def test_abandon_refuses_a_draft_whose_done_never_started(drafted):
    res = drafted.post("/api/splitparts/H1/abandon")

    assert res.status_code == 409
    assert drafted.get("/api/splitparts/H1").status_code == 200


def test_abandon_refuses_while_a_job_runs_for_it(env, monkeypatch):
    fail_second_create(monkeypatch, env)
    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})
    appmod._splitparts_job.update({"pid": "H1", "state": "running"})

    assert env.post("/api/splitparts/H1/abandon").status_code == 409
    assert env.get("/api/splitparts/H1").status_code == 200


def test_abandon_of_a_missing_draft_is_404(client):
    assert client.post("/api/splitparts/H1/abandon").status_code == 404


# minors


def test_a_failed_first_create_leaves_the_split_unlocked(env, monkeypatch):
    def boom(name, **kw):
        raise SpotifyError(429, "rate limited")
    monkeypatch.setattr(appmod.sp, "create_playlist_full", boom)

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert progress(env)["state"] == "error"
    assert appmod.store.splitparts()["drafts"]["H1"]["done"] is None
    d = env.get("/api/splitparts/H1").json()
    uri = d["parts"][0]["uris"][0]
    assert env.post("/api/splitparts/H1/move", json={"uri": uri, "to": "p2"}).status_code == 200


def test_a_cooldown_says_to_wait_before_pressing_done_again(env, monkeypatch):
    def boom(name, **kw):
        raise SpotifyError(429, "rate limited")
    monkeypatch.setattr(appmod.sp, "create_playlist_full", boom)

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert progress(env)["message"].endswith("your split is saved; press Done again after the cooldown")


def test_an_ordinary_failure_still_says_press_done_again_to_continue(env, monkeypatch):
    def boom(name, **kw):
        raise SpotifyError(500, "server error")
    monkeypatch.setattr(appmod.sp, "create_playlist_full", boom)

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert progress(env)["message"].endswith("press Done again to continue")


def test_draft_answers_carry_no_track_list(env, monkeypatch):
    d = env.get("/api/splitparts/H1").json()
    uri = d["parts"][0]["uris"][0]
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: "snap-1")

    answers = [d,
               env.post("/api/splitparts/H1/move", json={"uri": uri, "to": "p2"}).json(),
               env.post("/api/splitparts/H1/parts").json(),
               env.post("/api/splitparts/H1/parts/p3/remove", json={"dry": False}).json(),
               env.post("/api/splitparts/H1/reread").json()]

    assert all("tracks" not in a and "parts" in a and "songs" in a for a in answers)
    assert appmod.store.splitparts()["drafts"]["H1"]["tracks"]   # still stored for the engine


def test_the_summary_names_the_folder_and_the_original(env):
    env.post("/api/splitparts/H1/done", json={"names": {"p1": "Rocky", "p2": "Jazzy"},
                                               "folder": "ROOT / Mix", "original": "subset"})

    s = progress(env)["summary"]
    assert s["folder"] == "ROOT / Mix" and s["name"] == "Big Home"


def test_the_parts_actions_sit_above_the_song_list():
    """A part can hold hundreds of songs; its actions (Done, Remove this
    part, Start over, Discard) must not be a long scroll away below it."""
    html = (appmod.STATIC / "index.html").read_text()
    chips, done_row = html.index('id="sp-chips"'), html.index('id="sp-done-row"')
    actions, song_list = html.index('id="sp-actions"'), html.index('id="sp-list"')
    assert chips < done_row < actions < song_list


# ---- a part's preview: the house player over the part's songs ---------------


@pytest.fixture
def previewing(drafted, monkeypatch):
    asked = []

    class FakeDeezer:
        def fetch_preview(self, artist, title, exclude=()):
            asked.append((artist, title, list(exclude)))
            return {"url": f"https://cdn/{title}.mp3", "deezer_id": 5, "miss": False}

    monkeypatch.setattr(appmod, "_deezer_client", lambda: FakeDeezer())
    appmod._preview_cache.clear()
    appmod.store.save_preview_rejects({})
    drafted.asked = asked
    yield drafted
    appmod._preview_cache.clear()
    appmod.store.save_preview_rejects({})


def part_uris(c, key):
    return next(p["uris"] for p in c.get("/api/splitparts/H1").json()["parts"] if p["key"] == key)


def test_a_part_preview_starts_at_the_tapped_song_in_the_parts_order(previewing):
    uris = part_uris(previewing, "p1")

    page = previewing.get("/api/splitparts/H1/preview",
                          params={"key": "p1", "start": uris[2]}).json()

    assert [c["uri"] for c in page["clips"]] == uris[2:2 + appmod.PREVIEW_CLIPS]
    assert page["next_offset"] == 2 + appmod.PREVIEW_CLIPS
    assert page["total"] == len(uris)
    assert set(page) == {"clips", "next_offset", "tracks", "total"}
    c0 = page["clips"][0]
    assert c0["deezer_id"] == 5 and c0["url"].startswith("https://cdn/") and c0["name"] and c0["artist"]
    assert previewing.calls == []   # zero Spotify calls


def test_the_next_page_carries_on_down_the_part(previewing):
    uris = part_uris(previewing, "p1")
    first = previewing.get("/api/splitparts/H1/preview",
                           params={"key": "p1", "start": uris[2]}).json()

    page = previewing.get("/api/splitparts/H1/preview",
                          params={"key": "p1", "offset": first["next_offset"]}).json()

    assert [c["uri"] for c in page["clips"]] == uris[5:5 + appmod.PREVIEW_CLIPS]


def test_without_a_start_the_part_plays_from_its_top(previewing):
    uris = part_uris(previewing, "p2")

    page = previewing.get("/api/splitparts/H1/preview", params={"key": "p2"}).json()

    assert [c["uri"] for c in page["clips"]] == uris[:appmod.PREVIEW_CLIPS]
    assert page["total"] == len(uris)


def test_the_last_page_says_the_part_is_walked(previewing):
    uris = part_uris(previewing, "p2")

    page = previewing.get("/api/splitparts/H1/preview",
                          params={"key": "p2", "start": uris[-1]}).json()

    assert [c["uri"] for c in page["clips"]] == [uris[-1]]
    assert page["next_offset"] is None


def test_local_files_and_nameless_songs_are_never_offered(previewing):
    uris = part_uris(previewing, "p1")

    def strip(d):
        for t in d["tracks"]:
            if t["uri"] == uris[0]:
                t["is_local"] = True
            if t["uri"] == uris[1]:
                t["name"] = ""
            if t["uri"] == uris[2]:
                t["artists"] = []
    edit_draft(strip)

    page = previewing.get("/api/splitparts/H1/preview", params={"key": "p1"}).json()

    assert [c["uri"] for c in page["clips"]] == uris[3:3 + appmod.PREVIEW_CLIPS]
    # a start that is not a candidate starts at the top of the candidates
    page = previewing.get("/api/splitparts/H1/preview",
                          params={"key": "p1", "start": uris[0]}).json()
    assert page["clips"][0]["uri"] == uris[3]


def test_a_rejected_recording_is_excluded_from_the_search(previewing):
    uris = part_uris(previewing, "p1")
    appmod.store.save_preview_rejects({uris[0]: {"artist": "", "title": "", "rejected": [77]}})

    previewing.get("/api/splitparts/H1/preview", params={"key": "p1", "start": uris[0]})

    assert previewing.asked[0][2] == [77]


def test_a_deezer_failure_or_miss_skips_that_song(previewing, monkeypatch):
    uris = part_uris(previewing, "p1")
    songs = appmod.store.splitparts()["drafts"]["H1"]["songs"]
    broken, missing = songs[uris[0]]["name"], songs[uris[1]]["name"]

    class Flaky:
        def fetch_preview(self, artist, title, exclude=()):
            if title == broken:
                raise RuntimeError("deezer down")
            if title == missing:
                return {"miss": True}
            return {"url": f"https://cdn/{title}.mp3", "deezer_id": 6, "miss": False}

    monkeypatch.setattr(appmod, "_deezer_client", lambda: Flaky())

    page = previewing.get("/api/splitparts/H1/preview", params={"key": "p1"})

    assert page.status_code == 200
    assert [c["uri"] for c in page.json()["clips"]] == uris[2:2 + appmod.PREVIEW_CLIPS]


def test_no_draft_or_no_such_part_is_a_404(previewing):
    assert previewing.get("/api/splitparts/H2/preview", params={"key": "p1"}).status_code == 404
    assert previewing.get("/api/splitparts/H1/preview", params={"key": "p9"}).status_code == 404


def test_a_moved_song_never_comes_back_from_a_cached_page(previewing):
    uris = part_uris(previewing, "p1")
    previewing.get("/api/splitparts/H1/preview", params={"key": "p1", "start": uris[0]})
    previewing.post("/api/splitparts/H1/move", json={"uri": uris[1], "to": "p2"})

    page = previewing.get("/api/splitparts/H1/preview",
                          params={"key": "p1", "start": uris[0]}).json()

    assert uris[1] not in [c["uri"] for c in page["clips"]]
    assert page["total"] == len(uris) - 1


# ---- move one song to another playlist instead -------------------------------

ALPHA = {"id": "A1", "name": "alpha", "owner": "me", "editable": True,
         "total": 3, "snapshot_id": "snap-a", "image": None, "description": ""}
ARCHIVED = {"id": "Z1", "name": "🗄️ old stuff", "owner": "me", "editable": True,
            "total": 9, "snapshot_id": "snap-z", "image": None, "description": ""}
THEIRS = {"id": "T1", "name": "Theirs", "owner": "them", "editable": False,
          "total": 50, "snapshot_id": "snap-t", "image": None, "description": ""}
PLAIN = {"id": "P1", "name": "just a playlist", "owner": "me", "editable": True,
         "total": 7, "snapshot_id": "snap-p", "image": None, "description": ""}
LIKED = {"id": "liked", "name": "Liked Songs", "owner": "you", "editable": True,
         "total": 900, "snapshot_id": None, "image": None, "description": ""}


@pytest.fixture
def elsewhere(drafted, monkeypatch):
    cache = appmod.store.cache()
    cache["playlist_list"]["items"] = [HOME, SMALL, OTHER, ALPHA, ARCHIVED, THEIRS, LIKED]
    cache["playlists"]["A1"] = {"snapshot_id": "snap-a", "tracks": [], "fetched_at": 1.0}
    appmod.store.save_cache(cache)
    appmod.store.save_folders({"A1": {"path": "ROOT / Mix"}, "H2": {"path": "ROOT"}})
    cfg = appmod.store.config()
    cfg["home_ids"] = ["H1", "H2", "A1", "Z1", "T1"]
    appmod.store.save_config(cfg)
    log = []

    def add(pid, uris, bulk=False, spend_reserve=False):
        log.append(("add", pid, [uris] if isinstance(uris, str) else list(uris)))
        return "snap-added"

    def remove(pid, uri):
        log.append(("remove", pid, [uri]))
        return "snap-removed"

    def remove_many(pid, uris):
        log.append(("remove", pid, list(uris)))
        return "snap-removed"

    def listing(refresh=False):
        log.append(("my_playlists",))
        return [HOME, SMALL, OTHER, ALPHA, ARCHIVED, THEIRS]

    def snapshot(pid):
        log.append(("snapshot", pid))
        return snaps[pid]

    # What Spotify says now: the draft holds snap-1, A1's track cache snap-a.
    snaps = {"H1": "snap-1", "A1": "snap-a"}
    monkeypatch.setattr(appmod.sp, "snapshot", snapshot)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", add)
    monkeypatch.setattr(appmod.sp, "remove_from_playlist", remove)
    monkeypatch.setattr(appmod.sp, "remove_many", remove_many)
    monkeypatch.setattr(appmod.sp, "my_playlists", listing)
    drafted.log = log
    drafted.snaps = snaps
    return drafted


def stored():
    return appmod.store.splitparts()["drafts"]["H1"]


def test_targets_are_the_other_editable_homes_sorted_with_folders(elsewhere):
    res = elsewhere.get("/api/splitparts/H1/targets")

    assert res.status_code == 200
    targets = res.json()["targets"]
    # Homes only: the [Inbox] input is left out, and so are an archived and a
    # read-only playlist even when they sit in home_ids.
    assert [t["id"] for t in targets] == ["A1", "H2"]   # "alpha" < "tiny"
    assert targets[0] == {"id": "A1", "name": "alpha", "folder": "ROOT / Mix"}
    assert elsewhere.log == [] and elsewhere.calls == []      # the cached listing, zero calls


def test_a_song_cannot_be_moved_to_a_playlist_that_is_neither_a_home_nor_an_input(elsewhere):
    cache = appmod.store.cache()
    cache["playlist_list"]["items"].append(PLAIN)
    appmod.store.save_cache(cache)
    uri = stored()["parts"][0]["uris"][0]

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "P1"})

    assert res.status_code == 400
    assert elsewhere.log == []


def test_moving_a_song_elsewhere_adds_first_then_removes(elsewhere):
    uri = stored()["parts"][0]["uris"][0]

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    assert res.status_code == 200, res.text
    assert elsewhere.log == [("snapshot", "H1"), ("add", "A1", [uri]), ("remove", "H1", [uri])]
    body = res.json()
    assert body["moved_to"] == {"id": "A1", "name": "alpha"}
    assert "tracks" not in body
    assert all(uri not in p["uris"] for p in body["parts"])
    assert uri not in body["songs"] and body["edited"] is True


def test_the_draft_forgets_the_song_and_takes_the_new_snapshot(elsewhere):
    d = stored()
    uri = d["parts"][0]["uris"][0]
    d["unknown"] = [uri]
    appmod.store.save_splitparts({"version": 1, "drafts": {"H1": d}})

    elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    d = stored()
    assert all(uri not in p["uris"] for p in d["parts"])
    assert uri not in d["unknown"] and uri not in d["songs"]
    assert uri not in [t["uri"] for t in d["tracks"]]
    assert d["snapshot_id"] == "snap-removed" and d["edited"] is True
    assert sum(len(p["uris"]) for p in d["parts"]) == 23


def test_the_local_cache_and_listing_follow_the_move(elsewhere):
    uri = stored()["parts"][0]["uris"][0]

    elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    cache = appmod.store.cache()
    assert uri not in [t["uri"] for t in cache["playlists"]["H1"]["tracks"]]
    assert uri in [t["uri"] for t in cache["playlists"]["A1"]["tracks"]]
    assert cache["playlists"]["H1"]["snapshot_id"] == "snap-removed"
    assert cache["playlists"]["A1"]["snapshot_id"] == "snap-added"
    by_id = {p["id"]: p for p in cache["playlist_list"]["items"]}
    assert by_id["H1"]["snapshot_id"] == "snap-removed" and by_id["H1"]["total"] == 23
    assert by_id["A1"]["total"] == 4


def test_a_split_song_moved_elsewhere_lets_done_go_ahead(elsewhere, monkeypatch):
    uri = stored()["parts"][0]["uris"][0]
    elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: "snap-removed")
    monkeypatch.setattr(appmod.sp, "create_playlist_full",
                        lambda name, **kw: (f"N-{name}", f"snap-N-{name}"))
    monkeypatch.setattr(appmod.sp, "rename_playlist", lambda pid, name: None)

    elsewhere.post("/api/splitparts/H1/done", json={"names": {"p1": "Rocky", "p2": "Jazzy"},
                                                    "original": "subset"})

    prog = elsewhere.get("/api/splitparts/H1/progress").json()
    assert prog["state"] == "done", prog


@pytest.mark.parametrize("bad", ["unknown", "local", "self", "archived", "theirs", "liked", "nope"])
def test_refusals_cost_nothing_and_change_nothing(elsewhere, bad):
    d = stored()
    uri = d["parts"][0]["uris"][0]
    to = {"self": "H1", "archived": "Z1", "theirs": "T1", "liked": "liked", "nope": "NOPE"}.get(bad, "A1")
    if bad == "unknown":
        uri = "spotify:track:never"
    if bad == "local":
        edit_draft(lambda d: d["songs"][uri].update(local=True))
    before = stored()

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": to})

    assert res.status_code == 400, res.text
    assert elsewhere.log == []
    assert stored() == before


def test_refused_once_done_has_started(elsewhere):
    edit_draft(lambda d: d.update(done={"opts": {}, "created": {}, "filled": [], "original_done": False}))
    uri = stored()["parts"][0]["uris"][0]

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    assert res.status_code == 409
    assert res.json()["detail"] == appmod._SPLITPARTS_DONE_STARTED_MSG
    assert elsewhere.log == []


def test_refused_while_a_job_runs_for_this_split(elsewhere):
    appmod._splitparts_job.update({"pid": "H1", "state": "running"})
    uri = stored()["parts"][0]["uris"][0]

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    assert res.status_code == 409 and "already running" in res.json()["detail"]
    assert elsewhere.log == []


def test_no_draft_is_a_404(elsewhere):
    res = elsewhere.post("/api/splitparts/H2/elsewhere", json={"uri": "x", "to_id": "A1"})

    assert res.status_code == 404
    assert elsewhere.log == []


def test_a_failed_add_changes_nothing(elsewhere, monkeypatch):
    def add(pid, uris, bulk=False, spend_reserve=False):
        raise SpotifyError(500, "boom")
    monkeypatch.setattr(appmod.sp, "add_to_playlist", add)
    before = stored()
    uri = before["parts"][0]["uris"][0]

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    assert res.status_code >= 400
    assert elsewhere.log == [("snapshot", "H1")] and stored() == before


def test_added_but_not_removed_says_it_is_in_both_and_keeps_the_draft(elsewhere, monkeypatch):
    def remove(pid, uri):
        elsewhere.log.append(("remove", pid, [uri]))
        raise SpotifyError(500, "boom")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist", remove)
    monkeypatch.setattr(appmod.sp, "remove_many", lambda pid, uris: remove(pid, uris[0]))
    before = stored()
    uri = before["parts"][0]["uris"][0]

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    assert res.status_code >= 400
    assert "both" in res.json()["detail"] and "alpha" in res.json()["detail"]
    assert [e[0] for e in elsewhere.log] == ["snapshot", "add", "remove"]
    assert stored() == before


# ---- fix round 1 ----------------------------------------------------------


def _cache_holds(uri, pid="A1"):
    cache = appmod.store.cache()
    cache["playlists"][pid]["tracks"] = [{"uri": uri, "name": "x", "artists": []}]
    appmod.store.save_cache(cache)


def test_a_destination_proven_to_hold_the_song_gets_no_second_add(elsewhere):
    """The retry after a "now in both playlists" error is then a clean
    recovery: only the removal is left to do — but only once the cached
    destination is shown to be current (its snapshot still matches)."""
    uri = stored()["parts"][0]["uris"][0]
    _cache_holds(uri)

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    assert res.status_code == 200, res.text
    assert elsewhere.log == [("snapshot", "H1"), ("snapshot", "A1"), ("remove", "H1", [uri])]
    assert res.json()["moved_to"] == {"id": "A1", "name": "alpha"}
    assert all(uri not in p["uris"] for p in stored()["parts"])
    assert [t["uri"] for t in appmod.store.cache()["playlists"]["A1"]["tracks"]] == [uri]


def test_a_stale_cached_destination_still_gets_the_add(elsewhere):
    """A days-old cache can hold a song the user since removed from the
    destination in Spotify: skipping the add then would leave the song in
    neither playlist. A possible duplicate is the safe direction."""
    uri = stored()["parts"][0]["uris"][0]
    _cache_holds(uri)
    elsewhere.snaps["A1"] = "snap-a-moved-on"

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    assert res.status_code == 200, res.text
    assert elsewhere.log == [("snapshot", "H1"), ("snapshot", "A1"),
                             ("add", "A1", [uri]), ("remove", "H1", [uri])]


def test_a_cached_destination_without_a_snapshot_still_gets_the_add(elsewhere):
    uri = stored()["parts"][0]["uris"][0]
    _cache_holds(uri)
    cache = appmod.store.cache()
    cache["playlists"]["A1"]["snapshot_id"] = None
    appmod.store.save_cache(cache)

    elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    # nothing to compare against: no destination read, just the add
    assert elsewhere.log == [("snapshot", "H1"), ("add", "A1", [uri]), ("remove", "H1", [uri])]


def test_a_playlist_changed_in_spotify_is_refused_before_any_write(elsewhere):
    elsewhere.snaps["H1"] = "snap-9"
    before = stored()
    uri = before["parts"][0]["uris"][0]

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "A1"})

    assert res.status_code == 409
    assert res.json()["detail"] == appmod._SPLITPARTS_CHANGED_MSG
    assert "re-read" in appmod._SPLITPARTS_CHANGED_MSG
    assert elsewhere.log == [("snapshot", "H1")]
    assert stored() == before


def test_a_second_move_matches_the_snapshot_the_first_one_left(elsewhere):
    first, second = stored()["parts"][0]["uris"][:2]
    elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": first, "to_id": "A1"})
    elsewhere.snaps["H1"] = "snap-removed"     # Spotify's snapshot after that removal

    res = elsewhere.post("/api/splitparts/H1/elsewhere", json={"uri": second, "to_id": "A1"})

    assert res.status_code == 200, res.text


def test_a_tap_on_an_unplayable_song_starts_at_the_next_playable_one(previewing):
    uris = part_uris(previewing, "p1")
    edit_draft(lambda d: [t.update(is_local=True) for t in d["tracks"] if t["uri"] in (uris[3], uris[-1])])

    page = previewing.get("/api/splitparts/H1/preview",
                          params={"key": "p1", "start": uris[3]}).json()
    assert page["clips"][0]["uri"] == uris[4]

    # nothing playable after it: the top of the part
    page = previewing.get("/api/splitparts/H1/preview",
                          params={"key": "p1", "start": uris[-1]}).json()
    assert page["clips"][0]["uri"] == uris[0]

    # not in the part at all: the top of the part
    page = previewing.get("/api/splitparts/H1/preview",
                          params={"key": "p1", "start": "spotify:track:elsewhere"}).json()
    assert page["clips"][0]["uri"] == uris[0]


def test_split_preview_pages_are_keyed_on_a_hash_and_expired_ones_go(previewing):
    import time as _time
    appmod._preview_cache[("stale",)] = (_time.monotonic() - appmod.PREVIEW_TTL - 1, {})

    previewing.get("/api/splitparts/H1/preview", params={"key": "p1"})

    assert ("stale",) not in appmod._preview_cache
    split_keys = [k for k in appmod._preview_cache if k[0] == "split"]
    assert split_keys
    for k in split_keys:
        assert isinstance(k[-1], str) and len(k[-1]) == 40 and all(c in "0123456789abcdef" for c in k[-1])


def test_the_song_list_clears_the_open_preview_player():
    """The house player floats over the bottom of the screen; while it is
    open the last rows of a part must still scroll clear of it."""
    import re
    css = (appmod.STATIC / "style.css").read_text()
    js = (appmod.STATIC / "app.js").read_text()
    assert 'document.body.classList.add("previewing")' in js
    m = re.search(r"body\.previewing\s+#sp-list\s*\{[^}]*padding-bottom:\s*[^;]+;", css)
    assert m, "no padding under #sp-list while previewing"


# ---- hold a song to play it in Spotify ---------------------------------


def test_play_starts_the_song_in_the_playlist_being_split(drafted, monkeypatch):
    played = []
    monkeypatch.setattr(appmod.sp, "play_in_context",
                        lambda pid, uri: played.append((pid, uri)), raising=False)
    uri = drafted.get("/api/splitparts/H1").json()["parts"][0]["uris"][0]

    res = drafted.post("/api/splitparts/H1/play", json={"uri": uri})

    assert res.status_code == 200 and res.json() == {"ok": True}
    assert played == [("H1", uri)]


def test_play_refuses_a_song_not_in_the_split(drafted, monkeypatch):
    played = []
    monkeypatch.setattr(appmod.sp, "play_in_context",
                        lambda pid, uri: played.append((pid, uri)), raising=False)

    res = drafted.post("/api/splitparts/H1/play", json={"uri": "spotify:track:nope"})

    assert res.status_code == 400 and played == []


def test_play_without_a_device_says_so(drafted, monkeypatch):
    from sortify.spotify import SpotifyError

    def no_device(pid, uri):
        raise SpotifyError(404, "no active device")
    monkeypatch.setattr(appmod.sp, "play_in_context", no_device, raising=False)
    uri = drafted.get("/api/splitparts/H1").json()["parts"][0]["uris"][0]

    res = drafted.post("/api/splitparts/H1/play", json={"uri": uri})

    assert res.status_code == 400 and "No active Spotify device" in res.json()["detail"]


def test_play_in_context_starts_the_playlist_at_that_song(monkeypatch):
    from sortify import spotify as spmod
    sent = []
    client = spmod.Spotify.__new__(spmod.Spotify)
    monkeypatch.setattr(client, "request", lambda method, path, **kw: sent.append((method, path, kw)), raising=False)

    client.play_in_context("P1", "spotify:track:t1")

    assert sent == [("PUT", "/me/player/play", {"json": {
        "context_uri": "spotify:playlist:P1", "offset": {"uri": "spotify:track:t1"}}})]


# ---- a part can go into an existing home at Done -----------------------------
#
# Brief 2026-10-21 ("At Done"): instead of becoming a new playlist, a part's
# songs can be added to a home that already exists. Nothing is created for
# it; a song the home already holds is not added again, judged only from a
# track list proven current by the home's snapshot.

THROTTLE = {"id": "TB", "name": "THROTTLE BACK", "owner": "me", "editable": True,
            "total": 2, "snapshot_id": "snap-tb-0", "image": None, "description": ""}


def _track(uri):
    return {"uri": uri, "name": f"held {uri}", "artists": [{"id": "ah", "name": "Held"}],
            "is_local": False}


@pytest.fixture
def into(env, monkeypatch):
    """`env`, plus a small model of Spotify for the homes a part goes into:
    each add appends and moves the snapshot on, as Spotify does, so a
    resume sees what an earlier attempt really left there."""
    d = spa.get_draft(appmod.store, "H1")
    p2 = d["parts"][1]["uris"]
    held = p2[0]                                    # TB already holds one of p2's songs
    spot = {"TB": {"n": 0, "uris": ["spotify:track:other", held]}}
    cache = appmod.store.cache()
    cache["playlist_list"]["items"] = [HOME, SMALL, OTHER, dict(THROTTLE), ARCHIVED, LIKED]
    cache["playlists"]["TB"] = {"snapshot_id": "snap-tb-0", "fetched_at": 1.0,
                                "tracks": [_track(u) for u in spot["TB"]["uris"]]}
    appmod.store.save_cache(cache)
    appmod.store.update_config(home_ids=["H1", "H2", "H3", "TB", "Z1"])
    log = env.log

    def snap_of(pid):
        return f"snap-{pid.lower()}-{spot[pid]['n']}"

    def snapshot(pid):
        log.append(("snapshot", pid))
        return snap_of(pid) if pid in spot else "snap-1"

    def tracks(pid):
        log.append(("tracks", pid))
        return [_track(u) for u in spot[pid]["uris"]] if pid in spot else list(TRACKS)

    def add(pid, uris, bulk=False, spend_reserve=False):
        log.append(("add", pid, list(uris)))
        if pid in spot:
            spot[pid]["uris"] += list(uris)
            spot[pid]["n"] += 1
            return snap_of(pid)
        return f"snap-add-{pid}"

    monkeypatch.setattr(appmod.sp, "snapshot", snapshot)
    monkeypatch.setattr(appmod.sp, "playlist_tracks", tracks)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", add)
    env.spot = spot
    env.held = held
    env.p2 = list(p2)
    env.p1 = list(d["parts"][0]["uris"])
    return env


def grow_p2(n):
    """More songs in p2, held the way a real draft holds them: in the part,
    in `tracks` and in `songs`."""
    def grow(dr):
        for i in range(n):
            u = f"spotify:track:x{i}"
            dr["parts"][1]["uris"].append(u)
            dr["tracks"].append({"uri": u, "name": f"Extra {i}", "artists": [], "is_local": False})
            dr["songs"][u] = {"name": f"Extra {i}", "artists": "", "local": False}
    edit_draft(grow)


def into_adds(c, pid="TB"):
    return [u for e in c.log if e[0] == "add" and e[1] == pid for u in e[2]]


def test_plan_prices_a_part_going_into_an_existing_home(into):
    plan = into.get("/api/splitparts/H1/plan",
                    params={"original": "subset", "into": "p2:TB"}).json()

    # snapshot + (create + 1 add) for p1 + (TB's snapshot + 1 add) for p2 + rename
    assert plan["calls"] == 6
    assert plan["creates"] == 1
    # 10 songs, one already in the cached (assumed current) list: 9 to add
    assert plan["into_parts"] == [{"key": "p2", "to_id": "TB", "to_name": "THROTTLE BACK", "count": 9}]
    assert into.log == [] and into.calls == []           # the plan itself costs nothing


def test_the_plan_without_into_lists_no_into_parts(into):
    plan = into.get("/api/splitparts/H1/plan", params={"original": "subset"}).json()

    assert plan["into_parts"] == [] and plan["creates"] == 2


def test_the_plan_counts_a_read_when_nothing_of_the_home_is_cached(into):
    cache = appmod.store.cache()
    del cache["playlists"]["TB"]
    appmod.store.save_cache(cache)

    plan = into.get("/api/splitparts/H1/plan",
                    params={"original": "subset", "into": "p2:TB"}).json()

    # nothing cached to trust: the track read (TB holds 2, one page) is certain
    assert plan["into_parts"][0]["count"] == 10
    assert plan["calls"] == 7


def test_done_adds_a_part_to_the_home_and_creates_nothing_for_it(into):
    before = appmod.store.config()
    res = into.post("/api/splitparts/H1/done", json={
        "names": {"p1": "Rocky", "p2": "[Not a name]"}, "folder": "ROOT / Mix",
        "original": "subset", "into": {"p2": "TB"}})
    assert res.status_code == 200, res.text

    p = progress(into)
    assert p["state"] == "done", p
    creates = [e for e in into.log if e[0] == "create"]
    assert [e[1] for e in creates] == ["Rocky"]
    assert into_adds(into) == [u for u in into.p2 if u != into.held]
    cfg = appmod.store.config()
    assert "TB" in cfg["home_ids"] and "TB" not in cfg["sticky_home_ids"]
    assert set(cfg["sticky_home_ids"]) - set(before.get("sticky_home_ids") or []) == {"N1"}
    assert into.filed == [("N1", "Rocky", "ROOT / Mix")]  # no filing for an into-part
    assert p["summary"]["created"] == [{"id": "N1", "name": "Rocky", "count": 14}]
    assert p["summary"]["into"] == [{"key": "p2", "id": "TB", "name": "THROTTLE BACK",
                                     "count": 9, "already": 1}]
    # every write to TB came after the snapshot that proved its cache
    first_tb = next(i for i, e in enumerate(into.log) if e[1:2] == ("TB",))
    assert into.log[first_tb] == ("snapshot", "TB")
    assert ("tracks", "TB") not in into.log


def test_into_adds_go_in_batches_of_100(into):
    grow_p2(140)

    into.post("/api/splitparts/H1/done", json={"names": {}, "folder": None,
                                                "original": "subset", "into": {"p2": "TB"}})

    assert progress(into)["state"] == "done"
    batches = [e[2] for e in into.log if e[0] == "add" and e[1] == "TB"]
    assert [len(b) for b in batches] == [100, 49]
    assert progress(into)["summary"]["into"][0]["count"] == 149


def test_the_homes_cache_and_listing_follow_the_adds(into):
    into.post("/api/splitparts/H1/done", json={"names": {}, "folder": None,
                                                "original": "subset", "into": {"p2": "TB"}})

    cache = appmod.store.cache()
    entry = cache["playlists"]["TB"]
    assert entry["snapshot_id"] == "snap-tb-1"
    assert [t["uri"] for t in entry["tracks"]] == into.spot["TB"]["uris"]
    tb = next(p for p in cache["playlist_list"]["items"] if p["id"] == "TB")
    assert tb["snapshot_id"] == "snap-tb-1" and tb["total"] == 11


def test_a_stale_cache_is_reread_and_a_song_it_wrongly_listed_is_added(into):
    # In Spotify the held song was taken out of TB since the cache was read.
    into.spot["TB"]["uris"].remove(into.held)
    into.spot["TB"]["n"] = 7

    into.post("/api/splitparts/H1/done", json={"names": {}, "folder": None,
                                                "original": "part", "part": "p1",
                                                "into": {"p2": "TB"}})

    assert progress(into)["state"] == "done"
    assert ("tracks", "TB") in into.log
    assert into_adds(into) == into.p2                     # the held song too: never lost
    assert progress(into)["summary"]["into"][0]["count"] == 10
    removed = next(e for e in into.log if e[0] == "remove")
    assert into.held in removed[2]


def test_a_resume_after_a_failed_batch_neither_duplicates_nor_skips(into, monkeypatch):
    grow_p2(140)
    want = [u for u in spa.get_draft(appmod.store, "H1")["parts"][1]["uris"] if u != into.held]
    real_add = appmod.sp.add_to_playlist
    seen = {"n": 0}

    def flaky(pid, uris, bulk=False, spend_reserve=False):
        if pid == "TB":
            seen["n"] += 1
            if seen["n"] == 2:
                raise SpotifyError(429, "rate limited")
        return real_add(pid, uris)

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)
    body = {"names": {}, "folder": None, "original": "part", "part": "p1", "into": {"p2": "TB"}}

    into.post("/api/splitparts/H1/done", json=body)
    assert progress(into)["state"] == "error"
    assert not any(e[0] == "remove" for e in into.log)    # the original waits

    res = into.post("/api/splitparts/H1/done", json=body)
    assert res.status_code == 200, res.text
    assert progress(into)["state"] == "done"
    added = into_adds(into)
    assert sorted(added) == sorted(want) and len(added) == len(set(added))
    assert progress(into)["summary"]["into"][0]["count"] == len(want)


def test_a_batch_that_landed_before_its_answer_failed_is_not_added_again(into, monkeypatch):
    """The add reached Spotify but its response never came back: the next
    attempt reads TB's snapshot, finds it moved on, and re-reads the list."""
    real_add = appmod.sp.add_to_playlist
    lost = []

    def lossy(pid, uris, bulk=False, spend_reserve=False):
        real_add(pid, uris)
        if pid == "TB" and not lost:
            lost.append(1)
            raise SpotifyError(500, "connection reset")
        return f"snap-tb-{into.spot['TB']['n']}"

    monkeypatch.setattr(appmod.sp, "add_to_playlist", lossy)
    body = {"names": {}, "folder": None, "original": "subset", "into": {"p2": "TB"}}
    into.post("/api/splitparts/H1/done", json=body)
    assert progress(into)["state"] == "error"

    into.post("/api/splitparts/H1/done", json=body)

    assert progress(into)["state"] == "done"
    assert into.spot["TB"]["uris"].count(into.p2[1]) == 1
    assert len(into.spot["TB"]["uris"]) == len(set(into.spot["TB"]["uris"]))


def test_the_original_is_touched_only_after_the_into_part_and_loses_its_songs(into):
    res = into.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": None, "original": "part", "part": "p1", "into": {"p2": "TB"}})
    assert res.status_code == 200, res.text

    assert progress(into)["state"] == "done"
    kinds = [e[0] for e in into.log]
    assert "create" not in kinds
    last_tb = max(i for i, e in enumerate(into.log) if e[1:2] == ("TB",))
    remove_at = kinds.index("remove")
    assert remove_at > last_tb
    assert sorted(into.log[remove_at][2]) == sorted(into.p2)   # the held song leaves H1 too
    assert progress(into)["summary"]["created"] == []


def test_two_parts_can_go_into_the_same_home(into):
    res = into.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": None, "original": "subset", "into": {"p1": "TB", "p2": "TB"}})
    assert res.status_code == 200, res.text

    assert progress(into)["state"] == "done"
    added = into_adds(into)
    assert sorted(added) == sorted(into.p1 + [u for u in into.p2 if u != into.held])
    assert not any(e[0] == "create" for e in into.log)


@pytest.mark.parametrize("bad", ["X1", "H1", "Z1", "liked", "NOPE", "T1"])
def test_a_target_that_is_not_an_offered_home_is_refused_before_any_call(into, bad):
    if bad == "T1":
        cache = appmod.store.cache()
        cache["playlist_list"]["items"].append(THEIRS)
        appmod.store.save_cache(cache)
        appmod.store.update_config(home_ids=["H1", "H2", "H3", "TB", "Z1", "T1"])
    before = stored()

    res = into.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": None, "original": "subset", "into": {"p2": bad}})
    plan = into.get("/api/splitparts/H1/plan", params={"original": "subset", "into": f"p2:{bad}"})

    assert res.status_code == 400, res.text
    assert plan.status_code == 400
    assert into.log == [] and stored() == before


@pytest.mark.parametrize("into_", [{"p1": "TB"}, {"p9": "TB"}])
def test_the_kept_part_or_an_unknown_part_cannot_go_into_a_home(into, into_):
    res = into.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": None, "original": "part", "part": "p1", "into": into_})

    assert res.status_code == 400
    assert into.log == []


def test_a_malformed_into_on_the_plan_is_a_400(into):
    res = into.get("/api/splitparts/H1/plan", params={"original": "subset", "into": "p2"})

    assert res.status_code == 400


def test_at_least_two_counts_destinations_not_created_playlists(into):
    # kept part + one into-part: two destinations and nothing created
    ok = into.get("/api/splitparts/H1/plan",
                  params={"original": "part", "part": "p1", "into": "p2:TB"})
    assert ok.status_code == 200 and ok.json()["creates"] == 0

    # every song into one home, the original kept as a subset: one destination
    edit_draft(lambda dr: dr["parts"][0]["uris"].extend(dr["parts"][1]["uris"])
               or dr["parts"][1].update(uris=[]))
    res = into.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": None, "original": "subset", "into": {"p1": "TB"}})
    assert res.status_code == 400 and "at least 2" in res.json()["detail"]
    assert into.log == []


def test_a_resumed_done_keeps_its_into_target_even_once_it_is_no_longer_offered(into, monkeypatch):
    """Decided: a resume keeps the opts it started with, `into` included.
    Refusing would strand the split (every other action 409s once Done has
    started), and the run only ever adds to the home the user confirmed —
    no song can be lost by finishing it."""
    def boom(name, **kw):
        into.log.append(("create", name, None))
        raise SpotifyError(429, "rate limited")

    real_create = appmod.sp.create_playlist_full
    monkeypatch.setattr(appmod.sp, "create_playlist_full", boom)
    body = {"names": {}, "folder": None, "original": "subset", "into": {"p2": "TB"}}
    into.post("/api/splitparts/H1/done", json=body)
    assert progress(into)["state"] == "error"
    # p1's create failed before anything was written: not started, so a
    # changed target is still refused on the next first attempt
    assert stored()["done"] is None

    # This time p1 is created, then the into-part's add fails.
    monkeypatch.setattr(appmod.sp, "create_playlist_full", real_create)
    real_add = appmod.sp.add_to_playlist
    failed = []

    def flaky(pid, uris, bulk=False, spend_reserve=False):
        if pid == "TB" and not failed:
            failed.append(1)
            raise SpotifyError(429, "rate limited")
        return real_add(pid, uris)

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)
    into.post("/api/splitparts/H1/done", json=body)
    assert progress(into)["state"] == "error" and stored()["done"] is not None

    appmod.store.update_config(home_ids=["H1", "H2", "H3"])   # TB stopped being a home
    res = into.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert res.status_code == 200, res.text
    assert progress(into)["state"] == "done"
    assert sorted(into_adds(into)) == sorted(u for u in into.p2 if u != into.held)


def test_the_finish_screens_plan_prices_the_stored_into_even_when_no_longer_offered(into):
    edit_draft(lambda dr: dr.update(done={"opts": {"names": {}, "folder": None, "original": "subset",
                                                   "part": None, "into": {"p2": "TB"}},
                                          "created": {"p1": "N1"}, "filled": [], "original_done": False}))
    appmod.store.update_config(home_ids=["H1", "H2", "H3"])

    res = into.get("/api/splitparts/H1/plan", params={"original": "subset", "into": "p2:TB"})

    assert res.status_code == 200, res.text
    assert res.json()["into_parts"][0]["to_name"] == "THROTTLE BACK"


def test_a_done_started_before_into_existed_still_resumes(into, monkeypatch):
    real_add = appmod.sp.add_to_playlist
    failed = []

    def flaky(pid, uris, bulk=False, spend_reserve=False):
        if pid == "N2" and not failed:
            failed.append(1)
            raise SpotifyError(429, "rate limited")
        return real_add(pid, uris)

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)
    into.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})
    edit_draft(lambda dr: dr["done"]["opts"].pop("into", None))   # opts stored by the old code

    into.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert progress(into)["state"] == "done"
    assert progress(into)["summary"]["into"] == []


def test_a_failed_first_into_add_leaves_the_split_unlocked(into, monkeypatch):
    """Nothing written yet: `done` must not lock the split (the same rule
    as a failed first create)."""
    def boom(pid, uris, bulk=False, spend_reserve=False):
        raise SpotifyError(429, "rate limited")
    monkeypatch.setattr(appmod.sp, "add_to_playlist", boom)

    into.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "part",
                                                "part": "p1", "into": {"p2": "TB"}})

    assert progress(into)["state"] == "error"
    assert stored()["done"] is None



# ---- back to a buffer: one song from the split to an input -------------------

BUF_A = {"id": "B1", "name": "[Hazy]", "owner": "me", "editable": True,
         "total": 4, "snapshot_id": "snap-b1", "image": None, "description": ""}
BUF_BOMB = {"id": "B2", "name": "Progressive rock", "owner": "me", "editable": True,
            "total": 8, "snapshot_id": "snap-b2", "image": None, "description": ""}
BUF_OLD = {"id": "B3", "name": "🗄️ [Gone]", "owner": "me", "editable": True,
           "total": 1, "snapshot_id": "snap-b3", "image": None, "description": ""}
BUF_THEIRS = {"id": "B4", "name": "[Theirs]", "owner": "them", "editable": False,
              "total": 3, "snapshot_id": "snap-b4", "image": None, "description": ""}


@pytest.fixture
def buffers(elsewhere):
    cache = appmod.store.cache()
    cache["playlist_list"]["items"] += [BUF_A, BUF_BOMB, BUF_OLD, BUF_THEIRS, PLAIN]
    cache["playlists"]["B1"] = {"snapshot_id": "snap-b1", "tracks": [], "fetched_at": 1.0}
    appmod.store.save_cache(cache)
    folders = appmod.store.folders()
    folders["B2"] = {"path": "ROOT / THE BOMB"}
    appmod.store.save_folders(folders)
    # X1 "[Inbox]" is an input by id; liked is marked one too, and must still
    # never be offered; H1 (the split) matching no rule is not one.
    appmod.store.update_config(input_ids=["X1", "liked", "B3"], input_sets=INPUT_SETS)
    elsewhere.snaps["B1"] = "snap-b1"
    return elsewhere


def test_buffer_targets_are_the_inputs_with_their_set_sorted_and_free(buffers):
    res = buffers.get("/api/splitparts/H1/targets", params={"kind": "buffer"})

    assert res.status_code == 200, res.text
    assert res.json()["targets"] == [
        {"id": "B1", "name": "[Hazy]", "set": "buffer"},
        {"id": "X1", "name": "[Inbox]", "set": "buffer"},
        {"id": "B2", "name": "Progressive rock", "set": "the-bomb"},
    ]
    assert buffers.log == [] and buffers.calls == []


def test_homeless_is_no_buffer_target_as_on_the_now_card(buffers):
    appmod.store.update_config(homeless_id="B1")

    ids = [t["id"] for t in buffers.get("/api/splitparts/H1/targets",
                                        params={"kind": "buffer"}).json()["targets"]]

    assert "B1" not in ids and "X1" in ids


def test_the_default_targets_stay_homes_only(buffers):
    homes = buffers.get("/api/splitparts/H1/targets").json()["targets"]
    also = buffers.get("/api/splitparts/H1/targets", params={"kind": "home"}).json()["targets"]

    assert [t["id"] for t in homes] == ["A1", "H2"] and also == homes


def test_an_unknown_targets_kind_is_a_400(buffers):
    assert buffers.get("/api/splitparts/H1/targets", params={"kind": "subset"}).status_code == 400


def test_a_song_can_be_moved_back_to_a_buffer(buffers):
    uri = stored()["parts"][0]["uris"][0]

    res = buffers.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "B1"})

    assert res.status_code == 200, res.text
    assert buffers.log == [("snapshot", "H1"), ("add", "B1", [uri]), ("remove", "H1", [uri])]
    assert res.json()["moved_to"] == {"id": "B1", "name": "[Hazy]"}
    assert all(uri not in p["uris"] for p in stored()["parts"])
    assert uri in [t["uri"] for t in appmod.store.cache()["playlists"]["B1"]["tracks"]]


@pytest.mark.parametrize("to", ["B3", "B4", "liked", "P1"])
def test_an_archived_readonly_liked_or_plain_playlist_is_no_buffer_target(buffers, to):
    uri = stored()["parts"][0]["uris"][0]
    before = stored()

    res = buffers.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": to})

    assert res.status_code == 400, res.text
    assert buffers.log == [] and stored() == before


def test_moving_to_a_buffer_keeps_the_changed_playlist_guard(buffers):
    buffers.snaps["H1"] = "snap-9"
    uri = stored()["parts"][0]["uris"][0]

    res = buffers.post("/api/splitparts/H1/elsewhere", json={"uri": uri, "to_id": "B1"})

    assert res.status_code == 409
    assert buffers.log == [("snapshot", "H1")]


# ---- a hold opens the song in Now ------------------------------------------


def test_play_arms_the_settle_window_so_the_now_card_reaches_the_new_song(drafted, monkeypatch):
    """The hold switches to Now and repolls once Spotify has had a moment.
    If that answer is still the song from before, it must get the short
    settle TTL (as after Next), not the old song's whole remaining runtime."""
    monkeypatch.setattr(appmod.sp, "play_in_context", lambda pid, uri: None, raising=False)
    appmod._skip_settle.update(uri=None, until=0.0)
    appmod._now_cache.update(at=9_999_999.0, value={"track": {"uri": "spotify:track:before"}}, ttl=300)
    uri = drafted.get("/api/splitparts/H1").json()["parts"][0]["uris"][0]
    try:
        res = drafted.post("/api/splitparts/H1/play", json={"uri": uri})

        assert res.status_code == 200
        assert appmod._skip_settle["uri"] == "spotify:track:before"
        assert appmod._now_cache["at"] == 0.0
        assert drafted.calls == []
    finally:
        appmod._skip_settle.update(uri=None, until=0.0)


def test_playing_the_song_already_playing_arms_no_settle_window(drafted, monkeypatch):
    monkeypatch.setattr(appmod.sp, "play_in_context", lambda pid, uri: None, raising=False)
    appmod._skip_settle.update(uri=None, until=0.0)
    uri = drafted.get("/api/splitparts/H1").json()["parts"][0]["uris"][0]
    appmod._now_cache.update(at=9_999_999.0, value={"track": {"uri": uri}}, ttl=300)

    drafted.post("/api/splitparts/H1/play", json={"uri": uri})

    assert appmod._skip_settle["uri"] is None


# ---- name the parts while editing ------------------------------------------


def test_a_part_is_named_in_the_draft_trimmed_and_marked_edited(drafted):
    res = drafted.post("/api/splitparts/H1/parts/p1/name", json={"name": "  Big Home // loud  "})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["parts"][0]["name"] == "Big Home // loud" and body["edited"] is True
    assert "tracks" not in body
    assert stored()["parts"][0]["name"] == "Big Home // loud"
    assert drafted.calls == []


def test_an_empty_part_name_is_refused(drafted):
    before = stored()

    res = drafted.post("/api/splitparts/H1/parts/p1/name", json={"name": "   "})

    assert res.status_code == 400
    assert stored() == before


def test_naming_an_unknown_part_is_refused(drafted):
    assert drafted.post("/api/splitparts/H1/parts/p9/name", json={"name": "x"}).status_code == 400


def test_naming_a_part_is_refused_once_done_has_started(drafted):
    edit_draft(lambda d: d.update(done={"opts": {}, "created": {}, "filled": [], "original_done": False}))
    before = stored()

    res = drafted.post("/api/splitparts/H1/parts/p1/name", json={"name": "Later"})

    assert res.status_code == 409
    assert res.json()["detail"] == appmod._SPLITPARTS_DONE_STARTED_MSG
    assert stored() == before


def test_done_uses_the_name_drafted_for_a_part(env):
    env.post("/api/splitparts/H1/parts/p1/name", json={"name": "Big Home // loud"})

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert progress(env)["state"] == "done"
    assert [e[1] for e in env.log if e[0] == "create"][0] == "Big Home // loud"


def test_a_drafted_name_still_meets_the_twin_check(env):
    env.post("/api/splitparts/H1/parts/p1/name", json={"name": "tiny"})   # H2 is "Tiny"

    res = env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert res.status_code == 400 and "already exists" in res.json()["detail"]
    assert env.log == []


def test_the_part_name_field_sits_between_the_done_row_and_the_actions():
    html = (appmod.STATIC / "index.html").read_text()
    done_row, name = html.index('id="sp-done-row"'), html.index('id="sp-part-name"')
    assert done_row < name < html.index('id="sp-actions"')
