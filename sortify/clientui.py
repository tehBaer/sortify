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
