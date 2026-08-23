"""The queue HTTP endpoints: enqueue's echo gate, pause/resume/cancel, the
effective-state read, and the no-self-start thread lifecycle.

Reuses test_queue_worker.py's fixture data (PLQ with a 3-track "big" pile and
a 1-track "tiny" pile — 2 creates + 2 batched adds = 4 calls) and its `wait_done`
helper, copied here rather than imported: pytest test modules are not import
targets in this repo. Governor.interval() is forced to 0 so a full drain
happens in milliseconds; individual tests that need a LIVE worker to still be
running when the endpoint acts (pause/cancel) force it back up (ruling P1).

Fix round 1 (review) added: C1 (test_enqueue_is_free's mock now actually
records), I1 (the no-self-start pin also lives in test_no_proactive_work.py),
I2/R-T9a (enqueue refuses on pending/current, not on state), I3 (pause honours
the guard's refusal instead of always answering ok), I4 (resume after a fully-
exited worker starts a fresh thread), M1 (resume's check+write are atomic and
preserve stop_reason), M2 (cancel doesn't erase what already landed), M3
(pile_ids=[] is refused, not treated as "all"), M4/R-T9c (pause/resume/cancel
require the matching playlist_id; GET stays global).

Fix round 2 added: a deterministic regression test for `_worker_may_stop`
itself (distilling round 1's stress repro), and made test_enqueue_is_free
structural instead of timing-based by gating the worker's first action on an
Event instead of racing a check against real thread scheduling.
"""

import inspect
import threading
import time

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)
import sortify.pacing as pacing
from sortify.store import Store

PILE_URIS = [f"spotify:track:m{i}" for i in range(3)]
TINY = [f"spotify:track:t{i}" for i in range(1)]


@pytest.fixture
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="", bulk=False, spend_reserve=False: calls.append(("create", name)) or f"NEW-{name}")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False, spend_reserve=False: calls.append(("add", pid, uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "bulk_block_reason", lambda spend_reserve=False: None)
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 0.0)
    s = Store()
    originals = {f: getattr(s, f)() for f in ("splits", "queue", "pacing")}
    last_429_original = appmod.sp.last_429
    cooldown_original = appmod.sp.cooldown_until
    s.save_splits({"version": 1, "splits": {"PLQ": {
        "created_at": "t", "snapshot_id": None, "params": {},
        "piles": [
            {"id": "p1", "name": "big", "tags": [], "uris": list(PILE_URIS)},
            {"id": "p2", "name": "tiny", "tags": [], "uris": list(TINY)},
        ],
        "decided": {}, "active_sitting": None}}})
    c = TestClient(appmod.app)
    c.calls = calls
    try:
        yield c
    finally:
        appmod._queue_wake.set()
        worker = appmod._queue_worker
        if worker:
            worker.join(timeout=5)
        appmod._queue_worker = None
        appmod._queue_wake.clear()
        s.save_splits(originals["splits"])
        s.save_queue(originals["queue"])
        s.save_pacing(originals["pacing"])
        appmod.sp.last_429 = last_429_original
        appmod.sp.cooldown_until = cooldown_original
        if worker:
            assert not worker.is_alive(), "worker thread leaked past teardown"


def wait_done(s, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if s.queue()["state"] in ("done", "stopped", "paused"):
            worker = appmod._queue_worker
            if worker:
                worker.join(timeout=2)
                assert not worker.is_alive(), "worker thread did not exit after settling"
            return s.queue()
        time.sleep(0.01)
    raise AssertionError(f"queue never settled: {s.queue()}")


def test_enqueue_requires_the_exact_echoed_price(client):
    """Same contract the one-shot endpoint had (finding I1): the number on
    the button is the number the server verifies. 2 creates + 2 batched
    adds (100 tracks per call) = 4."""
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 3})
    assert r.status_code == 409 and "4" in r.json()["detail"]
    assert Store().queue()["state"] == "stopped" and client.calls == []


