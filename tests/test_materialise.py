"""Materialising a pile as a permanent Spotify playlist.

The counterpart to tests/test_sittings.py: same hazard (create-then-record is
not atomic), opposite lifecycle (permanent, uncapped, never auto-unfollowed).
Almost every assertion here is about a number of Spotify calls — the Feb-2026
API has no batch add, so a 309-track pile is 310 calls, over half the day's
DAILY_CAP, and a misclick that spends them silently is the failure this
endpoint is shaped around.

data/splits.json and data/cache.json are shared for the whole test session
(see conftest.py), and "PLM" here is deliberately not an id any other module
uses — but the fixture still captures and restores both files, because a
leaked materialisation record on a shared id would change what every other
suite's `_pile_progress` reports.
"""

import threading

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify.spotify import SpotifyError
from sortify.store import TAGS_VERSION, Store

PILE_URIS = [f"spotify:track:m{i}" for i in range(5)]
BIG_URIS = [f"spotify:track:b{i}" for i in range(60)]


def _split(piles=None):
    piles = piles or [
        {"id": "p1", "name": "cumbia · latin · salsa", "tags": ["cumbia"], "uris": list(PILE_URIS)},
        {"id": "p2", "name": "big one", "tags": ["big"], "uris": list(BIG_URIS)},
    ]
    return {"version": 1, "splits": {"PLM": {
        "created_at": "2026-08-18T10:00:00Z", "snapshot_id": None,
        "params": {"resolution": 1.0, "min_pile": 15, "tag_floor": 10,
                   "max_tags_per_artist": 8},
        "piles": piles, "decided": {}, "active_sitting": None}}}


@pytest.fixture
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="": calls.append(("create", name)) or "NEWP")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri: calls.append(("add", pid, uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "unfollow_playlist",
                        lambda pid: calls.append(("unfollow", pid)))
    s = Store()
    original_splits = s.splits()
    original_cache = s.cache()
    s.save_splits(_split())
    cache = s.cache()
    cache["playlists"]["PLM"] = {"tracks": [
        {"uri": u, "duration_ms": 300000, "artists": [{"id": "a", "name": "A"}]}
        for u in PILE_URIS + BIG_URIS]}
    s.save_cache(cache)
    c = TestClient(appmod.app)
    c.calls = calls
    try:
        yield c
    finally:
        appmod._pending_materialise.clear()
        Store().save_splits(original_splits)
        Store().save_cache(original_cache)


def record(pile_id="p1"):
    return Store().splits()["splits"]["PLM"].get("materialised", {}).get(pile_id)


def post(client, pile_id, expected, **extra):
    return client.post(f"/api/split/PLM/materialise",
                       json={"pile_id": pile_id, "expected_calls": expected, **extra})


# ---- the cost -------------------------------------------------------------


def test_cost_is_exactly_one_create_plus_one_add_per_track(client):
    """The whole endpoint in one assertion: len(uris) + 1, and not one call
    more. No probe of the destination, no re-read of the source, no
    verification pass — every one of those would be invisible in the response
    and visible only in the ledger."""
    r = post(client, "p1", len(PILE_URIS) + 1)
    assert r.status_code == 200
    assert len(client.calls) == len(PILE_URIS) + 1
    assert client.calls[0] == ("create", "cumbia · latin · salsa")
    assert [c[2] for c in client.calls[1:]] == PILE_URIS
    assert r.json()["calls_spent"] == len(PILE_URIS) + 1


def test_the_advertised_cost_is_what_it_spends(client):
    """The number the pile row shows and the number the ledger sees are the
    same number, computed in one place (`_materialise_plan`)."""
    advertised = next(p for p in client.get("/api/split/PLM").json()["piles"]
                      if p["id"] == "p1")["materialise_calls"]
    assert advertised == len(PILE_URIS) + 1
    post(client, "p1", advertised)
    assert len(client.calls) == advertised


def test_a_mismatched_confirmation_spends_nothing(client):
    """A misclick on a row rendered before someone else re-clustered — or
    before an earlier run got halfway — must not spend the price it never
    displayed."""
    r = post(client, "p1", 3)
    assert r.status_code == 409
    assert "6" in r.json()["detail"]
    assert client.calls == []
    assert record() is None


def test_confirmation_must_match_exactly_not_merely_cover(client):
    """Over-confirming is a mismatch too: it means the caller was looking at
    something other than this pile."""
    assert post(client, "p1", 99).status_code == 409
    assert client.calls == []


def test_a_big_pile_is_not_capped_like_a_sitting(client):
    """SITTING_MAX_TRACKS exists because a sitting is 2 hours of listening.
    A saved playlist is the whole pile — 61 calls here, 310 on the real
    309-track one."""
    r = post(client, "p2", len(BIG_URIS) + 1)
    assert r.status_code == 200
    assert sum(1 for c in client.calls if c[0] == "add") == 60
    assert len(BIG_URIS) > appmod.SITTING_MAX_TRACKS


# ---- the record -----------------------------------------------------------


def test_the_record_names_the_playlist_and_every_track_that_landed(client):
    post(client, "p1", len(PILE_URIS) + 1)
    rec = record()
    assert rec["playlist_id"] == "NEWP"
    assert rec["added"] == PILE_URIS
    assert rec["name"] == "cumbia · latin · salsa"


def test_the_record_is_written_before_the_create_call(client, monkeypatch):
    """The lesson that took four rounds on the sitting path: a create whose
    response never arrives must still leave something on disk pointing at
    this pile. Recording afterwards leaves a real playlist sortify has never
    heard of."""
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="": (_ for _ in ()).throw(
                            SpotifyError(429, "cooldown landed on the create")))
    assert post(client, "p1", len(PILE_URIS) + 1).status_code == 502
    rec = record()
    assert rec is not None and rec["playlist_id"] is None and rec["added"] == []


