"""Pure naming rules — no network, no store.

The rules come from the 2026-08-20 design doc: Homes are ALL CAPS, Inputs
are [bracketed], an emoji prefix marks a derived playlist and exempts it
from both. `violations` only ever proposes renames for playlists the user
owns (editable) with a marked role.
"""

from sortify.naming import propose, violations


# ---- propose: home rule ----------------------------------------------------

def test_home_lowercase_proposes_upper():
    assert propose("beach vibes", "home") == "BEACH VIBES"

def test_home_already_caps_conforms():
    assert propose("BEACH VIBES", "home") is None

def test_home_mixed_case_proposes_upper():
    assert propose("Beach Vibes", "home") == "BEACH VIBES"

def test_home_non_alphabetic_name_is_not_flagged():
    # upper() is a no-op on "1234 · ???" — proposing an identical name
    # would be a rename that changes nothing.
    assert propose("1234 · ???", "home") is None

def test_home_emoji_prefix_is_exempt():
    # The emoji IS the subset marker; these are valid as-is.
    assert propose("🐾 quiet corner", "home") is None


# ---- propose: input rule ---------------------------------------------------

def test_input_unbracketed_proposes_brackets():
    assert propose("new finds", "input") == "[new finds]"

def test_input_already_bracketed_conforms():
    assert propose("[new finds]", "input") is None

def test_input_conformance_uses_configured_pattern_when_given():
    # config's input_name_pattern is the convention's source of truth.
    assert propose("<new finds>", "input", input_pattern=r"^<.+>$") is None
    assert propose("new finds", "input", input_pattern=r"^<.+>$") == "[new finds]"

def test_input_emoji_prefix_is_exempt():
    assert propose("🧸 inbox", "input") is None

def test_whitespace_is_stripped_before_judging():
    assert propose("  BEACH VIBES  ", "home") is None
    assert propose("  new finds  ", "input") == "[new finds]"


# ---- violations ------------------------------------------------------------

PLAYLISTS = [
    {"id": "h1", "name": "beach vibes", "editable": True},
    {"id": "h2", "name": "ALREADY FINE", "editable": True},
    {"id": "i1", "name": "new finds", "editable": True},
    {"id": "i2", "name": "[inbox]", "editable": True},
    {"id": "x1", "name": "someone elses", "editable": False},
    {"id": "u1", "name": "unmarked lowercase", "editable": True},
]

def test_violations_flags_only_marked_editable_nonconforming():
    rows = violations(PLAYLISTS, input_ids={"i1", "i2"}, home_ids={"h1", "h2", "x1"})
    assert rows == [
        {"playlist_id": "h1", "current": "beach vibes",
         "proposed": "BEACH VIBES", "rule": "homes are ALL CAPS"},
        {"playlist_id": "i1", "current": "new finds",
         "proposed": "[new finds]", "rule": "inputs are [bracketed]"},
    ]

def test_violations_input_role_wins_over_home():
    # Same precedence as /api/playlists: input first.
    both = [{"id": "b1", "name": "double marked", "editable": True}]
    rows = violations(both, input_ids={"b1"}, home_ids={"b1"})
    assert rows[0]["proposed"] == "[double marked]"

def test_violations_empty_when_everything_conforms():
    ok = [{"id": "h2", "name": "ALREADY FINE", "editable": True}]
    assert violations(ok, input_ids=set(), home_ids={"h2"}) == []


# ---- split_output_name: {source} · pile ------------------------------------

from sortify.naming import split_output_name


def test_split_output_name_composes_with_the_pile_separator():
    assert split_output_name("{teh bomb}", "jazz · funk · Bossa Nova") == \
        "{teh bomb} · jazz · funk · Bossa Nova"


def test_split_output_name_without_source_is_the_bare_pile_name():
    assert split_output_name(None, "untagged") == "untagged"
    assert split_output_name("  ", "untagged") == "untagged"


def test_split_output_name_truncates_the_pile_half_not_the_source():
    source = "S" * 40
    out = split_output_name(source, "p" * 90)
    assert len(out) <= 100
    assert out.startswith(source + " · ")
    assert out.endswith("…")


def test_split_output_name_degenerate_source_survives_alone():
    # A source name so long no pile half fits: the source is the grouping
    # key, so it wins and the pile half is dropped entirely.
    out = split_output_name("S" * 99, "jazz")
    assert out == "S" * 99


def test_split_output_titles_trip_no_naming_rules():
    # Design §3: split outputs are created unmarked, and the title shape must
    # not fullmatch the input pattern — so an unmarked output draws no
    # proposal at all. Marking one by hand puts it outside that contract: it
    # then gets whatever its role ordinarily gets, because nothing
    # special-cases the ` · ` shape once the user has claimed the playlist.
    playlists = [{"id": "X1", "name": "{teh bomb} · jazz · funk", "editable": True}]
    assert violations(playlists, input_ids=set(), home_ids=set()) == []
    # And the input pattern does not swallow it either:
    rows = violations(playlists, input_ids={"X1"}, home_ids=set())
    assert rows and rows[0]["proposed"] == "[{teh bomb} · jazz · funk]"
