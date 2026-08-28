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
