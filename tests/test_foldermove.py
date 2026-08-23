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
            ]},
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


def test_verify_move_checks_tree_truth():
    assert verify_move(TREE, "pl_lite", "ROOT / Y'no") is True
    assert verify_move(TREE, "pl_lite", None) is False
    assert verify_move(TREE, "pl_loose", None) is True
    assert verify_move(TREE, "pl_loose", "ROOT") is False
