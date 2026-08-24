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

TABLET = "192.168.1.29:5555"  # the SM-T595 — NOT .113, that's the birdcam
ADB = os.path.expanduser("~/kode/tools/platform-tools/adb")

ROW_MENU_X = 1059    # track-row ⋮ column on the 1200x1920 portrait screen
SHARE_ROW_X = 300    # "Share" row in the context sheet: first item, this far
SHARE_ROW_DY = 119   # under the sheet's title (menu items dump unlabeled)
DUMP_PATH = "/sdcard/claude_ui.xml"

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


def track_row_menu_point(xml: str, title: str) -> tuple[int, int]:
    """Tap point for `title`'s row ⋮ on the album page: the fixed menu
    column, at the title node's height."""
    for node in _NODE.findall(xml):
        if _attr(node, "resource-id").endswith("id/title_text") and \
                _attr(node, "text") == title:
            return ROW_MENU_X, _center(node)[1]
    raise UiStepError(f"track {title!r} is not on screen")


def sheet_header_center(xml: str, title: str) -> tuple[int, int]:
    """Center of the context sheet's title header — the node with the
    track's text and NO resource-id. Album rows carry id/title_text, so
    they can't satisfy this; its presence proves the sheet is open."""
    for node in _NODE.findall(xml):
        if _attr(node, "text") == title and not _attr(node, "resource-id"):
            return _center(node)
    raise UiStepError(f"context sheet for {title!r} did not open")


def _adb(*args: str) -> str:
    """Run one adb command against the tablet; returns stdout as text."""
    proc = subprocess.run(
        [ADB, "-s", TABLET, *args],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise UiStepError(
            f"adb {' '.join(args)} failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def _dump(run) -> str:
    run("shell", f"uiautomator dump {DUMP_PATH}")
    return run("shell", f"cat {DUMP_PATH}")


def _tap(run, x: int, y: int) -> None:
    run("shell", f"input tap {x} {y}")


def share_track(track_id: str, title: str, friend: str,
                run=None, sleep=None) -> list[str]:
    """Send `track_id` to `friend` via Spotify Messages on the tablet.

    Verifies each step from a fresh UI dump and aborts on the first
    surprise; the finally block always pauses playback (the deep link
    autoplays) and puts the screen back to sleep. Returns the friend
    names seen in the share sheet, for the targets cache."""
    run = run if run is not None else _adb
    sleep = sleep if sleep is not None else time.sleep
    run("shell", "input keyevent KEYCODE_WAKEUP")
    run("shell", "wm dismiss-keyguard")
    run("shell", "input keyevent KEYCODE_BACK")  # a left-open drawer/menu
    # would otherwise swallow the intent ("brought to front")
    run("shell", "am start -a android.intent.action.VIEW "
        f"-d spotify:track:{track_id}")
    sleep(8)
    try:
        _tap(run, *track_row_menu_point(_dump(run), title))
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
