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

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)
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
    appmod._skip_settle.update(uri=None, until=0.0)
    yield
    appmod._now_cache.update(at=0.0, value=None, ttl=NOW_TTL_IDLE)
    appmod._skip_settle.update(uri=None, until=0.0)


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


# ---- skip settle ------------------------------------------------------------
#
# Right after /api/player/next, Spotify can still report the track that was
# just skipped away from. Caching that answer normally would pin the WRONG
# track for its remaining runtime (minutes), with NOW_FORCE_MIN_INTERVAL
# blocking every correction attempt — in blind mode there is no way to even
# notice. While the pre-skip uri keeps coming back, the answer only lives
# NOW_SKIP_SETTLE_TTL, so the client's server-paced poll re-checks promptly.


def test_an_unsettled_post_skip_answer_is_cached_only_briefly(clock, monkeypatch):
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: playing(progress_ms=10_000))
    _currently_playing_shared()  # cache holds spotify:track:x, minutes left

    # What player_next does: note the pre-skip uri and drop the cache.
    appmod._skip_settle.update(
        uri="spotify:track:x", until=clock[0] + appmod.NOW_SKIP_SETTLE_WINDOW)
    appmod._now_cache["at"] = 0.0

    clock[0] += 1
    _, stale_in = _currently_playing_shared(force=True)  # upstream: still track x
    assert stale_in <= appmod.NOW_SKIP_SETTLE_TTL

    # The next paced poll lands just past the short TTL and finds the truth;
    # from then on the normal remaining-runtime TTL applies again.
    clock[0] += stale_in + 0.5
    settled = {**playing(progress_ms=0),
               "track": {"uri": "spotify:track:y", "duration_ms": TRACK_MS}}
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: settled)
    value, stale_in = _currently_playing_shared()
    assert value["track"]["uri"] == "spotify:track:y"
    assert stale_in > appmod.NOW_SKIP_SETTLE_TTL
    assert appmod._skip_settle["uri"] is None  # settled → suspicion cleared


def test_the_settle_window_expires_so_a_replayed_track_is_not_suspect_forever(
        clock, monkeypatch):
    """The same uri coming back MINUTES after a skip is the user replaying
    the track, not Spotify lagging — normal caching must resume."""
    appmod._skip_settle.update(uri="spotify:track:x", until=clock[0] + 5)
    clock[0] += 60
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: playing(progress_ms=10_000))
    _, stale_in = _currently_playing_shared()
    assert stale_in > appmod.NOW_SKIP_SETTLE_TTL


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


def test_an_idle_poll_lists_inputs_for_the_start_cta(route_client, monkeypatch):
    """When nothing is playing, the input switcher is the page's one useful
    action ("start an input…"), so the idle payload has to name the inputs.
    They must come from config plus the cached listing only — not a profile
    build, which can fetch a missing listing or refuse with the >40-homes 400.
    An idle poll therefore stays exactly one Spotify call, like any other."""
    s = Store()
    cache = s.cache()
    cache["playlist_list"]["items"] = NOW_LISTING + [
        {"id": "IN9", "name": "[Inbox]", "owner": "me", "editable": True,
         "total": 1, "snapshot_id": "snap-in9", "image": None}]
    s.save_cache(cache)

    def idle(method, path, background=False, **kwargs):
        route_client.spotify_calls.append((method, path))
        return {}

    monkeypatch.setattr(appmod.sp, "request", idle)

    body = route_client.get("/api/now").json()

    assert body["playing"] is False
    assert [l["id"] for l in body["inputs"]] == ["IN9"]
    assert route_client.spotify_calls == [("GET", "/me/player/currently-playing")]


def test_the_poll_reports_how_old_its_answer_is(route_client):
    """`fetched_ago_ms` is the display-only honesty behind the client's
    "updated Ns ago" line — near zero on the poll that fetched, and growing
    on every cached serve after it. In blind mode it is the only visible
    proof the blurred card reflects what is actually playing."""
    first = route_client.get("/api/now").json()
    assert first["fetched_ago_ms"] < 1000

    later = route_client.get("/api/now").json()  # served from cache
    assert later["fetched_ago_ms"] >= first["fetched_ago_ms"]
    assert route_client.spotify_calls == [("GET", "/me/player/currently-playing")]


def test_player_next_marks_the_preskip_uri_suspect_and_drops_the_cache(route_client):
    """The route half of the skip-settle tests above: /api/player/next must
    remember what it skipped away from (so a not-yet-settled Spotify answer
    repeating that uri is cached only briefly) and drop the now-cache (so the
    client's settle-poll goes upstream instead of reading back the track we
    just left)."""
    assert route_client.get("/api/now").json()["playing"] is True

    assert route_client.post("/api/player/next").status_code == 200

    assert appmod._skip_settle["uri"] == "spotify:track:x"
    assert appmod._now_cache["at"] == 0.0


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


# ---- the two-phase card: light poll + /api/now/suggest ----------------------
#
# Presenting a track used to block on the whole suggestion pipeline — a
# possible profile rebuild, up to ~8 sequential Last.fm requests, and ~10MB of
# JSON re-parsing — before the client could even show the track's name. The
# split: `?light=1` returns just the card (track, context, sitting) as fast as
# the now-cache allows, and /api/now/suggest computes the suggestion side for
# the already-cached track afterwards, spending no currently-playing call of
# its own.