def test_nothing_is_reserved_and_no_decision_is_recorded(client):
    """Materialising is not a sitting and not a triage decision. It must
    neither block a sitting nor mark a single track decided."""
    post(client, "p1", len(PILE_URIS) + 1)
    split = Store().splits()["splits"]["PLM"]
    assert split["active_sitting"] is None
    assert split["decided"] == {}
    assert client.post("/api/split/PLM/sitting",
                       json={"pile_id": "p1", "target_minutes": 30}).status_code == 200


def test_a_sitting_does_not_block_materialising(client):
    client.post("/api/split/PLM/sitting", json={"pile_id": "p1", "target_minutes": 30})
    client.calls.clear()
    assert post(client, "p1", len(PILE_URIS) + 1).status_code == 200


# ---- resume ---------------------------------------------------------------


def test_a_failure_partway_leaves_only_the_missing_tracks_to_add(client, monkeypatch):
    """A 429 cooldown on add 4 of 6 is the realistic failure. The retry must
    cost what is left, re-use the same playlist, and add nothing twice."""
    def flaky(pid, uri):
        client.calls.append(("add", pid, uri))
        if len([c for c in client.calls if c[0] == "add"]) == 4:
            raise SpotifyError(429, "cooldown landed mid-run")
        return "snap"

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)
    assert post(client, "p1", len(PILE_URIS) + 1).status_code == 502
    assert record()["added"] == PILE_URIS[:3]

    # What the pile row now offers: two tracks, no second create.
    pile = next(p for p in client.get("/api/split/PLM").json()["piles"] if p["id"] == "p1")
    assert pile["materialise_calls"] == 2
    assert pile["materialised"] == {"playlist_id": "NEWP", "added": 3,
                                    "name": "cumbia · latin · salsa", "stale": False}

    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri: client.calls.append(("add", pid, uri)) or "snap")
    client.calls.clear()
    assert post(client, "p1", 2).status_code == 200
    assert client.calls == [("add", "NEWP", PILE_URIS[3]), ("add", "NEWP", PILE_URIS[4])]
    assert record()["added"] == PILE_URIS


def test_finishing_a_finished_pile_costs_nothing(client):
    post(client, "p1", len(PILE_URIS) + 1)
    client.calls.clear()
    r = post(client, "p1", 0)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "playlist_id": "NEWP", "added": 5, "total": 5,
                        "calls_spent": 0, "complete": True}
    assert client.calls == []


def test_a_repeated_pile_uri_is_added_once(client):
    """`split_tracks` does not deduplicate — the same track added to the
    source playlist twice appears twice in its pile — but a permanent
    playlist should hold it once, and the cost must say so up front."""
    s = Store()
    payload = s.splits()
    payload["splits"]["PLM"]["piles"][0]["uris"] = PILE_URIS + [PILE_URIS[0]]
    s.save_splits(payload)
    pile = next(p for p in client.get("/api/split/PLM").json()["piles"] if p["id"] == "p1")
    assert pile["materialise_calls"] == len(PILE_URIS) + 1
    assert post(client, "p1", len(PILE_URIS) + 1).status_code == 200
    assert sum(1 for c in client.calls if c[0] == "add") == len(PILE_URIS)


