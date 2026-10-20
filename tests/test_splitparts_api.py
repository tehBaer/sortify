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
