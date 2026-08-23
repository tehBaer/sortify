"""Drive the box's Spotify desktop client UI, headless.

Session management (Xvfb :94 + snap client + exclusive lock) and the raw
interaction primitives (keystrokes, screenshots, OCR text location). The
deterministic planning/verification around this lives in foldermove.py.

Display :93 belongs to rootlist.sync_client's folder refresh; this module
uses :94 and a file lock so the two can never fight over the
single-instance snap client.

a11y tree: not pursued (probed 2026-08-23 — pyatspi is not installable
without system packages, and CEF's AT-SPI export is typically hollow).
The OCR path below is the driver; the interaction map documents the UI
facts it relies on.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import time
from contextlib import contextmanager

LOCK_PATH = "~/state/spotify/client-ui.lock"
DISPLAY = ":94"
WINDOW_TIMEOUT = 60

# UI interaction map, established live against snap client 1.2.95 on
# 2026-08-23 (Xvfb :94, 1280x800). Every entry is a fact about the UI,
# re-checkable by screenshot; when a redesign breaks one, find_text raises
# UiStepError and the run aborts instead of clicking blind.
#
# Mapped flow (t5-01..t5-14 screenshots, see the Task 5 ledger entry):
#   1. Expanded sidebar shows "Your Library"; collapsed shows icons only.
#      The library filter is a magnifier ICON (no OCR-able text): it sits
#      at FILTER_OFFSET from the "Playlists" chip's OCR center.
#   2. Typing in the filter finds playlists nested in folders too. Rows
#      show the name; right-click (button 3) on the name opens the menu.
#   3. "Move to folder" submenu carries its own "Find a folder" box:
#      click it, ctrl+a, type the folder's LEAF name, wait, click the
#      filtered row. Result rows show the parent path under the leaf name
#      (e.g. "Y'no" over "ROOT") — OCR both to disambiguate duplicates.
#   4. "Remove from folders" appears in that submenu only when the
#      playlist currently sits in a folder.
#   5. CEF under Xvfb lags input→paint by up to ~2s: wait SETTLE after
#      every input and verify by screenshot before acting on the result.
LIBRARY_HINT = "Your Library"            # sidebar-is-expanded marker
LIBRARY_FILTER_ANCHOR = "Playlists"      # chip the filter icon hangs under
LIBRARY_FILTER_OFFSET = (-21, 42)        # icon center relative to the chip's
MENU_MOVE_TO_FOLDER = "Move to folder"
FOLDER_SEARCH_HINT = "Find a folder"     # submenu's own search box
MENU_REMOVE_FROM_FOLDER = "Remove from folders"
MENU_CREATE_FOLDER = "Create folder"     # row menu AND folder submenu
MENU_CREATE_PLAYLIST = "Create playlist"
MENU_DELETE = "Delete"
CONFIRM_DELETE = "Delete"                # confirm-dialog button; verified live
                                         # at the Task 8 acceptance run
SETTLE = 2.0                             # seconds; see map note 5


class UiStepError(Exception):
    """A UI step's expected state was absent; the run was aborted."""


@contextmanager
def client_lock():
    path = os.path.expanduser(LOCK_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UiStepError(
                "another client-UI session (or folder refresh) is running — "
                "try again when it finishes"
            )
        yield
    finally:
        os.close(fd)  # releases the flock


def screenshot(display: str):
    """Grab the virtual display as a PIL image (ImageMagick `import`)."""
    from io import BytesIO

    from PIL import Image

    env = dict(os.environ, DISPLAY=display)
    r = subprocess.run(
        ["import", "-window", "root", "png:-"],
        env=env, capture_output=True,
    )
    if r.returncode != 0 or not r.stdout:
        raise UiStepError(f"screenshot of {display} failed: {r.stderr[:200]!r}")
    return Image.open(BytesIO(r.stdout)).convert("RGB")


def find_text(img, text: str) -> tuple[int, int]:
    """Center of `text` in the image, by OCR word sequence, case-insensitive."""
    import pytesseract

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    words = [w.strip().lower() for w in data["text"]]
    want = [w for w in text.lower().split() if w]
    n = len(data["text"])
    for i in range(n - len(want) + 1):
        if words[i : i + len(want)] == want:
            xs = [data["left"][j] for j in range(i, i + len(want))]
            x2 = [data["left"][j] + data["width"][j] for j in range(i, i + len(want))]
            ys = [data["top"][j] for j in range(i, i + len(want))]
            y2 = [data["top"][j] + data["height"][j] for j in range(i, i + len(want))]
            return (min(xs) + max(x2)) // 2, (min(ys) + max(y2)) // 2
    raise UiStepError(f"text {text!r} not found on screen")


def _xdo(display: str, *args: str) -> None:
    env = dict(os.environ, DISPLAY=display)
    r = subprocess.run(["xdotool", *args], env=env, capture_output=True)
    if r.returncode != 0:
        raise UiStepError(f"xdotool {' '.join(args)} failed: {r.stderr[:200]!r}")


def key(display: str, keys: str) -> None:
    _xdo(display, "key", "--clearmodifiers", keys)


def type_text(display: str, s: str) -> None:
    _xdo(display, "type", "--delay", "60", s)


def click(display: str, x: int, y: int) -> None:
    _xdo(display, "mousemove", str(x), str(y))
    _xdo(display, "click", "1")


def _wait_for_window(display: str, timeout: int) -> None:
    """Poll xdotool until the client's main window exists."""
    deadline = time.time() + timeout
    env = dict(os.environ, DISPLAY=display)
    while time.time() < deadline:
        r = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", "spotify"],
            env=env, capture_output=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            time.sleep(3)  # let the UI finish first paint
            return
        time.sleep(1)
    raise UiStepError(f"no Spotify window appeared on {display} within {timeout}s")


class ClientSession:
    def __init__(self):
        self.display = DISPLAY
        self._lock = None
        self._xvfb = None
        self._client = None

    def __enter__(self):
        for tool in ("xdotool", "Xvfb", "snap", "import", "tesseract"):
            if not shutil.which(tool):
                raise RuntimeError(f"{tool} is not installed — cannot drive the client")
        self._lock = client_lock()
        self._lock.__enter__()
        try:
            self._xvfb = subprocess.Popen(
                ["Xvfb", self.display, "-screen", "0", "1280x800x24"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            env = dict(os.environ, DISPLAY=self.display)
            self._client = subprocess.Popen(
                ["snap", "run", "spotify", "--disable-gpu",
                 "--force-renderer-accessibility"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _wait_for_window(self.display, WINDOW_TIMEOUT)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        # Client first, display second — same proven order as sync_client.
        if self._client is not None:
            self._client.terminate()
            try:
                self._client.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._client.kill()
            subprocess.run(["pkill", "-f", "/snap/spotify"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
            self._client = None
        if self._xvfb is not None:
            self._xvfb.terminate()
            try:
                self._xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._xvfb.kill()
            self._xvfb = None
        if self._lock is not None:
            self._lock.__exit__(None, None, None)
            self._lock = None
