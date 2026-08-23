"""Naming-convention rules for playlists.

House conventions (design doc 2026-08-20): Home playlists are ALL CAPS,
input playlists are [bracketed], and an emoji prefix marks a derived
super/subset playlist that is exempt from both. Pure functions over the
cached listing shape — no store, no network, so checking costs nothing.

Folder rules ({…} subset folders) are deferred: the Web API cannot rename
folders, so when they arrive they will be flag-only rows. `violations`
already returns per-row `rule` strings, so a new rule type is one more
branch here, not a new shape.
"""

from __future__ import annotations

import re

from .folders import _is_caps, starts_with_emoji


def propose(name: str, role: str, input_pattern: str | None = None) -> str | None:
    """The conforming form of `name` under `role`, or None when nothing
    needs to change: already conforming, emoji-exempt, or a no-op rename
    (a caps-rule name with no letters to uppercase)."""
    s = name.strip()
    if not s or starts_with_emoji(s):
        return None
    if role == "home":
        if _is_caps(s):
            return None
        proposed = s.upper()
        return proposed if proposed != s else None
    if role == "input":
        pattern = input_pattern or r"^\[.+\]$"
        if re.fullmatch(pattern, s):
            return None
        return f"[{s}]"
    return None


def violations(playlists: list[dict], input_ids: set[str], home_ids: set[str],
               input_pattern: str | None = None) -> list[dict]:
    """Naming violations among the user's own marked playlists.

    Input beats home when both are marked — the same precedence
    /api/playlists uses when it labels roles.
    """
    out = []
    for p in playlists:
        if not p.get("editable"):
            continue
        role = ("input" if p["id"] in input_ids
                else "home" if p["id"] in home_ids
                else None)
        if role is None:
            continue
        proposed = propose(p["name"], role, input_pattern)
        if proposed is not None:
            out.append({
                "playlist_id": p["id"],
                "current": p["name"],
                "proposed": proposed,
                "rule": ("inputs are [bracketed]" if role == "input"
                         else "homes are ALL CAPS"),
            })
    return out


MAX_PLAYLIST_NAME = 100   # Spotify's name cap
_SEP = " · "


def split_output_name(source_name: str | None, pile_name: str) -> str:
    """The title for a materialised pile: `{source} · pile`, ≤100 chars.

    The source name is what groups a split's outputs in the client, so under
    truncation it survives whole and the pile half gives way (design §3).
    Fixed at create time — a later rename of the source does not ripple.
    """
    pile = pile_name.strip()
    src = (source_name or "").strip()
    if not src:
        return pile[:MAX_PLAYLIST_NAME]
    title = f"{src}{_SEP}{pile}"
    if len(title) <= MAX_PLAYLIST_NAME:
        return title
    room = MAX_PLAYLIST_NAME - len(src) - len(_SEP) - 1   # -1 for the ellipsis
    if room < 1:
        return src[:MAX_PLAYLIST_NAME]
    return f"{src}{_SEP}{pile[:room]}…"
