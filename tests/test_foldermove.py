import sys

import pytest

from sortify.foldermove import ResolveError, resolve_folder, resolve_playlist

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
    p = plan_move(ITEMS, TREE, "HAZE", "Y'no")
    assert p == MovePlan("pl_haze", "HAZE", "ROOT", "ROOT / Y'no")


def test_plan_move_out_to_top_level():
    p = plan_move(ITEMS, TREE, "LITE", None)
    assert p == MovePlan("pl_lite", "LITE", "ROOT / Y'no", None)


def test_plan_move_noop_refused():
    with pytest.raises(ResolveError) as e:
        plan_move(ITEMS, TREE, "LITE", "Y'no")
    assert "already" in str(e.value)


def test_plan_move_out_when_already_loose_refused():
    with pytest.raises(ResolveError):
        plan_move(ITEMS, TREE, "Loose One", None)


def test_plan_move_unique_leaf_destination_still_resolves():
    # "Y'no" is a unique leaf in TREE — planning into it must still work.
    p = plan_move(ITEMS, TREE, "HAZE", "Y'no")
    assert p.to_path == "ROOT / Y'no"


def test_plan_move_ambiguous_leaf_destination_refused():
    # Two folders are both named "Vault" (ROOT / Vault and
    # ROOT / Y'no / Vault) — the desktop client's folder search can only
    # match on the leaf name, so sortify must refuse rather than gamble.
    with pytest.raises(ResolveError) as e:
        plan_move(ITEMS, TREE, "HAZE", "ROOT / Vault")
    msg = str(e.value)
    assert "Vault" in msg
    assert "ROOT / Vault" in msg
    assert "ROOT / Y'no / Vault" in msg


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
