"""Now-playing polling cost.

The shipped bug: a 5s server cache paired with a 6s client poll, so every poll
missed and an open tab burned ~600 calls/hour just watching one track play.
The fix makes the cache last until the track ends and lets the server hand the
client its next poll time, so the two can no longer disagree.

Everything above the "the endpoint itself" divider tests the helper
(`_currently_playing_shared`) in isolation. That is not enough on its own:
the helper is not what the client polls, and a second `sp.currently_playing()`
added anywhere in `now_playing()` doubles the cost of every poll while every
helper-level assertion here still passes. The route-level tests at the bottom
count calls at `Spotify.request()` for that reason.
"""

import time

import pytest
from fastapi.testclient import TestClient

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
from sortify.store import Store

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


# ---- the endpoint itself ----------------------------------------------------
#
# Every test above drives `_currently_playing_shared()` directly, so all of
# them pin the helper and none of them pin GET /api/now — the thing an open
# tab actually calls, and the thing this branch grew a `_sitting_for_context`
# read in. Inserting a second `sp.currently_playing()` into `now_playing()`
# doubled the cost of every poll with the whole suite still green. That is the
# shape of the original ~600-calls/hour bug, so it gets a test at the level
# where it happens.
#
# Counted at `Spotify.request()` — the single chokepoint currently_playing,
# my_playlists, playlist_tracks and everything else funnels through — rather
# than at the methods this endpoint happens to call today, so ANY call the
# handler grows shows up here rather than being absorbed by a stand-in fake.
# Same reasoning as test_get_split_spends_no_api_calls in test_split_api.py.

RAW_CURRENTLY_PLAYING = {
    "item": {
        "uri": "spotify:track:x", "id": "x", "name": "X", "type": "track",
        "is_local": False, "duration_ms": TRACK_MS,
        "artists": [{"id": "a1", "name": "A"}],
        "album": {"name": "Alb", "images": [{"url": "http://img/1"}]},
    },
    "is_playing": True,
    "progress_ms": 1_000,
    "context": {"type": "playlist", "uri": "spotify:playlist:CTX1"},
}

NOW_LISTING = [{"id": "CTX1", "name": "Some playlist", "owner": "me", "editable": False,
                "total": 3, "snapshot_id": "snap-ctx1", "image": None}]


@pytest.fixture
def route_client(monkeypatch):
    """GET /api/now with every Spotify call trapped and counted.

    cache.json / config.json / splits.json are one set of files for the whole
    test session (see conftest.py), so this captures and restores them —
    otherwise the injected playlist listing would follow other test files
    around depending on run order.

    The listing it primes is deliberately non-editable and not input-named, so
    `_ensure_profiles` resolves zero homes and zero inputs: the route's own
    cost is then exactly the currently-playing call, with nothing else to hide
    a regression behind.
    """
    s = Store()
    original_cache, original_config, original_splits = s.cache(), s.config(), s.splits()
    original_tags = s.tags()

    cache = s.cache()
    cache["playlist_list"] = {"fetched_at": 0.0, "items": NOW_LISTING}
    s.save_cache(cache)
    s.save_config({**original_config, "input_ids": [], "home_ids": [],
                   "input_name_pattern": r"^\[.+\]$"})
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0

    # This file's `force=1` tests exist to pin polling cost, not Last.fm
    # fetching (that's tests/test_now_tag_fetch.py) — default to "no key
    # configured" so `_fetch_missing_now_tags` is a guaranteed no-op here and
    # none of these tests can reach the real Last.fm service just because
    # this machine happens to have a real key file on disk.
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)

    calls = []

    def trap(method, path, background=False, **kwargs):
        calls.append((method, path))
        return RAW_CURRENTLY_PLAYING

    # Any method-level stand-in sitting on the client instance would swallow
    # the call before it reached the trap, and `sp` is a module-level
    # singleton: monkeypatch.setattr(sp, "currently_playing", ...) in an
    # earlier test leaves a real instance attribute behind even after undo
    # (undo re-sets the bound method it read off the class). Strip anything
    # shadowing the real methods so the trap is genuinely reachable — the
    # same reason test_split_api.py delattrs before trapping request().
    for name in ("currently_playing", "my_playlists", "playlist_tracks"):
        if name in vars(appmod.sp):
            monkeypatch.delattr(appmod.sp, name)
    monkeypatch.setattr(appmod.sp, "request", trap)

    c = TestClient(appmod.app)
    c.spotify_calls = calls
    try:
        yield c
    finally:
        s = Store()
        s.save_cache(original_cache)
        s.save_config(original_config)
        s.save_splits(original_splits)
        s.save_tags(original_tags)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0


def test_one_poll_of_the_endpoint_costs_one_call(route_client):
    r = route_client.get("/api/now")
    assert r.status_code == 200
    assert r.json()["playing"] is True
    assert route_client.spotify_calls == [("GET", "/me/player/currently-playing")]


def test_polling_the_endpoint_inside_the_ttl_costs_nothing(route_client):
    """The regression that shipped once: an open tab polling on its own clock
    against a shorter cache, every poll a miss."""
    assert route_client.get("/api/now").status_code == 200
    for _ in range(10):
        assert route_client.get("/api/now").status_code == 200
    assert route_client.spotify_calls == [("GET", "/me/player/currently-playing")]


def test_forced_refreshes_of_the_endpoint_obey_the_floor(route_client):
    """`?force=1` is explicit user action (opening the view, refocusing the
    tab), so it skips the TTL — but never NOW_FORCE_MIN_INTERVAL. Ten forced
    polls land well inside that 10s floor, so nine of them must be free; a
    force that bypassed the floor would make refocusing a tab a paid event."""
    for _ in range(10):
        assert route_client.get("/api/now?force=1").status_code == 200
    assert route_client.spotify_calls == [("GET", "/me/player/currently-playing")]


def test_the_sitting_lookup_on_the_poll_path_is_free(route_client):
    """`_sitting_for_context` is this branch's addition to the hottest path in
    the app. It answers from splits.json on purpose; resolving the context
    playlist against Spotify instead would put a call on every poll of every
    open tab."""
    Store().save_splits({"version": 1, "splits": {"PL_NOW": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None, "params": {},
        "piles": [{"id": "p1", "name": "dream pop", "tags": [], "uris": ["spotify:track:x"]}],
        "decided": {"spotify:track:x": {"action": "keep", "to_id": "H1", "at": "t"}},
        "active_sitting": {"playlist_id": "CTX1", "pile_id": "p1",
                           "uris": ["spotify:track:x"],
                           "started_at": "2026-08-17T10:05:00Z", "claim": "c1"},
    }}})

    body = route_client.get("/api/now").json()

    assert body["sitting"]["split_id"] == "PL_NOW"
    assert body["sitting"]["decided"] == {
        "spotify:track:x": {"action": "keep", "to_id": "H1", "at": "t"}}
    assert route_client.spotify_calls == [("GET", "/me/player/currently-playing")]
