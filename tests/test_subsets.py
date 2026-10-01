"""Subset playlists: selections any song can join, and any song can leave.

Spec: docs/superpowers/specs/2026-08-28-subset-playlists-design.md, revised
2026-10-01 (docs/superpowers/specs/ emoji-subsets design).
Zero-Spotify-call throughout: fake transports and monkeypatched clients only.

A subset is its NAME: a playlist of ours that starts with an emoji other than
the archive marker (playlist_roles.py, shared with spotify-autoqueuer). It was
an opt-in id list (`subset_ids`) from 2026-08-28 until 2026-10-01, and a
`{braced}` name before that; config's `subset_ids` is no longer read, which
`test_subset_ids_in_config_is_ignored` pins.

Role exclusivity rests on `_effective_subset_ids` handing role_of the input
and home flags, so an input or home that happens to be emoji-led stays what
it is (`test_inputs_and_homes_win_over_a_subset_name`). An archived name
(🗄️) is no role at all — not a subset, not an input.
"""

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

def _pl(pid, name, editable=True, total=10):
    return {"id": pid, "name": name, "owner": "me" if editable else "them",
            "editable": editable, "total": total, "snapshot_id": f"s-{pid}",
            "image": None, "description": ""}


SUBSET_LISTING = [
    _pl("s1", "\U0001F43E solfest", total=22),
    _pl("s2", "\U0001F43E ny jazz", total=40),
    _pl("notmine", "\U0001F43E someone else", editable=False, total=5),
    _pl("plain", "Ordinary Home", total=9),
    _pl("inp", "[Buffer]", total=3),
    _pl("arch", "\U0001F5C4\uFE0F \U0001F43E old best", total=4),
]


def _cfg(**over):
    base = {
        "input_ids": [], "home_ids": [],
        "input_name_pattern": r"^\[.+\]$",
    }
    base.update(over)
    return base


def test_an_emoji_led_playlist_of_ours_is_a_subset():
    assert appmod._effective_subset_ids(_cfg(), SUBSET_LISTING) == {"s1", "s2"}


def test_archived_is_no_subset_and_no_input():
    listing = [_pl("a1", "\U0001F5C4\uFE0F [Old inbox]"),
               _pl("a2", "\U0001F5C4\uFE0F \U0001F43E old best")]
    cfg = _cfg(input_ids=["a1"])
    assert appmod._effective_subset_ids(cfg, listing) == set()
    assert appmod._effective_input_ids(cfg, listing) == set()


def test_not_ours_is_no_subset():
    listing = [_pl("x1", "\U0001F171\uFE0Farvakt", editable=False)]
    assert appmod._effective_subset_ids(_cfg(), listing) == set()


def test_subset_ids_in_config_is_ignored():
    listing = [_pl("p1", "plain")]
    assert appmod._effective_subset_ids(_cfg(subset_ids=["p1"]), listing) == set()


def test_inputs_and_homes_win_over_a_subset_name():
    """An emoji-led home or input must never also be a subset."""
    cfg = _cfg(home_ids=["s2"], input_ids=["s1"])
    assert appmod._effective_subset_ids(cfg, SUBSET_LISTING) == set()


def test_an_id_missing_from_the_listing_is_dropped():
    assert appmod._effective_subset_ids(_cfg(home_ids=["ghost"]), []) == set()


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
    appmod.store.save_config(_cfg(home_ids=["h1"]))
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


def test_subset_targets_are_the_emoji_led_playlists_of_ours(wired):
    """The picker offers every playlist of ours whose name starts with an
    emoji (archive marker excepted) — the same set the act guard enforces,
    because both go through `_effective_subset_ids`."""
    ids = {t["id"] for t in appmod._subset_targets_payload(wired)}
    assert ids == {"s1", "s2"}
    assert "plain" not in ids       # editable, but no emoji
    assert "notmine" not in ids     # emoji-led, but not ours to edit
    assert "arch" not in ids        # archived is no role


def test_renaming_a_playlist_puts_it_in_the_picker(wired):
    """The name is the whole definition, so a state that lists an emoji-led
    playlist offers it, whatever else is in it."""
    state = {"playlists": wired["playlists"] + [_pl("new", "\U0001F984 unicorns")]}
    ids = {t["id"] for t in appmod._subset_targets_payload(state)}
    assert ids == {"s1", "s2", "new"}


from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(appmod.app, raise_server_exceptions=False)


def test_playlists_view_marks_role_archived_and_eligibility(client, monkeypatch):
    listing = SUBSET_LISTING + [_pl("h1", "Home One")]
    appmod.store.save_config(_cfg(home_ids=["h1"]))
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: listing)
    monkeypatch.setattr(appmod, "_split_summary", lambda pid, splits: None)
    rows = {p["id"]: p for p in client.get("/api/playlists").json()["playlists"]}
    assert (rows["s1"]["role"], rows["s1"]["subset_eligible"]) == ("subset", True)
    assert (rows["arch"]["role"], rows["arch"]["archived"]) == (None, True)
    assert rows["s1"]["archived"] is False
    assert rows["plain"]["role"] is None and rows["plain"]["subset_eligible"] is True
    assert rows["notmine"]["role"] is None and rows["notmine"]["subset_eligible"] is False
    assert rows["inp"]["role"] == "input" and rows["inp"]["subset_eligible"] is False
    assert rows["h1"]["role"] == "home" and rows["h1"]["subset_eligible"] is False


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
    appmod.store.save_config(_cfg())
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
    appmod.store.save_config(_cfg())
    _seed_listing(SUBSET_LISTING)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", lambda *a, **k: "snap")
    res = client.post("/api/act", json={
        "action": "move", "uri": "spotify:track:z", "from_id": None, "to_id": "s1"})
    assert res.status_code == 200


def test_act_guard_covers_exactly_what_the_picker_offers(client, monkeypatch):
    """The guard keys on the name rule, and so does the picker.

    `plain` is an ordinary destination and a move into it is an ordinary
    move; `s1` is emoji-led, so a move into it out of an input is refused.
    The two reaches are the same set by construction and cannot drift apart.
    """
    appmod.store.save_config(_cfg())
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
        "action": "move", "uri": "spotify:track:z", "from_id": "inp", "to_id": "plain"})
    assert unmarked.status_code == 200


def test_act_guard_skips_rather_than_fetches_with_no_cached_listing(client, monkeypatch):
    """I1: sp.my_playlists() fetches (~21 paginated calls, ~60s stall) when
    cache["playlist_list"] is absent. The guard must read the cache directly
    and skip itself when there is nothing cached, never fetch to enforce."""
    appmod.store.save_config(_cfg())
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
        listing = SUBSET_LISTING + [_pl("h1", "Home One", total=12)]
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
        appmod.store.save_config(_cfg(home_ids=["h1"]))
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
        # But the emoji-led ones are still reachable by hand.
        assert {t["id"] for t in data["subset_targets"]} == {"s1", "s2"}
    finally:
        appmod.store.save_cache(original_cache)
        appmod.store.save_config(original_config)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)
