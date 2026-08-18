"""Materialising a pile as a permanent Spotify playlist.

The counterpart to tests/test_sittings.py: same hazard (create-then-record is
not atomic), opposite lifecycle (permanent, uncapped, never auto-unfollowed).
Almost every assertion here is about a number of Spotify calls — the Feb-2026
API has no batch add, so a 309-track pile is 310 calls, over half the day's
DAILY_CAP, and a misclick that spends them silently is the failure this
machinery is shaped around.

data/splits.json and data/cache.json are shared for the whole test session
(see conftest.py), and "PLM" here is deliberately not an id any other module
uses — but the fixture still captures and restores both files, because a
leaked materialisation record on a shared id would change what every other
suite's `_pile_progress` reports.

The one-shot `POST /api/split/{id}/materialise` endpoint that used to drive
this machinery in a blocking loop is gone (Task 7): delivery now happens one
Spotify call at a time through `_materialise_tick`, paced by the queue worker.
Everything that tested the endpoint's echo/refusal and double-click/409
semantics moved with it — Task 9's queue endpoint re-covers that ground. What
stands here is the machinery `_materialise_tick` still calls: `_materialise_plan`,
`_claim_materialisation`, `_rerecord_materialisation`, the fingerprint check,
and the create_split re-cluster sweep (Task 6).
"""

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
                        lambda name, description="", bulk=False:
                            calls.append(("create", name)) or "NEWP")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False: calls.append(("add", pid, uri)) or "snap")
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


# ---- _materialise_tick -----------------------------------------------------


def tick(pile_id="p1"):
    return appmod._materialise_tick("PLM", pile_id)


def test_first_tick_stamps_record_then_creates_only(client):
    out = tick()
    assert out == {"spent": 1, "done": False, "gone": False}
    assert client.calls == [("create", "cumbia · latin · salsa")]
    rec = record()
    assert rec["playlist_id"] == "NEWP" and rec["added"] == []
    assert rec["fingerprint"] and rec["claim"]


def test_each_following_tick_adds_exactly_one_track_in_order(client):
    tick()
    for i, uri in enumerate(PILE_URIS):
        out = tick()
        assert out["spent"] == 1
        assert client.calls[-1] == ("add", "NEWP", uri)
        assert record()["added"] == PILE_URIS[: i + 1]
    assert tick() == {"spent": 0, "done": True, "gone": False}
    assert len(client.calls) == len(PILE_URIS) + 1   # cost identical to the old loop


def test_tick_resumes_a_partial_record_without_a_second_create(client):
    tick(); tick(); tick()                    # create + 2 adds
    out = tick()
    assert out["spent"] == 1 and client.calls.count(("create", "cumbia · latin · salsa")) == 1


def test_tick_on_a_stale_record_sweeps_it_and_starts_fresh(client):
    tick(); tick()
    s = Store(); payload = s.splits()
    payload["splits"]["PLM"]["piles"][0]["uris"] = ["spotify:track:changed"]
    s.save_splits(payload)
    out = tick()
    assert out["spent"] == 1 and client.calls[-1][0] == "create"
    hist = Store().splits()["splits"]["PLM"]["materialised_history"]
    assert hist[-1]["playlist_id"] == "NEWP"


def test_tick_spends_from_the_bulk_bucket(client, monkeypatch):
    """The unattended job must never masquerade as interactive traffic."""
    seen = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="", bulk=False: seen.append(bulk) or "NEWP")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False: seen.append(bulk) or "snap")
    tick(); tick()
    assert seen == [True, True]


def test_a_vanished_pile_reports_gone_and_spends_nothing(client):
    assert tick("nope") == {"spent": 0, "done": True, "gone": True}
    assert client.calls == []


def test_tick_holds_nothing_blocking_under_split_lock(client, monkeypatch):
    """Same rule the sitting path pins: the Spotify call happens outside
    _split_lock, or every /api/now poll stalls behind the worker. Covers both
    mutation kinds, and records whether each fake actually ran rather than
    asserting inside it, so a fake that's never called can't pass vacuously."""
    observed = {"create": None, "add": None}

    def create_checking_lock(name, description="", bulk=False):
        observed["create"] = not appmod._split_lock.locked()
        return "NEWP"

    def add_checking_lock(pid, uri, bulk=False):
        observed["add"] = not appmod._split_lock.locked()
        return "snap"

    monkeypatch.setattr(appmod.sp, "create_playlist", create_checking_lock)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", add_checking_lock)
    tick()
    tick()
    assert observed["create"] is True, "create_playlist ran with _split_lock held"
    assert observed["add"] is True, "add_to_playlist ran with _split_lock held"


def test_advertised_calls_equals_calls_actually_spent_via_ticks(client):
    """The number GET /api/split shows for a pile and the number of ticks it
    actually takes to finish it must be the same number — `_materialise_plan`
    is still the one place that cost is computed, the tick is just a new way
    of spending it one call at a time."""
    pile = next(p for p in client.get("/api/split/PLM").json()["piles"] if p["id"] == "p1")
    advertised = pile["materialise_calls"]
    assert advertised == len(PILE_URIS) + 1
    for _ in range(advertised):
        out = tick()
        assert out["spent"] == 1
    assert len(client.calls) == advertised
    assert tick() == {"spent": 0, "done": True, "gone": False}


# ---- restored invariants (delivered one tick at a time, not one loop) -----


def test_a_create_failure_still_leaves_a_record_pointing_at_the_pile(client, monkeypatch):
    """The lesson that took four rounds on the sitting path: a create whose
    response never arrives must still leave something on disk pointing at
    this pile. Recording afterwards leaves a real playlist sortify has never
    heard of. `_materialise_tick` raises SpotifyError, not the plain
    exception create_playlist threw."""
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="", bulk=False: (_ for _ in ()).throw(
                            SpotifyError(429, "cooldown landed on the create")))
    with pytest.raises(SpotifyError):
        tick()
    rec = record()
    assert rec is not None and rec["playlist_id"] is None and rec["added"] == []