# The keys the light poll deliberately withholds and the suggest call carries.
# `subsets` was one of them until 2026-08-28: subsets are no longer scored or
# offered, so the key does not exist on either response any more (see
# tests/test_subsets.py). `subset_targets` — the picker's list — stays.
LIGHT_HEAVY_KEYS = ("suggestions", "subset_targets", "homes")


def test_light_poll_returns_the_track_without_the_suggestion_payload(route_client):
    body = route_client.get("/api/now?light=1").json()
    assert body["playing"] is True
    assert body["light"] is True
    assert body["track"]["uri"] == "spotify:track:x"
    assert body["track"]["sortable"] is True
    for heavy in LIGHT_HEAVY_KEYS:
        assert heavy not in body
    assert route_client.spotify_calls == [("GET", "/me/player/currently-playing")]


def test_light_poll_resolves_context_from_the_cached_listing(route_client):
    """The card needs `context` up front (the Remove button and the context
    line hang off it), but from the cached listing only — the light branch
    must never be able to trigger a profile build."""
    body = route_client.get("/api/now?light=1").json()
    assert body["context"] == {"id": "CTX1", "name": "Some playlist", "is_input": False}


def test_light_poll_carries_the_sitting(route_client):
    """The sitting card renders in phase 1, so the light payload includes the
    same free splits.json lookup the full endpoint does."""
    Store().save_splits({"version": 1, "splits": {"PL_NOW": {
        "created_at": "t", "snapshot_id": None, "params": {},
        "piles": [{"id": "p1", "name": "dream pop", "tags": [], "uris": ["spotify:track:x"]}],
        "decided": {},
        "active_sitting": {"playlist_id": "CTX1", "pile_id": "p1",
                           "uris": ["spotify:track:x"], "started_at": "t", "claim": "c1"},
    }}})
    body = route_client.get("/api/now?light=1").json()
    assert body["sitting"]["split_id"] == "PL_NOW"


def test_a_forced_light_poll_does_not_reach_lastfm(route_client, monkeypatch):
    """Enrichment moved to the suggest phase: the light poll's whole point is
    to be fast, so even `force=1` must not put Last.fm round trips on it."""
    fetched = []
    monkeypatch.setattr(appmod, "_fetch_missing_now_tags", lambda t: fetched.append(t))
    assert route_client.get("/api/now?force=1&light=1").status_code == 200
    assert fetched == []


def test_suggest_serves_the_cached_track_without_a_spotify_call(route_client):
    route_client.get("/api/now?light=1")  # primes the now-cache: the one call
    body = route_client.get("/api/now/suggest").json()
    assert body["playing"] is True
    assert body["track_uri"] == "spotify:track:x"
    for key in LIGHT_HEAVY_KEYS + ("inputs", "context"):
        assert key in body
    assert route_client.spotify_calls == [("GET", "/me/player/currently-playing")]


def test_suggest_with_no_cached_answer_reports_not_playing(route_client):
    body = route_client.get("/api/now/suggest").json()
    assert body["playing"] is False
    assert route_client.spotify_calls == []


def test_suggest_fetches_lastfm_only_when_forced(route_client, monkeypatch):
    """The same gate the full endpoint has always had, relocated: an explicit
    user action (`force=1`) may spend bounded Last.fm calls to sharpen the
    suggestions; the passive follow-up may not."""
    fetched = []
    monkeypatch.setattr(appmod, "_fetch_missing_now_tags", lambda t: fetched.append(t))
    route_client.get("/api/now?light=1")
    route_client.get("/api/now/suggest")
    assert fetched == []
    route_client.get("/api/now/suggest?force=1")
    assert len(fetched) == 1


def test_light_then_suggest_matches_the_full_payload(route_client):
    """The two-phase pair must add up to exactly what the one-shot endpoint
    serves — a key that drifts out of the union is a field the split silently
    dropped."""
    light = route_client.get("/api/now?light=1").json()
    sugg = route_client.get("/api/now/suggest").json()
    full = route_client.get("/api/now").json()
    merged = {**light, **{k: v for k, v in sugg.items() if k != "track_uri"}}
    merged.pop("light")
    assert set(merged) == set(full)


# ---- picker recency: last_added_at on the homes payload ---------------------


def test_last_added_at_is_the_latest_added_at_or_none():
    import sortify.app as appmod
    tracks = [
        {"uri": "a", "added_at": "2026-01-05T10:00:00Z"},
        {"uri": "b", "added_at": "2026-08-20T09:30:00Z"},
        {"uri": "c"},  # local/odd entries carry no added_at
    ]
    assert appmod._last_added_at(tracks) == "2026-08-20T09:30:00Z"
    assert appmod._last_added_at([]) is None
    assert appmod._last_added_at([{"uri": "c"}]) is None


def test_homes_payload_carries_last_added_at():
    import sortify.app as appmod
    state = {
        "homes": [
            {"id": "h1", "name": "One", "image": None, "total": 3, "snapshot_id": "s1"},
            {"id": "h2", "name": "Two", "image": None, "total": 0, "snapshot_id": "s2"},
        ],
        "last_added": {"h1": "2026-08-20T09:30:00Z"},
    }
    payload = appmod._homes_payload(state)
    by_id = {h["id"]: h for h in payload}
    assert by_id["h1"]["last_added_at"] == "2026-08-20T09:30:00Z"
    assert by_id["h2"]["last_added_at"] is None