# ---- re-splitting and re-clustering ---------------------------------------


def test_a_recluster_makes_the_record_stale_and_the_next_save_starts_fresh(client):
    """Pile ids are positional, so after a re-cluster `p1` is different music
    under the same id. Resuming into the old playlist would pour the new
    pile's tracks into it."""
    post(client, "p1", len(PILE_URIS) + 1)
    s = Store()
    payload = s.splits()
    payload["splits"]["PLM"]["piles"][0]["uris"] = ["spotify:track:z1", "spotify:track:z2"]
    s.save_splits(payload)

    pile = next(p for p in client.get("/api/split/PLM").json()["piles"] if p["id"] == "p1")
    assert pile["materialised"]["stale"] is True
    assert pile["materialise_calls"] == 3  # a new playlist, at full price

    client.calls.clear()
    assert post(client, "p1", 3).status_code == 200
    assert client.calls[0][0] == "create"
    assert record()["added"] == ["spotify:track:z1", "spotify:track:z2"]
    # The old playlist is still traceable rather than forgotten.
    history = Store().splits()["splits"]["PLM"]["materialised_history"]
    assert [h["playlist_id"] for h in history] == ["NEWP"]


def test_a_resplit_that_rebuilds_the_same_piles_keeps_the_record(client, monkeypatch):
    """Re-running a split is the ordinary way to resume an interrupted
    Last.fm walk and usually yields the very same piles. Losing the record
    there would offer to spend the full price on a second copy."""
    post(client, "p1", len(PILE_URIS) + 1)
    saved = record()

    piles = Store().splits()["splits"]["PLM"]["piles"]
    monkeypatch.setattr(appmod, "split_tracks", lambda tracks, artists, params: piles)
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: object())
    monkeypatch.setattr(appmod, "enrich", lambda names, cached, fm, now: cached)
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: [
        {"id": "PLM", "name": "PLM", "owner": "me", "editable": True,
         "total": 65, "snapshot_id": None, "image": None}])
    monkeypatch.setattr(appmod.sp, "playlist_tracks",
                        lambda pid: Store().cache()["playlists"]["PLM"]["tracks"])

    # tags.json is session-shared and another module deliberately parks a
    # version-1 file in it; create_split refuses those, so stamp a valid
    # envelope here and put back whatever was there (see this module's
    # docstring on isolation).
    original_tags = Store().tags()
    try:
        Store().save_tags({"version": TAGS_VERSION, "artists": {}})
        assert client.post("/api/split/PLM").status_code == 200
    finally:
        Store().save_tags(original_tags)
    assert record() == saved
    pile = next(p for p in client.get("/api/split/PLM").json()["piles"] if p["id"] == "p1")
    assert pile["materialise_calls"] == 0


# ---- refusals that cost nothing -------------------------------------------


def test_unknown_split_and_unknown_pile_are_free(client):
    assert client.post("/api/split/NOPE/materialise",
                       json={"pile_id": "p1", "expected_calls": 6}).status_code == 404
    assert post(client, "p99", 6).status_code == 404
    assert client.calls == []


def test_the_reads_around_it_cost_nothing(client):
    """GET /api/split gained a cost field; it must still be a pure local
    read, like every other GET in the split family."""
    post(client, "p1", len(PILE_URIS) + 1)
    client.calls.clear()
    for _ in range(3):
        assert client.get("/api/split/PLM").status_code == 200
    assert client.calls == []


# ---- concurrency ----------------------------------------------------------


def test_a_second_request_for_the_same_pile_is_refused_while_one_runs(client):
    """Two tabs, or a double-click. Without the in-flight guard the second
    attempt re-stamps the claim, the first one's writes stop landing, and its
    playlist becomes exactly the litter this design exists to prevent."""
    started = threading.Event()
    release = threading.Event()

    def slow_create(name, description=""):
        client.calls.append(("create", name))
        started.set()
        release.wait(5)
        return "NEWP"

    appmod.sp.create_playlist = slow_create
    try:
        out = {}
        t = threading.Thread(target=lambda: out.update(
            first=post(client, "p1", len(PILE_URIS) + 1).status_code))
        t.start()
        assert started.wait(5)
        second = post(client, "p1", len(PILE_URIS) + 1)
        release.set()
        t.join(10)
    finally:
        release.set()
    assert second.status_code == 409
    assert out["first"] == 200
    assert sum(1 for c in client.calls if c[0] == "create") == 1


