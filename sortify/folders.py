"""Parse the folder hierarchy extracted from the Spotify desktop client.

The Web API has no notion of folders, so the tree comes from
github.com/mikez/spotify-folders run on a machine with the desktop app.
House convention: home folders are the ALL-CAPS ones; anything in a
normal-case folder (trips etc.) is not a filing destination.
"""

from __future__ import annotations

import re
import unicodedata


def starts_with_emoji(name: str) -> bool:
    """True for names opening with an emoji/symbol (the user's marker for
    derived superset/subset playlists, which are never filing destinations)."""
    s = name.strip()
    if not s:
        return False
    return ord(s[0]) >= 0x1F000 or unicodedata.category(s[0]) == "So"


def home_name_excluded(name: str, patterns: list[str], emoji: bool) -> bool:
    """Name-shape rules for playlists that are never filing destinations:
    emoji prefix (derived super/subsets), plus configurable regexes for
    markers like __start__/__stop__, {…} and <…>."""
    s = name.strip()
    if emoji and starts_with_emoji(s):
        return True
    return any(re.fullmatch(p, s) for p in patterns)


def creatable_home_name_problem(
    name: str, input_pattern: str | None, exclude_patterns: list[str], exclude_emoji: bool
) -> str | None:
    """Why `name` cannot become a home playlist, or None if it can.

    Checked before the create call is spent: an input-shaped name would be
    re-read as an input by the pattern union on the very next request, and a
    home-excluded shape would be dropped from homes by every folder ingest —
    both would silently produce something other than what was asked for.
    """
    s = name.strip()
    if not s:
        return "the name is empty"
    if input_pattern and re.fullmatch(input_pattern, s):
        return (
            f"{s!r} matches the input name pattern — it would become an "
            "input, not a home"
        )
    if home_name_excluded(s, exclude_patterns, exclude_emoji):
        return (
            f"{s!r} has a shape that is excluded from homes "
            "(emoji prefix, or a marker like {…}, <…>, __…__)"
        )
    return None


def _is_caps(name: str) -> bool:
    return name == name.upper() and any(c.isalpha() for c in name)


def select_home_ids(mapping: dict, prefixes: list[str], excludes: list[str]) -> set[str]:
    """Playlist ids whose folder path sits under one of `prefixes`.

    Prefixes match whole segments ("ROOT" does not match "ROOT EX"); any
    path containing an excluded segment name (case-insensitive) is skipped.
    """
    pref_segs = [tuple(p.split(" / ")) for p in prefixes]
    excl = {e.upper() for e in excludes}
    out = set()
    for pid, info in mapping.items():
        segs = tuple(info["path"].split(" / "))
        if any(seg.upper() in excl for seg in segs):
            continue
        if any(segs[: len(ps)] == ps for ps in pref_segs):
            out.add(pid)
    return out


def extract_folder_map(tree) -> dict[str, dict]:
    """Walk the spotify-folders JSON. Returns {playlist_id: {path, caps}}.

    `path` is "Folder / Subfolder"; `caps` is True when any folder on the
    path is ALL-CAPS (→ home candidate). Playlists loose at the root level
    are not in any folder and don't appear at all.
    """
    out: dict[str, dict] = {}

    def walk(node, path: tuple[str, ...]):
        if isinstance(node, list):
            for child in node:
                walk(child, path)
            return
        if not isinstance(node, dict):
            return
        uri = node.get("uri") or ""
        if node.get("type") == "playlist" or ":playlist:" in uri:
            pid = uri.rsplit(":", 1)[-1]
            if pid and path:
                out[pid] = {"path": " / ".join(path), "caps": any(_is_caps(p) for p in path)}
            return
        children = node.get("children")
        if isinstance(children, list):
            name = (node.get("name") or "").strip()
            walk(children, path + ((name,) if name else ()))

    walk(tree, ())
    return out
