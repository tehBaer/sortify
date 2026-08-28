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
