"""Subset playlists: selections any song can join, and any song can leave.

Spec: docs/superpowers/specs/2026-08-28-subset-playlists-design.md.
Zero-Spotify-call throughout: fake transports and monkeypatched clients only.

A subset was originally identified by a `{braced}` name. That requirement is
gone: the chip that marks one only renders on rows the Playlists view draws
(200 of ~990), so a name rule made most of the library unmarkable in
practice. Marking is now the entire definition, which is why the tests below
are about the opt-in list and never about names.

Role exclusivity used to lean partly on that name rule — `{}` is also in
`home_name_exclude_patterns`, so a subset could not accidentally be a home.
It now rests entirely on `_effective_subset_ids` dropping ids that are inputs
or homes, which `test_inputs_and_homes_win_over_a_subset_mark` pins.
"""

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


def test_a_marked_playlist_resolves_whatever_it_is_called():
    """The `{}` requirement is gone: marking IS the definition of a subset.

    It was dropped because the chip that does the marking only renders on
    rows the Playlists view actually draws (200 of ~990), so a name rule
    made most of the library unmarkable in practice.
    """
    assert appmod._effective_subset_ids(
        _cfg(subset_ids=["plain"]), SUBSET_LISTING) == {"plain"}


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


def test_subsets_build_no_profile(wired):
    """The scoring machinery is gone: subsets are destinations you choose,
    never things that propose themselves. Building a profile is the ONLY
    reason marking one ever cost a Spotify call, so its absence is what makes
    marking free — this pins that it stays absent."""
    assert "subset_profiles" not in wired
    assert not hasattr(appmod, "_subset_matches")
    assert not hasattr(appmod, "SUBSET_WARM_BUDGET")


def test_subset_targets_are_exactly_the_marked_subsets(wired):
    """The picker offers what you marked — no more, no less.

    It used to offer every `{}`-named playlist whether marked or not, because
    the name convention gave "a subset" a meaning of its own. With the name
    requirement gone, marking is the whole definition, so the picker's reach
    and the opt-in list are the same set by construction. The alternative —
    offering all ~990 owned playlists — would ship the library in every
    /api/now poll to save a marking step.

    `wired` marks s1 and s2 only.
    """
    ids = {t["id"] for t in appmod._subset_targets_payload(wired)}
    assert ids == {"s1", "s2"}
    assert "plain" not in ids       # editable, but never marked
    assert "notmine" not in ids     # marked or not, not ours to edit


def test_marking_an_unbraced_playlist_puts_it_in_the_picker(wired, monkeypatch):
    """The regression the {} removal exists to prevent: a plainly-named
    playlist the user marked must be reachable, not silently filtered."""
    appmod.store.save_config(_cfg(subset_ids=["s1", "plain"], home_ids=["h1"]))
    ids = {t["id"] for t in appmod._subset_targets_payload(wired)}
    assert ids == {"s1", "plain"}


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


def test_act_guard_covers_exactly_what_the_picker_offers(client, monkeypatch):
    """The guard keys on the opt-in list, and so does the picker.

    It used to key on the `{}` name, because back then the picker reached
    every {}-named playlist and a marked-only guard would have missed most
    of them. With the name requirement gone, being marked IS what makes a
    playlist a subset — so `s2`, {}-shaped but never marked, is an ordinary
    destination and a move into it is an ordinary move. The two reaches are
    now the same set by construction and cannot drift apart.
    """
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    _seed_listing(SUBSET_LISTING)
    spent = []
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda *a, **k: spent.append(a) or "snap")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist",
                        lambda *a, **k: spent.append(a) or "snap")

    marked = client.post("/api/act", json={
        "action": "move", "uri": "spotify:track:z", "from_id": "inp", "to_id": "s1"})
    assert marked.status_code == 400
    assert "subset" in marked.json()["detail"].lower()
    assert spent == []          # refused before anything was spent

    unmarked = client.post("/api/act", json={
        "action": "move", "uri": "spotify:track:z", "from_id": "inp", "to_id": "s2"})
    assert unmarked.status_code == 200


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


def test_marking_many_subsets_is_free_and_never_refused(client):
    """Marking used to be refused past a call budget, because each new mark
    meant reading that playlist to score against. Nothing is read now, so a
    save of every subset at once must go through — the budget guard it would
    have tripped is deleted, not merely raised."""
    appmod.store.save_config(_cfg())
    _seed_listing(SUBSET_LISTING)
    every = [p["id"] for p in SUBSET_LISTING if p.get("editable")]
    res = client.post("/api/config", json={
        "input_ids": [], "home_ids": [], "home_hints": {}, "subset_ids": every})
    assert res.status_code == 200
    assert set(appmod.store.config()["subset_ids"]) == set(every)


# ---- /api/now's subset payload ---------------------------------------------

NOW_HOME_TRACK = {"uri": "spotify:track:z", "id": "z", "name": "Z", "type": "track",
                   "is_local": False, "duration_ms": 210_000,
                   "artists": [{"id": "ar1", "name": "Ar One"}],
                   "album": {"name": "Alb", "images": [{"url": "http://img/1"}]}}

NOW_RAW_CURRENTLY_PLAYING = {
    "item": NOW_HOME_TRACK, "is_playing": True, "progress_ms": 1_000,
    "context": None,
}


def test_now_route_offers_targets_and_suggests_nothing(monkeypatch):
    """The route must carry the picker's list and NOT propose subsets.

    Exercised through the real endpoint rather than a helper: the absence of
    a key is exactly the kind of thing a unit test of a deleted function
    cannot check, and re-adding scoring would most plausibly show up here
    first — as a `subsets` array quietly reappearing in the payload.
    """
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
        # Nothing proposes a subset any more.
        assert "subsets" not in data
        # But the marked ones are still reachable by hand.
        assert [t["id"] for t in data["subset_targets"]] == ["s1"]
    finally:
        appmod.store.save_cache(original_cache)
        appmod.store.save_config(original_config)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)