def test_tick_resumes_after_a_spotify_error_mid_pile_without_a_second_create(client, monkeypatch):
    """A 429 cooldown partway through is the realistic failure. The next tick
    must cost what is left, re-use the same playlist, and add nothing
    twice."""
    tick()   # create
    tick()   # add PILE_URIS[0]

    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False: (_ for _ in ()).throw(
                            SpotifyError(429, "cooldown landed mid-run")))
    with pytest.raises(SpotifyError):
        tick()
    assert record()["added"] == PILE_URIS[:1]

    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False: client.calls.append(("add", pid, uri))
                        or "snap")
    out = tick()
    assert out["spent"] == 1
    assert client.calls[-1] == ("add", "NEWP", PILE_URIS[1])
    assert client.calls.count(("create", "cumbia · latin · salsa")) == 1


def test_a_repeated_pile_uri_is_added_once_across_ticks(client):
    """`split_tracks` does not deduplicate — the same track added to the
    source playlist twice appears twice in its pile — but a permanent
    playlist should hold it once, and the advertised cost must say so up
    front."""
    s = Store()
    payload = s.splits()
    payload["splits"]["PLM"]["piles"][0]["uris"] = PILE_URIS + [PILE_URIS[0]]
    s.save_splits(payload)
    pile = next(p for p in client.get("/api/split/PLM").json()["piles"] if p["id"] == "p1")
    assert pile["materialise_calls"] == len(PILE_URIS) + 1

    for _ in range(len(PILE_URIS) + 1):
        assert tick()["spent"] == 1
    assert sum(1 for c in client.calls if c[0] == "add") == len(PILE_URIS)
    assert tick() == {"spent": 0, "done": True, "gone": False}


def test_ticking_a_pile_to_completion_touches_neither_sitting_nor_decisions(client):
    """Materialising is not a sitting and not a triage decision. It must
    neither block a sitting nor mark a single track decided, no matter how
    many ticks it takes."""
    for _ in range(len(PILE_URIS) + 1):
        tick()
    split = Store().splits()["splits"]["PLM"]
    assert split["active_sitting"] is None
    assert split["decided"] == {}
    assert client.post("/api/split/PLM/sitting",
                       json={"pile_id": "p1", "target_minutes": 30}).status_code == 200


# ---- the record going missing under a running tick -------------------------


def _foreign_record(playlist_id="OTHER", added=None):
    """What a takeover looks like on disk: the slot is occupied, but by a
    record with somebody else's claim. `if not record` cannot tell this from
    a record that is still ours — only comparing the claim can."""
    return {"playlist_id": playlist_id, "pile_id": "p1", "name": "cumbia · latin · salsa",
            "fingerprint": "whatever", "track_count": 5,
            "added": added or [], "claim": "a-different-claim",
            "created_at": "x", "updated_at": "x"}


def _overwrite_record_with_a_foreign_one(added=None):
    s = Store()
    payload = s.splits()
    payload["splits"]["PLM"]["materialised"]["p1"] = _foreign_record(added=added)
    s.save_splits(payload)


def test_tick_create_race_unfollows_the_orphan_and_raises_spotify_error(client, monkeypatch):
    """A re-cluster (or another writer) can replace this pile's record
    between create_playlist returning and the CAS that claims it — and a
    materialisation now spans many ticks, possibly hours apart, so that race
    window is open far longer than it ever was inside one blocking request.
    The just-created playlist is still empty, so unfollowing it is safe; the
    replacing record must be left alone; and the tick's contract (R-T7a)
    means this surfaces as SpotifyError, not the HTTPException the hazard
    helper raises internally."""
    def create_then_replace(name, description="", bulk=False):
        client.calls.append(("create", name))
        _overwrite_record_with_a_foreign_one()
        return "NEWP"

    monkeypatch.setattr(appmod.sp, "create_playlist", create_then_replace)
    with pytest.raises(SpotifyError) as exc:
        tick()
    assert exc.value.status == 409
    assert client.calls == [("create", "cumbia · latin · salsa"), ("unfollow", "NEWP")]
    assert record() == _foreign_record()


def test_tick_add_race_re_adopts_without_unfollow_and_raises_spotify_error(
    client, monkeypatch, caplog
):
    """Same race at the other end of a tick, where the slot is occupied so
    re-recording cannot help either. The playlist is real and holds a track
    the user paid for, so it is never unfollowed — the only honest outcome is
    to stop, leave the other record alone, and say so in the log the way
    `_recover_orphan` does."""
    tick()   # create

    def add_then_replace(pid, uri, bulk=False):
        client.calls.append(("add", pid, uri))
        _overwrite_record_with_a_foreign_one(added=["spotify:track:other"])
        return "snap"

    monkeypatch.setattr(appmod.sp, "add_to_playlist", add_then_replace)
    with caplog.at_level("ERROR", logger="uvicorn.error"):
        with pytest.raises(SpotifyError) as exc:
            tick()
    assert exc.value.status == 409
    assert not any(c[0] == "unfollow" for c in client.calls)
    rec = record()
    assert rec["claim"] == "a-different-claim"
    assert rec["added"] == ["spotify:track:other"]
    assert "NEWP" in caplog.text, "the stranded playlist id must reach the log"
    assert "by hand" in caplog.text


# ---- re-splitting and re-clustering ---------------------------------------


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

    # Same re-cluster pattern as the resplit tests elsewhere: force the split
    # back to its original p1/p2 piles (no p9) with every network source
    # faked out, so this is a free local re-cluster, not a live one.
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
