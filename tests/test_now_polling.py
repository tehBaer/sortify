"""Now-playing polling cost.

The shipped bug: a 5s server cache paired with a 6s client poll, so every poll
missed and an open tab burned ~600 calls/hour just watching one track play.
The fix makes the cache last until the track ends and lets the server hand the
client its next poll time, so the two can no longer disagree.
"""

import time

import pytest

from sortify import app as appmod
from sortify.app import (
    NOW_FORCE_MIN_INTERVAL,
    NOW_TTL_IDLE,
    NOW_TTL_MAX,
    NOW_TTL_MIN,
    _currently_playing_shared,
    _now_ttl,
    _poll_after_ms,
)

TRACK_MS = 210_000  # a 3.5-minute track


def playing(progress_ms, duration_ms=TRACK_MS, is_playing=True):
    return {
        "track": {"uri": "spotify:track:x", "duration_ms": duration_ms},
        "is_playing": is_playing,
        "progress_ms": progress_ms,
        "context_playlist_id": None,
    }


@pytest.fixture(autouse=True)
def _clear_now_cache():
    appmod._now_cache.update(at=0.0, value=None, ttl=NOW_TTL_IDLE)
    yield
    appmod._now_cache.update(at=0.0, value=None, ttl=NOW_TTL_IDLE)


@pytest.fixture
def clock(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


# ---- ttl derivation --------------------------------------------------------


def test_ttl_is_the_tracks_remaining_runtime():
    assert _now_ttl(playing(progress_ms=10_000)) == pytest.approx(201.0)


def test_ttl_clamped_between_floor_and_ceiling():
    assert _now_ttl(playing(progress_ms=TRACK_MS - 500)) == NOW_TTL_MIN
    assert _now_ttl(playing(progress_ms=0, duration_ms=3_600_000)) == NOW_TTL_MAX


def test_ttl_idle_when_paused_or_silent():
    assert _now_ttl(None) == NOW_TTL_IDLE
    assert _now_ttl(playing(progress_ms=5_000, is_playing=False)) == NOW_TTL_IDLE


def test_ttl_falls_back_when_the_payload_lacks_timing():
    assert _now_ttl(playing(progress_ms=None)) == NOW_TTL_MIN
    assert _now_ttl(playing(progress_ms=0, duration_ms=None)) == NOW_TTL_MIN


# ---- the regression: client pace vs cache lifetime --------------------------


def test_client_never_polls_before_the_cache_goes_stale(clock, monkeypatch):
    """The exact shape of the old bug — poll interval shorter than the TTL."""
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: playing(progress_ms=0))
    _, stale_in = _currently_playing_shared()
    assert _poll_after_ms(stale_in) / 1000 >= stale_in


def test_an_hour_of_playback_costs_about_one_call_per_track(clock, monkeypatch):
    calls = []
    start = clock[0]

    def fake_now():
        calls.append(clock[0])
        return playing(progress_ms=((clock[0] - start) % 210) * 1000)

    monkeypatch.setattr(appmod.sp, "currently_playing", fake_now)

    # Drive the loop the way the client does: ask, then sleep exactly as long
    # as the server told us to.
    while clock[0] < start + 3600:
        _, stale_in = _currently_playing_shared()
        clock[0] += _poll_after_ms(stale_in) / 1000

    assert len(calls) <= 20, f"{len(calls)} calls/hour — the 6s-poll bug was ~600"


def test_many_tabs_still_cost_one_call(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(
        appmod.sp, "currently_playing", lambda: (calls.append(1), playing(progress_ms=0))[1]
    )
    for _ in range(25):  # 25 tabs polling at once
        _currently_playing_shared()
    assert len(calls) == 1


# ---- forced refresh --------------------------------------------------------


def test_force_bypasses_the_predicted_ttl(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(
        appmod.sp, "currently_playing", lambda: (calls.append(1), playing(progress_ms=0))[1]
    )
    _currently_playing_shared()
    clock[0] += NOW_FORCE_MIN_INTERVAL + 1
    _currently_playing_shared()          # inside the 211s TTL — no call
    assert len(calls) == 1
    _currently_playing_shared(force=True)  # user asked — call
    assert len(calls) == 2


def test_force_cannot_outrun_its_own_floor(clock, monkeypatch):
    calls = []
    monkeypatch.setattr(
        appmod.sp, "currently_playing", lambda: (calls.append(1), playing(progress_ms=0))[1]
    )
    _currently_playing_shared(force=True)
    for _ in range(50):  # mashing refresh
        clock[0] += 0.1
        _currently_playing_shared(force=True)
    assert len(calls) == 1


def test_force_still_reports_the_automatic_pace(clock, monkeypatch):
    """A rejected force must not tell the client to come back in 10s forever."""
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: playing(progress_ms=0))
    _currently_playing_shared()
    clock[0] += 1
    _, stale_in = _currently_playing_shared(force=True)
    assert stale_in > NOW_FORCE_MIN_INTERVAL
