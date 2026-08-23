"""Resolve names and orchestrate client-UI playlist moves between folders.

Everything deterministic lives here: name -> id resolution from the cached
playlist listing (never a fetch), folder-path resolution from the extracted
tree, the move plan, and post-move verification against the rootlist.
The UI driving itself lives in sortify/clientui.py.

Zero Web API calls anywhere in this module — see the spec at
docs/superpowers/specs/2026-08-23-spfolders-folder-moves-design.md.
"""

from __future__ import annotations

import sys
import time as _time
from dataclasses import dataclass

from .folders import extract_folder_map


class ResolveError(Exception):
    """User-printable resolution failure; message lists candidates."""


def _match(query: str, candidates: list[tuple[str, str]], kind: str) -> tuple[str, str]:
    """candidates: (key, display_name). Exact ci match first, else unique
    ci substring. Anything else raises with the candidate list."""
    q = query.strip().lower()
    exact = [c for c in candidates if c[1].lower() == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        listing = ", ".join(f"{n} ({k})" for k, n in exact)
        raise ResolveError(f"{kind} name {query!r} is ambiguous: {listing}")
    sub = [c for c in candidates if q in c[1].lower()]
    if len(sub) == 1:
        return sub[0]
    if not sub:
        raise ResolveError(f"no {kind} matches {query!r}")
    listing = ", ".join(n for _, n in sub[:8])
    raise ResolveError(f"{kind} name {query!r} is ambiguous: {listing}")


def resolve_playlist(
    items: list[dict], mapping: dict, query: str
) -> tuple[str, str, str | None]:
    """(playlist_id, canonical_name, current_folder_path_or_None)."""
    pid, name = _match(query, [(p["id"], p["name"]) for p in items], "playlist")
    return pid, name, (mapping.get(pid) or {}).get("path")


def _folder_paths(tree) -> list[str]:
    out: list[str] = []

    def walk(node, path):
        if isinstance(node, list):
            for c in node:
                walk(c, path)
            return
        if not isinstance(node, dict) or not isinstance(node.get("children"), list):
            return
        name = (node.get("name") or "").strip()
        here = path + ((name,) if name else ())
        if name:
            out.append(" / ".join(here))
        walk(node["children"], here)

    walk(tree, ())
    return out


def resolve_folder(tree: dict, path_query: str) -> str:
    paths = _folder_paths(tree)
    q = path_query.strip().lower()
    exact = [p for p in paths if p.lower() == q]
    if len(exact) == 1:
        return exact[0]
    # Fall back to matching on the leaf folder name alone.
    leaf = [p for p in paths if p.split(" / ")[-1].lower() == q]
    if len(leaf) == 1:
        return leaf[0]
    if len(exact) > 1 or len(leaf) > 1:
        raise ResolveError(f"folder {path_query!r} is ambiguous: {', '.join(exact or leaf)}")
    raise ResolveError(
        f"no folder matches {path_query!r}; known folders:\n  " + "\n  ".join(paths)
    )


@dataclass(frozen=True)
class MovePlan:
    playlist_id: str
    playlist_name: str
    from_path: str | None   # None = top level
    to_path: str | None     # None = top level (--out)


def plan_move(
    items: list[dict], tree: dict, playlist_query: str, dest_query: str | None
) -> MovePlan:
    mapping = extract_folder_map(tree)
    pid, name, current = resolve_playlist(items, mapping, playlist_query)
    dest = resolve_folder(tree, dest_query) if dest_query is not None else None
    if current == dest:
        where = f"in {dest!r}" if dest else "at the top level"
        raise ResolveError(f"{name} is already {where}")
    return MovePlan(pid, name, current, dest)


def verify_move(tree: dict, playlist_id: str, expected_path: str | None) -> bool:
    actual = (extract_folder_map(tree).get(playlist_id) or {}).get("path")
    return actual == expected_path


def execute_move(
    plan: MovePlan,
    session_cls=None,
    mover=None,
    extractor=None,
    settle_seconds: float = 3.0,
    precheck: bool = False,
) -> None:
    """Drive the move and verify it against the rootlist. Raises on failure.

    The injectable seams (session_cls/mover/extractor) exist for tests; the
    defaults are the real client session, UI sequence, and cache extraction.
    """
    from . import clientui, rootlist

    session_cls = session_cls or clientui.ClientSession
    mover = mover or clientui.move_playlist_ui
    extractor = extractor or rootlist.extract_tree

    def verified() -> bool:
        # The LevelDB can lag the UI: re-extract a few times before ruling.
        for _ in range(5):
            if verify_move(extractor(), plan.playlist_id, plan.to_path):
                return True
            _time.sleep(settle_seconds)
        return False

    with session_cls() as session:
        for attempt in (1, 2):
            # Slow-flush guard: never re-drive a move that already landed.
            if (precheck or attempt == 2) and verify_move(
                extractor(), plan.playlist_id, plan.to_path
            ):
                return
            mover(session, plan)
            if verified():
                return
    raise RuntimeError(
        f"move not verified: {plan.playlist_name} did not land at "
        f"{plan.to_path or 'top level'} — the tree still shows otherwise. "
        "Nothing further was attempted."
    )


def _load_inputs():
    from .rootlist import extract_tree
    from .store import Store

    entry = Store().cache().get("playlist_list") or {}
    items = entry.get("items") or []
    if not items:
        print("no cached playlist listing — open sortify and press Refresh first")
        sys.exit(2)
    return items, extract_tree()


def _print_tree(tree, indent=0):
    if isinstance(tree, dict):
        name = (tree.get("name") or "").strip()
        if name:
            print("  " * indent + name + "/")
            indent += 1
        for c in tree.get("children") or []:
            _print_tree(c, indent)


def main() -> None:
    from . import rootlist

    args = sys.argv[1:]
    if not args or args[0] not in ("tree", "move"):
        print(__doc__ or "usage: spfolders tree [--sync] | "
              "spfolders move <playlist> (<folder> | --out) [--dry-run]")
        sys.exit(0 if args and args[0] in ("-h", "--help") else 2)

    if args[0] == "tree":
        if "--sync" in args:
            print("waking the client to sync (~45s)…")
            rootlist.sync_client()
        print(f"tree as of {rootlist.cache_mtime() or 'unknown'}")
        _print_tree(rootlist.extract_tree())
        return

    # move
    rest = [a for a in args[1:] if not a.startswith("--")]
    flags = {a for a in args[1:] if a.startswith("--")}
    if not rest or (len(rest) == 1 and "--out" not in flags) or len(rest) > 2:
        print('usage: spfolders move "<playlist>" ("<folder>" | --out) [--dry-run]')
        sys.exit(2)
    dest = None if "--out" in flags else rest[1]
    items, tree = _load_inputs()
    try:
        plan = plan_move(items, tree, rest[0], dest)
    except ResolveError as e:
        print(f"refused: {e}")
        sys.exit(2)
    frm = plan.from_path or "top level"
    to = plan.to_path or "top level"
    print(f"plan: move {plan.playlist_name!r}  {frm}  →  {to}")
    if "--dry-run" in flags:
        print("dry run — nothing done. Zero API calls either way.")
        return
    try:
        execute_move(plan)
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)
    print("verified: the rootlist shows the playlist at its new path.")
    print("note: data/folders.json updates on the next folder re-import.")