def test_enqueue_orders_smallest_first_and_starts_the_worker(client):
    # Batching collapses the fixture's 3-track and 1-track piles to the same
    # price (a create plus one batch each), so "smallest first" only has
    # something to order once a pile needs more than one batch. Grow "big"
    # past 100 tracks here — 1 create + 2 batches = 3, against tiny's 2.
    s = Store()
    payload = s.splits()
    payload["splits"]["PLQ"]["piles"][0]["uris"] = [
        f"spotify:track:m{i}" for i in range(150)]
    s.save_splits(payload)
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 5})
    assert r.status_code == 200 and r.json()["queued"] == ["p2", "p1"]
    q = wait_done(Store())
    assert q["state"] == "done"


def test_enqueue_records_the_spend_reserve_flag(client):
    """The per-run override: spend_reserve rides the enqueue into queue.json,
    where the worker (and a later resume) reads it."""
    r = client.post("/api/split/PLQ/queue",
                    json={"pile_ids": ["p2"], "expected_calls": 2,
                          "spend_reserve": True})
    assert r.status_code == 200
    q = wait_done(Store())
    assert q["state"] == "done" and q["spend_reserve"] is True


def test_enqueue_spend_reserve_defaults_off(client):
    r = client.post("/api/split/PLQ/queue",
                    json={"pile_ids": ["p2"], "expected_calls": 2})
    assert r.status_code == 200
    q = wait_done(Store())
    assert q["state"] == "done" and q["spend_reserve"] is False


def test_enqueue_is_free(client, monkeypatch):
    """The click costs 0: pricing comes from _materialise_plan (local), and
    the response returns before the worker can spend anything.

    C1 fix (review round 1): the original mock blocked WITHOUT recording, so
    the `client.calls == []` assertion below passed trivially even for a
    mutant where enqueue itself spent a call before returning — there was
    nothing that a synchronous spend could have appended to. A later version
    recorded before blocking inside `create_playlist`, which caught that
    mutation but still relied on the worker thread not yet having reached
    that call by the time this test checked — true in practice (GIL,
    minimal work in between) but not structurally guaranteed.

    Minor 3 (review round 2): remove the timing dependency entirely by
    gating the worker's very first action inside `_drain_queue_body`
    (`Governor.note_interruption()`, called immediately after construction,
    before anything Spotify-related) on an Event this test holds closed.
    The worker literally cannot get past that point until the gate opens,
    so `client.calls == []` is deterministic, not merely likely — while
    still catching the same C1 mutation (a synchronous spend inside
    `enqueue_piles` itself happens before this gate is ever reached).
    """
    gate = threading.Event()
    original_note_interruption = pacing.Governor.note_interruption

    def gated_note_interruption(self):
        gate.wait(5)
        return original_note_interruption(self)

    monkeypatch.setattr(pacing.Governor, "note_interruption", gated_note_interruption)
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 4})
    assert r.status_code == 200
    assert client.calls == []
    gate.set()


def test_second_enqueue_while_active_is_refused(client, monkeypatch):
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 30.0)
    client.post("/api/split/PLQ/queue", json={"pile_ids": ["p1"], "expected_calls": 2})
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": ["p2"], "expected_calls": 2})
    assert r.status_code == 409
    # Cancel rather than leave a 30s-paced worker for teardown to wait out —
    # a single wake() only interrupts the CURRENT pacing sleep, not the
    # queue's "running" state, so the worker would just tick on and re-enter
    # a fresh 30s wait before teardown's join(timeout=5) ever caught it.
    client.delete("/api/split/PLQ/queue")


def test_pause_resume_round_trip(client, monkeypatch):
    # Ruling P1: force the interval large so the worker is still alive when
    # /pause acts on it, then drop it back to 0 so resume's continuation
    # drains to completion in milliseconds like the rest of this file.
    speed = {"v": 30.0}
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: speed["v"])
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 4})
    assert client.post("/api/split/PLQ/queue/pause").status_code == 200
    q = Store().queue()
    assert q["state"] == "paused"
    speed["v"] = 0.0
    r = client.post("/api/split/PLQ/queue/resume")
    assert r.status_code == 200
    assert wait_done(Store())["state"] == "done"
    assert len(client.calls) == 4        # pause+resume cost nothing extra


