"""POST /api/split/{playlist_id}/decide — a keep costs one Spotify call, a
reject costs none.

data/splits.json (and, defensively, data/cache.json) are shared for the
whole test session (see conftest.py) and "PL1" is used by several other
split test modules, so the `client` fixture here captures whatever was on
disk before it overwrites those keys and restores it after every test
regardless of pass/fail — the leak pattern CLAUDE.md and the sittings tests
both call out.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify.spotify import LIKED_ID, SpotifyError
from sortify.store import Store


def _split_payload(decided=None):
    return {"version": 1, "splits": {"PL1": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None, "params": {},
        "piles": [{"id": "p1", "name": "dream pop", "tags": [],
                   "uris": ["spotify:track:a", "spotify:track:b"]}],
        "decided": decided or {}, "active_sitting": None}}}


@pytest.fixture
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri: calls.append(("add", pid, uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist",
                        lambda pid, uri: calls.append(("remove", pid, uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "save_to_liked",
                        lambda uri: calls.append(("liked", uri)))
    s = Store()
    original_splits = s.splits()
    original_cache = s.cache()
    s.save_splits(_split_payload())
    c = TestClient(appmod.app)
    c.calls = calls
    try:
        yield c
    finally:
        Store().save_splits(original_splits)
        Store().save_cache(original_cache)


# ---- the budget invariant ---------------------------------------------------


def test_reject_spends_no_api_calls(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "reject"})
    assert r.status_code == 200
    assert client.calls == []


def test_keep_adds_once_and_never_removes(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    assert r.status_code == 200
    assert client.calls == [("add", "HOME1", "spotify:track:a")]


def test_keep_to_liked_songs_uses_save_to_liked_not_add(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": LIKED_ID})
    assert r.status_code == 200
    assert client.calls == [("liked", "spotify:track:a")]


# ---- recording & bookkeeping ------------------------------------------------


def test_decision_is_recorded(client):
    client.post("/api/split/PL1/decide",
                json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    d = Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]
    assert d["action"] == "keep"
    assert d["to_id"] == "HOME1"


def test_remaining_count_shrinks(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "reject"})
    assert r.json()["remaining"] == 1


# ---- validation --------------------------------------------------------------


def test_keep_requires_destination(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep"})
    assert r.status_code == 400
    assert client.calls == []


def test_unknown_action_rejected(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "burn"})
    assert r.status_code == 400
    assert client.calls == []


def test_decide_unknown_playlist_404s(client):
    r = client.post("/api/split/DOES-NOT-EXIST/decide",
                    json={"uri": "spotify:track:a", "action": "reject"})
    assert r.status_code == 404
    assert client.calls == []


def test_decide_on_a_uri_not_in_the_split_404s(client):
    # Design decision: a decision on a track outside every pile is refused,
    # not silently accepted — accepting it would inflate `decided` without
    # inflating the pile-derived `total`, corrupting `remaining` for every
    # decision that follows.
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:not-in-any-pile", "action": "reject"})
    assert r.status_code == 404
    assert client.calls == []
    assert Store().splits()["splits"]["PL1"]["decided"] == {}


# ---- idempotency & the correction design decision ---------------------------


def test_deciding_twice_does_not_double_add(client):
    body = {"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"}
    client.post("/api/split/PL1/decide", json=body)
    client.post("/api/split/PL1/decide", json=body)
    assert len([c for c in client.calls if c[0] == "add"]) == 1


def test_rejecting_twice_is_a_free_noop(client):
    body = {"uri": "spotify:track:a", "action": "reject"}
    client.post("/api/split/PL1/decide", json=body)
    r = client.post("/api/split/PL1/decide", json=body)
    assert r.status_code == 200
    assert client.calls == []


def test_reject_then_keep_is_honoured_as_a_free_correction(client):
    """A reject never touched Spotify, so changing your mind about one costs
    exactly what a fresh keep would — nothing extra."""
    client.post("/api/split/PL1/decide", json={"uri": "spotify:track:a", "action": "reject"})
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    assert r.status_code == 200
    assert client.calls == [("add", "HOME1", "spotify:track:a")]
    d = Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]
    assert d["action"] == "keep" and d["to_id"] == "HOME1"


def test_keep_then_reject_is_a_noop_not_a_removal(client):
    """A keep is final through this endpoint — undoing one means moving/
    removing an ordinary playlist track, which /api/act + /api/undo already
    do; decide() does not grow a second, hidden way to spend a call."""
    client.post("/api/split/PL1/decide",
                json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    client.calls.clear()
    r = client.post("/api/split/PL1/decide", json={"uri": "spotify:track:a", "action": "reject"})
    assert r.status_code == 200
    assert client.calls == []  # no remove_from_playlist call
    d = Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]
    assert d["action"] == "keep"  # unchanged


# ---- failure handling ---------------------------------------------------------


def test_failed_keep_does_not_strand_the_track_as_decided(client, monkeypatch):
    """If the add never lands, the reservation must roll back — otherwise a
    transient 429/502 would permanently mark the track "decided" with no way
    to retry it through this endpoint."""
    def boom(pid, uri):
        raise SpotifyError(502, "upstream hiccup")
    monkeypatch.setattr(appmod.sp, "add_to_playlist", boom)

    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    assert r.status_code == 502
    assert Store().splits()["splits"]["PL1"]["decided"] == {}


def test_failed_keep_can_be_retried_after_rollback(client, monkeypatch):
    calls = {"n": 0}

    def flaky(pid, uri):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SpotifyError(502, "upstream hiccup")
        return "snap"
    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)

    body = {"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"}
    r1 = client.post("/api/split/PL1/decide", json=body)
    assert r1.status_code == 502
    r2 = client.post("/api/split/PL1/decide", json=body)
    assert r2.status_code == 200
    assert calls["n"] == 2
    assert Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]["action"] == "keep"


def test_failed_keep_correction_restores_the_original_reject(client, monkeypatch):
    """Rolling back a failed reject->keep correction must restore the reject,
    not just clear it, so the pile's decided-count doesn't regress."""
    client.post("/api/split/PL1/decide", json={"uri": "spotify:track:a", "action": "reject"})

    def boom(pid, uri):
        raise SpotifyError(502, "upstream hiccup")
    monkeypatch.setattr(appmod.sp, "add_to_playlist", boom)

    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    assert r.status_code == 502
    d = Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]
    assert d["action"] == "reject"
