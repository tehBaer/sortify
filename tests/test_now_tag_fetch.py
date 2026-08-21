"""Bounded on-demand Last.fm tag fetch for unknown now-playing artists.

`_fetch_missing_now_tags` is the only place `/api/now` may ever call Last.fm,
and it may only run from the `?force=1` branch — explicit user action
(opening or refocusing the view), never the passive poll. This mirrors
test_now_polling.py's route-level style (a trapped Spotify client, an
isolated cache/config snapshot) but adds a trapped Last.fm client instead of
counting Spotify calls.
"""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)
from sortify.app import (
    NOW_FETCH_MAX_ARTISTS,
    NOW_FETCH_MAX_SIMILAR_ARTISTS,
    NOW_FETCH_MIN_INTERVAL,
    NOW_FETCH_TIMEOUT,
    _fetch_missing_now_tags,
    _merge_save_lastfm_artists,
    _merge_save_lastfm_tracks,
    _merge_save_tag_artists,
)
from sortify.store import Store
from sortify.tags import ArtistTags, LastFm, LastFmError, track_key

TRACK_MS = 210_000


def track_with_artists(artist_ids):
    return {
        "uri": "spotify:track:x", "id": "x", "name": "X", "type": "track",
        "is_local": False, "duration_ms": TRACK_MS,
        "artists": [{"id": aid, "name": f"Artist {aid}"} for aid in artist_ids],
        "album": "Alb", "image": None,
    }


RAW_CURRENTLY_PLAYING = {
    "item": {
        "uri": "spotify:track:x", "id": "x", "name": "X", "type": "track",
        "is_local": False, "duration_ms": TRACK_MS,
        "artists": [{"id": "a1", "name": "A"}, {"id": "a2", "name": "B"},
                    {"id": "a3", "name": "C"}, {"id": "a4", "name": "D"}],
        "album": {"name": "Alb", "images": [{"url": "http://img/1"}]},
    },
    "is_playing": True,
    "progress_ms": 1_000,
    "context": {"type": "playlist", "uri": "spotify:playlist:CTX1"},
}

NOW_LISTING = [{"id": "CTX1", "name": "Some playlist", "owner": "me", "editable": False,
                "total": 3, "snapshot_id": "snap-ctx1", "image": None}]


@pytest.fixture(autouse=True)
def _isolate_tags_json():
    """tags.json is shared across the whole test session (see conftest.py's
    module-wide data dir) — every test in this file writes to it, so restore
    whatever was there before regardless of which test ran."""
    original = Store().tags()
    yield
    Store().save_tags(original)


@pytest.fixture(autouse=True)
def _isolate_lastfm_tracks_json():
    """Same isolation, for lastfm_tracks.json — the piggyback's track-record
    step writes it too, and it shares this file's module-wide data dir."""
    original = Store().lastfm_tracks()
    yield
    Store().save_lastfm_tracks(original)


@pytest.fixture(autouse=True)
def _isolate_lastfm_artists_json():
    """Same isolation, for lastfm_artists.json — the piggyback's
    artist-similar step (Task 4) writes it too, and it shares this file's
    module-wide data dir."""
    original = Store().lastfm_artists()
    yield
    Store().save_lastfm_artists(original)


@pytest.fixture(autouse=True)
def _reset_now_fetch_floor():
    """`_now_fetch_last_attempt` is process-global state guarding
    NOW_FETCH_MIN_INTERVAL. Without a reset, one test's real-clock fetch
    would poison every other test that calls `_fetch_missing_now_tags`
    within the next 60s of wall time — which, run back to back, is all of
    them."""
    appmod._now_fetch_last_attempt = 0.0
    yield
    appmod._now_fetch_last_attempt = 0.0


class RaisingFm:
    """Stands in for a Last.fm client that must never be touched — real-key
    hazard discipline: every method the piggyback could possibly call
    (artist tags AND the track-record pair) raises, so a passive poll that
    accidentally reaches ANY of them fails loudly instead of silently
    falling through to a real key/network call."""

    def top_tags(self, name):
        raise AssertionError(f"Last.fm must not be called for the passive poll (got {name!r})")

    def track_similar(self, artist, title):
        raise AssertionError(
            f"Last.fm must not be called for the passive poll (getSimilar {artist!r}/{title!r})"
        )

    def track_top_tags(self, artist, title):
        raise AssertionError(
            f"Last.fm must not be called for the passive poll (getTopTags {artist!r}/{title!r})"
        )

    def artist_similar(self, name):
        raise AssertionError(
            f"Last.fm must not be called for the passive poll (getSimilar artist {name!r})"
        )


class FakeFm:
    """Records every artist name it was asked about."""

    def __init__(self, tags_by_name=None, error_by_name=None):
        self.tags_by_name = tags_by_name or {}
        self.error_by_name = error_by_name or {}
        self.calls = []

    def top_tags(self, name):
        self.calls.append(name)
        if name in self.error_by_name:
            raise self.error_by_name[name]
        got = self.tags_by_name.get(name)
        if got is None:
            return None
        from sortify.tags import ArtistTags
        return ArtistTags(matched_name=name, tags=got)


