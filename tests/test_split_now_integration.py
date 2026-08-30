"""The reload gap, closed: after a page reload, the client's pointer to an
active sitting used to live only in memory, so the Now view had no way to
tell a sitting's disposable playlist apart from any other. Filing a track
then went through /api/act instead of /api/split/.../decide — a Spotify call
spent, nothing written to `decided`, and `pick_sitting` would serve the exact
same track again in a later sitting: paying to decide it twice.

`/api/now` now reports which split (if any) owns the currently-playing
context, from a local `store.splits()` read piggybacked on the poll that
already exists — zero Spotify calls, and it survives reload by construction
since there is no client state to lose. `/api/playlists` similarly grows a
`split` summary (pile count, remaining) per playlist, so one already split
doesn't look untouched in the picker — also a local read.
"""

import pytest

import sortify.app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)
from sortify.store import Store


def _splits_payload(pile_id="p1", uris=("spotify:track:a", "spotify:track:b", "spotify:track:c"),
                     sitting_playlist_id="sit1", decided=None):
    return {"version": 1, "splits": {"PL_NOW": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None, "params": {},
        "piles": [{"id": pile_id, "name": "dream pop", "tags": ["dream pop"],
                   "uris": list(uris)}],
        "decided": decided or {},
        "active_sitting": {"playlist_id": sitting_playlist_id, "pile_id": pile_id,
                            "uris": list(uris), "started_at": "2026-08-17T10:05:00Z",
                            "claim": "c1"},
    }}}


@pytest.fixture
def splits_store():
    """splits.json is one file shared for the whole test session (see
    conftest.py) — same discipline as test_split_decisions.py: capture
    whatever was there, restore it after, regardless of pass/fail."""
    original = Store().splits()
    try:
        yield
    finally:
        Store().save_splits(original)


# ---- _sitting_for_context ----------------------------------------------------


def test_no_context_means_no_sitting(splits_store):
    Store().save_splits(_splits_payload())
    assert appmod._sitting_for_context(None) is None


def test_a_context_that_matches_no_sitting_is_not_one(splits_store):
    Store().save_splits(_splits_payload())
    assert appmod._sitting_for_context("some-other-playlist") is None


def test_the_sitting_playlist_resolves_to_its_split_and_pile(splits_store):
    Store().save_splits(_splits_payload())

    result = appmod._sitting_for_context("sit1")

    assert result["split_id"] == "PL_NOW"
    assert result["pile_id"] == "p1"
    assert result["pile_name"] == "dream pop"
    assert result["uris"] == ["spotify:track:a", "spotify:track:b", "spotify:track:c"]


def test_decided_is_restricted_to_this_sittings_own_uris(splits_store):
    """A uri decided under some other pile/sitting must not leak into what
    this sitting's card thinks is already decided."""
    decided = {
        "spotify:track:a": {"action": "keep", "to_id": "home1", "at": "t"},
        "spotify:track:zzz": {"action": "reject", "at": "t"},  # not in this sitting
    }
    Store().save_splits(_splits_payload(decided=decided))

    result = appmod._sitting_for_context("sit1")

    assert result["decided"] == {"spotify:track:a": decided["spotify:track:a"]}


def test_no_active_sitting_at_all_is_not_one(splits_store):
    payload = _splits_payload()
    payload["splits"]["PL_NOW"]["active_sitting"] = None
    Store().save_splits(payload)

    assert appmod._sitting_for_context("sit1") is None


# ---- _split_summary -----------------------------------------------------------


def test_an_unsplit_playlist_has_no_summary(splits_store):
    Store().save_splits(_splits_payload())
    assert appmod._split_summary("some-other-playlist") is None


def test_split_summary_counts_piles_and_what_remains(splits_store):
    decided = {"spotify:track:a": {"action": "reject", "at": "t"}}
    Store().save_splits(_splits_payload(decided=decided))

    summary = appmod._split_summary("PL_NOW")

    assert summary == {"piles": 1, "remaining": 2}  # 3 uris, 1 decided


# ---- wired into the endpoints --------------------------------------------------

MINIMAL_STATE = {"playlists": [], "input_ids": set(), "tag_artists": {},
                  "profiles": {}, "homes": [], "inputs": []}


