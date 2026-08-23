"""Lock and session-composition tests. No real client: subprocess and
xdotool are monkeypatched; only the lock uses the real filesystem."""
import os

import pytest

from sortify import clientui
from sortify.clientui import ClientSession, UiStepError, client_lock


def test_client_lock_is_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(clientui, "LOCK_PATH", str(tmp_path / "ui.lock"))
    with client_lock():
        with pytest.raises(UiStepError, match="another client-UI session"):
            with client_lock():
                pass
    with client_lock():  # released after the first exits
        pass


def test_session_composes_commands_and_tears_down(monkeypatch, tmp_path):
    monkeypatch.setattr(clientui, "LOCK_PATH", str(tmp_path / "ui.lock"))
    launched, killed = [], []

    class FakeProc:
        def __init__(self, cmd):
            self.cmd = cmd
        def terminate(self):
            killed.append(self.cmd[0])
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    def fake_popen(cmd, **kw):
        launched.append(cmd)
        return FakeProc(cmd)

    monkeypatch.setattr(clientui.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(clientui.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(clientui, "_wait_for_window", lambda display, timeout: None)
    monkeypatch.setattr(clientui.shutil, "which", lambda name: "/usr/bin/" + name)

    with ClientSession() as s:
        assert s.display == ":94"
    assert launched[0][0] == "Xvfb" and launched[0][1] == ":94"
    assert launched[1][:3] == ["snap", "run", "spotify"]
    assert "--force-renderer-accessibility" in launched[1]
    assert killed == ["snap", "Xvfb"]  # client down before the display


def test_session_requires_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(clientui, "LOCK_PATH", str(tmp_path / "ui.lock"))
    monkeypatch.setattr(clientui.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="xdotool"):
        ClientSession().__enter__()
