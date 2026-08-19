"""The queue worker: one call per tick, governor-paced, and every way it must
STOP spending. Threads here are real (house rule: verify by execution);
intervals are forced to ~0 so a test runs in milliseconds."""

import threading
import time

import pytest

import sortify.app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)
import sortify.pacing as pacing
from sortify.spotify import QUIET_AFTER_COOLDOWN, Spotify, SpotifyError
from sortify.store import Store


class FakeResponse:
    """Mirrors tests/test_budget.py's FakeResponse — a stand-in for the
    httpx.Response object at the sp.http.request wire seam."""

    def __init__(self, status_code=200, headers=None, text="", json_body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json_body = json_body
        self.content = b"1" if json_body is not None else text.encode()

    def json(self):
        return self._json_body

PILE_URIS = [f"spotify:track:m{i}" for i in range(3)]
TINY = [f"spotify:track:t{i}" for i in range(1)]


@pytest.fixture
def worker_env(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="", bulk=False, spend_reserve=False: calls.append(("create", name)) or f"NEW-{name}")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False, spend_reserve=False: calls.append(("add", pid, uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "bulk_block_reason", lambda spend_reserve=False: None)
    # No queue test should ever reach the reconcile read — every pile here
    # starts fresh — but the fake is mandatory anyway: an unstubbed
    # `playlist_tracks` on a widened reconcile predicate would put a REAL
    # Spotify call inside the test suite, which is this project's hardest
    # rule (CLAUDE.md). It records so a stray read shows up in `calls`
    # rather than passing silently.
    monkeypatch.setattr(appmod.sp, "playlist_tracks",
                        lambda pid, bulk=False, spend_reserve=False:
                            calls.append(("read", pid)) or [])
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
    appmod._reconciled.clear()   # process-global, like _pending_materialise
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
    assert calls[1] == ("add", "NEW-tiny", TINY)
    assert calls[2] == ("create", "big")
    assert calls[3] == ("add", "NEW-big", PILE_URIS)   # one batch, not one call each
    assert len(calls) == 4                             # 2 creates + 2 batches, no probes


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

    def quota_create(name, description="", bulk=False, spend_reserve=False):
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

    def flaky_add(pid, uri, bulk=False, spend_reserve=False):
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
    # set() only — never set()+clear() from the nudging side. Producers in
    # app.py (pause, cancel) only ever set(); the WAITER clears, right after
    # its own `_queue_wake.wait()` returns. Clearing here too raced that
    # wait(): if the worker was between its post-wait clear() and its next
    # wait() call, both our set and our clear landed first, the signal was
    # gone before wait() ever looked at it, and the worker rode out the full
    # 60s chunk — wait_done then timed out at "quiet" about 1 run in 30.
    appmod._queue_wake.set()
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
                        lambda spend_reserve=False: ("reserve", time.time() + 60) if blocked["on"] else None)
    start_queue(s, pending=("p2",))
    deadline = time.time() + 5
    while time.time() < deadline and s.queue()["state"] != "sleeping":
        time.sleep(0.01)
    assert s.queue()["state"] == "sleeping" and calls == []
    blocked["on"] = False
    appmod._queue_wake.set()          # set() only — the waiter does the clear()
    q = wait_done(s)
    assert q["state"] == "done" and len(calls) == 2


def test_governor_halves_from_a_real_429_stamped_by_request_itself(worker_env, monkeypatch):
    """I-3 / R-F2: the spec's acceptance sketch wants the wire seam exercised
    — a real 429 that Spotify.request() itself classifies, stamps onto
    sp.last_429, and retries through — not a hand-crafted last_429 dict
    assigned by a fake create_playlist/add_to_playlist like the other tests
    in this file use. So here create_playlist and add_to_playlist are left
    as the REAL bound methods, and only sp.http.request is faked (the same
    seam test_budget.py's test_request_records_the_last_429_... fakes),
    with sp._access_token stubbed so no real auth/refresh happens and
    time.sleep patched to a no-op so request()'s Retry-After wait doesn't
    make this test slow.
    """
    calls, s = worker_env
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        Spotify.create_playlist.__get__(appmod.sp))
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        Spotify.add_to_playlist.__get__(appmod.sp))
    monkeypatch.setattr(appmod.sp, "bulk_block_reason",
                        Spotify.bulk_block_reason.__get__(appmod.sp))
    monkeypatch.setattr(appmod.sp, "_access_token", lambda: "tok")
    monkeypatch.setattr(time, "sleep", lambda secs: None)

    responses = [
        FakeResponse(200, json_body={"id": "NEW-tiny"}),               # create_playlist
        FakeResponse(429, headers={"Retry-After": "1"},                # add, attempt 1: rate 429
                     text='{"error": {"status": 429}}'),
        FakeResponse(200, json_body={"snapshot_id": "snap1"}),         # add, attempt 2: retried OK
    ]
    monkeypatch.setattr(appmod.sp.http, "request", lambda *a, **k: responses.pop(0))

    start_queue(s, pending=("p2",))     # the 1-track pile — one create, one add
    q = wait_done(s)
    assert q["state"] == "done"
    # create_playlist/add_to_playlist are the REAL bound methods here (not
    # the calls-list-recording fakes worker_env's fixture installs), so the
    # record materialised into splits.json is what proves both API calls
    # actually landed rather than `calls`.
    record = s.splits()["splits"]["PLQ"]["materialised"]["p2"]
    assert record["playlist_id"] == "NEW-tiny" and record["added"] == [TINY[0]]

    # request() itself classified and stamped the 429 — this test never wrote
    # to appmod.sp.last_429 directly, unlike test_rate_429_halves_and_keeps_going.
    assert appmod.sp.last_429 and appmod.sp.last_429["kind"] == "rate"
    # The governor's halving is driven by THAT stamp: START_RATE (1.8) halved
    # and floored at MIN_RATE (0.9).
    assert s.pacing()["rate_per_min"] == pacing.MIN_RATE


