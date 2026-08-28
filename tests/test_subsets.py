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