def _now_payload(uri="spotify:track:a", ctx_id="sit1"):
    return {
        "track": {"uri": uri, "id": "a", "name": "A Song", "type": "track",
                  "is_local": False, "duration_ms": 200000, "artists": [],
                  "album": None, "image": None},
        "is_playing": True, "progress_ms": 1000, "context_playlist_id": ctx_id,
    }


@pytest.fixture
def now_client(monkeypatch):
    """Bypasses the real profile-building machinery (Spotify/home-playlist
    reads) the same way test_playlist_cache.py does — this is testing the
    `sitting` field, not profile suggestion."""
    monkeypatch.setattr(appmod, "_ensure_profiles", lambda force=False: MINIMAL_STATE)
    monkeypatch.setattr(appmod.sugg, "suggest", lambda *a, **k: [])
    appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)


def test_now_reports_the_sitting_for_a_sitting_playlist(splits_store, now_client, monkeypatch):
    Store().save_splits(_splits_payload())
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: _now_payload())

    resp = appmod.now_playing()

    assert resp["sitting"]["split_id"] == "PL_NOW"
    assert resp["sitting"]["pile_name"] == "dream pop"


def test_now_reports_no_sitting_for_an_ordinary_playlist(splits_store, now_client, monkeypatch):
    Store().save_splits(_splits_payload())
    monkeypatch.setattr(appmod.sp, "currently_playing",
                        lambda: _now_payload(ctx_id="some-ordinary-playlist"))

    resp = appmod.now_playing()

    assert resp["sitting"] is None


# ---- suggestions survive a bad tags.json -----------------------------------

NOW_TAGS_LISTING = [
    {"id": "home1", "name": "Home One", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "snap-home1", "image": None},
]


def test_now_survives_a_wrong_version_tags_json(monkeypatch):
    """A stale/mismatched tags.json must degrade suggestions to
    artist-overlap-only, not break /api/now — a polling endpoint hit every
    PROFILE_TTL, unlike the user-triggered split flow where the same
    mismatch should fail loud (`_tag_artists_checked`). This simulates the
    historical v1 shape (tags stored as plain strings, not
    `{"name", "count"}` dicts) that used to crash `clean_tags` deep inside
    `split_tracks`; `suggest._cleaned_tags` must swallow it instead."""
    s = Store()
    original_cache, original_config, original_tags = s.cache(), s.config(), s.tags()

    cache = s.cache()
    cache["playlist_list"] = {"fetched_at": 0.0, "items": NOW_TAGS_LISTING}
    cache["playlists"]["home1"] = {
        "snapshot_id": "snap-home1",
        "tracks": [{"uri": "spotify:track:h1", "type": "track", "is_local": False,
                    "id": "h1", "artists": [{"id": "a1", "name": "Artist One"}]}],
        "fetched_at": 0.0,
    }
    s.save_cache(cache)
    s.save_config({**original_config, "input_ids": [], "home_ids": []})
    s.save_tags({"version": 1, "artists": {
        "a1": {"name": "Artist One", "tags": ["dream pop", "seen live"], "miss": False},
    }})
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0

    # Same singleton-leak defence as test_now_polling.py's route_client: an
    # earlier test's monkeypatch.setattr on this module-level `sp` can leave
    # a stand-in behind even after its own teardown.
    for name in ("currently_playing", "my_playlists", "playlist_tracks"):
        if name in vars(appmod.sp):
            monkeypatch.delattr(appmod.sp, name)

    new_track = {"uri": "spotify:track:new", "type": "track", "is_local": False,
                 "id": "new", "artists": [{"id": "a1", "name": "Artist One"}]}
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: {
        "track": new_track, "is_playing": True, "progress_ms": 1000,
        "context_playlist_id": None,
    })
    appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)

    try:
        resp = appmod.now_playing()
    finally:
        s.save_cache(original_cache)
        s.save_config(original_config)
        s.save_tags(original_tags)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0

    assert resp["playing"] is True
    suggestions = resp["suggestions"]
    assert suggestions and suggestions[0]["playlist_id"] == "home1"
    assert all("dream pop" not in r for r in suggestions[0]["reasons"])
    assert any("Artist One" in r for r in suggestions[0]["reasons"])


