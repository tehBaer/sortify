"""POST /api/split/{playlist_id}/decide — a keep costs one Spotify call, a
reject costs none.

data/splits.json (and, defensively, data/cache.json) are shared for the
whole test session (see conftest.py) and "PL1" is used by several other
split test modules, so the `client` fixture here captures whatever was on
disk before it overwrites those keys and restores it after every test
regardless of pass/fail — the leak pattern CLAUDE.md and the sittings tests
both call out.

Fix Round 1 (review of commit 735899b) added: the destination cache mirror
(`_cache_move`) so a later `/api/act` doesn't double-add; the `"pending"`
marker + `_pending_keeps` in-process tracking that closes the
process-death-mid-call gap while keeping the concurrency guarantee the
reviewer stress-tested; the `_remaining` occurrence-based count (duplicate
uris in a pile, and stale `decided` entries a re-split dropped); the
`"changed"`/`"decision"` fields so a no-op is distinguishable from a real
write; and `action="undecide"`, which frees a rejected uri back to undecided
(never a kept one) for zero calls.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify.spotify import LIKED_ID, SpotifyError
from sortify.store import Store


def _split_payload(decided=None, uris=("spotify:track:a", "spotify:track:b")):
    return {"version": 1, "splits": {"PL1": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None, "params": {},
        "piles": [{"id": "p1", "name": "dream pop", "tags": [],
                   "uris": list(uris)}],
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
    original_pending = set(appmod._pending_keeps)
    s.save_splits(_split_payload())
    c = TestClient(appmod.app)
    c.calls = calls
    try:
        yield c
    finally:
        Store().save_splits(original_splits)
        Store().save_cache(original_cache)
        appmod._pending_keeps.clear()
        appmod._pending_keeps.update(original_pending)


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
    assert "pending" not in d  # cleared once the add actually landed


def test_remaining_count_shrinks(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "reject"})
    assert r.json()["remaining"] == 1


def test_successful_decision_reports_changed_true_and_the_decision(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    body = r.json()
    assert body["changed"] is True
    assert body["decision"] == {"action": "keep", "to_id": "HOME1"}


# ---- Important 1: the destination cache mirror ------------------------------


def test_keep_mirrors_into_destination_cache_so_act_does_not_double_add(client):
    """Regression for the review's measured repro: a keep that stamps the
    destination's snapshot fresh without putting the track in the cached
    list makes `_cached_tracks` look up to date forever, so a later
    /api/act on the same track spent a second, duplicate add call."""
    s = Store()
    cache = s.cache()
    cache["playlists"]["PL1"] = {
        "snapshot_id": "snap-src", "fetched_at": 0,
        "tracks": [{"uri": "spotify:track:a", "id": "a", "name": "A"}],
    }
    cache["playlists"]["HOME1"] = {
        "snapshot_id": "snap-home", "fetched_at": 0, "tracks": [],
    }
    s.save_cache(cache)

    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    assert r.status_code == 200
    assert client.calls == [("add", "HOME1", "spotify:track:a")]

    dest_tracks = Store().cache()["playlists"]["HOME1"]["tracks"]
    assert any(t["uri"] == "spotify:track:a" for t in dest_tracks)

    # The control: /api/act on the same uri to the same destination must now
    # recognise it's already there and spend nothing.
    client.calls.clear()
    r2 = client.post("/api/act", json={"action": "move", "uri": "spotify:track:a",
                                       "to_id": "HOME1"})
    assert r2.status_code == 200
    assert r2.json()["note"] == "already in destination"
    assert client.calls == []


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
    # not silently accepted — accepting it would add a `decided` entry no
    # pile's occurrence count could ever account for, corrupting
    # `remaining` for every decision that follows.
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
    assert r.json()["changed"] is False


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
    assert r.json()["changed"] is False
    d = Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]
    assert d["action"] == "keep"  # unchanged


def test_no_op_reports_the_settled_decision_not_the_requested_one(client):
    """Minor fix: a no-op used to be byte-identical to a real change. A
    mis-click keep-to-Metal-then-keep-to-Chill must be told nothing moved,
    and what actually is recorded, not silently swallowed."""
    client.post("/api/split/PL1/decide",
                json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    client.calls.clear()
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "OTHER_HOME"})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is False
    assert body["decision"] == {"action": "keep", "to_id": "HOME1"}
    assert client.calls == []


# ---- action="undecide" -------------------------------------------------------


def test_undecide_clears_a_reject_for_free(client):
    client.post("/api/split/PL1/decide", json={"uri": "spotify:track:a", "action": "reject"})
    r = client.post("/api/split/PL1/decide", json={"uri": "spotify:track:a", "action": "undecide"})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True
    assert body["decision"] is None
    assert body["remaining"] == 2
    assert "spotify:track:a" not in Store().splits()["splits"]["PL1"]["decided"]
    assert client.calls == []


def test_undecided_track_is_eligible_for_a_sitting_again(client):
    """The whole point of undecide: pick_sitting skips anything in
    `decided`, so an accidental reject must not permanently drop a track
    out of every future sitting."""
    from sortify.split import pick_sitting
    client.post("/api/split/PL1/decide", json={"uri": "spotify:track:a", "action": "reject"})
    client.post("/api/split/PL1/decide", json={"uri": "spotify:track:a", "action": "undecide"})
    decided = Store().splits()["splits"]["PL1"]["decided"]
    picked = pick_sitting(["spotify:track:a", "spotify:track:b"],
                          {"spotify:track:a": 1000, "spotify:track:b": 1000},
                          decided, target_ms=10_000_000)
    assert "spotify:track:a" in picked


def test_undecide_on_a_keep_is_a_noop(client):
    client.post("/api/split/PL1/decide",
                json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    client.calls.clear()
    r = client.post("/api/split/PL1/decide", json={"uri": "spotify:track:a", "action": "undecide"})
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert client.calls == []
    d = Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]
    assert d["action"] == "keep"


def test_undecide_on_an_already_undecided_uri_is_a_noop(client):
    r = client.post("/api/split/PL1/decide", json={"uri": "spotify:track:a", "action": "undecide"})
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert r.json()["decision"] is None


def test_undecide_on_a_uri_not_in_the_split_404s(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:nope", "action": "undecide"})
    assert r.status_code == 404


# ---- Important 3 / the duplicate-uri open question ---------------------------


def test_remaining_handles_a_uri_that_occurs_twice_in_the_source(client):
    """split_tracks does not deduplicate (checked in sortify/split.py: it
    appends one bucket entry per source track, not per distinct uri), so a
    track added twice to the underlying playlist occurs twice in a pile's
    `uris`. `decided` is keyed by uri, so a single decide() call must settle
    every occurrence at once — `remaining` should reach 0 exactly when every
    *distinct* uri has a decision, not stall waiting for a second one."""
    s = Store()
    s.save_splits(_split_payload(
        uris=["spotify:track:a", "spotify:track:a", "spotify:track:b"]))
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "reject"})
    assert r.json()["remaining"] == 1  # both occurrences of "a" settled at once
    r2 = client.post("/api/split/PL1/decide",
                     json={"uri": "spotify:track:b", "action": "reject"})
    assert r2.json()["remaining"] == 0


def test_remaining_does_not_go_negative_from_a_decided_uri_no_pile_still_has(client):
    """A re-split/recluster can carry `decided` forward for a uri that no
    longer appears in any pile (Spotify dropped it, or the user deleted it
    from the source). The naive `total - len(decided)` would inflate
    `total`'s complement by exactly the entries that vanished, going
    negative; the per-pile-occurrence count must not."""
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["decided"] = {
        "spotify:track:ghost": {"action": "reject", "to_id": None, "at": "x"}
    }
    s.save_splits(payload)
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "reject"})
    assert r.json()["remaining"] == 1  # only "b" is left; the ghost doesn't count


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
    assert ("PL1", "spotify:track:a") not in appmod._pending_keeps


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


def test_local_bookkeeping_failure_after_a_successful_add_does_not_roll_back(client, monkeypatch):
    """Minor fix: except Exception used to wrap _apply_snapshot as well as
    the add. If _apply_snapshot raised after the add landed, the old code
    rolled back a call that had already succeeded, so a retry double-added.
    It must instead stay a completed keep, with the pending marker cleared
    (not stuck forever) so a same-uri retry sees a real no-op, not another
    add."""
    def boom(*a, **k):
        raise RuntimeError("local bug")
    monkeypatch.setattr(appmod, "_apply_snapshot", boom)

    with pytest.raises(RuntimeError):
        client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})

    assert client.calls == [("add", "HOME1", "spotify:track:a")]
    d = Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]
    assert d["action"] == "keep"
    assert "pending" not in d
    assert ("PL1", "spotify:track:a") not in appmod._pending_keeps

    r2 = client.post("/api/split/PL1/decide",
                     json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    assert r2.json()["changed"] is False
    assert len([c for c in client.calls if c[0] == "add"]) == 1


# ---- Important 2: process-death-mid-call recovery ---------------------------


def test_a_pending_keep_left_by_a_dead_process_is_retried(client):
    """Simulates a systemd restart between the reservation write and the add
    completing: splits.json is seeded directly (bypassing decide(), so
    _pending_keeps — reset fresh on every process start — never learns about
    it), exactly like what a fresh process would see on disk after a crash."""
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["decided"] = {
        "spotify:track:a": {"action": "keep", "to_id": "HOME1", "at": "x", "pending": True},
    }
    s.save_splits(payload)
    assert ("PL1", "spotify:track:a") not in appmod._pending_keeps  # fresh-process precondition

    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert client.calls == [("add", "HOME1", "spotify:track:a")]
    d = Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]
    assert d["action"] == "keep"
    assert "pending" not in d


def test_a_pending_keep_still_in_flight_in_this_process_is_not_retried(client, monkeypatch):
    """The other side of the same coin, exercised without real threads: a
    second decide() call for the same uri arriving WHILE the first is still
    inside its (synchronous, same-thread) Spotify call must see it as
    genuinely in-flight via `_pending_keeps` and no-op — not race it into a
    second add."""
    reentrant_results = []

    def add_and_reenter(pid, uri):
        client.calls.append(("add", pid, uri))
        r2 = client.post("/api/split/PL1/decide",
                         json={"uri": uri, "action": "keep", "to_id": pid})
        reentrant_results.append(r2.json())
        return "snap"

    monkeypatch.setattr(appmod.sp, "add_to_playlist", add_and_reenter)
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    assert r.status_code == 200
    assert len([c for c in client.calls if c[0] == "add"]) == 1
    assert reentrant_results[0]["changed"] is False