class FakeFmFull(FakeFm):
    """`FakeFm` plus the track-level pair (`track_similar`, `track_top_tags`)
    the piggyback's track-record step calls, and `artist_similar` for the
    artist-similar step (Task 4). Track methods are keyed by
    `(artist, title)`; `track_error_by_key` simulates a non-"not found"
    Last.fm failure on either call — matching `fetch_track`'s own contract
    that neither call wraps a raised exception before it propagates.
    `artist_similar` is keyed by artist name, mirroring `FakeFm.top_tags`;
    `similar_error_by_name` simulates a failure — `LastFm.artist_similar`
    also never wraps a raised exception, matching the track pair."""

    def __init__(self, tags_by_name=None, error_by_name=None,
                 track_similar_by_key=None, track_tags_by_key=None,
                 track_error_by_key=None,
                 similar_by_name=None, similar_error_by_name=None):
        super().__init__(tags_by_name=tags_by_name, error_by_name=error_by_name)
        self.track_similar_by_key = track_similar_by_key or {}
        self.track_tags_by_key = track_tags_by_key or {}
        self.track_error_by_key = track_error_by_key or {}
        self.track_calls = []
        self.similar_by_name = similar_by_name or {}
        self.similar_error_by_name = similar_error_by_name or {}
        self.similar_calls = []

    def track_similar(self, artist, title):
        self.track_calls.append(("similar", artist, title))
        key = (artist, title)
        if key in self.track_error_by_key:
            raise self.track_error_by_key[key]
        return self.track_similar_by_key.get(key)

    def track_top_tags(self, artist, title):
        self.track_calls.append(("top_tags", artist, title))
        key = (artist, title)
        if key in self.track_error_by_key:
            raise self.track_error_by_key[key]
        return self.track_tags_by_key.get(key)

    def artist_similar(self, name):
        self.similar_calls.append(name)
        if name in self.similar_error_by_name:
            raise self.similar_error_by_name[name]
        return self.similar_by_name.get(name)


@pytest.fixture
def isolated_now(monkeypatch):
    """Same isolation as test_now_polling.py's route_client: a clean cache /
    config / splits / tags snapshot restored after the test, and the shared
    profile cache reset so it always rebuilds off this test's fixtures."""
    s = Store()
    original_cache, original_config = s.cache(), s.config()
    original_splits, original_tags = s.splits(), s.tags()

    cache = s.cache()
    cache["playlist_list"] = {"fetched_at": 0.0, "items": NOW_LISTING}
    s.save_cache(cache)
    s.save_config({**original_config, "input_ids": [], "home_ids": [],
                   "input_name_pattern": r"^\[.+\]$"})
    s.save_tag_artists({})
    s.save_lastfm_tracks({"version": 1, "tracks": {}})
    s.save_lastfm_artists({"version": 1, "artists": {}})
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0

    calls = []

    def trap(method, path, background=False, **kwargs):
        calls.append((method, path))
        return RAW_CURRENTLY_PLAYING

    for name in ("currently_playing", "my_playlists", "playlist_tracks"):
        if name in vars(appmod.sp):
            monkeypatch.delattr(appmod.sp, name)
    monkeypatch.setattr(appmod.sp, "request", trap)
    appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)

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
        appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)


# ---- the unit: _fetch_missing_now_tags -------------------------------------


def test_no_key_means_no_client_and_no_fetch(monkeypatch):
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)

    def boom():
        raise AssertionError("LastFm must not be constructed without a key")

    monkeypatch.setattr(appmod, "LastFm", boom)
    n = _fetch_missing_now_tags(track_with_artists(["a1"]))
    assert n == 0


