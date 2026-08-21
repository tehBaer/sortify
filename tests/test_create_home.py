"""Creating home playlists from inside sortify.

Spec: docs/superpowers/specs/2026-08-21-create-home-playlists-design.md.
Everything here is zero-Spotify-call: fake transports and monkeypatched
clients only.
"""

from sortify.folders import creatable_home_name_problem

INPUT_PAT = r"^\[.+\]$"
EXCLUDES = [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"]


def problem(name):
    return creatable_home_name_problem(
        name, input_pattern=INPUT_PAT, exclude_patterns=EXCLUDES, exclude_emoji=True
    )


def test_ordinary_names_are_creatable():
    for name in ("Late Night", "HAZE 2", "Ærlig talt", "  padded  "):
        assert problem(name) is None, name


def test_input_shaped_names_are_refused_as_would_be_inputs():
    # The pattern union in _effective_input_ids beats the home_ids config
    # list, so "[Foo]" would become an input on the very next request.
    msg = problem("[Foo]")
    assert msg and "input" in msg


def test_home_excluded_shapes_are_refused():
    for name in ("{alle sanger}", "<motor>", "__start__", "🐾 subset", "🔈 haze"):
        assert problem(name), name


def test_empty_and_whitespace_names_are_refused():
    assert problem("")
    assert problem("   ")


def test_no_input_pattern_configured_skips_that_check():
    assert creatable_home_name_problem(
        "[Foo]", input_pattern=None, exclude_patterns=EXCLUDES, exclude_emoji=True
    ) is None
