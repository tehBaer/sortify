"""Resolve names and orchestrate client-UI playlist moves between folders.

Everything deterministic lives here: name -> id resolution from the cached
playlist listing (never a fetch), folder-path resolution from the extracted
tree, the move plan, and post-move verification against the rootlist.
The UI driving itself lives in sortify/clientui.py.

Zero Web API calls anywhere in this module — see the spec at
docs/superpowers/specs/2026-08-23-spfolders-folder-moves-design.md.
"""

from __future__ import annotations

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
