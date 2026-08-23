"""Input sets: which named/foldered group an input playlist belongs to."""
import pytest

from sortify.inputsets import DEFAULT_KEY, matched_ids, pattern_for, resolve_sets, set_of

SETS = [
    {"key": "buffer", "label": "buffer", "pattern": r"^\[.+\]$"},
    {"key": "other", "label": "other", "pattern": r"^<.*>$"},
    {"key": "the-bomb", "label": "the bomb", "path_segment": "THE BOMB"},
]


def test_resolve_sets_returns_configured():
    assert resolve_sets({"input_sets": SETS}) == SETS


def test_resolve_sets_falls_back_to_legacy_pattern():
    got = resolve_sets({"input_name_pattern": r"^\[.+\]$"})
    assert len(got) == 1
    assert got[0]["key"] == DEFAULT_KEY
    assert got[0]["pattern"] == r"^\[.+\]$"


def test_resolve_sets_empty_when_nothing_configured():
    assert resolve_sets({}) == []


def test_set_of_matches_pattern():
    assert set_of("[Hazy]", None, SETS) == "buffer"
    assert set_of("<ethno>", "ROOT / Hominin", SETS) == "other"


def test_set_of_matches_path_segment():
    assert set_of("jazz · funk · Bossa Nova", "THE BOMB", SETS) == "the-bomb"


def test_set_of_path_segment_matches_whole_segment_only():
    # "THE BOMB SQUAD" must not match the "THE BOMB" segment
    assert set_of("x", "THE BOMB SQUAD", SETS) is None


def test_set_of_returns_none_when_nothing_matches():
    assert set_of("missing liked", "GENERATED", SETS) is None
    assert set_of("SMELT AF", "ROOT / Hominin / ARCHIVED", SETS) is None


def test_set_of_first_match_wins():
    sets = [{"key": "a", "pattern": r"^<.*>$"}, {"key": "b", "pattern": r"^<e.*>$"}]
    assert set_of("<ethno>", None, sets) == "a"


def test_set_of_ignores_surrounding_whitespace():
    assert set_of("  [Hazy]  ", None, SETS) == "buffer"


def test_matched_ids_unions_every_set():
    playlists = [
        {"id": "b1", "name": "[Hazy]"},
        {"id": "o1", "name": "<ethno>"},
        {"id": "t1", "name": "jazz · funk"},
        {"id": "n1", "name": "missing liked"},
    ]
    folders = {"t1": {"path": "THE BOMB"}, "n1": {"path": "GENERATED"}}
    assert matched_ids(playlists, folders, {"input_sets": SETS}) == {"b1", "o1", "t1"}


def test_pattern_for_returns_none_for_folder_defined_set():
    assert pattern_for("buffer", SETS) == r"^\[.+\]$"
    assert pattern_for("the-bomb", SETS) is None
    assert pattern_for("nosuch", SETS) is None


# ---- per-set naming rules --------------------------------------------------

from sortify.naming import violations

PLAYLISTS = [
    {"id": "b1", "name": "Hazy", "editable": True},          # buffer, unbracketed
    {"id": "o1", "name": "<ethno>", "editable": True},       # other, correct
    {"id": "t1", "name": "jazz · funk", "editable": True},   # the-bomb, folder set
    {"id": "b2", "name": "[Ursus]", "editable": True},       # buffer, correct
]
FOLDERS = {"t1": {"path": "THE BOMB"}}
INPUT_IDS = {"b1", "o1", "t1", "b2"}


def _rows():
    return {r["playlist_id"]: r for r in violations(
        PLAYLISTS, INPUT_IDS, set(), sets=SETS, folders=FOLDERS)}


def test_naming_proposes_the_default_sets_wrapper_for_unattributable_names():
    # For pattern sets the NAME is the membership, so a misnamed playlist
    # cannot be attributed to "other" — it is judged by the default set.
    assert _rows()["b1"]["proposed"] == "[Hazy]"


def test_naming_passes_correctly_wrapped_other_set_member():
    assert "o1" not in _rows()


def test_propose_derives_the_wrapper_from_the_sets_pattern():
    from sortify.naming import propose
    assert propose("ethno", "input", r"^<.*>$") == "<ethno>"
    assert propose("Hazy", "input", r"^\[.+\]$") == "[Hazy]"


def test_naming_skips_folder_defined_set():
    # THE BOMB names are descriptive on purpose — never flag them.
    assert "t1" not in _rows()


def test_naming_passes_correctly_named_input():
    assert "b2" not in _rows()


def test_naming_rule_text_names_the_set_and_avoids_raw_regex():
    rule = _rows()["b1"]["rule"]
    assert "buffer" in rule and "[wrapped]" in rule and "^" not in rule


def test_naming_without_sets_keeps_legacy_behaviour():
    rows = violations([{"id": "x", "name": "Hazy", "editable": True}], {"x"}, set())
    assert rows[0]["proposed"] == "[Hazy]"