def test_cancel_clears_pending_but_keeps_records(client, monkeypatch):
    # Ruling P1: force the interval large so the worker is still alive (and
    # pending has more than "current" left) when pause/cancel act on it.
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 30.0)
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 4})
    # M2 (review round 1): wait for the first tick to actually land before
    # pausing — a create call stamps split["materialised"][pile_id] BEFORE
    # the Spotify call itself (see _materialise_tick), so once `client.calls`
    # is non-empty that record is guaranteed to exist; the 30s interval
    # above is what keeps a second tick from also landing and racing ahead.
    deadline = time.time() + 5
    while time.time() < deadline and not client.calls:
        time.sleep(0.01)
    assert client.calls                                   # the first tick landed
    client.post("/api/split/PLQ/queue/pause")
    r = client.delete("/api/split/PLQ/queue")
    assert r.status_code == 200
    q = Store().queue()
    assert q["state"] == "stopped" and q["stop_reason"] == "cancelled" and q["pending"] == []
    # whatever landed stays resumable at the price of what's left:
    # re-enqueue prices only the remainder (may be 0..6 depending on timing).
    materialised = Store().splits()["splits"]["PLQ"].get("materialised") or {}
    assert materialised, "cancel must not erase what the worker already recorded"


def test_enqueue_over_a_paused_run_with_pending_is_refused(client, monkeypatch):
    """R-T9a (review round 1, I2): a paused, resumable run is still user
    work — replacing it needs an explicit cancel first, regardless of what
    `state` says. The old guard only looked at state (running/sleeping/
    quiet) and let a second enqueue silently clobber a paused run's plan."""
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 30.0)
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 4})
    assert client.post("/api/split/PLQ/queue/pause").status_code == 200
    # A call already in flight when /pause landed is allowed to finish and
    # write its own "progress" snapshot afterwards (same allowance
    # test_pause_is_instant_and_the_thread_exits pins in test_queue_worker.py)
    # — so compare only the fields the refused enqueue could have touched,
    # not `progress`/`updated_at`, which can legitimately still move.
    before = Store().queue()
    assert before["state"] == "paused" and before["pending"]
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": ["p1"], "expected_calls": 2})
    assert r.status_code == 409
    after = Store().queue()
    assert (after["state"], after["pending"], after["current"], after["playlist_id"]) == \
           (before["state"], before["pending"], before["current"], before["playlist_id"])
    client.delete("/api/split/PLQ/queue")


def test_pause_after_full_drain_is_refused(client):
    """I3 (review round 1): pausing a queue that already finished must not
    resurrect it into "paused" — and must answer 409, not {"ok": true}."""
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 4})
    assert wait_done(Store())["state"] == "done"
    r = client.post("/api/split/PLQ/queue/pause")
    assert r.status_code == 409
    assert Store().queue()["state"] == "done"


def test_pause_of_a_cancelled_queue_is_refused(client, monkeypatch):
    """I3 (review round 1): same guard, for a cancel instead of a full
    drain — a cancelled queue must stay "stopped", not flip to "paused"."""
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 30.0)
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 4})
    assert client.delete("/api/split/PLQ/queue").status_code == 200
    r = client.post("/api/split/PLQ/queue/pause")
    assert r.status_code == 409
    assert Store().queue()["state"] == "stopped"


def test_empty_pile_ids_is_refused_not_treated_as_all(client):
    """M3 (review round 1): `[]` used to be caught by `body.pile_ids or
    [...all...]`'s falsy check and silently expanded to every pile — an
    explicit empty selection must 409 instead."""
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": [], "expected_calls": 0})
    assert r.status_code == 409
    assert Store().queue()["pending"] == [] and client.calls == []


