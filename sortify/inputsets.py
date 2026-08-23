"""Input sets — which named or foldered group an input playlist belongs to.

Inputs used to be one flat concept: anything matching `input_name_pattern`
plus explicitly marked ids. That could not express "these are inputs, but a
different set from those", which is what a library accumulates once some
inputs are day-to-day buffers and others are one-off filing projects.

A set matches EITHER by name pattern (`[Hazy]`, `<ethno>`) OR by folder
segment (everything inside THE BOMB). The folder form exists so a set can
be declared without renaming playlists whose names are already meaningful
— "Progressive rock · classic rock · Psychedelic Rock" says more than any
bracketing convention would.

Folder-defined sets deliberately have no name rule: `pattern_for` returns
None for them, and the naming checker skips those playlists rather than
inventing a wrapper for names that were never meant to carry one.
"""

from __future__ import annotations

import re

# Explicitly marked inputs that match no set's rule land here, so a
# hand-marked playlist is never left without a group.
DEFAULT_KEY = "buffer"


def resolve_sets(cfg: dict) -> list[dict]:
    """Ordered input sets, newest config shape first.

    Falls back to the single `input_name_pattern` so configs (and tests)
    written before sets existed keep resolving inputs exactly as they did.
    """
    sets = cfg.get("input_sets")
    if sets:
        return sets
    pattern = cfg.get("input_name_pattern")
    if pattern:
        return [{"key": DEFAULT_KEY, "label": DEFAULT_KEY, "pattern": pattern}]
    return []


def set_of(name: str, path: str | None, sets: list[dict]) -> str | None:
    """The key of the first set matching this playlist, or None."""
    stripped = (name or "").strip()
    segments = (path or "").split(" / ")
    for s in sets:
        pattern = s.get("pattern")
        if pattern and re.fullmatch(pattern, stripped):
            return s["key"]
        segment = s.get("path_segment")
        # Whole-segment match: "THE BOMB" must not match "THE BOMB SQUAD".
        if segment and segment in segments:
            return s["key"]
    return None


def matched_ids(playlists: list[dict], folders: dict, cfg: dict) -> set[str]:
    """Ids matching any set's rule — the pattern half of input resolution."""
    sets = resolve_sets(cfg)
    if not sets:
        return set()
    out = set()
    for p in playlists:
        path = (folders.get(p["id"]) or {}).get("path")
        if set_of(p.get("name", ""), path, sets):
            out.add(p["id"])
    return out


def pattern_for(key: str, sets: list[dict]) -> str | None:
    """The name pattern a set enforces, or None if it is folder-defined."""
    for s in sets:
        if s.get("key") == key:
            return s.get("pattern")
    return None
