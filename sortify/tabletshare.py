"""Send a track into Spotify Messages by driving the tablet's Spotify app.

The Web API has no Messages endpoint and the desktop client has no Messages
UI (both verified 2026-08-24), so the Android tablet is the route: deep-link
the track, open its row menu, Share, tap the friend's avatar. Android's
share sheet labels every target with content-desc "Share to <name>", so the
flow is driven from `uiautomator dump` output — a real UI tree, no OCR.
Client-side sharing spends zero Spotify Web API quota.

This module is pure planning/parsing for now; the adb orchestration layer
grows here as the flow hardens.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.parse

TABLET = "192.168.1.29:5555"  # the SM-T595 — NOT .113, that's the birdcam
ADB = os.path.expanduser("~/kode/tools/platform-tools/adb")

SHARE_ROW_X = 300    # "Share" row in the context sheet: first item, this far
SHARE_ROW_DY = 119   # under the sheet's title (menu items dump unlabeled)
DUMP_PATH = "/sdcard/claude_ui.xml"
OPEN_TRIES = 8       # polls for the album page; a cold app start on the
OPEN_DELAY = 4       # SM-T595 outlived a single 8s wait (2026-08-24)

_NODE = re.compile(r"<node[^>]+/?>")
_BOUNDS = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')


def _attr(node: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', node)
    return m.group(1) if m else ""


def _center(node: str) -> tuple[int, int]:
    x1, y1, x2, y2 = map(int, _BOUNDS.search(node).groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _label(node: str) -> str:
    return _attr(node, "content-desc") or _attr(node, "text")


class UiStepError(Exception):
    """A UI step's expected state was absent; the share was aborted."""


class DeviceOffline(Exception):
    """adb has no connection to the tablet — recoverable by reconnecting."""


def node_center(xml: str, label: str) -> tuple[int, int]:
    """Center of the first node whose content-desc (or text) is `label`."""
    for node in _NODE.findall(xml):
        if _label(node) == label:
            return _center(node)
    raise UiStepError(f"{label!r} is not on screen")


def share_targets(xml: str) -> list[tuple[str, tuple[int, int]]]:
    """The share sheet's friend/group avatars: (name, tap point) pairs.

    Targets carry content-desc "Share to <name>"; Search / Create group /
    Copy share link are actions, not targets, and don't match the prefix.
    """
    out = []
    for node in _NODE.findall(xml):
        desc = _attr(node, "content-desc")
        if desc.startswith("Share to "):
            out.append((desc[len("Share to "):], _center(node)))
    return out


def search_row_point(xml: str, title: str) -> tuple[int, int]:
    """Center of `title`'s row in the search results. Exact match on the
    id/title node, so a remix row can't shadow the original."""
    for node in _NODE.findall(xml):
        if _attr(node, "resource-id").endswith("id/title") and \
                _attr(node, "text") == title:
            return _center(node)
    raise UiStepError(f"track {title!r} is not in the search results")


def sheet_header_center(xml: str, title: str) -> tuple[int, int]:
    """Center of the context sheet's title header — the node with the
    track's text and NO resource-id. Album rows carry id/title_text, so
    they can't satisfy this; its presence proves the sheet is open."""
    for node in _NODE.findall(xml):
        if _attr(node, "text") == title and not _attr(node, "resource-id"):
            return _center(node)
    raise UiStepError(f"context sheet for {title!r} did not open")