def test_fetches_at_most_three_unknown_artists(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(tags_by_name={f"Artist {i}": [{"name": "rock", "count": 50}]
                              for i in ("a1", "a2", "a3", "a4")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1", "a2", "a3", "a4"]))

    assert n == NOW_FETCH_MAX_ARTISTS == 3
    assert len(fm.calls) == 3
    saved = Store().tag_artists()
    assert len(saved) == 3
    for aid in list(saved):
        assert saved[aid]["tags"] == [{"name": "rock", "count": 50}]
        assert saved[aid]["miss"] is False


def test_known_artists_including_misses_are_not_refetched(monkeypatch):
    Store().save_tag_artists({
        "a1": {"name": "Artist a1", "lastfm_name": None, "tags": [],
               "fetched_at": "2026-08-17T00:00:00Z", "miss": True},
        "a2": {"name": "Artist a2", "lastfm_name": "Artist a2",
               "tags": [{"name": "jazz", "count": 20}],
               "fetched_at": "2026-08-17T00:00:00Z", "miss": False},
    })
    fm = FakeFm()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1", "a2"]))

    assert n == 0
    assert fm.calls == []


def test_non_code_6_error_leaves_the_artist_absent(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(error_by_name={"Artist a1": LastFmError("Last.fm error 29: rate limited")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert n == 0
    assert "a1" not in Store().tag_artists()


def test_code_6_not_found_is_recorded_as_a_permanent_miss(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(tags_by_name={"Artist a1": None})  # None -> "not found"
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert n == 1
    saved = Store().tag_artists()
    assert saved["a1"]["miss"] is True


def test_a_partial_batch_persists_what_succeeded_before_the_error(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(
        tags_by_name={"Artist a1": [{"name": "rock", "count": 50}]},
        error_by_name={"Artist a2": LastFmError("Last.fm error 8: service offline")},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1", "a2"]))

    saved = Store().tag_artists()
    assert "a1" in saved
    assert "a2" not in saved
    assert n == 1


# ---- the route: only ?force=1 may ever fetch --------------------------------


def test_passive_poll_never_touches_last_fm(isolated_now, monkeypatch):
    # Fix round 1, I4: a raising fake here is toothless — `now_playing`'s
    # `?force=1` branch wraps `_fetch_missing_now_tags` in a broad `except
    # Exception: log.exception(...)`, so if a future regression called it on
    # a passive poll too, `RaisingFm`'s AssertionError would be silently
    # swallowed by that same broad except and this test would still see a
    # 200 — a false negative. A counting fake that never raises, asserted
    # empty afterward, catches that regression directly instead of relying
    # on an exception surviving a catch-all.
    fm = FakeFmFull()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)
    r = isolated_now.get("/api/now")
    assert r.status_code == 200
    assert r.json()["playing"] is True
    assert fm.calls == []
    assert fm.similar_calls == []
    assert fm.track_calls == []


def test_forced_poll_fetches_and_persists_unknown_artists(isolated_now, monkeypatch):
    fm = FakeFm(tags_by_name={n: [{"name": "rock", "count": 50}]
                              for n in ("A", "B", "C", "D")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    r = isolated_now.get("/api/now?force=1")

    assert r.status_code == 200
    assert sorted(fm.calls) == ["A", "B", "C"]  # capped at 3, first-seen order
    saved = Store().tag_artists()
    assert {"a1", "a2", "a3"} <= set(saved)
    assert "a4" not in saved


def test_forced_poll_with_no_key_returns_200_with_no_fetch(isolated_now, monkeypatch):
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)
    r = isolated_now.get("/api/now?force=1")
    assert r.status_code == 200
    assert Store().tag_artists() == {}


def test_forced_poll_survives_a_last_fm_failure(isolated_now, monkeypatch):
    fm = FakeFm(error_by_name={
        n: LastFmError("Last.fm error 29: rate limited") for n in ("A", "B", "C", "D")
    })
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    r = isolated_now.get("/api/now?force=1")

    assert r.status_code == 200
    assert Store().tag_artists() == {}


def test_a_fetched_artist_is_visible_to_the_very_next_request(isolated_now, monkeypatch):
    """Task 1's ruling: suggestions read tag_artists guard-on-read, not off a
    profile-build-time snapshot — a fetch must be visible without a restart
    and without waiting out PROFILE_TTL."""
    fm = FakeFm(tags_by_name={n: [{"name": "rock", "count": 50}]
                              for n in ("A", "B", "C", "D")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    isolated_now.get("/api/now?force=1")
    fm.calls.clear()
    # Next request, passive: must see the freshly-written tags without
    # calling Last.fm again (write-once) and without a process restart.
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: RaisingFm())
    r = isolated_now.get("/api/now")

    assert r.status_code == 200
    assert Store().tag_artists()["a1"]["tags"] == [{"name": "rock", "count": 50}]


# ---- latency + retry floor: fix round, Important 1 --------------------------
#
# `_fetch_missing_now_tags` runs inline inside `?force=1`, a request the user
# is waiting on. Two separate bounds: a short per-request Last.fm timeout
# (this path only — the splitter's own `top_tags` call keeps its 15s), and a
# floor on how often an *attempt* happens at all, so a persistent Last.fm
# failure can't turn every subsequent force into an unfloored retry.


def test_a_failed_fetch_is_not_retried_within_the_floor(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(error_by_name={"Artist a1": LastFmError("Last.fm error 29: rate limited")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)
    clock = {"t": 1_000.0}

    n1 = _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])
    assert n1 == 0
    assert fm.calls == ["Artist a1"]

    clock["t"] += NOW_FETCH_MIN_INTERVAL - 1  # still inside the floor
    n2 = _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])

    assert n2 == 0
    assert fm.calls == ["Artist a1"]  # not called again — the client was never touched


def test_a_fetch_after_the_floor_elapses_does_retry(monkeypatch):
    Store().save_tag_artists({})
    fm = FakeFm(error_by_name={"Artist a1": LastFmError("Last.fm error 29: rate limited")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)
    clock = {"t": 1_000.0}

    _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])
    assert fm.calls == ["Artist a1"]

    clock["t"] += NOW_FETCH_MIN_INTERVAL + 1  # past the floor
    _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])

    assert fm.calls == ["Artist a1", "Artist a1"]


def test_the_fetch_client_and_timeout_reach_the_request_call(monkeypatch):
    """A real `LastFm` instance (not a test double) must have both its
    transport AND its per-request `_timeout` bounded to NOW_FETCH_TIMEOUT,
    so a slow Last.fm can't turn this into a ~46s hang inside a request the
    user is waiting on. Asserting only that `httpx.Client(timeout=...)` gets
    constructed isn't enough: `LastFm.top_tags` passes `timeout=self._timeout`
    explicitly on every `.get(...)` call, which in httpx overrides whatever
    the client itself was built with — so this drives the real (unmocked)
    `enrich()` -> `top_tags()` path and checks the timeout actually reaches
    that `.get(...)` call, not just the client constructor. The splitter's
    own `_lastfm_client()` calls are untouched by this — the swap only ever
    happens on the instance handed to this fetch."""
    Store().save_tag_artists({})
    real_fm = LastFm("fake-key", client=object())  # object(): truthy, no real httpx.Client built
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: real_fm)

    captured_client_kwargs = {}
    captured_get_kwargs = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"toptags": {"tag": [], "@attr": {"artist": "Artist a1"}}}

    class FakeHttpxClient:
        def get(self, url, params=None, timeout=None):
            captured_get_kwargs["timeout"] = timeout
            return FakeResponse()

    def fake_client(*, timeout=None, **kwargs):
        captured_client_kwargs["timeout"] = timeout
        return FakeHttpxClient()

    monkeypatch.setattr(appmod.httpx, "Client", fake_client)

    n = _fetch_missing_now_tags(track_with_artists(["a1"]))  # real enrich()/top_tags(), no mock

    assert captured_client_kwargs["timeout"] == NOW_FETCH_TIMEOUT == 5.0
    assert captured_get_kwargs["timeout"] == NOW_FETCH_TIMEOUT == 5.0
    assert isinstance(real_fm._client, FakeHttpxClient)
    assert real_fm._timeout == NOW_FETCH_TIMEOUT == 5.0
    assert n == 1  # the real round trip actually completed and got recorded


# ---- concurrency: fix round 1, Important 1 ---------------------------------
#
# FastAPI's sync routes run in a threadpool, so two overlapping `?force=1`
# requests genuinely execute concurrently — not a hypothetical. Real threads,
# a gated fake client, and a non-blocking `threading.Lock` around the whole
# fetch: the second overlapping caller must see the lock held and back off
# immediately (fetching nothing this round) rather than either blocking or
# racing the first caller's Last.fm call for the same artist.


def test_concurrent_force_fetches_never_double_call_last_fm(monkeypatch):
    Store().save_tag_artists({})
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class GatedFm:
        def top_tags(self, name):
            calls.append(name)
            entered.set()
            # Held open long enough for the second, overlapping call to run
            # its own (non-blocking) lock attempt while this one is in flight.
            release.wait(timeout=2)
            return ArtistTags(matched_name=name, tags=[{"name": "rock", "count": 50}])

    monkeypatch.setattr(appmod, "_lastfm_client", lambda: GatedFm())

    results: list[int] = []

    def run():
        results.append(_fetch_missing_now_tags(track_with_artists(["a1"])))

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)

    t1.start()
    assert entered.wait(timeout=2), "first fetch never reached Last.fm"
    t2.start()
    t2.join(timeout=2)  # non-blocking lock: must return promptly, fetching nothing
    release.set()
    t1.join(timeout=2)

    assert not t1.is_alive() and not t2.is_alive()
    assert calls == ["Artist a1"]  # never called twice for the same artist
    assert sorted(results) == [0, 1]  # one fetched, one backed off
    assert Store().tag_artists()["a1"]["tags"] == [{"name": "rock", "count": 50}]


# ---- lost-update guard: fix round 1, Important 2 ---------------------------


def test_merge_save_does_not_lose_a_concurrent_writers_work():
    """The fetch path (and the split flow's enrich walk) reads tags.json,
    does slow network work, then saves — a classic read-modify-write race.
    A writer landing in between must not be clobbered by the other's save."""
    Store().save_tag_artists({})

    # This call's own work, computed against a since-gone-stale snapshot...
    stale_view = Store().tag_artists()
    assert stale_view == {}

    # ...while another writer (e.g. a split's enrich walk) saves fresh work
    # in the meantime.
    Store().save_tag_artists({
        "other": {"name": "Other", "lastfm_name": None, "tags": [],
                   "fetched_at": "t0", "miss": False},
    })

    _merge_save_tag_artists({
        "a1": {"name": "Artist a1", "lastfm_name": "Artist a1",
               "tags": [{"name": "rock", "count": 50}], "fetched_at": "t1", "miss": False},
    })

    saved = Store().tag_artists()
    assert set(saved) == {"other", "a1"}  # neither writer's work was dropped


def test_merge_save_never_overwrites_an_existing_entry():
    """Write-once: an entry already on disk always wins, hit or miss alike —
    same rule `enrich` itself follows."""
    Store().save_tag_artists({
        "a1": {"name": "Real Name", "lastfm_name": "Real Name",
               "tags": [{"name": "jazz", "count": 20}], "fetched_at": "t0", "miss": False},
    })

    _merge_save_tag_artists({
        "a1": {"name": "Stale Overwrite", "lastfm_name": None, "tags": [],
               "fetched_at": "t1", "miss": True},
    })

    saved = Store().tag_artists()["a1"]
    assert saved["name"] == "Real Name"
    assert saved["miss"] is False
    assert saved["tags"] == [{"name": "jazz", "count": 20}]


def test_merge_save_refuses_when_a_malformed_reread_would_clobber_the_cache(monkeypatch):
    """Task 5 review, Important: `store.tag_artists()` degrades a malformed
    (but valid-JSON) envelope to `{}` rather than raising — guard-on-read,
    the same behaviour every other reader relies on. Without a check for
    that here, a malformed re-read taken *inside the lock* would make
    `_merge_save_tag_artists` treat a real ~1400-entry cache as correctly
    gone and overwrite it with just this call's `new_entries`.

    Simulates the malformed re-read by making the baseline read (before the
    lock) see the real seeded data, then the lock's own fresh re-read see
    `{}` — exactly the shape a malformed envelope produces."""
    appmod.store.save_tag_artists({
        "a1": {"name": "Real", "lastfm_name": "Real", "tags": [{"name": "jazz", "count": 20}],
               "fetched_at": "t0", "miss": False},
    })

    calls = {"n": 0}
    real_tag_artists = appmod.store.tag_artists

    def flaky_tag_artists():
        calls["n"] += 1
        # First call is `_merge_save_tag_artists`'s baseline (outside the
        # lock): real data. Every call after that stands in for the lock's
        # own fresh re-read seeing a malformed envelope.
        return real_tag_artists() if calls["n"] == 1 else {}

    monkeypatch.setattr(appmod.store, "tag_artists", flaky_tag_artists)

    _merge_save_tag_artists({
        "a2": {"name": "New", "lastfm_name": "New", "tags": [], "fetched_at": "t1", "miss": False},
    })

    # save_tag_artists was never called with the guard tripped, so the file
    # on disk (read directly, bypassing the now-patched tag_artists) still
    # holds the real pre-existing entry and never picked up "a2".
    on_disk = json.loads((appmod.store.dir / "tags.json").read_text())["artists"]
    assert on_disk == {
        "a1": {"name": "Real", "lastfm_name": "Real", "tags": [{"name": "jazz", "count": 20}],
               "fetched_at": "t0", "miss": False},
    }


def test_merge_save_refuses_a_partial_shrink_not_just_total_collapse(monkeypatch):
    """Re-review, residual: a total collapse (baseline non-empty, re-read
    empty) is only the extreme case. tags.json is append-only — no code
    path anywhere ever deletes an entry — so ANY shrink between two reads
    taken moments apart is anomalous, not a legitimate race (a real
    concurrent writer only ever adds entries). A truncated re-read (5 seeded
    entries down to 2, none of them zero) must be refused exactly like the
    total-collapse case, not just the `not current` extreme."""
    seed = {
        f"a{i}": {"name": f"Artist {i}", "lastfm_name": f"Artist {i}", "tags": [],
                   "fetched_at": "t0", "miss": False}
        for i in range(1, 6)
    }
    appmod.store.save_tag_artists(seed)

    calls = {"n": 0}
    real_tag_artists = appmod.store.tag_artists

    def flaky_tag_artists():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_tag_artists()  # baseline: all 5
        truncated = real_tag_artists()
        return {k: truncated[k] for k in list(truncated)[:2]}  # re-read: only 2

    monkeypatch.setattr(appmod.store, "tag_artists", flaky_tag_artists)

    _merge_save_tag_artists({
        "a6": {"name": "New", "lastfm_name": "New", "tags": [], "fetched_at": "t1", "miss": False},
    })

    on_disk = json.loads((appmod.store.dir / "tags.json").read_text())["artists"]
    assert on_disk == seed  # untouched — the guard tripped before any save


# ---- piggyback: the lastfm_tracks.json record fetch (Task 3) ---------------
#
# `_fetch_missing_now_tags` extends past the artist-tags step to also fetch
# the now-playing track's OWN `lastfm_tracks.json` record (getSimilar + track
# top tags) when it has none — same lock, same 60s floor, same bounded
# client, never on the passive poll, never blocking the response on failure.


def _known_artist(name="Artist a1"):
    return {"name": name, "lastfm_name": name, "tags": [], "fetched_at": "t0", "miss": False}


def test_absent_track_record_is_fetched_on_force(monkeypatch):
    """Artist tags already known (so only the track-record step has
    anything to do) — its record must appear under the FIRST credited
    artist's key, matching `scripts/backfill_similar.py`'s own fetch-key
    convention."""
    Store().save_tag_artists({"a1": _known_artist()})
    Store().save_lastfm_tracks({"version": 1, "tracks": {}})
    fm = FakeFmFull(
        track_similar_by_key={("Artist a1", "X"): [{"name": "Z", "match": 0.5,
                                                      "artist": {"name": "Y"}}]},
        track_tags_by_key={("Artist a1", "X"): ["rock"]},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    n = _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert n == 0  # no artist TAGS fetched — only the track-record side effect
    key = track_key("Artist a1", "X")
    saved = Store().lastfm_track_map()[key]
    assert saved["miss"] is False
    assert saved["tags"] == ["rock"]
    assert fm.track_calls == [("similar", "Artist a1", "X"), ("top_tags", "Artist a1", "X")]


def test_present_hit_track_record_is_not_refetched(monkeypatch):
    Store().save_tag_artists({"a1": _known_artist()})
    key = track_key("Artist a1", "X")
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        key: {"similar": [], "tags": [], "fetched_at": "original", "miss": False},
    }})
    fm = FakeFmFull()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert fm.track_calls == []
    assert Store().lastfm_track_map()[key]["fetched_at"] == "original"