def test_pause_resume_cancel_require_the_matching_playlist_id(client, monkeypatch):
    """R-T9c (review round 1, M4): a stale tab pointed at some other
    playlist must not be able to pause/resume/cancel PLQ's queue. GET is
    deliberately exempt (it's the global read path boxdash polls)."""
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 30.0)
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 4})
    assert client.post("/api/split/OTHER/queue/pause").status_code == 409
    assert client.post("/api/split/OTHER/queue/resume").status_code == 409
    assert client.delete("/api/split/OTHER/queue").status_code == 409
    assert Store().queue()["playlist_id"] == "PLQ" and Store().queue()["state"] == "running"
    # the real playlist's queue is still actionable
    assert client.post("/api/split/PLQ/queue/pause").status_code == 200
    client.delete("/api/split/PLQ/queue")


def test_resume_preserves_stop_reason_until_the_worker_actually_runs(client, monkeypatch):
    """M1 (review round 1): resume's force=True write must not itself clear
    a quota/error stop_reason the user is still looking at — only the
    worker's own unconditional "running" transition does that, once a run
    is genuinely under way again. Stubbing _start_queue_worker isolates the
    endpoint's own write from the real worker, which would otherwise clear
    it within milliseconds anyway."""
    q = Store().queue()
    q.update(playlist_id="PLQ", pending=["p1"], current=None, state="stopped",
             stop_reason="quota")
    Store().save_queue(q)
    started = []
    monkeypatch.setattr(appmod, "_start_queue_worker", lambda: started.append(1))
    r = client.post("/api/split/PLQ/queue/resume")
    assert r.status_code == 200 and started == [1]
    q2 = Store().queue()
    assert q2["state"] == "running" and q2["stop_reason"] == "quota"


def test_resume_emptiness_check_and_write_are_one_atomic_step(client):
    """M1 (review round 1): the "nothing queued" check and the state write
    now share one _queue_lock acquisition, so there's no window for a
    concurrent cancel to empty pending/current between them. Exercised here
    via the ordinary path: nothing was ever enqueued, so resume must 409
    without starting a worker."""
    r = client.post("/api/split/PLQ/queue/resume")
    assert r.status_code == 409
    assert appmod._queue_worker is None


def test_resume_after_a_full_exit_starts_a_fresh_worker(client, monkeypatch):
    """I4 (review round 1): once the old worker has genuinely, fully exited
    (joined), resume must start a NEW thread and drain to completion — this
    pins the structural half of the fix (the worker clears _queue_worker to
    None, under _queue_lock, as its own last act).

    The narrower race this test doesn't cover — a resume landing in the
    split second between the old worker's DECISION to stop and that
    cleanup — turned out to be common rather than rare (measured ~1 run in
    5 for a plain pause-then-resume) and was closed by `_worker_may_stop`
    (review round 1's own follow-up); see
    `test_worker_may_stop_keeps_the_same_worker_running_when_a_resume_races_in`
    below for a deterministic test of THAT mechanism specifically.
    """
    speed = {"v": 30.0}
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: speed["v"])
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 4})
    assert client.post("/api/split/PLQ/queue/pause").status_code == 200
    old_worker = appmod._queue_worker
    if old_worker:
        old_worker.join(timeout=5)
    assert appmod._queue_worker is None            # fully exited and self-cleared
    speed["v"] = 0.0
    r = client.post("/api/split/PLQ/queue/resume")
    assert r.status_code == 200
    new_worker = appmod._queue_worker
    assert new_worker is not None and new_worker is not old_worker
    assert wait_done(Store())["state"] == "done"


