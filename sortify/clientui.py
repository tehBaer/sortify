"""Drive the box's Spotify desktop client UI, headless.

Session management (Xvfb :94 + snap client + exclusive lock) and the raw
interaction primitives (keystrokes, screenshots, OCR text location). The
deterministic planning/verification around this lives in foldermove.py.

Display :93 belongs to rootlist.sync_client's folder refresh; this module
uses :94 and a file lock so the two can never fight over the
single-instance snap client.
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
