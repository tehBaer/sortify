"""The Refresh button: bounded cost, and a rebuild that blocks nothing.

Measured 2026-09-18 against the real account, before this split: one
POST /api/refresh took **424.7s and 90 Spotify calls**, and a concurrent
/api/now/suggest never returned inside 300s. Two separate faults —
an unbounded button, and a lock held across minutes of network I/O.

Zero API calls: everything here runs against stubs.
"""

import threading
import time

import pytest

from sortify import app as appmod


@pytest.fixture(autouse=True)
def _clean_profile_state():
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    yield
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0


# ---- the button re-reads the listing and nothing else ---------------------

def test_refresh_re_reads_the_listing(monkeypatch):
    asked = []
    monkeypatch.setattr(appmod.sp, "my_playlists",
                        lambda refresh=False: asked.append(refresh) or [])
    appmod.refresh_profiles()
    assert asked == [True]


def test_refresh_does_not_rebuild_profiles_in_the_request(monkeypatch):
    """The 70 calls of home re-reads are what made the button take 7 minutes.

    The listing is what the button is for; rebuilding profiles on top of it
    is what made its cost unbounded and its duration a multiple of what the
    UI promised.
    """
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: [])
    built = []
    monkeypatch.setattr(appmod, "_ensure_profiles",
                        lambda force=False: built.append(force) or {"homes": []})
    appmod.refresh_profiles()
    assert built == []


def test_refresh_invalidates_so_the_next_read_rebuilds(monkeypatch):
    """Not rebuilding must not mean serving the pre-refresh profile."""
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: [])
    appmod._profile_state.update(built_at=time.time(), profiles={"x": {}})
    appmod.refresh_profiles()
    assert appmod._profile_state["built_at"] == 0.0


def test_refresh_reports_what_it_spent(monkeypatch):
    """The one user action that bursts has to name its own cost."""
    spent = iter([10, 30])
    monkeypatch.setattr(appmod.sp, "budget_spent", lambda: next(spent))
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: [{"id": "a"}])
    out = appmod.refresh_profiles()
    assert out["calls_spent"] == 20
    assert out["playlists"] == 1


# ---- the rebuild does not hold the lock across network I/O ----------------

def _stub_build(monkeypatch, delay=0.0, marker=None):
    """Replace the expensive half with something slow but free."""
    def fake(cfg=None):
        if delay:
            time.sleep(delay)
        return {"profiles": {"p": {}}, "homes": [], "inputs": [],
                "playlists": [], "input_ids": set(), "playlist_artists": {},
                "last_added": {}, "built_at": time.time(),
                **({"marker": marker} if marker else {})}
    monkeypatch.setattr(appmod, "_build_profiles", fake)


def test_a_reader_is_not_blocked_while_a_rebuild_is_running(monkeypatch):
    """The fault the measurement caught: /api/now/suggest never returned.

    With a warm profile in hand, a second caller must be served the existing
    one immediately rather than queue behind minutes of fetching.
    """
    _stub_build(monkeypatch, delay=1.0)
    appmod._profile_state.update(built_at=time.time(), profiles={"warm": {}})

    done = threading.Event()
    threading.Thread(target=lambda: (appmod._ensure_profiles(force=True),
                                     done.set()), daemon=True).start()
    time.sleep(0.2)  # let the rebuild get inside the slow part

    started = time.time()
    appmod._ensure_profiles()
    assert time.time() - started < 0.5, "reader queued behind the rebuild"
    assert done.wait(5)


def test_the_first_ever_build_still_blocks(monkeypatch):
    """With nothing cached there is no stale profile to serve, so a caller
    must wait rather than be handed an empty one."""
    _stub_build(monkeypatch, delay=0.3)
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    state = appmod._ensure_profiles()
    assert state.get("profiles")


def test_only_one_rebuild_runs_at_a_time(monkeypatch):
    """Two concurrent misses must not both pay for the fetching."""
    runs = []

    def fake(cfg=None):
        runs.append(1)
        time.sleep(0.4)
        return {"profiles": {"p": {}}, "homes": [], "inputs": [], "playlists": [],
                "input_ids": set(), "playlist_artists": {}, "last_added": {},
                "built_at": time.time()}

    monkeypatch.setattr(appmod, "_build_profiles", fake)
    appmod._profile_state.update(built_at=time.time(), profiles={"warm": {}})

    threads = [threading.Thread(target=lambda: appmod._ensure_profiles(force=True))
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(runs) == 1


def test_a_failed_rebuild_clears_the_in_progress_flag(monkeypatch):
    """A raise inside the build must not wedge every later rebuild."""
    def boom(cfg=None):
        raise RuntimeError("spotify is down")

    monkeypatch.setattr(appmod, "_build_profiles", boom)
    appmod._profile_state.update(built_at=time.time(), profiles={"warm": {}})
    with pytest.raises(RuntimeError):
        appmod._ensure_profiles(force=True)
    assert not appmod._profile_state.get("building")


def test_the_swap_publishes_the_new_profile(monkeypatch):
    _stub_build(monkeypatch, marker="fresh")
    appmod._profile_state.update(built_at=0.0, profiles={"stale": {}})
    state = appmod._ensure_profiles(force=True)
    assert state["marker"] == "fresh"