def _adb_once(args: tuple[str, ...]) -> str:
    """Run one adb command against the tablet; returns stdout as text.

    A `connect` is addressed to the adb server, not to a device, so it must
    not carry -s: with it, adb refuses the very command meant to heal the
    missing device.
    """
    cmd = [ADB, *args] if args[:1] == ("connect",) else [ADB, "-s", TABLET, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        if "not found" in err or "device offline" in err:
            raise DeviceOffline(err)
        raise UiStepError(f"adb {' '.join(args)} failed: {err or proc.returncode}")
    return proc.stdout


def _adb_with_reconnect(*args: str, attempt=_adb_once) -> str:
    """`adb connect` is not durable — the tablet dozing drops the link and
    every later command fails "device not found" until something
    reconnects (live failure 2026-08-25). Heal once, then retry."""
    try:
        return attempt(args)
    except DeviceOffline:
        pass
    try:
        attempt(("connect", TABLET))
        return attempt(args)
    except DeviceOffline as e:
        raise UiStepError(
            f"the tablet is not reachable at {TABLET} — is it awake and on "
            f"Wi-Fi? ({e})")


def _adb(*args: str) -> str:
    return _adb_with_reconnect(*args)


def is_group(name: str) -> bool:
    """Group chats render as comma-joined member names ("A, B +4"); a
    single person or phone-number thread never contains ", "."""
    return ", " in name


def display_targets(raw: list[str], aliases: dict[str, str]) -> list[str]:
    """The picker's view of the cached targets: groups hidden, aliases
    applied. The cache itself stays raw — the tablet's share sheet only
    knows the raw names."""
    return [aliases.get(name, name) for name in raw if not is_group(name)]


def resolve_target(display: str, aliases: dict[str, str]) -> str:
    """Map a picker name back to the share sheet's raw name."""
    for raw, alias in aliases.items():
        if alias == display:
            return raw
    return display


def _dump(run) -> str:
    run("shell", f"uiautomator dump {DUMP_PATH}")
    return run("shell", f"cat {DUMP_PATH}")


def _tap(run, x: int, y: int) -> None:
    run("shell", f"input tap {x} {y}")


def _long_press(run, x: int, y: int) -> None:
    # a same-point swipe with a duration is Android's scriptable long-press
    run("shell", f"input swipe {x} {y} {x} {y} 800")


def share_track(title: str, artist: str, friend: str,
                run=None, sleep=None) -> list[str]:
    """Send the track to `friend` via Spotify Messages on the tablet.

    Entry is the SEARCH deep link, never a track link: a track VIEW intent
    autoplays and steals the user's playback via Connect (measured live
    2026-08-24, spotify: and https forms both). Search leaves playback
    alone; the result row is long-pressed for its context sheet. Every
    step is verified from a fresh UI dump, aborting on the first surprise;
    the finally block pauses any stray playback and sleeps the screen.
    Returns the friend names seen in the share sheet, for the cache."""
    run = run if run is not None else _adb
    sleep = sleep if sleep is not None else time.sleep
    run("shell", "input keyevent KEYCODE_WAKEUP")
    run("shell", "wm dismiss-keyguard")
    run("shell", "input keyevent KEYCODE_BACK")  # a left-open drawer/menu
    # would otherwise swallow the intent ("brought to front")
    query = urllib.parse.quote(f"{title} {artist}")
    run("shell", "am start -a android.intent.action.VIEW "
        f"-d spotify:search:{query}")
    sleep(8)
    try:
        for attempt in range(OPEN_TRIES):
            try:
                point = search_row_point(_dump(run), title)
                break
            except UiStepError:
                if attempt == OPEN_TRIES - 1:
                    raise
                sleep(OPEN_DELAY)
        _long_press(run, *point)
        sleep(2)
        _, hy = sheet_header_center(_dump(run), title)
        _tap(run, SHARE_ROW_X, hy + SHARE_ROW_DY)
        sleep(3)
        targets = share_targets(_dump(run))
        points = dict(targets)
        if friend not in points:
            raise UiStepError(
                f"{friend!r} is not a share target; saw: "
                + ", ".join(name for name, _ in targets))
        _tap(run, *points[friend])
        sleep(2)
        _tap(run, *node_center(_dump(run), "Send"))
        sleep(2)
        return [name for name, _ in targets]
    finally:
        run("shell", "input keyevent KEYCODE_MEDIA_PAUSE")
        run("shell", "input keyevent KEYCODE_SLEEP")