def test_playlists_endpoint_carries_the_split_summary(splits_store, monkeypatch):
    Store().save_splits(
        _splits_payload(decided={"spotify:track:a": {"action": "reject", "at": "t"}}))
    monkeypatch.setattr(
        appmod.sp, "my_playlists",
        lambda refresh=False: [
            {"id": "PL_NOW", "name": "Big One", "owner": "me", "editable": True,
             "total": 3, "snapshot_id": "s1", "image": None},
        ],
    )

    out = {p["id"]: p for p in appmod.playlists()["playlists"]}

    assert out["PL_NOW"]["split"] == {"piles": 1, "remaining": 2}
    assert out[appmod.LIKED_ID]["split"] is None


def test_playlists_reads_splits_json_once_not_per_playlist(splits_store, monkeypatch):
    """Regression pin: `_split_summary` was originally called inside the
    per-playlist loop, each call doing its own `store.splits()` — a full
    disk read + JSON parse per playlist. Against a real ~1000-playlist
    account with a splits.json grown to hundreds of KB, that turned every
    /api/playlists response (nav-lists, btn-back, btn-split-back, and every
    post-refresh reload) into a ~1.4s stall. `playlists()` must read
    splits.json at most once for the whole listing, however many playlists
    it returns."""
    Store().save_splits(_splits_payload())
    many_playlists = [
        {"id": f"PLX{i}", "name": f"list {i}", "owner": "me", "editable": True,
         "total": 3, "snapshot_id": f"s{i}", "image": None}
        for i in range(50)
    ]
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: many_playlists)

    calls = []
    real_splits = Store.splits

    def counting_splits(self):
        calls.append(1)
        return real_splits(self)

    monkeypatch.setattr(Store, "splits", counting_splits)

    out = appmod.playlists()

    assert len(out["playlists"]) == 51  # 50 + Liked Songs
    assert len(calls) == 1, f"store.splits() called {len(calls)} times for 51 playlists"


def test_now_suggestions_use_cooc_evidence_from_cached_playlists(monkeypatch):
    """The co-occurrence corpus is every cached playlist, read straight from
    cache.json by _ensure_profiles — a playing track whose artist is tagless
    and unknown to every home can still surface a weak guess because the
    user already files that artist next to a home artist in some other
    cached playlist (here: an inbox playlist that is neither home nor listed
    input)."""
    s = Store()
    original_cache, original_config, original_tags = s.cache(), s.config(), s.tags()

    cache = s.cache()
    cache["playlist_list"] = {"fetched_at": 0.0, "items": NOW_TAGS_LISTING}
    cache["playlists"]["home1"] = {
        "snapshot_id": "snap-home1",
        "tracks": [{"uri": "spotify:track:h1", "type": "track", "is_local": False,
                    "id": "h1", "artists": [{"id": "ha", "name": "Home Artist"}]}],
        "fetched_at": 0.0,
    }
    cache["playlists"]["inbox"] = {
        "snapshot_id": "snap-inbox",
        "tracks": [
            {"uri": "spotify:track:i1", "type": "track", "is_local": False,
             "id": "i1", "artists": [{"id": "seed-a", "name": "Seed Artist"}]},
            {"uri": "spotify:track:i2", "type": "track", "is_local": False,
             "id": "i2", "artists": [{"id": "ha", "name": "Home Artist"}]},
        ],
        "fetched_at": 0.0,
    }
    s.save_cache(cache)
    s.save_config({**original_config, "input_ids": [], "home_ids": []})
    s.save_tags({"version": 2, "artists": {}})
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0

    for name in ("currently_playing", "my_playlists", "playlist_tracks"):
        if name in vars(appmod.sp):
            monkeypatch.delattr(appmod.sp, name)

    new_track = {"uri": "spotify:track:new", "type": "track", "is_local": False,
                 "id": "new", "artists": [{"id": "seed-a", "name": "Seed Artist"}]}
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: {
        "track": new_track, "is_playing": True, "progress_ms": 1000,
        "context_playlist_id": None,
    })
    appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)

    try:
        resp = appmod.now_playing()
    finally:
        s.save_cache(original_cache)
        s.save_config(original_config)
        s.save_tags(original_tags)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0

    suggestions = resp["suggestions"]
    assert suggestions and suggestions[0]["playlist_id"] == "home1"
    assert suggestions[0]["weak"] is True
    assert "next to Home Artist" in suggestions[0]["reasons"]
