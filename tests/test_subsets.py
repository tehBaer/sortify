"""Subset playlists: {} selections that any song can join.

Spec: docs/superpowers/specs/2026-08-28-subset-playlists-design.md.
Zero-Spotify-call throughout: fake transports and monkeypatched clients only.
"""

from sortify.folders import home_name_excluded, is_subset_name

SUBSET_PAT = r"^\{.*\}$"
HOME_EXCLUDES = [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"]


def test_braced_names_are_subsets():
    for name in ("{solfest}", "{ny jazz}", "{teh bomb}", "{}", "  {tøft}  "):
        assert is_subset_name(name, SUBSET_PAT), name


def test_other_shapes_are_not_subsets():
    for name in ("[Hazy]", "<motor>", "__start__", "THROTTLE BACK PSY", "🐾 sub", ""):
        assert not is_subset_name(name, SUBSET_PAT), name


def test_a_subset_name_can_never_be_a_home():
    """The binding invariant (spec §1): the two rules must not drift apart and
    let one playlist be both a home and a subset."""
    for name in ("{solfest}", "{}", "{ny jazz}"):
        assert is_subset_name(name, SUBSET_PAT)
        assert home_name_excluded(name, HOME_EXCLUDES, emoji=True), name


from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

SUBSET_LISTING = [
    {"id": "s1", "name": "{solfest}", "owner": "me", "editable": True,
     "total": 22, "snapshot_id": "s-s1", "image": None, "description": ""},
    {"id": "s2", "name": "{ny jazz}", "owner": "me", "editable": True,
     "total": 40, "snapshot_id": "s-s2", "image": None, "description": ""},
    {"id": "notmine", "name": "{someone else}", "owner": "them", "editable": False,
     "total": 5, "snapshot_id": "s-nm", "image": None, "description": ""},
    {"id": "plain", "name": "Ordinary Home", "owner": "me", "editable": True,
     "total": 9, "snapshot_id": "s-pl", "image": None, "description": ""},
    {"id": "inp", "name": "[Buffer]", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-in", "image": None, "description": ""},
]


def _cfg(**over):
    base = {
        "input_ids": [], "home_ids": [], "subset_ids": [],
        "input_name_pattern": r"^\[.+\]$",
        "subset_name_pattern": r"^\{.*\}$",
    }
    base.update(over)
    return base


def test_marked_braced_playlists_resolve():
    cfg = _cfg(subset_ids=["s1", "s2"])
    assert appmod._effective_subset_ids(cfg, SUBSET_LISTING) == {"s1", "s2"}


def test_unmarked_braced_playlists_do_not_resolve():
    """Opting in gates suggestion; an unmarked {} playlist is still filable
    by hand, but must never build a profile or propose itself (spec §1)."""
    assert appmod._effective_subset_ids(_cfg(), SUBSET_LISTING) == set()


def test_a_marked_playlist_that_is_not_braced_is_dropped():
    assert appmod._effective_subset_ids(_cfg(subset_ids=["plain"]), SUBSET_LISTING) == set()


def test_a_marked_playlist_we_cannot_edit_is_dropped():
    assert appmod._effective_subset_ids(_cfg(subset_ids=["notmine"]), SUBSET_LISTING) == set()


def test_inputs_and_homes_win_over_a_subset_mark():
    """Stale config must never make one playlist two roles at once."""
    cfg = _cfg(subset_ids=["s1", "s2"], home_ids=["s2"], input_ids=["s1"])
    assert appmod._effective_subset_ids(cfg, SUBSET_LISTING) == set()


def test_an_id_missing_from_the_listing_is_dropped():
    assert appmod._effective_subset_ids(_cfg(subset_ids=["ghost"]), SUBSET_LISTING) == set()


def test_the_pattern_defaults_when_config_omits_it():
    cfg = {"input_ids": [], "home_ids": [], "subset_ids": ["s1"],
           "input_name_pattern": r"^\[.+\]$"}
    assert appmod._effective_subset_ids(cfg, SUBSET_LISTING) == {"s1"}


import pytest


@pytest.fixture
def wired(monkeypatch):
    """A profile state built from fakes — no Store writes, no HTTP."""
    listing = SUBSET_LISTING + [
        {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
         "total": 12, "snapshot_id": "s-h1", "image": None, "description": ""},
    ]
    tracks = {
        "h1": [{"uri": "spotify:track:a", "id": "a", "name": "A", "is_local": False,
                "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
                "added_at": "2026-01-01T00:00:00Z"}],
        "s1": [{"uri": "spotify:track:b", "id": "b", "name": "B", "is_local": False,
                "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
                "added_at": "2026-01-01T00:00:00Z"}],
        "s2": [{"uri": "spotify:track:c", "id": "c", "name": "C", "is_local": False,
                "type": "track", "artists": [{"id": "ar9", "name": "Ar Nine"}],
                "added_at": "2026-01-01T00:00:00Z"}],
    }
    appmod.store.save_config(_cfg(subset_ids=["s1", "s2"], home_ids=["h1"]))
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: listing)
    monkeypatch.setattr(appmod, "_cached_tracks", lambda pid, snap: tracks.get(pid, []))
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    return appmod._ensure_profiles(force=True)


PLAYING = {"uri": "spotify:track:z", "id": "z", "name": "Z", "is_local": False,
           "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}]}


def test_only_opted_in_subsets_get_profiles(wired):
    assert set(wired["subset_profiles"]) == {"s1", "s2"}


def test_a_subset_sharing_the_artist_matches(wired):
    matches = appmod._subset_matches(wired, PLAYING, appmod.store.tag_artists(),
                                     appmod.store.lastfm_track_map(), {})
    assert [m["playlist_id"] for m in matches] == ["s1"]
    assert matches[0]["name"] == "{solfest}"
    assert any("Ar One" in r for r in matches[0]["reasons"])


def test_matches_never_include_weak_guesses(wired):
    """suggest()'s sub-threshold tier exists to force a decision that must be
    made; a curated selection is optional, so a guess there is noise."""
    matches = appmod._subset_matches(wired, PLAYING, appmod.store.tag_artists(),
                                     appmod.store.lastfm_track_map(), {})
    assert all(not m.get("weak") for m in matches)
    assert "s2" not in [m["playlist_id"] for m in matches]


def test_at_most_two_matches(wired, monkeypatch):
    fake = [{"playlist_id": p, "pct": 90, "already": False, "reasons": []}
            for p in ("s1", "s2", "s3")]
    monkeypatch.setattr(appmod.sugg, "suggest", lambda *a, **k: fake)
    matches = appmod._subset_matches(wired, PLAYING, {}, {}, {})
    assert len(matches) == 2


def test_a_subset_the_track_is_already_in_is_flagged(wired):
    track = {**PLAYING, "uri": "spotify:track:b", "id": "b"}
    matches = appmod._subset_matches(wired, track, appmod.store.tag_artists(),
                                     appmod.store.lastfm_track_map(), {})
    assert any(m["already"] for m in matches if m["playlist_id"] == "s1")


def test_the_similar_artist_map_reaches_the_subset_scorer(wired, monkeypatch):
    """Subsets claim parity with homes; a signal homes get and subsets don't
    is a silent scoring difference no other test would show."""
    seen = {}
    def recorder(track, profiles, tag_artists, track_map=None, artist_map=None,
                 playlist_artists=None):
        seen["artist_map"] = artist_map
        return []
    monkeypatch.setattr(appmod.sugg, "suggest", recorder)
    appmod._subset_matches(wired, PLAYING, {}, {}, {"ar1": ["ar2"]})
    assert seen["artist_map"] == {"ar1": ["ar2"]}


def test_subset_targets_include_every_brace_playlist_not_just_opted_in(wired):
    """Filing by hand must never be gated by a list curated for suggestions.

    Both s1 and s2 are {}-shaped and editable, so both are reachable even
    though only the opted-in ones can suggest themselves.
    """
    ids = {t["id"] for t in appmod._subset_targets_payload(wired)}
    assert ids == {"s1", "s2"}
    assert "plain" not in ids       # not {}-shaped
    assert "notmine" not in ids     # {}-shaped but not ours to edit


from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(appmod.app, raise_server_exceptions=False)


def test_config_persists_subset_ids(client, monkeypatch):
    appmod.store.save_config(_cfg())
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: SUBSET_LISTING)
    res = client.post("/api/config", json={
        "input_ids": [], "home_ids": [], "home_hints": {}, "subset_ids": ["s1"]})
    assert res.status_code == 200
    assert appmod.store.config()["subset_ids"] == ["s1"]


def test_playlists_listing_reports_the_subset_role(client, monkeypatch):
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: SUBSET_LISTING)
    monkeypatch.setattr(appmod, "_split_summary", lambda pid, splits: None)
    rows = {p["id"]: p for p in client.get("/api/playlists").json()["playlists"]}
    assert rows["s1"]["role"] == "subset"
    assert rows["s2"]["role"] is None      # {}-shaped but not opted in