def test_present_miss_track_record_is_not_refetched(monkeypatch):
    """The piggyback has no `--refetch-misses` equivalent — a cached
    `miss: true` counts as known, same as a hit."""
    Store().save_tag_artists({"a1": _known_artist()})
    key = track_key("Artist a1", "X")
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        key: {"similar": [], "tags": [], "fetched_at": "original", "miss": True},
    }})
    fm = FakeFmFull()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert fm.track_calls == []


def test_record_present_under_a_co_artists_key_is_not_refetched(monkeypatch):
    """A collab's record fetched under a co-artist's credit (the same
    cross-artist convention `sortify.suggest._track_record` and
    `scripts/backfill_similar.py`'s `tracks_to_fetch` both follow) must not
    be re-fetched just because this track is reached from a different
    credited artist first."""
    Store().save_tag_artists({"a1": _known_artist("Artist a1"), "a2": _known_artist("Artist a2")})
    co_artist_key = track_key("Artist a2", "X")
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        co_artist_key: {"similar": [], "tags": [], "fetched_at": "original", "miss": False},
    }})
    fm = FakeFmFull()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1", "a2"]))

    assert fm.track_calls == []


def test_track_fetch_failure_leaves_absent_and_never_raises(monkeypatch):
    Store().save_tag_artists({"a1": _known_artist()})
    Store().save_lastfm_tracks({"version": 1, "tracks": {}})
    fm = FakeFmFull(
        track_error_by_key={("Artist a1", "X"): LastFmError("Last.fm error 29: rate limited")},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1"]))  # must not raise

    key = track_key("Artist a1", "X")
    assert key not in Store().lastfm_track_map()


def test_forced_poll_track_fetch_failure_still_returns_200(isolated_now, monkeypatch):
    fm = FakeFmFull(
        tags_by_name={n: [] for n in ("A", "B", "C", "D")},
        track_error_by_key={("A", "X"): LastFmError("Last.fm error 29: rate limited")},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    r = isolated_now.get("/api/now?force=1")

    assert r.status_code == 200
    key = track_key("A", "X")
    assert key not in Store().lastfm_track_map()


def test_forced_poll_fetches_the_track_record_too(isolated_now, monkeypatch):
    fm = FakeFmFull(
        tags_by_name={n: [{"name": "rock", "count": 50}] for n in ("A", "B", "C", "D")},
        track_similar_by_key={("A", "X"): []},
        track_tags_by_key={("A", "X"): ["ballad"]},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    r = isolated_now.get("/api/now?force=1")

    assert r.status_code == 200
    key = track_key("A", "X")
    saved = Store().lastfm_track_map()[key]
    assert saved["tags"] == ["ballad"]
    assert saved["miss"] is False


def test_passive_poll_never_fetches_the_track_record(isolated_now, monkeypatch):
    # Fix round 1, I4: same reasoning as `test_passive_poll_never_touches_last_fm`
    # — a raising fake here would have its AssertionError swallowed by the
    # `?force=1` branch's broad except, making it toothless. Counting fake,
    # asserted empty, instead.
    fm = FakeFmFull()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)
    r = isolated_now.get("/api/now")
    assert r.status_code == 200
    assert Store().lastfm_track_map() == {}
    assert fm.calls == []
    assert fm.track_calls == []
    assert fm.similar_calls == []
    assert Store().lastfm_artist_map() == {}


def test_floor_is_shared_between_artist_and_track_fetch(monkeypatch):
    """A single `NOW_FETCH_MIN_INTERVAL` floor covers BOTH fetch kinds: when
    an attempt was just made for one reason, a request that would otherwise
    trigger the OTHER kind must still back off until the floor elapses."""
    Store().save_tag_artists({})
    Store().save_lastfm_tracks({"version": 1, "tracks": {}})
    fm = FakeFmFull(
        error_by_name={"Artist a1": LastFmError("Last.fm error 29: rate limited")},
        track_error_by_key={("Artist a1", "X"): LastFmError("Last.fm error 29: rate limited")},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)
    clock = {"t": 1_000.0}

    _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])
    assert fm.calls == ["Artist a1"]
    # track_similar raises, so fetch_track never reaches track_top_tags —
    # one attempted call is still "the track fetch was attempted".
    assert fm.track_calls == [("similar", "Artist a1", "X")]

    clock["t"] += NOW_FETCH_MIN_INTERVAL - 1  # still inside the floor
    fm.calls.clear()
    fm.track_calls.clear()
    _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])

    assert fm.calls == []  # neither fetch kind attempted again
    assert fm.track_calls == []

    clock["t"] += 2  # past the floor
    _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])

    assert fm.calls == ["Artist a1"]  # both retried once the floor elapses
    assert fm.track_calls == [("similar", "Artist a1", "X")]