def test_worker_may_stop_keeps_the_same_worker_running_when_a_resume_races_in(client, monkeypatch):
    """Review round 2, finding 2: `_worker_may_stop` is the load-bearing
    single-writer invariant behind I4's fix, and had zero DETERMINISTIC
    committed coverage — only the 150-iteration stress repro described in
    the round-1 fix report, which wasn't itself committed. This distills
    that repro's mechanism into a single deterministic run.

    Forces the exact interleaving the fix protects: `_queue_next_action`
    reads "paused" and decides to stop; before `_worker_may_stop`'s own,
    LATER, separate read of queue.json, a resume's forced write lands —
    performed here through the exact same `_apply_queue_state(..., force=
    True, keep_stop_reason=True)` call `resume_queue` itself makes, so the
    write is byte-for-byte what a real resume produces. No second thread is
    ever started (`_start_queue_worker` is stubbed to record, not act) —
    the SAME worker must simply keep going and drain the queue, exactly as
    `_worker_may_stop` returning False (instead of exiting) makes it do.

    Two timing pitfalls found (and fixed) while building this deterministic
    version, both worth naming since they're easy to reintroduce:

    1. Writing "paused" WITHOUT `_queue_lock` races the worker's own
       lock-protected progress write and can be silently clobbered back to
       "running" — production's `pause_queue` always writes under the lock;
       this test now does too.
    2. `inspect.stack()[1].function == "_worker_may_stop"` targets the
       exact call site regardless of how many OTHER "paused" reads happen
       first (the pile_count_at_enqueue check, `_queue_next_action`'s own
       read) — a plain "Nth paused read" counter is fragile to reorderings
       and was flaky in practice.
    """
    # Force the interval large first (ruling P1) — otherwise a 6-call drain
    # with fake, non-blocking Spotify calls can finish before this test even
    # gets to writing "paused" below. Dropped back to 0 only AFTER the race
    # has actually landed (never based on a timing guess), so the resumed
    # continuation still drains in milliseconds.
    speed = {"v": 30.0}
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: speed["v"])
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 4})
    assert r.status_code == 200

    # Let the first tick land for real (same pattern as
    # test_cancel_clears_pending_but_keeps_records and test_queue_worker.py's
    # test_pause_is_instant_and_the_thread_exits) so the worker is reliably
    # parked in its post-tick governor wait, not mid-startup.
    deadline = time.time() + 5
    while time.time() < deadline and not client.calls:
        time.sleep(0.01)
    assert client.calls

    # Pause "for real" — under _queue_lock, same as pause_queue itself —
    # so the worker's next _queue_next_action() sees a terminal state and
    # decides to stop.
    s = Store()
    with appmod._queue_lock:
        q = s.queue(); q["state"] = "paused"; s.save_queue(q)

    spawned = []
    monkeypatch.setattr(appmod, "_start_queue_worker", lambda: spawned.append(1))

    real_queue = appmod.store.queue
    state = {"raced": False}

    def racing_queue():
        result = real_queue()
        if (not state["raced"] and result.get("state") == "paused"
                and inspect.stack()[1].function == "_worker_may_stop"):
            state["raced"] = True
            fresh = real_queue()
            appmod._apply_queue_state(fresh, "running", None,
                                      force=True, keep_stop_reason=True)
            return real_queue()
        return result

    monkeypatch.setattr(appmod.store, "queue", racing_queue)
    appmod._queue_wake.set()                # what the pause endpoint would do

    deadline = time.time() + 5
    while time.time() < deadline and not state["raced"]:
        time.sleep(0.001)
    assert state["raced"], "the race never actually happened — test isn't exercising anything"
    speed["v"] = 0.0                        # now safe: the race has already landed

    q_final = wait_done(Store())
    assert q_final["state"] == "done"
    assert spawned == [], "a second worker was started — _worker_may_stop lost the race"


def test_restart_shows_a_running_file_as_paused_and_starts_nothing(client):
    """The one-click promise ends with the process: after a restart the queue
    loads paused with a Resume button — no code path from boot to traffic."""
    s = Store(); q = s.queue()
    q.update(playlist_id="PLQ", pending=["p1"], state="running")
    s.save_queue(q)                       # what a crash mid-run leaves behind
    r = client.get("/api/split/PLQ/queue")
    assert r.json()["queue"]["state"] == "paused"
    assert appmod._queue_worker is None and client.calls == []
