import sys

import pytest

from sortify.foldermove import (
    ResolveError, _check_leaf_unique, resolve_folder, resolve_playlist,
)

TREE = {
    "type": "folder",
    "children": [
        {"name": "ROOT", "type": "folder",
         "uri": "spotify:user:u:folder:aa", "children": [
            {"name": "Y'no", "type": "folder",
             "uri": "spotify:user:u:folder:bb", "children": [
                {"type": "playlist", "uri": "spotify:playlist:pl_lite"},
                # A second folder sharing a leaf name with one below —
                # covers Finding 1's ambiguous-leaf refusal without
                # disturbing the "y'no" unique-leaf test.
                {"name": "Vault", "type": "folder",
                 "uri": "spotify:user:u:folder:cc", "children": []},
            ]},
            {"name": "Vault", "type": "folder",
             "uri": "spotify:user:u:folder:dd", "children": []},
            {"name": "Attic", "type": "folder",
             "uri": "spotify:user:u:folder:ee", "children": []},
            {"type": "playlist", "uri": "spotify:playlist:pl_haze"},
        ]},
        {"type": "playlist", "uri": "spotify:playlist:pl_loose"},
    ],
}

ITEMS = [
    {"id": "pl_lite", "name": "LITE", "editable": True},
    {"id": "pl_haze", "name": "HAZE", "editable": True},
    {"id": "pl_loose", "name": "Loose One", "editable": True},
    {"id": "pl_dupe1", "name": "Dupe", "editable": True},
    {"id": "pl_dupe2", "name": "Dupe", "editable": True},
    {"id": "pl_other", "name": "Lite snacks", "editable": True},
]


def mapping():
    from sortify.folders import extract_folder_map
    return extract_folder_map(TREE)


def test_resolve_playlist_exact_name_case_insensitive():
    pid, name, path = resolve_playlist(ITEMS, mapping(), "lite")
    assert (pid, name, path) == ("pl_lite", "LITE", "ROOT / Y'no")


def test_resolve_playlist_top_level_has_no_path():
    pid, name, path = resolve_playlist(ITEMS, mapping(), "Loose One")
    assert (pid, name, path) == ("pl_loose", "Loose One", None)


def test_resolve_playlist_unique_substring_falls_back():
    pid, name, _ = resolve_playlist(ITEMS, mapping(), "loose")
    assert pid == "pl_loose"


def test_resolve_playlist_duplicate_names_refuse():
    with pytest.raises(ResolveError) as e:
        resolve_playlist(ITEMS, mapping(), "Dupe")
    assert "pl_dupe1" in str(e.value) and "pl_dupe2" in str(e.value)


def test_resolve_playlist_ambiguous_substring_lists_candidates():
    with pytest.raises(ResolveError) as e:
        resolve_playlist(ITEMS, mapping(), "lit")  # LITE and "Lite snacks"
    assert "LITE" in str(e.value) and "Lite snacks" in str(e.value)


def test_resolve_playlist_unknown_name():
    with pytest.raises(ResolveError):
        resolve_playlist(ITEMS, mapping(), "no such thing")


def test_resolve_folder_exact_path():
    assert resolve_folder(TREE, "ROOT / Y'no") == "ROOT / Y'no"


def test_resolve_folder_by_unique_leaf_name():
    assert resolve_folder(TREE, "y'no") == "ROOT / Y'no"


def test_resolve_folder_unknown():
    with pytest.raises(ResolveError):
        resolve_folder(TREE, "NOPE / NOWHERE")


from sortify.foldermove import MovePlan, plan_move, verify_move


def test_plan_move_into_folder():
    p = plan_move(ITEMS, TREE, "HAZE", "Attic")
    assert p == MovePlan("pl_haze", "HAZE", "ROOT", "ROOT / Attic")


def test_plan_move_out_to_top_level():
    p = plan_move(ITEMS, TREE, "LITE", None)
    assert p == MovePlan("pl_lite", "LITE", "ROOT / Y'no", None)


def test_plan_move_noop_refused():
    # Checked before the search-ambiguity guard: being already there is a
    # better answer than "that folder cannot be targeted".
    with pytest.raises(ResolveError) as e:
        plan_move(ITEMS, TREE, "LITE", "Y'no")
    assert "already" in str(e.value)