def test_sleep_loop_does_not_rewrite_queue_json_for_an_unchanged_block(worker_env, monkeypatch):
    """M-4: the sleep loop polls in <=60s chunks, but re-labelling the SAME
    ongoing block (e.g. a multi-hour bulk-reserve wait) every chunk must not
    rewrite queue.json — only entering the sleep and eventually leaving it
    are real transitions worth a save."""
    calls, s = worker_env
    writes = []
    real_save_queue = appmod.store.save_queue

    def spy_save_queue(payload):
        writes.append(payload.get("state"))
        real_save_queue(payload)
    monkeypatch.setattr(appmod.store, "save_queue", spy_save_queue)

    unblock = {"on": False}

    def block_reason(spend_reserve=False):
        return None if unblock["on"] else ("reserve", time.time() + 0.3)
    monkeypatch.setattr(appmod.sp, "bulk_block_reason", block_reason)

    start_queue(s, pending=("p2",))
    time.sleep(2.5)      # several ~1s-floored polling chunks, still blocked
    assert writes.count("sleeping") == 1, writes   # one entry write, not one per chunk

    unblock["on"] = True
    appmod._queue_wake.set()          # set() only — the waiter does the clear()
    q = wait_done(s)
    assert q["state"] == "done"
    assert calls == [("create", "tiny"), ("add", "NEW-tiny", TINY)]
    # "running" also gets written by the two ticks' own progress saves
    # (legitimate — progress actually changes each tick), so this only pins
    # the thing M-4 is actually about: the block itself was one write in,
    # one write out, not one pair per 60s polling chunk.
    assert writes.count("sleeping") == 1, writes


def test_progress_snapshot_is_written_for_boxdash(worker_env):
    calls, s = worker_env
    start_queue(s)
    wait_done(s)
    prog = s.queue()["progress"]
    assert prog["pile_count"] == 2 and prog["spent_today"] >= 0
    assert prog["daily_cap"] == 1000 and prog["reserve"] == 150


def test_the_worker_consults_the_block_with_the_runs_spend_reserve_flag(worker_env, monkeypatch):
    """A run enqueued with spend_reserve=True must ask bulk_block_reason with
    that flag — otherwise the whole feature is a stored bool nothing reads."""
    calls, s = worker_env
    seen = []
    monkeypatch.setattr(appmod.sp, "bulk_block_reason",
                        lambda spend_reserve=False: seen.append(spend_reserve) or None)
    q = s.queue()
    q["spend_reserve"] = True
    s.save_queue(q)
    start_queue(s, pending=("p2",))
    q = wait_done(s)
    assert q["state"] == "done"
    assert seen and all(seen), seen


def test_the_tick_spends_with_the_runs_spend_reserve_flag(worker_env, monkeypatch):
    """The flag has to ride all the way into the Spotify calls, where
    _spend_budget's reserve guard actually lives."""
    calls, s = worker_env
    seen = []
    monkeypatch.setattr(
        appmod.sp, "create_playlist",
        lambda name, description="", bulk=False, spend_reserve=False:
            seen.append(("create", spend_reserve)) or f"NEW-{name}")
    monkeypatch.setattr(
        appmod.sp, "add_to_playlist",
        lambda pid, uri, bulk=False, spend_reserve=False:
            seen.append(("add", spend_reserve)) or "snap")
    q = s.queue()
    q["spend_reserve"] = True
    s.save_queue(q)
    start_queue(s, pending=("p2",))
    q = wait_done(s)
    assert q["state"] == "done"
    assert seen and all(flag for _, flag in seen), seen


def test_queue_drains_a_250_track_pile_in_four_calls(worker_env):
    # 250 uris: 1 create + 3 batches. The whole run must cost 4 Spotify
    # calls — the headline number of the 2026-08-23 design.
    calls, s = worker_env
    big = [f"spotify:track:q{i}" for i in range(250)]
    payload = s.splits()
    payload["splits"]["PLQ"]["piles"] = [
        {"id": "p1", "name": "big", "tags": [], "uris": big}]
    s.save_splits(payload)
    split = s.splits()["splits"]["PLQ"]
    assert appmod._materialise_plan(split, split["piles"][0])["calls"] == 4
    start_queue(s, pending=("p1",))
    q = wait_done(s)
    assert q["state"] == "done"
    assert [c[0] for c in calls] == ["create", "add", "add", "add"]
    assert [len(c[2]) for c in calls[1:]] == [100, 100, 50]
    added = s.splits()["splits"]["PLQ"]["materialised"]["p1"]["added"]
    assert added == big
