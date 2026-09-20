"""File a newly created playlist into a folder, in the background.

The Web API has no folders, so a playlist created from inside sortify lands
at the top level of the library. The only thing on this box that can move it
is the desktop client, driven through `foldermove`/`clientui` — about a
minute of OCR-guided UI work, and zero Spotify calls.

That cost is why this is a job and not part of the create request: the row
appears at once, and the move lands afterwards. A failure is never
destructive — the playlist exists, it is just still at the top level, and
the status says why.

The one hard precondition is the NAME. The client has no notion of ids in
its UI; `move_playlist_ui` finds the row by filtering the sidebar on the
name. Two playlists sharing a name would be a coin flip over which one
moves, so filing refuses rather than guess.
"""

from __future__ import annotations

import threading

from .foldermove import MovePlan, ResolveError, resolve_folder, _check_leaf_unique


class FilingError(Exception):
    """A filing job that could not be completed; message is user-printable."""


def file_playlist(
    playlist_id: str,
    name: str,
    dest_path: str,
    *,
    items: list[dict],
    tree_extractor=None,
    executor=None,
) -> str:
    """Move `playlist_id` from the top level into `dest_path`. Returns the path.

    The seams are injectable for tests; the defaults extract this box's
    rootlist and drive the real client.
    """
    if tree_extractor is None:
        from . import rootlist
        tree_extractor = rootlist.extract_tree
    if executor is None:
        from .foldermove import execute_move
        executor = execute_move

    wanted = (name or "").strip().lower()
    twins = [p for p in items
             if p.get("id") != playlist_id and (p.get("name") or "").strip().lower() == wanted]
    if twins:
        raise FilingError(
            f"another playlist is also called {name!r} — the client finds the "
            "row by name, so sortify will not guess which one to move")

    try:
        tree = tree_extractor()
    except RuntimeError as e:
        raise FilingError(str(e)) from e
    try:
        dest = resolve_folder(tree, dest_path)
        _check_leaf_unique(tree, dest)
    except ResolveError as e:
        raise FilingError(str(e)) from e

    plan = MovePlan(playlist_id, (name or "").strip(), None, dest)
    try:
        executor(plan)
    except FilingError:
        raise
    except Exception as e:  # UiStepError, RuntimeError from a failed verify
        raise FilingError(str(e)) from e
    return dest


_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def status(playlist_id: str) -> dict:
    """What happened to this playlist's filing job: idle/filing/filed/failed."""
    with _jobs_lock:
        return dict(_jobs.get(playlist_id) or {"state": "idle"})


def _set(playlist_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs[playlist_id] = fields


def start(
    playlist_id: str,
    name: str,
    dest_path: str,
    *,
    items: list[dict] | None = None,
    on_success=None,
    runner=None,
    threaded: bool = True,
) -> None:
    """Begin filing; the status is readable from `status()` throughout.

    `threaded=False` runs it inline — what the tests use, and what a caller
    that is already on its own thread would want.
    """
    if runner is None:
        def runner():
            return file_playlist(playlist_id, name, dest_path, items=items or [])

    def run():
        try:
            landed = runner() or dest_path
        except FilingError as e:
            _set(playlist_id, state="failed", folder=None, error=str(e))
            return
        except Exception as e:  # a broken seam must not kill the thread silently
            _set(playlist_id, state="failed", folder=None, error=repr(e))
            return
        _set(playlist_id, state="filed", folder=landed, error=None)
        if on_success is not None:
            on_success(landed)

    _set(playlist_id, state="filing", folder=dest_path, error=None)
    if threaded:
        threading.Thread(target=run, name=f"filing-{playlist_id}", daemon=True).start()
    else:
        run()
