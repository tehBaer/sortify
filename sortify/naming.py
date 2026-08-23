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

from . import inputsets
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
        return _wrap(s, pattern)
    return None


# Candidate wrappers, tried in order. The proposed fix is derived by
# WRAPPING and re-testing against the set's own pattern, never by assuming
# a shape: a set that declares ^<.+>$ must be offered <name>, and offering
# [name] there would propose a "fix" that still violates the rule.
_WRAPPER_PAIRS = (("[", "]"), ("<", ">"), ("{", "}"))


def _wrap(s: str, pattern: str) -> str | None:
    for open_c, close_c in _WRAPPER_PAIRS:
        candidate = f"{open_c}{s}{close_c}"
        if re.fullmatch(pattern, candidate):
            return candidate
    # No wrapper satisfies this pattern; flagging a violation we cannot
    # describe a fix for would be worse than staying silent.
    return None


def _describe(pattern: str) -> str:
    """Human phrasing of a set's convention, for the violation message."""
    for open_c, close_c in _WRAPPER_PAIRS:
        if re.fullmatch(pattern, f"{open_c}x{close_c}"):
            return f"{open_c}wrapped{close_c}"
    return "named to the set's convention"


def violations(playlists: list[dict], input_ids: set[str], home_ids: set[str],
               input_pattern: str | None = None, sets: list[dict] | None = None,
               folders: dict | None = None) -> list[dict]:
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
        pattern, set_label = input_pattern, None
        if role == "input" and sets:
            path = ((folders or {}).get(p["id"]) or {}).get("path")
            key = inputsets.set_of(p["name"], path, sets)
            if key is None:
                # Explicitly marked but matching no set: judge it by the
                # default set's convention rather than skipping silently.
                key = inputsets.DEFAULT_KEY
            pattern = inputsets.pattern_for(key, sets)
            set_label = key
            if pattern is None:
                # Folder-defined set — its names carry no convention.
                continue
        proposed = propose(p["name"], role, pattern)
        if proposed is not None:
            if role == "input":
                rule = ("%s inputs are %s" % (set_label, _describe(pattern))
                        if set_label else "inputs are [bracketed]")
            else:
                rule = "homes are ALL CAPS"
            out.append({
                "playlist_id": p["id"],
                "current": p["name"],
                "proposed": proposed,
                "rule": rule,
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