def test_the_guard_is_per_pile_not_per_split(client):
    """p2 must not be blocked by p1's run — they are different playlists with
    no shared state beyond the file."""
    appmod._pending_materialise.add(("PLM", "p1"))
    try:
        assert post(client, "p1", len(PILE_URIS) + 1).status_code == 409
        assert post(client, "p2", len(BIG_URIS) + 1).status_code == 200
    finally:
        appmod._pending_materialise.discard(("PLM", "p1"))


def test_no_spotify_call_is_made_while_the_split_lock_is_held(client, monkeypatch):
    """Same rule as the sitting path (see its own copy of this test): holding
    `_split_lock` across a network call serialises every decide, start and
    finish in the app behind one request — and here that request is a
    310-call, ~26-minute walk. A non-reentrant Lock makes a same-thread
    `acquire(blocking=False)` inside the fake answer it directly."""
    observed = {"create": None, "adds": []}

    def create_checking_the_lock(name, description=""):
        observed["create"] = appmod._split_lock.acquire(blocking=False)
        if observed["create"]:
            appmod._split_lock.release()
        return "NEWP"

    def add_checking_the_lock(pid, uri):
        got = appmod._split_lock.acquire(blocking=False)
        observed["adds"].append(got)
        if got:
            appmod._split_lock.release()
        return "snap"

    monkeypatch.setattr(appmod.sp, "create_playlist", create_checking_the_lock)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", add_checking_the_lock)
    assert post(client, "p1", len(PILE_URIS) + 1).status_code == 200
    assert observed["create"] is True, "create_playlist ran with _split_lock held"
    assert observed["adds"] and all(observed["adds"]), "an add ran with _split_lock held"


# ---- the record going missing under a running attempt ---------------------


def test_an_empty_playlist_whose_record_vanished_is_unfollowed(client, monkeypatch):
    """The create/record gap, from the other side: if the record this attempt
    wrote is gone by the time create returns, nothing points at the new
    playlist. It is still empty, so removing it is both safe and the only way
    it does not become litter."""
    def create_then_wipe(name, description=""):
        client.calls.append(("create", name))
        s = Store()
        payload = s.splits()
        payload["splits"]["PLM"]["materialised"] = {}
        s.save_splits(payload)
        return "NEWP"

    monkeypatch.setattr(appmod.sp, "create_playlist", create_then_wipe)
    r = post(client, "p1", len(PILE_URIS) + 1)
    assert r.status_code == 409
    assert client.calls == [("create", "cumbia · latin · salsa"), ("unfollow", "NEWP")]
    assert record() is None


def _overwrite_record_with_a_foreign_one():
    """What a takeover looks like on disk: the slot is occupied, but by a
    record with somebody else's claim. `if not record` cannot tell this from
    a record that is still ours — only comparing the claim can."""
    s = Store()
    payload = s.splits()
    payload["splits"]["PLM"]["materialised"]["p1"] = {
        "playlist_id": "OTHER", "pile_id": "p1", "name": "cumbia · latin · salsa",
        "fingerprint": "whatever", "track_count": 5, "added": ["spotify:track:other"],
        "claim": "a-different-claim", "created_at": "x", "updated_at": "x"}
    s.save_splits(payload)


def test_a_replaced_record_is_not_written_to_after_the_create(client, monkeypatch):
    """The claim is checked, not merely the record's existence. Without the
    comparison this attempt stamps its own playlist id over a record that
    belongs to someone else — losing sight of one playlist and mislabelling
    another."""
    def create_then_take_over(name, description=""):
        client.calls.append(("create", name))
        _overwrite_record_with_a_foreign_one()
        return "NEWP"

    monkeypatch.setattr(appmod.sp, "create_playlist", create_then_take_over)
    assert post(client, "p1", len(PILE_URIS) + 1).status_code == 409
    assert ("unfollow", "NEWP") in client.calls
    assert record() == {
        "playlist_id": "OTHER", "pile_id": "p1", "name": "cumbia · latin · salsa",
        "fingerprint": "whatever", "track_count": 5, "added": ["spotify:track:other"],
        "claim": "a-different-claim", "created_at": "x", "updated_at": "x"}