def test_no_attempt_at_all_does_not_touch_the_floor(monkeypatch):
    """When everything (artist tags, track record, AND artist-similar) is
    already known, nothing was attempted — the floor timestamp must not
    move, matching the pre-piggyback rule that a fully-known request costs
    nothing and imposes no wait on the next one."""
    key = track_key("Artist a1", "X")
    Store().save_tag_artists({"a1": _known_artist()})
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        key: {"similar": [], "tags": [], "fetched_at": "t0", "miss": False},
    }})
    Store().save_lastfm_artists({"version": 1, "artists": {
        "a1": {"name": "Artist a1", "similar": [], "fetched_at": "t0", "miss": False},
    }})
    fm = FakeFmFull()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)
    appmod._now_fetch_last_attempt = 500.0

    _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: 1_000.0)

    assert appmod._now_fetch_last_attempt == 500.0  # untouched
    assert fm.calls == []
    assert fm.track_calls == []


# ---- fix round 1, Critical (C1): floor must advance even when the track-
# record read blows up on a corrupt lastfm_tracks.json ----------------------


def test_corrupt_lastfm_tracks_json_still_advances_the_floor(monkeypatch):
    """Proven repro: `store.lastfm_track_map()` used to be a bare read
    OUTSIDE any try in this function, so a truly corrupt (invalid-JSON, not
    just malformed-envelope) lastfm_tracks.json raised past the `if
    attempted:` floor stamp — even after the artist-tags step had already
    spent calls. That meant the floor never advanced and every subsequent
    `?force=1` re-spent those calls, unbounded. Fixed: the whole attempt is
    now inside one try/finally, so the floor advances regardless."""
    Store().save_tag_artists({})
    (appmod.store.dir / "lastfm_tracks.json").write_text("{not valid json")
    fm = FakeFmFull(tags_by_name={f"Artist {aid}": [{"name": "rock", "count": 50}]
                                  for aid in ("a1", "a2", "a3")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)
    clock = {"t": 1_000.0}

    n1 = _fetch_missing_now_tags(track_with_artists(["a1", "a2", "a3"]),
                                  clock=lambda: clock["t"])

    assert n1 == 3  # the artist-tags spend still happened and was persisted
    assert Store().tag_artists()  # saved despite the later crash on the track read
    assert appmod._now_fetch_last_attempt == 1_000.0  # the floor DID advance

    fm.calls.clear()
    clock["t"] += 10  # well within NOW_FETCH_MIN_INTERVAL (60s)
    n2 = _fetch_missing_now_tags(track_with_artists(["a1", "a2", "a3"]),
                                  clock=lambda: clock["t"])

    assert n2 == 0
    assert fm.calls == []  # nothing re-spent — the floor held


# ---- _merge_save_lastfm_tracks: same guarantees as _merge_save_tag_artists -


def test_merge_save_lastfm_tracks_never_overwrites_an_existing_entry():
    key = track_key("A", "B")
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        key: {"similar": [{"artist": "X", "track": "Y", "match": 0.9}],
              "tags": ["jazz"], "fetched_at": "t0", "miss": False},
    }})

    _merge_save_lastfm_tracks({key: {"similar": [], "tags": [], "fetched_at": "t1", "miss": True}})

    saved = Store().lastfm_track_map()[key]
    assert saved["fetched_at"] == "t0"
    assert saved["tags"] == ["jazz"]


