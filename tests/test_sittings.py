"""Sittings: materialise one listening session from a pile at a time.

data/splits.json and data/cache.json are shared for the whole test session
(see conftest.py), and "PL1" is also used by tests/test_split_api.py — so the
`client` fixture here captures whatever was on disk before it overwrites
those two keys, and restores it after every test regardless of pass/fail or
run order. Without that, a leaked active_sitting on "PL1" would make
create_split/recluster 409 in test_split_api.py whenever that file happens to
run after this one — the exact failure mode CLAUDE.md warns has already
happened twice in this plan.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify.spotify import SpotifyError
from sortify.split import pick_sitting
from sortify.store import Store

FIVE_MIN = 300000


def test_pick_sitting_fills_to_target_without_exceeding():
    uris = [f"u{i}" for i in range(20)]
    durations = {u: FIVE_MIN for u in uris}
    picked = pick_sitting(uris, durations, {}, target_ms=30 * 60 * 1000)
    assert len(picked) == 6


def test_pick_sitting_skips_decided_tracks():
    uris = [f"u{i}" for i in range(10)]
    durations = {u: FIVE_MIN for u in uris}
    picked = pick_sitting(uris, durations, {"u0": {}, "u1": {}}, target_ms=15 * 60 * 1000)
    assert picked == ["u2", "u3", "u4"]


def test_pick_sitting_preserves_order():
    uris = ["b", "a", "c"]
    durations = {u: FIVE_MIN for u in uris}
    assert pick_sitting(uris, durations, {}, target_ms=15 * 60 * 1000) == ["b", "a", "c"]


def test_pick_sitting_returns_at_least_one_track():
    """A single track longer than the target must still be servable."""
    picked = pick_sitting(["long"], {"long": 3 * 60 * 60 * 1000}, {}, target_ms=60 * 1000)
    assert picked == ["long"]


def test_pick_sitting_empty_when_all_decided():
    assert pick_sitting(["a"], {"a": FIVE_MIN}, {"a": {}}, target_ms=FIVE_MIN) == []


def _sitting_split(extra_piles=None):
    piles = [{"id": "p1", "name": "dream pop", "tags": ["dream pop"],
              "uris": [f"spotify:track:x{i}" for i in range(30)]}]
    if extra_piles:
        piles += extra_piles
    return {"version": 1, "splits": {"PL1": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None,
        "params": {"resolution": 1.0, "min_pile": 15},
        "piles": piles, "decided": {}, "active_sitting": None}}}


@pytest.fixture
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="": calls.append(("create", name)) or "NEW1")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri: calls.append(("add", uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "unfollow_playlist",
                        lambda pid: calls.append(("unfollow", pid)))
    s = Store()
    original_splits = s.splits()
    original_cache = s.cache()
    s.save_splits(_sitting_split())
    cache = s.cache()
    cache["playlists"]["PL1"] = {"tracks": [
        {"uri": f"spotify:track:x{i}", "duration_ms": FIVE_MIN,
         "artists": [{"id": "a", "name": "A"}]} for i in range(30)]}
    s.save_cache(cache)
    c = TestClient(appmod.app)
    c.calls = calls
    try:
        yield c
    finally:
        Store().save_splits(original_splits)
        Store().save_cache(original_cache)


def test_sitting_creates_playlist_and_adds_tracks(client):
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["sitting_id"] == "NEW1"
    assert len(body["uris"]) == 6
    assert client.calls[0][0] == "create"
    assert sum(1 for c in client.calls if c[0] == "add") == 6


def test_sitting_is_recorded_as_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active["playlist_id"] == "NEW1"
    assert active["pile_id"] == "p1"


def test_finish_unfollows_in_one_call(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    client.calls.clear()
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 200
    assert client.calls == [("unfollow", "NEW1")]
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


def test_second_sitting_refused_while_one_is_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 409


def test_sitting_on_exhausted_pile_400s(client):
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["decided"] = {
        f"spotify:track:x{i}": {"action": "reject", "to_id": None, "at": "x"} for i in range(30)}
    s.save_splits(payload)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 400


# ---- design decision 1: a re-split/recluster must not strand an active
# sitting's pile_id under a partition that no longer exists ------------------


def test_recluster_refused_while_sitting_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    r = client.post("/api/split/PL1/recluster", json={"min_pile": 5, "resolution": 1.0})
    assert r.status_code == 409
    # And it must not have spent anything getting there — same "fail for
    # free" contract as the tags-version guard in _tag_artists_checked.
    assert Store().splits()["splits"]["PL1"]["params"] == {"resolution": 1.0, "min_pile": 15}


def test_resplit_refused_while_sitting_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    r = client.post("/api/split/PL1")
    assert r.status_code == 409
    # Checked before the Last.fm key, before my_playlists(), before anything
    # that could spend or reach the network — so it fires on a bare split
    # record with no Last.fm client configured at all in this fixture.
    assert Store().splits()["splits"]["PL1"]["piles"][0]["id"] == "p1"


def test_recluster_and_resplit_work_again_once_the_sitting_is_finished(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    client.post("/api/split/PL1/sitting/finish")
    r = client.post("/api/split/PL1/recluster", json={"min_pile": 5, "resolution": 1.0})
    assert r.status_code == 200


# ---- design decision 3: recovering from an orphaned sitting playlist -------


def test_finish_recovers_when_the_sitting_playlist_is_already_gone(client, monkeypatch):
    """The user (or something else) unfollowed the sitting playlist outside
    sortify — maybe from the Spotify app directly. Without this, finish would
    404 forever on the missing playlist, active_sitting could never clear,
    and start_sitting would refuse every future sitting with 409: a
    permanently orphaned record for a playlist that is already gone."""
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})

    def already_gone(pid):
        raise SpotifyError(404, "playlist not found")

    monkeypatch.setattr(appmod.sp, "unfollow_playlist", already_gone)
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 200
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


def test_finish_still_raises_on_a_real_spotify_error(client, monkeypatch):
    """Only 404 (already gone) is swallowed — any other failure must still
    surface, and must not clear active_sitting out from under a playlist
    that may still exist."""
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})

    def rate_limited(pid):
        raise SpotifyError(429, "cooldown")

    monkeypatch.setattr(appmod.sp, "unfollow_playlist", rate_limited)
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 502
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is not None