def test_plan_move_out_when_already_loose_refused():
    with pytest.raises(ResolveError):
        plan_move(ITEMS, TREE, "Loose One", None)


def test_plan_move_unique_leaf_destination_still_resolves():
    # "Attic" is uniquely named and holds no folders — the one shape the
    # client's search returns exactly one row for.
    p = plan_move(ITEMS, TREE, "HAZE", "Attic")
    assert p.to_path == "ROOT / Attic"


def test_plan_move_into_a_folder_with_subfolders_is_refused():
    # "Y'no" holds "Vault", whose row would carry "Y'no" on its parent line.
    with pytest.raises(ResolveError) as e:
        plan_move(ITEMS, TREE, "HAZE", "Y'no")
    assert "Vault" in str(e.value)


def test_plan_move_ambiguous_leaf_destination_refused():
    # Two folders are both named "Vault" (ROOT / Vault and
    # ROOT / Y'no / Vault) — the desktop client's folder search can only
    # match on the leaf name, so sortify must refuse rather than gamble.
    with pytest.raises(ResolveError) as e:
        plan_move(ITEMS, TREE, "HAZE", "ROOT / Vault")
    msg = str(e.value)
    assert "Vault" in msg
    assert "ROOT / Y'no / Vault" in msg


# The client's folder search matches a folder's NAME and its ANCESTRY, and
# draws each hit as two lines — the name, and the parent path under it. So
# typing a leaf can put another folder's row first, and the mover clicks the
# first match it sees. Live on 2026-09-20: filing into "input" typed "input",
# matched the parent line of "input / inputlister / matra-esque", and moved
# the playlist in there. Every refusal below is a move that would otherwise
# land somewhere nobody asked for.
SEARCH_TREE = {
    "type": "folder",
    "children": [
        {"name": "input", "type": "folder", "uri": "spotify:user:u:folder:i1",
         "children": [
            {"name": "inputlister", "type": "folder", "uri": "spotify:user:u:folder:i2",
             "children": [
                {"name": "matra-esque", "type": "folder",
                 "uri": "spotify:user:u:folder:i3", "children": []},
             ]},
         ]},
        {"name": "solo", "type": "folder", "uri": "spotify:user:u:folder:s1",
         "children": []},
    ],
}


def test_a_leaf_that_is_part_of_another_folders_name_is_refused():
    with pytest.raises(ResolveError) as e:
        _check_leaf_unique(SEARCH_TREE, "input")
    msg = str(e.value)
    assert "inputlister" in msg


def test_a_folder_holding_other_folders_is_refused():
    # Searching "inputlister" also returns matra-esque, whose row shows
    # "input · inputlister" as its parent — a candidate the mover can hit.
    with pytest.raises(ResolveError) as e:
        _check_leaf_unique(SEARCH_TREE, "input / inputlister")
    assert "matra-esque" in str(e.value)


def test_a_uniquely_named_folder_with_no_subfolders_is_still_targetable():
    _check_leaf_unique(SEARCH_TREE, "solo")
    _check_leaf_unique(SEARCH_TREE, "input / inputlister / matra-esque")


def test_the_refusal_says_what_to_do_instead():
    with pytest.raises(ResolveError) as e:
        _check_leaf_unique(SEARCH_TREE, "input")
    assert "by hand" in str(e.value)


def test_verify_move_checks_tree_truth():
    assert verify_move(TREE, "pl_lite", "ROOT / Y'no") is True
    assert verify_move(TREE, "pl_lite", None) is False
    assert verify_move(TREE, "pl_loose", None) is True
    assert verify_move(TREE, "pl_loose", "ROOT") is False


def test_verify_move_none_true_for_top_level_playlist():
    assert verify_move(TREE, "pl_loose", None) is True


def test_verify_move_none_false_for_id_absent_from_tree():
    # A path of None is what BOTH "top level" and "not in the tree at
    # all" look like from extract_folder_map alone — verify_move must
    # not treat a vanished playlist as a successful move-to-top-level.
    assert verify_move(TREE, "pl_never_existed", None) is False


from sortify.foldermove import execute_move


