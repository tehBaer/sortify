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
    monkeypatch.setattr(clientui, "back_to_library", lambda *a, **k: None)
    monkeypatch.setattr(clientui.shutil, "which", lambda name: "/usr/bin/" + name)

    with ClientSession() as s:
        assert s.display == ":94"
    assert launched[0][0] == "Xvfb" and launched[0][1] == ":94"
    assert launched[1][:3] == ["snap", "run", "spotify"]
    assert "--force-renderer-accessibility" in launched[1]
    assert killed == ["snap", "Xvfb"]  # client down before the display


def test_move_sequence_aborts_on_missing_menu(monkeypatch):
    from sortify.foldermove import MovePlan

    calls = []
    monkeypatch.setattr(clientui, "screenshot", lambda d: "IMG")
    monkeypatch.setattr(clientui, "_wait_sidebar_stable", lambda *a, **k: None)
    monkeypatch.setattr(clientui, "click", lambda d, x, y: calls.append(("click", x, y)))
    monkeypatch.setattr(clientui, "right_click", lambda d, x, y: calls.append(("right_click", x, y)))
    monkeypatch.setattr(clientui, "key", lambda d, k: calls.append(("key", k)))
    monkeypatch.setattr(clientui, "type_text", lambda d, s: calls.append(("type", s)))

    def fake_find(img, text, below=None):
        calls.append(("find", text))
        if text == clientui.MENU_MOVE_TO_FOLDER:
            raise UiStepError(f"text {text!r} not found on screen")
        return (10, 10)

    monkeypatch.setattr(clientui, "find_text", fake_find)
    monkeypatch.setattr(clientui.time, "sleep", lambda s: None)

    session = ClientSession.__new__(ClientSession)  # no real __enter__
    session.display = ":94"
    plan = MovePlan("pl_x", "SOME LIST", "ROOT", "ROOT / Y'no")
    with pytest.raises(UiStepError):
        clientui.move_playlist_ui(session, plan)
    # Aborted at the menu step: the row menu never yielded "Move to
    # folder", and nothing was CLICKED after the last failed find — the
    # only later actions are the retry loop's Escape cleanups.
    last_find = max(i for i, c in enumerate(calls) if c == ("find", clientui.MENU_MOVE_TO_FOLDER))
    assert not any(c[0] in ("click", "right_click") for c in calls[last_find + 1 :])


def test_session_requires_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(clientui, "LOCK_PATH", str(tmp_path / "ui.lock"))
    monkeypatch.setattr(clientui.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="xdotool"):
        ClientSession().__enter__()


def test_sync_client_respects_ui_lock(tmp_path, monkeypatch):
    from sortify import rootlist
    monkeypatch.setattr(clientui, "LOCK_PATH", str(tmp_path / "ui.lock"))
    with client_lock():
        with pytest.raises(UiStepError, match="another client-UI session"):
            rootlist.sync_client(seconds=0)


# Screenshot + OCR primitives (Task 4)
from PIL import Image, ImageDraw, ImageFont

from sortify.clientui import find_text


def _img_with_text(lines):
    img = Image.new("RGB", (400, 40 * (len(lines) + 1)), "white")
    d = ImageDraw.Draw(img)
    # Use DejaVu truetype font at 24px for tesseract reliability (R2 ruling).
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except OSError:
        # Fallback to default bitmap font if DejaVu is not available
        font = ImageFont.load_default()
    for i, line in enumerate(lines):
        d.text((10, 10 + 40 * i), line, fill="black", font=font)
    return img


def test_find_text_locates_line():
    img = _img_with_text(["Create playlist", "Move to folder", "Delete"])
    x, y = find_text(img, "Move to folder")
    assert 0 < x < 400
    assert 40 < y < 90  # second line's band


def test_find_text_case_insensitive():
    img = _img_with_text(["Move to folder"])
    assert find_text(img, "move TO Folder")


def test_find_text_absent_raises():
    img = _img_with_text(["Delete"])
    with pytest.raises(UiStepError, match="Move to folder"):
        find_text(img, "Move to folder")
