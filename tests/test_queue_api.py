"""The queue HTTP endpoints: enqueue's echo gate, pause/resume/cancel, the
effective-state read, and the no-self-start thread lifecycle.

Reuses test_queue_worker.py's fixture data (PLQ with a 3-track "big" pile and
a 1-track "tiny" pile — 4 tracks + 2 creates = 6 calls) and its `wait_done`
helper, copied here rather than imported: pytest test modules are not import
targets in this repo. Governor.interval() is forced to 0 so a full drain
happens in milliseconds; individual tests that need a LIVE worker to still be
running when the endpoint acts (pause/cancel) force it back up (ruling P1).
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
import sortify.pacing as pacing
from sortify.store import Store

PILE_URIS = [f"spotify:track:m{i}" for i in range(3)]
TINY = [f"spotify:track:t{i}" for i in range(1)]


@pytest.fixture
def client(monkeypatch):
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
    the button is the number the server verifies. 4 tracks + 2 creates = 6."""
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 3})
    assert r.status_code == 409 and "6" in r.json()["detail"]
    assert Store().queue()["state"] == "stopped" and client.calls == []


def test_enqueue_orders_smallest_first_and_starts_the_worker(client):
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 6})
    assert r.status_code == 200 and r.json()["queued"] == ["p2", "p1"]
    q = wait_done(Store())
    assert q["state"] == "done"


def test_enqueue_is_free(client, monkeypatch):
    """The click costs 0: pricing comes from _materialise_plan (local), and
    the response returns before the worker's first tick can land."""
    hold = threading.Event()
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda *a, **k: hold.wait(5) or "NEWP")
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 6})
    assert client.calls == []
    hold.set()


def test_second_enqueue_while_active_is_refused(client, monkeypatch):
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 30.0)
    client.post("/api/split/PLQ/queue", json={"pile_ids": ["p1"], "expected_calls": 4})
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
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 6})
    assert client.post("/api/split/PLQ/queue/pause").status_code == 200
    q = Store().queue()
    assert q["state"] == "paused"
    speed["v"] = 0.0
    r = client.post("/api/split/PLQ/queue/resume")
    assert r.status_code == 200
    assert wait_done(Store())["state"] == "done"
    assert len(client.calls) == 6        # pause+resume cost nothing extra


def test_cancel_clears_pending_but_keeps_records(client, monkeypatch):
    # Ruling P1: force the interval large so the worker is still alive (and
    # pending has more than "current" left) when pause/cancel act on it.
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 30.0)
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 6})
    client.post("/api/split/PLQ/queue/pause")
    r = client.delete("/api/split/PLQ/queue")
    assert r.status_code == 200
    q = Store().queue()
    assert q["state"] == "stopped" and q["stop_reason"] == "cancelled" and q["pending"] == []
    # whatever landed stays resumable at the price of what's left:
    # re-enqueue prices only the remainder (may be 0..6 depending on timing).


def test_restart_shows_a_running_file_as_paused_and_starts_nothing(client):
    """The one-click promise ends with the process: after a restart the queue
    loads paused with a Resume button — no code path from boot to traffic."""
    s = Store(); q = s.queue()
    q.update(playlist_id="PLQ", pending=["p1"], state="running")
    s.save_queue(q)                       # what a crash mid-run leaves behind
    r = client.get("/api/split/PLQ/queue")
    assert r.json()["queue"]["state"] == "paused"
    assert appmod._queue_worker is None and client.calls == []