def _seed_listing(listing):
    """Put a playlist listing straight into the cache, the way a real listing
    fetch would leave it — the guard (I1/I2) reads this directly rather than
    calling sp.my_playlists(), so tests that exercise it must seed the cache,
    not just monkeypatch the client method."""
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 0.0, "items": listing}
    appmod.store.save_cache(cache)


def test_act_refuses_to_remove_from_an_input_when_filing_into_a_subset(client, monkeypatch):
    """A song put into a best-of has not been sorted — it must not leave the
    input it came from (spec §6). Structural, not a property of one caller."""
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    _seed_listing(SUBSET_LISTING)

    def boom(*a, **k):
        raise AssertionError("the guard must read the cached listing, not fetch")
    monkeypatch.setattr(appmod.sp, "my_playlists", boom)
    spent = []
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda *a, **k: spent.append(a) or "snap")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist",
                        lambda *a, **k: spent.append(a) or "snap")
    res = client.post("/api/act", json={
        "action": "move", "uri": "spotify:track:z", "from_id": "inp", "to_id": "s1"})
    assert res.status_code == 400
    assert "subset" in res.json()["detail"].lower()
    assert spent == []          # refused before anything was spent


def test_act_allows_adding_to_a_subset_without_a_from_id(client, monkeypatch):
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    _seed_listing(SUBSET_LISTING)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", lambda *a, **k: "snap")
    res = client.post("/api/act", json={
        "action": "move", "uri": "spotify:track:z", "from_id": None, "to_id": "s1"})
    assert res.status_code == 200