def test_a_replaced_record_is_not_written_to_mid_adds(client, monkeypatch, caplog):
    """Same check at the other end of the run, where the slot is occupied so
    re-recording cannot help either. The playlist is real and half full, so
    the only honest outcome is to stop, leave the other record alone, and say
    so in the log the way `_recover_orphan` does."""
    def add_then_take_over(pid, uri):
        client.calls.append(("add", pid, uri))
        if len([c for c in client.calls if c[0] == "add"]) == 2:
            _overwrite_record_with_a_foreign_one()
        return "snap"

    monkeypatch.setattr(appmod.sp, "add_to_playlist", add_then_take_over)
    with caplog.at_level("ERROR", logger="uvicorn.error"):
        assert post(client, "p1", len(PILE_URIS) + 1).status_code == 409
    assert not any(c[0] == "unfollow" for c in client.calls)
    assert record()["claim"] == "a-different-claim"
    assert record()["added"] == ["spotify:track:other"]
    assert "NEWP" in caplog.text, "the stranded playlist id must reach the log"
    assert "by hand" in caplog.text


def test_a_half_filled_playlist_whose_record_vanished_is_re_recorded(client, monkeypatch):
    """Past the first add the playlist holds tracks the user paid for, so it
    is never unfollowed — it is re-recorded, which is what keeps it findable
    (and resumable) instead of stranded."""
    def wipe_after_two(pid, uri):
        client.calls.append(("add", pid, uri))
        if len([c for c in client.calls if c[0] == "add"]) == 2:
            s = Store()
            payload = s.splits()
            payload["splits"]["PLM"]["materialised"] = {}
            s.save_splits(payload)
        return "snap"

    monkeypatch.setattr(appmod.sp, "add_to_playlist", wipe_after_two)
    r = post(client, "p1", len(PILE_URIS) + 1)
    assert r.status_code == 409
    assert not any(c[0] == "unfollow" for c in client.calls)
    rec = record()
    assert rec["playlist_id"] == "NEWP"
    assert rec["added"] == PILE_URIS[:2]
    # And the re-recorded state is resumable at the price of what is left.
    pile = next(p for p in client.get("/api/split/PLM").json()["piles"] if p["id"] == "p1")
    assert pile["materialise_calls"] == 3


def test_recluster_sweeps_records_for_vanished_pile_ids_to_history(client, monkeypatch):
    """9 piles → 8 must not orphan p9's record (finding I3): the playlist is
    real, and history is the only place it stays traceable. p1's record, kept
    because p1 still exists after the re-cluster, pins the other half: a
    sweep-everything implementation would also make this test pass."""
    s = Store()
    payload = s.splits()
    payload["splits"]["PLM"]["materialised"] = {
        "p1": {"playlist_id": "STILL1", "pile_id": "p1", "name": "cumbia · latin · salsa",
               "fingerprint": "whatever", "track_count": 5,
               "added": ["spotify:track:m0"], "claim": "c",
               "created_at": "t", "updated_at": "t"},
        "p9": {"playlist_id": "OLD9", "pile_id": "p9", "name": "gone pile",
               "fingerprint": "beef", "track_count": 3,
               "added": ["spotify:track:x"], "claim": "c",
               "created_at": "t", "updated_at": "t"}}
    s.save_splits(payload)

    # Same re-cluster pattern as
    # test_a_resplit_that_rebuilds_the_same_piles_keeps_the_record: force the
    # split back to its original p1/p2 piles (no p9) with every network
    # source faked out, so this is a free local re-cluster, not a live one.
    piles = Store().splits()["splits"]["PLM"]["piles"]
    monkeypatch.setattr(appmod, "split_tracks", lambda tracks, artists, params: piles)
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: object())
    monkeypatch.setattr(appmod, "enrich", lambda names, cached, fm, now: cached)
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: [
        {"id": "PLM", "name": "PLM", "owner": "me", "editable": True,
         "total": 65, "snapshot_id": None, "image": None}])
    monkeypatch.setattr(appmod.sp, "playlist_tracks",
                        lambda pid: Store().cache()["playlists"]["PLM"]["tracks"])

    original_tags = Store().tags()
    try:
        Store().save_tags({"version": TAGS_VERSION, "artists": {}})
        r = client.post("/api/split/PLM", json={})     # re-cluster, 0 calls
        assert r.status_code == 200
    finally:
        Store().save_tags(original_tags)

    split = Store().splits()["splits"]["PLM"]
    assert split["materialised"]["p1"]["playlist_id"] == "STILL1"  # still-existing id: kept
    assert "p9" not in split.get("materialised", {})
    hist = split["materialised_history"]
    assert hist and hist[-1]["playlist_id"] == "OLD9" and hist[-1]["swept"] == "recluster"
    assert client.calls == []                       # free, like every re-cluster
