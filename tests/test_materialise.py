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
    _split_lock, or every /api/now poll stalls behind the worker."""
    def slow_create(name, description="", bulk=False):
        assert not appmod._split_lock.locked()
        return "NEWP"
    monkeypatch.setattr(appmod.sp, "create_playlist", slow_create)
    tick()


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