def test_act_guard_covers_a_non_opted_in_subset_too(client, monkeypatch):
    """I2: the guard used to key on _effective_subset_ids (opt-in only), so
    ~69 of 70 {}-named playlists were unguarded — the picker in spec §5
    reaches all of them, opted in or not. `s2` is {}-shaped but never
    opted in (subset_ids only has s1), and must still be guarded."""
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    _seed_listing(SUBSET_LISTING)
    spent = []
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda *a, **k: spent.append(a) or "snap")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist",
                        lambda *a, **k: spent.append(a) or "snap")
    res = client.post("/api/act", json={
        "action": "move", "uri": "spotify:track:z", "from_id": "inp", "to_id": "s2"})
    assert res.status_code == 400
    assert "subset" in res.json()["detail"].lower()
    assert spent == []


def test_act_guard_skips_rather_than_fetches_with_no_cached_listing(client, monkeypatch):
    """I1: sp.my_playlists() fetches (~21 paginated calls, ~60s stall) when
    cache["playlist_list"] is absent. The guard must read the cache directly
    and skip itself when there is nothing cached, never fetch to enforce."""
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    cache = appmod.store.cache()
    cache.pop("playlist_list", None)
    appmod.store.save_cache(cache)

    def boom(*a, **k):
        raise AssertionError("the guard must never fetch when nothing is cached")
    monkeypatch.setattr(appmod.sp, "my_playlists", boom)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", lambda *a, **k: "snap")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist", lambda *a, **k: "snap")
    res = client.post("/api/act", json={
        "action": "move", "uri": "spotify:track:z", "from_id": "inp", "to_id": "s1"})
    assert res.status_code == 200


# ---- C1: opting subsets in is bounded --------------------------------------

BIG_SUBSET = {"id": "s3", "name": "{teh bomb}", "owner": "me", "editable": True,
              "total": 3000, "snapshot_id": "s-s3", "image": None, "description": ""}


def _seed_cache(listing, playlists=None):
    """A cached listing plus a cached-tracks dict — both from a clean slate,
    so a leftover cache["playlists"] entry from another test in this session
    can never make a subset look warm when these tests need it cold."""
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 0.0, "items": listing}
    cache["playlists"] = playlists or {}
    appmod.store.save_cache(cache)


def test_config_refuses_a_save_above_the_subset_warm_budget(client):
    """s3 alone (3000 tracks -> ceil(3000/100) = 30 calls) is already over
    SUBSET_WARM_BUDGET (25) — the save must be refused before anything is
    persisted, naming the count and the cost rather than silently truncating
    the list."""
    appmod.store.save_config(_cfg())
    _seed_cache(SUBSET_LISTING + [BIG_SUBSET])
    res = client.post("/api/config", json={
        "input_ids": [], "home_ids": [], "home_hints": {}, "subset_ids": ["s3"]})
    assert res.status_code == 400
    assert "call" in res.json()["detail"].lower()
    assert appmod.store.config()["subset_ids"] == []   # nothing persisted


def test_config_allows_a_save_at_or_below_the_subset_warm_budget(client):
    """s1 (22 tracks) + s2 (40 tracks) cost 1 + 1 = 2 calls, comfortably
    under the 25-call budget."""
    appmod.store.save_config(_cfg())
    _seed_cache(SUBSET_LISTING)
    res = client.post("/api/config", json={
        "input_ids": [], "home_ids": [], "home_hints": {}, "subset_ids": ["s1", "s2"]})
    assert res.status_code == 200
    assert appmod.store.config()["subset_ids"] == ["s1", "s2"]