def test_merge_save_lastfm_tracks_refuses_a_shrunk_reread(monkeypatch):
    seed = {
        f"k{i}": {"similar": [], "tags": [], "fetched_at": "t0", "miss": False}
        for i in range(1, 6)
    }
    appmod.store.save_lastfm_tracks({"version": 1, "tracks": seed})

    calls = {"n": 0}
    real_track_map = appmod.store.lastfm_track_map

    def flaky_track_map():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_track_map()  # baseline: all 5
        truncated = real_track_map()
        return {k: truncated[k] for k in list(truncated)[:2]}  # re-read: only 2

    monkeypatch.setattr(appmod.store, "lastfm_track_map", flaky_track_map)

    _merge_save_lastfm_tracks({"k6": {"similar": [], "tags": [], "fetched_at": "t1", "miss": False}})

    on_disk = json.loads((appmod.store.dir / "lastfm_tracks.json").read_text())["tracks"]
    assert on_disk == seed  # untouched — the guard tripped before any save


# ---- piggyback: the lastfm_artists.json artist-similar record (Task 4) ----
#
# `_fetch_missing_now_tags` extends past the track-record step to also fetch
# up to `NOW_FETCH_MAX_SIMILAR_ARTISTS` credited artists' OWN
# `lastfm_artists.json` record (getSimilar) when they have none — same lock,
# same 60s floor, same bounded client, never on the passive poll, never
# blocking the response on failure.