class FakeSession:
    entered = 0
    def __enter__(self):
        FakeSession.entered += 1
        return self
    def __exit__(self, *a):
        return False


def _tree_with(pid_path):
    """Minimal tree putting pl_haze at the given path (or top level)."""
    node = {"type": "playlist", "uri": "spotify:playlist:pl_haze"}
    if pid_path is None:
        return {"type": "folder", "children": [node]}
    return {"type": "folder", "children": [
        {"name": pid_path, "type": "folder", "uri": "spotify:user:u:folder:ff",
         "children": [node]}]}


def test_execute_move_verifies_against_fresh_tree():
    plan = MovePlan("pl_haze", "HAZE", None, "DEST")
    moved = []
    execute_move(
        plan, session_cls=FakeSession,
        mover=lambda s, p: moved.append(p),
        extractor=lambda: _tree_with("DEST"),
    )
    assert moved == [plan]


def test_execute_move_retries_whole_move_once_then_fails():
    plan = MovePlan("pl_haze", "HAZE", None, "DEST")
    attempts = []
    with pytest.raises(RuntimeError, match="not verified"):
        execute_move(
            plan, session_cls=FakeSession,
            mover=lambda s, p: attempts.append(p),
            extractor=lambda: _tree_with(None),   # never lands
            settle_seconds=0,
        )
    assert len(attempts) == 2  # one retry of the whole move


def test_execute_move_skips_ui_if_already_done():
    plan = MovePlan("pl_haze", "HAZE", None, "DEST")
    moved = []
    execute_move(
        plan, session_cls=FakeSession,
        mover=lambda s, p: moved.append(p),
        extractor=lambda: _tree_with("DEST"),
        precheck=True,
    )
    assert moved == []  # slow-flush guard: verified done before re-driving


def test_execute_move_aborted_mid_move_reports_actual_still_at_source():
    from sortify.clientui import UiStepError

    plan = MovePlan("pl_haze", "HAZE", "SRC", "DEST")

    def boom(s, p):
        raise UiStepError("text 'Find a folder' not found on screen")

    with pytest.raises(RuntimeError) as e:
        execute_move(
            plan, session_cls=FakeSession,
            mover=boom,
            extractor=lambda: _tree_with("SRC"),  # never budged
        )
    msg = str(e.value)
    assert "still at SRC" in msg


def test_execute_move_aborted_mid_move_reports_actual_new_location():
    from sortify.clientui import UiStepError

    plan = MovePlan("pl_haze", "HAZE", "SRC", "DEST")

    def boom(s, p):
        raise UiStepError("text 'Find a folder' not found on screen")

    with pytest.raises(RuntimeError) as e:
        execute_move(
            plan, session_cls=FakeSession,
            mover=boom,
            # A click landed the playlist somewhere unplanned before the
            # abort — neither the source nor the intended destination.
            extractor=lambda: _tree_with("SOMEWHERE_ELSE"),
        )
    msg = str(e.value)
    assert "now at SOMEWHERE_ELSE" in msg
    assert "not DEST" in msg


def test_cli_move_rejects_folder_and_out_together(monkeypatch, capsys):
    # "<folder>" and "--out" are mutually exclusive destinations; giving both
    # must be a usage error, not a silent pick of one over the other.
    from sortify.foldermove import main

    monkeypatch.setattr(
        sys, "argv", ["spfolders", "move", "HAZE", "Y'no", "--out"]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "usage: spfolders move" in capsys.readouterr().out


def test_cli_move_rejects_unknown_flag(monkeypatch, capsys):
    # A typo'd flag (e.g. --dryrun instead of --dry-run) must be a usage
    # error, never silently ignored — an ignored --dry-run would drive a
    # real move for a caller who explicitly asked for nothing to happen.
    from sortify.foldermove import main

    monkeypatch.setattr(
        sys, "argv", ["spfolders", "move", "HAZE", "Y'no", "--dryrun"]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "unknown flag" in capsys.readouterr().out


def test_cli_tree_rejects_unknown_flag(monkeypatch, capsys):
    from sortify.foldermove import main

    monkeypatch.setattr(sys, "argv", ["spfolders", "tree", "--sunc"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "unknown flag" in capsys.readouterr().out
