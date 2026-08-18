"""The queue worker: one call per tick, governor-paced, and every way it must
STOP spending. Threads here are real (house rule: verify by execution);
intervals are forced to ~0 so a test runs in milliseconds."""

import threading
import time

import pytest

import sortify.app as appmod
import sortify.pacing as pacing
from sortify.spotify import QUIET_AFTER_COOLDOWN, Spotify, SpotifyError
from sortify.store import Store

PILE_URIS = [f"spotify:track:m{i}" for i in range(3)]
TINY = [f"spotify:track:t{i}" for i in range(1)]


@pytest.fixture
def worker_env(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="", bulk=False: calls.append(("create", name)) or f"NEW-{name}")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False: calls.append(("add", pid, uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "bulk_block_reason", lambda: None)
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
    yield calls, s
    appmod._queue_wake.set()
    worker = appmod._queue_worker
    if worker:
        worker.join(timeout=5)
    appmod._queue_worker = None
    appmod._queue_wake.clear()
    # Minor 2 (fix round 2): restore the shared data dir BEFORE asserting on
    # the join below — a failed/hung-thread assertion must not skip these
    # and leave splits/queue/pacing poisoned for whatever test runs next.
    s.save_splits(originals["splits"])
    s.save_queue(originals["queue"])
    s.save_pacing(originals["pacing"])
    # last_429/cooldown_until are mutated by plain assignment in some tests
    # below (they model what spotify.py's request() would have set, from
    # the worker thread itself — see minor 3) rather than through a real
    # Spotify call or monkeypatch, so they need their own restore (I5).
    appmod.sp.last_429 = last_429_original
    appmod.sp.cooldown_until = cooldown_original
    if worker:
        assert not worker.is_alive(), "worker thread leaked past teardown"


def start_queue(s, pending=("p2", "p1")):
    q = s.queue()
    q.update(playlist_id="PLQ", pending=list(pending), current=None,
             state="running", stop_reason=None)
    s.save_queue(q)
    appmod._queue_wake.clear()
    appmod._queue_worker = threading.Thread(target=appmod._drain_queue, daemon=True)
    appmod._queue_worker.start()


def wait_done(s, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if s.queue()["state"] in ("done", "stopped", "paused"):
            # Task 9 fix round 1 (I4): the worker now clears appmod._queue_worker
            # to None, under _queue_lock, as its own last act — by the time this
            # poll notices a settled state the clearing may already have
            # happened, so read the reference once and tolerate None instead of
            # assuming the global still points at the (already-exited) thread.
            worker = appmod._queue_worker
            if worker:
                worker.join(timeout=2)
                assert not worker.is_alive(), "worker thread did not exit after settling"
            return s.queue()
        time.sleep(0.01)
    raise AssertionError(f"queue never settled: {s.queue()}")


def test_drains_every_pending_pile_in_order_one_call_per_tick(worker_env):
    # Renamed (M1): the worker just walks `pending` in the order it was
    # given — "smallest first" here is `start_queue`'s default argument
    # order, not something the worker decides. Sorting piles is Task 9's job.
    calls, s = worker_env
    start_queue(s)
    q = wait_done(s)
    assert q["state"] == "done"
    assert calls[0] == ("create", "tiny")               # first in the given pending order
    assert calls[1] == ("add", "NEW-tiny", TINY[0])
    assert calls[2] == ("create", "big")
    assert [c[2] for c in calls[3:]] == PILE_URIS
    assert len(calls) == len(TINY) + len(PILE_URIS) + 2  # exact total, no probes


def test_pause_is_instant_and_the_thread_exits(worker_env, monkeypatch):
    # NOTE (deviation from the brief): the brief's version gated on
    # `add_to_playlist` firing within 5s. But with Governor.interval() forced
    # to 30s, the worker's first tick for a brand-new pile is always its
    # `create_playlist` call, and the post-tick pacing wait (30s) blocks the
    # *next* tick — `add_to_playlist` — for the rest of that 30s. Verified by
    # running the test literally as brief-written: it times out waiting for
    # `add`. So this gates on the first call landing (the create) instead,
    # which still proves the real point: pause interrupts a long governor
    # sleep and the thread exits promptly rather than riding it out.
    calls, s = worker_env
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 30.0)
    start_queue(s, pending=("p1",))
    # Task 9 fix round 1 (I4): grab the thread object now, while it is
    # definitely still running — the worker clears appmod._queue_worker to
    # None as its own last act, and this test's whole point is racing the
    # pause against a thread that's about to exit, so the global itself is
    # not a safe thing to keep re-reading below.
    worker = appmod._queue_worker
    deadline = time.time() + 5
    while time.time() < deadline and not calls:
        time.sleep(0.01)
    assert calls                                          # the create call landed
    calls_at_pause = len(calls)
    q = s.queue(); q["state"] = "paused"; s.save_queue(q)
    appmod._queue_wake.set()                             # what the endpoint does
    t0 = time.time()
    worker.join(timeout=5)
    assert not worker.is_alive() and time.time() - t0 < 5
    assert s.queue()["state"] == "paused"                # resumable at the price of what's left
    # M2: at most the one call already in flight (if any) is allowed to
    # land after the pause is flipped — no NEW tick may start.
    assert len(calls) - calls_at_pause <= 1


def test_quota_trip_stops_permanently_with_reason(worker_env, monkeypatch):
    calls, s = worker_env
    invocations = {"n": 0}

    def quota_create(name, description="", bulk=False):
        invocations["n"] += 1
        # Minor 3 (fix round 2): this closure runs ON THE WORKER THREAD
        # (invoked from _materialise_tick inside _drain_queue), so it uses
        # plain assignment rather than monkeypatch.setattr — pytest's
        # monkeypatch undo list is not documented thread-safe. worker_env's
        # fixture captures/restores appmod.sp.last_429 for exactly this.
        appmod.sp.last_429 = {"ts": time.monotonic(), "kind": "quota", "retry_after": 86400}
        raise SpotifyError(429, "Spotify daily quota spent — cooldown ~23h.")
    monkeypatch.setattr(appmod.sp, "create_playlist", quota_create)
    start_queue(s, pending=("p2",))
    q = wait_done(s)
    assert q["state"] == "stopped" and q["stop_reason"] == "quota"
    hist = s.pacing()["history_429"]
    assert hist and hist[-1]["kind"] == "quota"
    assert invocations["n"] == 1                          # no retry into a quota trip (I4)


def test_rate_429_halves_and_keeps_going(worker_env, monkeypatch):
    # I4 + R-T8f (fix round 2): exercise the REAL bulk_block_reason() path
    # and ride through the actual production sequence a rate 429 causes —
    # a "sleeping" cooldown, THEN the 6h "quiet" period that follows it —
    # instead of the round-1 sleight of hand (cooldown_until = 0.0, which
    # never let the worker see "quiet" at all). The quiet period is
    # time-skipped rather than disabled: once "quiet" is observed, the
    # remembered cooldown is pushed far enough into the past that the real
    # bulk_block_reason() genuinely computes "quiet has elapsed", the same
    # arithmetic a real 6-hour wait would produce.
    monkeypatch.setattr(appmod.sp, "bulk_block_reason", Spotify.bulk_block_reason.__get__(appmod.sp))
    calls, s = worker_env
    first = {"burned": False}

    def flaky_add(pid, uri, bulk=False):
        if not first["burned"]:
            first["burned"] = True
            # Minor 3 (fix round 2): this closure runs ON THE WORKER THREAD
            # (invoked from _materialise_tick inside _drain_queue), so it
            # uses plain assignment rather than monkeypatch.setattr —
            # pytest's monkeypatch undo list is not documented thread-safe.
            # worker_env's fixture captures/restores both attributes.
            appmod.sp.last_429 = {"ts": time.monotonic(), "kind": "rate", "retry_after": 1}
            # Mirrors what spotify.py's request() sets on a real rate 429 —
            # flaky_add stands in for add_to_playlist entirely, bypassing
            # request()'s own cooldown bookkeeping. Short but real: long
            # enough to be reliably observed as "sleeping", short enough
            # that this test doesn't wait around for it.
            appmod.sp.cooldown_until = time.time() + 0.5
            raise SpotifyError(429, "rate limit hit — cooldown ~1 min")
        calls.append(("add", pid, uri)); return "snap"
    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky_add)
    start_queue(s, pending=("p2",))

    deadline = time.time() + 5
    while time.time() < deadline and s.queue()["state"] != "sleeping":
        time.sleep(0.01)
    assert s.queue()["state"] == "sleeping"               # the real cooldown, not a stub

    # The short real cooldown elapses on its own; once it does, the worker's
    # own bulk_block_reason() finds itself inside the post-cooldown quiet
    # window and moves to "quiet".
    deadline = time.time() + 5
    while time.time() < deadline and s.queue()["state"] != "quiet":
        time.sleep(0.01)
    assert s.queue()["state"] == "quiet"

    # Time-skip past the quiet window instead of waiting out 6 real hours —
    # push the remembered cooldown far enough into the past that
    # `now < cooldown_until + QUIET_AFTER_COOLDOWN` is already false.
    appmod.sp.cooldown_until = time.time() - QUIET_AFTER_COOLDOWN - 10
    appmod._queue_wake.set(); appmod._queue_wake.clear()
    q = wait_done(s)
    assert q["state"] == "done"
    assert [c[0] for c in calls].count("add") == 1        # the failed add was re-driven
    assert s.pacing()["history_429"][-1]["kind"] == "rate"
    # The halving survives BOTH sleeps — sleeping's and quiet's
    # note_interruption() calls only ever pull the rate down to at most
    # START_RATE, never back up past an already-halved value (R-T8f).
    assert s.pacing()["rate_per_min"] == pacing.MIN_RATE   # 1.8 halved, floored


def test_block_reason_sleeps_with_the_labelled_state(worker_env, monkeypatch):
    calls, s = worker_env
    blocked = {"on": True}
    monkeypatch.setattr(appmod.sp, "bulk_block_reason",
                        lambda: ("reserve", time.time() + 60) if blocked["on"] else None)
    start_queue(s, pending=("p2",))
    deadline = time.time() + 5
    while time.time() < deadline and s.queue()["state"] != "sleeping":
        time.sleep(0.01)
    assert s.queue()["state"] == "sleeping" and calls == []
    blocked["on"] = False
    appmod._queue_wake.set(); appmod._queue_wake.clear()
    q = wait_done(s)
    assert q["state"] == "done" and len(calls) == 2


def test_progress_snapshot_is_written_for_boxdash(worker_env):
    calls, s = worker_env
    start_queue(s)
    wait_done(s)
    prog = s.queue()["progress"]
    assert prog["pile_count"] == 2 and prog["spent_today"] >= 0
    assert prog["daily_cap"] == 600 and prog["reserve"] == 150