def test_absent_similar_artist_record_is_fetched_on_force(monkeypatch):
    """Artist tags and the track record already known (so only the
    artist-similar step has anything to do) — its record must appear under
    the artist's OWN Spotify id, not the track key convention the other two
    steps use."""
    Store().save_tag_artists({"a1": _known_artist()})
    key = track_key("Artist a1", "X")
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        key: {"similar": [], "tags": [], "fetched_at": "t0", "miss": False},
    }})
    Store().save_lastfm_artists({"version": 1, "artists": {}})
    fm = FakeFmFull(similar_by_name={"Artist a1": [{"artist": "Neighbour", "match": 0.8}]})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert fm.similar_calls == ["Artist a1"]
    saved = Store().lastfm_artist_map()["a1"]
    assert saved["miss"] is False
    assert saved["similar"] == [{"artist": "Neighbour", "match": 0.8}]
    assert saved["name"] == "Artist a1"


def test_present_hit_similar_artist_is_not_refetched(monkeypatch):
    Store().save_tag_artists({"a1": _known_artist()})
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        track_key("Artist a1", "X"): {"similar": [], "tags": [], "fetched_at": "t0", "miss": False},
    }})
    Store().save_lastfm_artists({"version": 1, "artists": {
        "a1": {"name": "Artist a1", "similar": [{"artist": "X", "match": 0.1}],
               "fetched_at": "original", "miss": False},
    }})
    fm = FakeFmFull()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert fm.similar_calls == []
    assert Store().lastfm_artist_map()["a1"]["fetched_at"] == "original"


def test_present_miss_similar_artist_is_not_refetched(monkeypatch):
    """The piggyback has no `--refetch-misses` equivalent — a cached
    `miss: true` counts as known, same as a hit."""
    Store().save_tag_artists({"a1": _known_artist()})
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        track_key("Artist a1", "X"): {"similar": [], "tags": [], "fetched_at": "t0", "miss": False},
    }})
    Store().save_lastfm_artists({"version": 1, "artists": {
        "a1": {"name": "Artist a1", "similar": [], "fetched_at": "t0", "miss": True},
    }})
    fm = FakeFmFull()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1"]))

    assert fm.similar_calls == []


def test_similar_fetch_takes_first_credited_artists_not_first_unknown(monkeypatch):
    """Unlike the artist-tags step (which scans every credited artist and
    stops once NOW_FETCH_MAX_SIMILAR_ARTISTS UNKNOWNS are found), this step
    slices `track["artists"][:NOW_FETCH_MAX_SIMILAR_ARTISTS]` outright, THEN
    skips whichever of those are already known. a1 (already known) still
    occupies a slot in the first-3 slice, so a4 — unknown, but past the
    slice — is never reached at all."""
    Store().save_tag_artists({aid: _known_artist(f"Artist {aid}") for aid in ("a1", "a2", "a3", "a4")})
    for aid in ("a1", "a2", "a3", "a4"):
        Store().save_lastfm_tracks({"version": 1, "tracks": {}})
    Store().save_lastfm_artists({"version": 1, "artists": {
        "a1": {"name": "Artist a1", "similar": [], "fetched_at": "t0", "miss": False},
    }})
    fm = FakeFmFull(similar_by_name={
        f"Artist {aid}": [] for aid in ("a2", "a3", "a4")
    })
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1", "a2", "a3", "a4"]))

    assert NOW_FETCH_MAX_SIMILAR_ARTISTS == 3
    assert sorted(fm.similar_calls) == ["Artist a2", "Artist a3"]  # a4 outside the first-3 slice
    saved = Store().lastfm_artist_map()
    assert "a4" not in saved


