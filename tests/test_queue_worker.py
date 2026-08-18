"""The queue worker: one call per tick, governor-paced, and every way it must
STOP spending. Threads here are real (house rule: verify by execution);
intervals are forced to ~0 so a test runs in milliseconds."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
import sortify.pacing as pacing
from sortify.spotify import SpotifyError
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
    s.save_splits({"version": 1, "splits": {"PLQ": {
        "created_at": "t", "snapshot_id": None, "params": {},
        "piles": [
            {"id": "p1", "name": "big", "tags": [], "uris": list(PILE_URIS)},
            {"id": "p2", "name": "tiny", "tags": [], "uris": list(TINY)},
        ],
        "decided": {}, "active_sitting": None}}})
    yield calls, s
    appmod._queue_wake.set()
    if appmod._queue_worker:
        appmod._queue_worker.join(timeout=5)
    appmod._queue_worker = None
    appmod._queue_wake.clear()
    s.save_splits(originals["splits"])
    s.save_queue(originals["queue"])
    s.save_pacing(originals["pacing"])


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
            appmod._queue_worker.join(timeout=2)
            return s.queue()
        time.sleep(0.01)
    raise AssertionError(f"queue never settled: {s.queue()}")


def test_drains_every_pile_smallest_first_one_call_per_tick(worker_env):
    calls, s = worker_env
    start_queue(s)
    q = wait_done(s)
    assert q["state"] == "done"
    assert calls[0] == ("create", "tiny")               # smallest pile first
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
    deadline = time.time() + 5
    while time.time() < deadline and not calls:
        time.sleep(0.01)
    assert calls                                          # the create call landed
    q = s.queue(); q["state"] = "paused"; s.save_queue(q)
    appmod._queue_wake.set()                             # what the endpoint does
    t0 = time.time()
    appmod._queue_worker.join(timeout=5)
    assert not appmod._queue_worker.is_alive() and time.time() - t0 < 5
    assert s.queue()["state"] == "paused"                # resumable at the price of what's left


def test_quota_trip_stops_permanently_with_reason(worker_env, monkeypatch):
    calls, s = worker_env
    def quota_create(name, description="", bulk=False):
        appmod.sp.last_429 = {"ts": time.time(), "kind": "quota", "retry_after": 86400}
        raise SpotifyError(429, "Spotify daily quota spent — cooldown ~23h.")
    monkeypatch.setattr(appmod.sp, "create_playlist", quota_create)
    start_queue(s, pending=("p2",))
    q = wait_done(s)
    assert q["state"] == "stopped" and q["stop_reason"] == "quota"
    hist = s.pacing()["history_429"]
    assert hist and hist[-1]["kind"] == "quota"
    assert calls == []                                   # no retry into a quota trip


def test_rate_429_halves_and_keeps_going(worker_env, monkeypatch):
    calls, s = worker_env
    first = {"burned": False}
    def flaky_add(pid, uri, bulk=False):
        if not first["burned"]:
            first["burned"] = True
            appmod.sp.last_429 = {"ts": time.time(), "kind": "rate", "retry_after": 1}
            raise SpotifyError(429, "rate limit hit — cooldown ~1 min")
        calls.append(("add", pid, uri)); return "snap"
    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky_add)
    start_queue(s, pending=("p2",))
    q = wait_done(s)
    assert q["state"] == "done"
    assert [c[0] for c in calls].count("add") == 1       # the failed add was re-driven
    assert s.pacing()["history_429"][-1]["kind"] == "rate"
    assert s.pacing()["rate_per_min"] == pacing.MIN_RATE  # 1.8 halved, floored


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
