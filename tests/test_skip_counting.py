"""Skip counting: now-playing answers feed the ledger shared with spotify-autoqueuer.

The detector and the ledger themselves are tested in spotify-ledger
(test_skip_ledger.py). These pin the wiring: fresh answers reach the detector,
cached ones cost nothing and count nothing twice, and Next supplies the exact
position that once-per-track polling cannot.
"""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from sortify.skip_ledger import SkipDetector

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)


def playing(track_id, progress_ms, duration_ms=200_000, is_playing=True):
    return {
        "track": {"uri": f"spotify:track:{track_id}", "id": track_id, "name": f"n-{track_id}",
                  "type": "track", "duration_ms": duration_ms, "artists": [{"id": "a", "name": "Art"}]},
        "is_playing": is_playing,
        "progress_ms": progress_ms,
        "context_playlist_id": None,
    }


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)
    appmod._skip_settle.update(uri=None, until=0.0)
    monkeypatch.setattr(appmod, "_skip_detector", SkipDetector())
    yield
    appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)
    appmod._skip_settle.update(uri=None, until=0.0)


@pytest.fixture
def clock(monkeypatch):
    now = [1_790_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def counts():
    path = os.environ["SPOTIFY_SKIP_LEDGER"]
    if not os.path.exists(path):
        return {}
    return {k: v["count"] for k, v in json.load(open(path))["tracks"].items()}


def test_an_early_change_between_polls_counts(clock, monkeypatch):
    answer = [playing("a", 5_000)]
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: answer[0])
    appmod._currently_playing_shared()
    clock[0] += 12
    answer[0] = playing("b", 1_000)
    appmod._currently_playing_shared(force=True)
    assert counts() == {"a": 1}


def test_a_song_that_played_on_is_not_counted(clock, monkeypatch):
    answer = [playing("a", 50_000)]
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: answer[0])
    appmod._currently_playing_shared()
    clock[0] += 12  # could have reached 62 s
    answer[0] = playing("b", 1_000)
    appmod._currently_playing_shared(force=True)
    assert counts() == {}


def test_next_supplies_the_exact_position(clock, monkeypatch):
    """Polled once per track, the next answer comes minutes later — too late to
    be sure. Pressing Next pins where the song was at that moment."""
    answer = [playing("a", 5_000)]
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: answer[0])
    monkeypatch.setattr(appmod.sp, "skip_next", lambda: None)
    appmod._currently_playing_shared()
    clock[0] += 20  # at 25 s when Next is pressed
    TestClient(appmod.app).post("/api/player/next")
    clock[0] += 50  # the next poll is late — without Next's reading, 75 s "could" have played
    answer[0] = playing("b", 1_000)
    appmod._currently_playing_shared(force=True)
    assert counts() == {"a": 1}


def test_next_with_an_expired_answer_says_nothing(clock, monkeypatch):
    answer = [playing("a", 5_000, is_playing=False)]  # paused: 60 s TTL
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: answer[0])
    monkeypatch.setattr(appmod.sp, "skip_next", lambda: None)
    appmod._currently_playing_shared()
    clock[0] += 400  # long expired; it may have been resumed and played out elsewhere
    TestClient(appmod.app).post("/api/player/next")
    clock[0] += 2
    answer[0] = playing("b", 1_000)
    appmod._currently_playing_shared(force=True)
    # The detector alone ignores a 402 s gap. Had Next re-fed the stale paused reading as
    # "now", 'a' would look skipped at 5 s two seconds ago.
    assert counts() == {}


def test_a_cached_answer_feeds_nothing(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(appmod, "_skip_observe", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: playing("a", 5_000))
    appmod._currently_playing_shared()
    appmod._currently_playing_shared()  # served from cache
    assert len(calls) == 1


def test_a_broken_ledger_never_breaks_now_playing(clock, monkeypatch):
    monkeypatch.setenv("SPOTIFY_SKIP_LEDGER", "/proc/nope/skips.json")
    answer = [playing("a", 5_000)]
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: answer[0])
    appmod._currently_playing_shared()
    clock[0] += 12  # past NOW_FORCE_MIN_INTERVAL
    answer[0] = playing("b", 0)
    value, _ = appmod._currently_playing_shared(force=True)
    assert value["track"]["id"] == "b"