def test_similar_fetch_failure_leaves_absent_and_never_raises(monkeypatch):
    Store().save_tag_artists({"a1": _known_artist()})
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        track_key("Artist a1", "X"): {"similar": [], "tags": [], "fetched_at": "t0", "miss": False},
    }})
    Store().save_lastfm_artists({"version": 1, "artists": {}})
    fm = FakeFmFull(similar_error_by_name={"Artist a1": LastFmError("Last.fm error 29: rate limited")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1"]))  # must not raise

    assert "a1" not in Store().lastfm_artist_map()


def test_a_partial_similar_batch_persists_what_succeeded_before_the_error(monkeypatch):
    """A failure on one credited artist must not lose an already-fetched
    sibling in the same batch."""
    Store().save_tag_artists({aid: _known_artist(f"Artist {aid}") for aid in ("a1", "a2")})
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        track_key("Artist a1", "X"): {"similar": [], "tags": [], "fetched_at": "t0", "miss": False},
    }})
    Store().save_lastfm_artists({"version": 1, "artists": {}})
    fm = FakeFmFull(
        similar_by_name={"Artist a1": [{"artist": "Y", "match": 0.2}]},
        similar_error_by_name={"Artist a2": LastFmError("Last.fm error 8: service offline")},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    _fetch_missing_now_tags(track_with_artists(["a1", "a2"]))

    saved = Store().lastfm_artist_map()
    assert "a1" in saved
    assert "a2" not in saved


def test_forced_poll_fetches_similar_artists_too(isolated_now, monkeypatch):
    fm = FakeFmFull(
        tags_by_name={n: [{"name": "rock", "count": 50}] for n in ("A", "B", "C", "D")},
        track_similar_by_key={("A", "X"): []},
        track_tags_by_key={("A", "X"): ["ballad"]},
        similar_by_name={n: [] for n in ("A", "B", "C")},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    r = isolated_now.get("/api/now?force=1")

    assert r.status_code == 200
    saved = Store().lastfm_artist_map()
    assert {"a1", "a2", "a3"} <= set(saved)
    assert "a4" not in saved


def test_forced_poll_similar_fetch_failure_still_returns_200(isolated_now, monkeypatch):
    fm = FakeFmFull(
        tags_by_name={n: [] for n in ("A", "B", "C", "D")},
        similar_error_by_name={n: LastFmError("Last.fm error 29: rate limited") for n in ("A", "B", "C")},
    )
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)

    r = isolated_now.get("/api/now?force=1")

    assert r.status_code == 200
    assert Store().lastfm_artist_map() == {}


def test_passive_poll_never_fetches_similar_artists(isolated_now, monkeypatch):
    fm = FakeFmFull()
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)
    r = isolated_now.get("/api/now")
    assert r.status_code == 200
    assert Store().lastfm_artist_map() == {}
    assert fm.similar_calls == []


def test_floor_is_shared_with_the_similar_artist_fetch(monkeypatch):
    """The single `NOW_FETCH_MIN_INTERVAL` floor covers this third step too —
    no third clock. Artist tags and the track record are seeded already-known
    so only the artist-similar step has anything to do this call: the
    earlier two steps share ONE try block ending at the first uncaught
    exception (see `_fetch_missing_now_tags`'s track-record step, which lets
    a `fetch_track` failure propagate to the function's own outer catch), so
    a track-step failure here would abort before ever reaching this step —
    this isolates the floor check to the step under test instead."""
    Store().save_tag_artists({"a1": _known_artist()})
    Store().save_lastfm_tracks({"version": 1, "tracks": {
        track_key("Artist a1", "X"): {"similar": [], "tags": [], "fetched_at": "t0", "miss": False},
    }})
    Store().save_lastfm_artists({"version": 1, "artists": {}})
    fm = FakeFmFull(similar_error_by_name={"Artist a1": LastFmError("Last.fm error 29: rate limited")})
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: fm)
    clock = {"t": 1_000.0}

    _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])
    assert fm.similar_calls == ["Artist a1"]

    clock["t"] += NOW_FETCH_MIN_INTERVAL - 1  # still inside the floor
    fm.similar_calls.clear()
    _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])

    assert fm.similar_calls == []  # not attempted again — the floor held

    clock["t"] += 2  # past the floor
    _fetch_missing_now_tags(track_with_artists(["a1"]), clock=lambda: clock["t"])

    assert fm.similar_calls == ["Artist a1"]  # retried once the floor elapses


# ---- _merge_save_lastfm_artists: same guarantees as _merge_save_lastfm_tracks -


def test_merge_save_lastfm_artists_never_overwrites_an_existing_entry():
    Store().save_lastfm_artists({"version": 1, "artists": {
        "a1": {"name": "Artist a1", "similar": [{"artist": "X", "match": 0.9}],
               "fetched_at": "t0", "miss": False},
    }})

    _merge_save_lastfm_artists({"a1": {"name": "Artist a1", "similar": [], "fetched_at": "t1",
                                        "miss": True}})

    saved = Store().lastfm_artist_map()["a1"]
    assert saved["fetched_at"] == "t0"
    assert saved["similar"] == [{"artist": "X", "match": 0.9}]


def test_merge_save_lastfm_artists_refuses_a_shrunk_reread(monkeypatch):
    seed = {
        f"a{i}": {"name": f"Artist {i}", "similar": [], "fetched_at": "t0", "miss": False}
        for i in range(1, 6)
    }
    appmod.store.save_lastfm_artists({"version": 1, "artists": seed})

    calls = {"n": 0}
    real_artist_map = appmod.store.lastfm_artist_map

    def flaky_artist_map():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_artist_map()  # baseline: all 5
        truncated = real_artist_map()
        return {k: truncated[k] for k in list(truncated)[:2]}  # re-read: only 2

    monkeypatch.setattr(appmod.store, "lastfm_artist_map", flaky_artist_map)

    _merge_save_lastfm_artists({"a6": {"name": "Artist 6", "similar": [], "fetched_at": "t1",
                                        "miss": False}})

    on_disk = json.loads((appmod.store.dir / "lastfm_artists.json").read_text())["artists"]
    assert on_disk == seed  # untouched — the guard tripped before any save
