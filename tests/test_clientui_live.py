"""The acceptance gate: create scratch objects via the client UI, move a
playlist in and out of a folder, verify each step from the rootlist, and
delete everything created. Zero Web API calls; several minutes of client
time. Run deliberately:

    .venv/bin/pytest -m clientui -v

Scratch objects only (user-approved policy, 2026-08-23): the test touches
nothing but the two "zz spfolders …" objects it creates itself. It starts
with a best-effort pre-clean of leftovers from earlier aborted runs.

The scratch playlist's id is found by diffing tree ids against an
in-session baseline taken just before creation. Diffing against sortify's
cached listing does NOT work: the rootlist also carries ~190 editorial
playlists (THIS IS <artist> etc.) the Web API listing never returns.
"""

from __future__ import annotations

import time

import pytest

from sortify import clientui, rootlist
from sortify.clientui import UiStepError
from sortify.foldermove import MovePlan, all_tree_ids, verify_move

SCRATCH_FOLDER = "zz spfolders scratch"
SCRATCH_LIST = "zz spfolders test list"

pytestmark = [pytest.mark.clientui, pytest.mark.timeout(900)]

# all_tree_ids lives in foldermove.py now — it's the same walk verify_move
# uses to tell "top level" from "not in the tree at all".
_all_tree_ids = all_tree_ids


def _folder_names(tree) -> set[str]:
    out: set[str] = set()

    def walk(n):
        if isinstance(n, dict):
            nm = (n.get("name") or "").strip()
            if nm and isinstance(n.get("children"), list):
                out.add(nm)
            for c in n.get("children") or []:
                walk(c)

    walk(tree)
    return out


def _settle(check, tries=5, wait=3):
    for _ in range(tries):
        if check():
            return True
        time.sleep(wait)
    return False


def _preclean(s) -> None:
    """Remove leftovers from earlier aborted runs; absence is fine.

    Loops per name: an aborted run can leave several same-named strays
    (it happened — two "New Folder"s nested in ROOT on 2026-08-23).
    """
    for name, deleter in (
        (SCRATCH_LIST, clientui.delete_playlist_ui),
        (SCRATCH_FOLDER, clientui.delete_folder_ui),
        # A run that died between create and rename leaves an unnamed one.
        (clientui.DEFAULT_FOLDER_NAME, clientui.delete_folder_ui),
    ):
        for _ in range(5):
            try:
                deleter(s, name)
            except UiStepError:
                break  # no (more) rows with this name


def test_scratch_cycle():
    with clientui.ClientSession() as s:
        _preclean(s)
        baseline = _all_tree_ids(rootlist.extract_tree())

        # -- create scratch folder + playlist, verify both from the rootlist
        clientui.create_folder_ui(s, SCRATCH_FOLDER)
        clientui.create_playlist_ui(s, SCRATCH_LIST)
        time.sleep(5)
        pid = None

        def playlist_visible():
            nonlocal pid
            new = _all_tree_ids(rootlist.extract_tree()) - baseline
            pid = next(iter(new)) if len(new) == 1 else None
            return pid is not None

        assert _settle(playlist_visible), "scratch playlist not in rootlist"
        assert SCRATCH_FOLDER in _folder_names(rootlist.extract_tree())

        # -- move in, verify from the cache (the real driver, end to end)
        clientui.move_playlist_ui(s, MovePlan(pid, SCRATCH_LIST, None, SCRATCH_FOLDER))
        assert _settle(
            lambda: verify_move(rootlist.extract_tree(), pid, SCRATCH_FOLDER)
        ), "move-in not reflected in rootlist"

        # -- move out, verify again
        clientui.move_playlist_ui(s, MovePlan(pid, SCRATCH_LIST, SCRATCH_FOLDER, None))
        assert _settle(
            lambda: verify_move(rootlist.extract_tree(), pid, None)
        ), "move-out not reflected in rootlist"

        # -- cleanup through the same UI
        clientui.delete_playlist_ui(s, SCRATCH_LIST)
        clientui.delete_folder_ui(s, SCRATCH_FOLDER)

    tree = rootlist.extract_tree()
    assert pid not in _all_tree_ids(tree), "scratch playlist survived"
    assert SCRATCH_FOLDER not in _folder_names(tree), "scratch folder survived"