def test_config_only_counts_newly_marked_subsets_toward_the_budget(client):
    """A subset already marked (and so, in the real world, already warmed on
    an earlier save) must not count again just because it appears in the
    same save as a new one — only the newly-marked, still-uncached ones do."""
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    # s1 is "already marked" and (per this test) already cached — its
    # re-appearance in the save below must cost nothing. s3 stays uncached
    # and alone exceeds the budget, so the save must still be refused.
    _seed_cache(SUBSET_LISTING + [BIG_SUBSET],
                playlists={"s1": {"snapshot_id": "s-s1", "tracks": [], "fetched_at": 0.0}})
    res = client.post("/api/config", json={
        "input_ids": [], "home_ids": [], "home_hints": {}, "subset_ids": ["s1", "s3"]})
    assert res.status_code == 400
    assert appmod.store.config()["subset_ids"] == ["s1"]   # unchanged


def test_ensure_profiles_backstop_refuses_an_over_budget_config(monkeypatch):
    """The `/api/config` refusal is the front door; this is the backstop in
    `_ensure_profiles_locked` for any other path that could leave `subset_ids`
    over budget (a hand-edited config, a future caller) — it must raise
    rather than pay the cold-fetch cost."""
    appmod.store.save_config(_cfg(subset_ids=["s3"], home_ids=["nope-no-such-home"]))
    listing = SUBSET_LISTING + [BIG_SUBSET]
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: listing)
    cache = appmod.store.cache()
    cache["playlists"] = {}
    appmod.store.save_cache(cache)
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        appmod._ensure_profiles(force=True)
    assert exc.value.status_code == 400
    assert "call" in str(exc.value.detail).lower()


# ---- M1: /api/now actually carries subsets ---------------------------------

NOW_HOME_TRACK = {"uri": "spotify:track:z", "id": "z", "name": "Z", "type": "track",
                   "is_local": False, "duration_ms": 210_000,
                   "artists": [{"id": "ar1", "name": "Ar One"}],
                   "album": {"name": "Alb", "images": [{"url": "http://img/1"}]}}

NOW_RAW_CURRENTLY_PLAYING = {
    "item": NOW_HOME_TRACK, "is_playing": True, "progress_ms": 1_000,
    "context": None,
}


def test_now_route_carries_subset_matches(monkeypatch):
    """Every other subset test calls `_subset_matches` directly, which means a
    re-swapped `(state, track)` argument order in the /api/now handler itself
    would silently return [] with nothing to catch it (the exact defect fixed
    once already). This exercises the real route."""
    from fastapi.testclient import TestClient

    original_cache, original_config = appmod.store.cache(), appmod.store.config()
    try:
        listing = SUBSET_LISTING + [
            {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
             "total": 12, "snapshot_id": "s-h1", "image": None, "description": ""},
        ]
        cache = appmod.store.cache()
        cache["playlist_list"] = {"fetched_at": 0.0, "items": listing}
        cache["playlists"] = {
            "h1": {"snapshot_id": "s-h1", "fetched_at": 0.0, "tracks": [
                {"uri": "spotify:track:a", "id": "a", "name": "A", "is_local": False,
                 "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
                 "added_at": "2026-01-01T00:00:00Z"}]},
            "s1": {"snapshot_id": "s-s1", "fetched_at": 0.0, "tracks": [
                {"uri": "spotify:track:b", "id": "b", "name": "B", "is_local": False,
                 "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
                 "added_at": "2026-01-01T00:00:00Z"}]},
        }
        appmod.store.save_cache(cache)
        appmod.store.save_config(_cfg(subset_ids=["s1"], home_ids=["h1"]))
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)

        for name in ("currently_playing", "my_playlists", "playlist_tracks"):
            if name in vars(appmod.sp):
                monkeypatch.delattr(appmod.sp, name)

        def trap(method, path, background=False, **kwargs):
            return NOW_RAW_CURRENTLY_PLAYING
        monkeypatch.setattr(appmod.sp, "request", trap)

        c = TestClient(appmod.app)
        res = c.get("/api/now")
        assert res.status_code == 200
        data = res.json()
        assert data["playing"] is True
        assert "subsets" in data
        assert len(data["subsets"]) <= 2
        assert any(m["playlist_id"] == "s1" for m in data["subsets"])
        s1_match = next(m for m in data["subsets"] if m["playlist_id"] == "s1")
        assert "already" in s1_match
    finally:
        appmod.store.save_cache(original_cache)
        appmod.store.save_config(original_config)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)
