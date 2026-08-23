# spfolders — Client-UI Folder Moves Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **NOTE:** Tasks 5 and 8 are interactive (screenshot-guided mapping of a live
> GUI). Inline execution is the better fit for this plan; a subagent cannot
> usefully do those tasks without the screenshot-read loop.

**Goal:** A `spfolders` CLI that moves playlists between Spotify folders by driving the box's own desktop client UI, verifying every move against the client's LevelDB rootlist.

**Architecture:** Two new modules. `sortify/clientui.py` owns the headless client session (Xvfb `:94`, file lock, xdotool keystrokes, screenshot+OCR text location) and the raw UI move sequence. `sortify/foldermove.py` owns everything deterministic: name→id resolution from the cached playlist listing, folder-path resolution from the extracted tree, the `MovePlan`, post-move verification, and the CLI. Reuses `sortify/rootlist.py` (peer-built) for extraction and client sync.

**Tech Stack:** Python 3.11+, xdotool, Xvfb, ImageMagick (`import`), pytesseract + Pillow + tesseract-ocr, fcntl file lock, pytest.

**Spec:** `docs/superpowers/specs/2026-08-23-spfolders-folder-moves-design.md`

## Global Constraints

- **Zero Web API calls.** Nothing in this feature may touch `api.spotify.com` or construct a `Spotify` client for fetching. Name resolution reads `Store().cache()["playlist_list"]` from disk and fails with guidance if absent — it never fetches.
- Display `:94` only; `:93` belongs to `rootlist.sync_client()`.
- Lock file `~/state/spotify/client-ui.lock` around any client launch.
- Every UI step asserts its expected state before acting; on doubt, abort. The only outcomes of `move` are "verified moved" and "verified not moved".
- Folder paths use the `"Parent / Child"` segment join, exactly as `sortify/folders.py` stores them.
- Live-client tests carry `@pytest.mark.clientui` and are excluded from the default `pytest -q` run.
- System packages assumed present (user installs once): `xdotool`, `tesseract-ocr`, `imagemagick` (plus already-present `Xvfb`, snap Spotify, logged in).

---

### Task 1: Playlist and folder name resolution (pure logic)

**Files:**
- Create: `sortify/foldermove.py`
- Test: `tests/test_foldermove.py`

**Interfaces:**
- Consumes: nothing from other tasks. `folders.extract_folder_map(tree)` from the existing codebase.
- Produces (used by Tasks 2, 6, 7):
  - `class ResolveError(Exception)` — message is user-printable, lists candidates.
  - `resolve_playlist(items: list[dict], mapping: dict, query: str) -> tuple[str, str, str | None]` — returns `(playlist_id, canonical_name, current_path_or_None)`. `items` is `cache["playlist_list"]["items"]` (dicts with `id`, `name`, `editable`); `mapping` is `extract_folder_map(tree)`.
  - `resolve_folder(tree: dict, path_query: str) -> str` — returns the canonical `"A / B"` path of a folder that exists in the tree.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_foldermove.py
import pytest

from sortify.foldermove import ResolveError, resolve_folder, resolve_playlist

TREE = {
    "type": "folder",
    "children": [
        {"name": "ROOT", "type": "folder",
         "uri": "spotify:user:u:folder:aa", "children": [
            {"name": "Y'no", "type": "folder",
             "uri": "spotify:user:u:folder:bb", "children": [
                {"type": "playlist", "uri": "spotify:playlist:pl_lite"},
            ]},
            {"type": "playlist", "uri": "spotify:playlist:pl_haze"},
        ]},
        {"type": "playlist", "uri": "spotify:playlist:pl_loose"},
    ],
}

ITEMS = [
    {"id": "pl_lite", "name": "LITE", "editable": True},
    {"id": "pl_haze", "name": "HAZE", "editable": True},
    {"id": "pl_loose", "name": "Loose One", "editable": True},
    {"id": "pl_dupe1", "name": "Dupe", "editable": True},
    {"id": "pl_dupe2", "name": "Dupe", "editable": True},
    {"id": "pl_other", "name": "Lite snacks", "editable": True},
]


def mapping():
    from sortify.folders import extract_folder_map
    return extract_folder_map(TREE)


def test_resolve_playlist_exact_name_case_insensitive():
    pid, name, path = resolve_playlist(ITEMS, mapping(), "lite")
    assert (pid, name, path) == ("pl_lite", "LITE", "ROOT / Y'no")


def test_resolve_playlist_top_level_has_no_path():
    pid, name, path = resolve_playlist(ITEMS, mapping(), "Loose One")
    assert (pid, name, path) == ("pl_loose", "Loose One", None)


def test_resolve_playlist_unique_substring_falls_back():
    pid, name, _ = resolve_playlist(ITEMS, mapping(), "loose")
    assert pid == "pl_loose"


def test_resolve_playlist_duplicate_names_refuse():
    with pytest.raises(ResolveError) as e:
        resolve_playlist(ITEMS, mapping(), "Dupe")
    assert "pl_dupe1" in str(e.value) and "pl_dupe2" in str(e.value)


def test_resolve_playlist_ambiguous_substring_lists_candidates():
    with pytest.raises(ResolveError) as e:
        resolve_playlist(ITEMS, mapping(), "lit")  # LITE and "Lite snacks"
    assert "LITE" in str(e.value) and "Lite snacks" in str(e.value)


def test_resolve_playlist_unknown_name():
    with pytest.raises(ResolveError):
        resolve_playlist(ITEMS, mapping(), "no such thing")


def test_resolve_folder_exact_path():
    assert resolve_folder(TREE, "ROOT / Y'no") == "ROOT / Y'no"


def test_resolve_folder_by_unique_leaf_name():
    assert resolve_folder(TREE, "y'no") == "ROOT / Y'no"


def test_resolve_folder_unknown():
    with pytest.raises(ResolveError):
        resolve_folder(TREE, "NOPE / NOWHERE")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_foldermove.py -v`
Expected: FAIL — `ModuleNotFoundError: sortify.foldermove` (or ImportError).

- [ ] **Step 3: Write the implementation**

```python
# sortify/foldermove.py
"""Resolve names and orchestrate client-UI playlist moves between folders.

Everything deterministic lives here: name -> id resolution from the cached
playlist listing (never a fetch), folder-path resolution from the extracted
tree, the move plan, and post-move verification against the rootlist.
The UI driving itself lives in sortify/clientui.py.

Zero Web API calls anywhere in this module — see the spec at
docs/superpowers/specs/2026-08-23-spfolders-folder-moves-design.md.
"""

from __future__ import annotations

from .folders import extract_folder_map


class ResolveError(Exception):
    """User-printable resolution failure; message lists candidates."""


def _match(query: str, candidates: list[tuple[str, str]], kind: str) -> tuple[str, str]:
    """candidates: (key, display_name). Exact ci match first, else unique
    ci substring. Anything else raises with the candidate list."""
    q = query.strip().lower()
    exact = [c for c in candidates if c[1].lower() == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        listing = ", ".join(f"{n} ({k})" for k, n in exact)
        raise ResolveError(f"{kind} name {query!r} is ambiguous: {listing}")
    sub = [c for c in candidates if q in c[1].lower()]
    if len(sub) == 1:
        return sub[0]
    if not sub:
        raise ResolveError(f"no {kind} matches {query!r}")
    listing = ", ".join(n for _, n in sub[:8])
    raise ResolveError(f"{kind} name {query!r} is ambiguous: {listing}")


def resolve_playlist(
    items: list[dict], mapping: dict, query: str
) -> tuple[str, str, str | None]:
    """(playlist_id, canonical_name, current_folder_path_or_None)."""
    pid, name = _match(query, [(p["id"], p["name"]) for p in items], "playlist")
    return pid, name, (mapping.get(pid) or {}).get("path")


def _folder_paths(tree) -> list[str]:
    out: list[str] = []

    def walk(node, path):
        if isinstance(node, list):
            for c in node:
                walk(c, path)
            return
        if not isinstance(node, dict) or not isinstance(node.get("children"), list):
            return
        name = (node.get("name") or "").strip()
        here = path + ((name,) if name else ())
        if name:
            out.append(" / ".join(here))
        walk(node["children"], here)

    walk(tree, ())
    return out


def resolve_folder(tree: dict, path_query: str) -> str:
    paths = _folder_paths(tree)
    q = path_query.strip().lower()
    exact = [p for p in paths if p.lower() == q]
    if len(exact) == 1:
        return exact[0]
    # Fall back to matching on the leaf folder name alone.
    leaf = [p for p in paths if p.split(" / ")[-1].lower() == q]
    if len(leaf) == 1:
        return leaf[0]
    if len(exact) > 1 or len(leaf) > 1:
        raise ResolveError(f"folder {path_query!r} is ambiguous: {', '.join(exact or leaf)}")
    raise ResolveError(
        f"no folder matches {path_query!r}; known folders:\n  " + "\n  ".join(paths)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_foldermove.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sortify/foldermove.py tests/test_foldermove.py
git commit -m "feat: spfolders name/folder resolution against cache and tree"
```

---

### Task 2: MovePlan and verification (pure logic)

**Files:**
- Modify: `sortify/foldermove.py` (append)
- Test: `tests/test_foldermove.py` (append)

**Interfaces:**
- Consumes: Task 1's `resolve_playlist`, `resolve_folder`, `ResolveError`.
- Produces (used by Tasks 6, 7):
  - `@dataclass(frozen=True) class MovePlan: playlist_id: str; playlist_name: str; from_path: str | None; to_path: str | None` (`None` = top level).
  - `plan_move(items, tree, playlist_query: str, dest_query: str | None) -> MovePlan` — `dest_query=None` means `--out`.
  - `verify_move(tree, playlist_id: str, expected_path: str | None) -> bool`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_foldermove.py`)

```python
from sortify.foldermove import MovePlan, plan_move, verify_move


def test_plan_move_into_folder():
    p = plan_move(ITEMS, TREE, "HAZE", "Y'no")
    assert p == MovePlan("pl_haze", "HAZE", "ROOT", "ROOT / Y'no")


def test_plan_move_out_to_top_level():
    p = plan_move(ITEMS, TREE, "LITE", None)
    assert p == MovePlan("pl_lite", "LITE", "ROOT / Y'no", None)


def test_plan_move_noop_refused():
    with pytest.raises(ResolveError) as e:
        plan_move(ITEMS, TREE, "LITE", "Y'no")
    assert "already" in str(e.value)


def test_plan_move_out_when_already_loose_refused():
    with pytest.raises(ResolveError):
        plan_move(ITEMS, TREE, "Loose One", None)


def test_verify_move_checks_tree_truth():
    assert verify_move(TREE, "pl_lite", "ROOT / Y'no") is True
    assert verify_move(TREE, "pl_lite", None) is False
    assert verify_move(TREE, "pl_loose", None) is True
    assert verify_move(TREE, "pl_loose", "ROOT") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_foldermove.py -v`
Expected: new tests FAIL with ImportError on `MovePlan`.

- [ ] **Step 3: Write the implementation** (append to `sortify/foldermove.py`)

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class MovePlan:
    playlist_id: str
    playlist_name: str
    from_path: str | None   # None = top level
    to_path: str | None     # None = top level (--out)


def plan_move(
    items: list[dict], tree: dict, playlist_query: str, dest_query: str | None
) -> MovePlan:
    mapping = extract_folder_map(tree)
    pid, name, current = resolve_playlist(items, mapping, playlist_query)
    dest = resolve_folder(tree, dest_query) if dest_query is not None else None
    if current == dest:
        where = f"in {dest!r}" if dest else "at the top level"
        raise ResolveError(f"{name} is already {where}")
    return MovePlan(pid, name, current, dest)


def verify_move(tree: dict, playlist_id: str, expected_path: str | None) -> bool:
    actual = (extract_folder_map(tree).get(playlist_id) or {}).get("path")
    return actual == expected_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_foldermove.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sortify/foldermove.py tests/test_foldermove.py
git commit -m "feat: spfolders move plan and rootlist verification logic"
```

---

### Task 3: Client session — lock, Xvfb, launch, ready-wait

**Files:**
- Create: `sortify/clientui.py`
- Test: `tests/test_clientui.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (used by Tasks 5, 6, 8):
  - `LOCK_PATH = "~/state/spotify/client-ui.lock"`, `DISPLAY = ":94"`.
  - `class UiStepError(Exception)` — a UI step's expected state was absent.
  - `client_lock()` — context manager; `fcntl.flock` exclusive, non-blocking; raises `UiStepError` if held.
  - `class ClientSession` — context manager. `__enter__` acquires the lock, starts Xvfb on `DISPLAY`, launches `snap run spotify --disable-gpu --force-renderer-accessibility`, waits for the main window via `xdotool search` (timeout 60s), returns self. `__exit__` terminates client (then `pkill -f /snap/spotify`), Xvfb, releases lock. Mirrors the teardown order proven in `rootlist.sync_client`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_clientui.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_clientui.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# sortify/clientui.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_clientui.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sortify/clientui.py tests/test_clientui.py
git commit -m "feat: locked headless client session for UI driving"
```

---

### Task 4: Screenshot + OCR primitives

**Files:**
- Modify: `sortify/clientui.py` (append)
- Modify: `pyproject.toml` (optional-dependencies)
- Test: `tests/test_clientui.py` (append)

**Interfaces:**
- Consumes: Task 3's `DISPLAY`, `UiStepError`.
- Produces (used by Tasks 5, 6, 8):
  - `screenshot(display: str) -> "PIL.Image.Image"` — via ImageMagick `import -window root`.
  - `find_text(img, text: str) -> tuple[int, int]` — center of the first place `text` appears (case-insensitive, consecutive OCR words); raises `UiStepError` if absent.
  - `key(display, keys: str)`, `type_text(display, s: str)`, `click(display, x: int, y: int)` — thin xdotool wrappers.

- [ ] **Step 1: Add the dependency group** in `pyproject.toml` under `[project.optional-dependencies]`:

```toml
# Drives the desktop client's UI (spfolders); OCR locates menu items on a
# virtual display. Not needed to run the server.
clientui = ["pytesseract>=0.3.10", "pillow>=10"]
```

Install: `.venv/bin/pip install -e '.[clientui]'`

- [ ] **Step 2: Write the failing tests** (append to `tests/test_clientui.py`; build the image with PIL so no binary fixture is needed)

```python
from PIL import Image, ImageDraw

from sortify.clientui import find_text


def _img_with_text(lines):
    img = Image.new("RGB", (400, 40 * (len(lines) + 1)), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((10, 10 + 40 * i), line, fill="black")  # default bitmap font
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_clientui.py -v`
Expected: new tests FAIL with ImportError on `find_text`.

- [ ] **Step 4: Write the implementation** (append to `sortify/clientui.py`)

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_clientui.py -v`
Expected: all PASS (pytesseract must find the system `tesseract` binary — install it first if `which tesseract` is empty).

- [ ] **Step 6: Commit**

```bash
git add sortify/clientui.py tests/test_clientui.py pyproject.toml
git commit -m "feat: screenshot + OCR + xdotool primitives for client driving"
```

---

### Task 5: INTERACTIVE — a11y probe and UI interaction mapping

**Files:**
- Modify: `sortify/clientui.py` (constants + docstring notes)
- No new tests (this task produces recorded facts, not logic).

**Interfaces:**
- Produces (used by Task 6): the named constants below, with values established against the live client, documented in `clientui.py`.

This task is a screenshot-guided session against the real client. It cannot
be done blind; the executor takes screenshots, reads them, and records what
the UI actually is. **User go required before starting** (it launches the
real logged-in client; it changes nothing).

- [ ] **Step 1: a11y probe (spec's open question).** In a `ClientSession`, run:

```bash
busctl --user tree org.a11y.atspi.Registry 2>/dev/null | head -30
python3 -c "import pyatspi" 2>&1
```

and, if pyatspi imports, walk the desktop for a `spotify` application and
count its accessible descendants. **Record the verdict in the clientui.py
module docstring** ("a11y tree: populated/hollow, probed YYYY-MM-DD").
If populated with named menu items: STOP, report back — the plan gets
amended to drive via AT-SPI instead of OCR (Tasks 6/8 change). If hollow
(the expected outcome for a snap CEF app): continue; OCR path stands.

- [ ] **Step 2: Map the moves.** In a `ClientSession`, alternate
`screenshot(":94")` (save to the scratchpad, read the image) with
keystrokes/clicks to establish and record each of these as constants in
`clientui.py`:

```python
# UI interaction map, established against client 1.2.95 on 2026-08-XX.
# Every entry is a fact about the UI, re-checkable by screenshot; if a
# redesign breaks one, find_text raises UiStepError and the run aborts.
LIBRARY_FILTER_HINT = "..."   # visible text that marks the library search/filter box
ROW_CONTEXT_KEY = "Menu"      # key that opens the row context menu (or button3 click)
MENU_MOVE_TO_FOLDER = "Move to folder"   # exact menu-item label
MENU_REMOVE_FROM_FOLDER = "..."          # exact label for "move out", if it exists
MENU_CREATE_FOLDER = "..."               # for the scratch-object test
MENU_DELETE = "..."                      # for scratch-object cleanup
CONFIRM_DELETE = "..."                   # label on the delete-confirmation button
```

The flow to map: focus the sidebar library filter (find its icon/text via
OCR, click), type a playlist name, select the row (arrow keys or click on
the OCR-located name), open its context menu, read the exact submenu
labels for folder moves. Also map: creating a folder, creating a playlist
(for the scratch test), and deleting both.

- [ ] **Step 3: Commit the recorded map**

```bash
git add sortify/clientui.py
git commit -m "docs: client 1.2.95 UI interaction map, probed live"
```

---

### Task 6: The move sequence

**Files:**
- Modify: `sortify/clientui.py` (append `move_playlist_ui`)
- Test: `tests/test_clientui.py` (append — sequence test with mocked primitives)

**Interfaces:**
- Consumes: Task 2's `MovePlan`; Task 4's primitives; Task 5's constants.
- Produces (used by Task 7): `move_playlist_ui(session: ClientSession, plan: MovePlan) -> None` — drives the UI only; raises `UiStepError` on any unexpected state; does NOT verify (that's the caller's job via the rootlist).

- [ ] **Step 1: Write the failing test** (append to `tests/test_clientui.py`). The test pins the *order and abort discipline* of the sequence with mocked primitives:

```python
from sortify.foldermove import MovePlan


def test_move_sequence_aborts_on_missing_menu(monkeypatch):
    calls = []
    monkeypatch.setattr(clientui, "screenshot", lambda d: "IMG")
    monkeypatch.setattr(clientui, "click", lambda d, x, y: calls.append(("click", x, y)))
    monkeypatch.setattr(clientui, "key", lambda d, k: calls.append(("key", k)))
    monkeypatch.setattr(clientui, "type_text", lambda d, s: calls.append(("type", s)))

    def fake_find(img, text):
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
    # Aborted at the menu step: no clicks after the failed find.
    assert calls[-1] == ("find", clientui.MENU_MOVE_TO_FOLDER)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_clientui.py -v`
Expected: FAIL — `move_playlist_ui` undefined.

- [ ] **Step 3: Write the implementation** (append to `sortify/clientui.py`; the exact waits/labels come from Task 5's map — the *shape* is fixed here):

```python
def move_playlist_ui(session: "ClientSession", plan) -> None:
    """Drive one move through the client UI. Verification is the caller's
    job (rootlist extraction) — this function only acts and asserts UI
    state step by step, aborting on the first surprise."""
    d = session.display
    # 1. Focus the library filter and find the playlist row.
    img = screenshot(d)
    click(d, *find_text(img, LIBRARY_FILTER_HINT))
    type_text(d, plan.playlist_name)
    time.sleep(1.5)  # filter debounce
    img = screenshot(d)
    x, y = find_text(img, plan.playlist_name)
    click(d, x, y)
    # 2. Context menu on the selected row.
    key(d, ROW_CONTEXT_KEY)
    time.sleep(0.8)
    img = screenshot(d)
    if plan.to_path is None:
        click(d, *find_text(img, MENU_REMOVE_FROM_FOLDER))
    else:
        click(d, *find_text(img, MENU_MOVE_TO_FOLDER))
        time.sleep(0.8)
        img = screenshot(d)
        # Submenu lists folder names; the leaf name is the label.
        click(d, *find_text(img, plan.to_path.split(" / ")[-1]))
    time.sleep(1.5)  # let the client commit and start syncing the rootlist
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_clientui.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sortify/clientui.py tests/test_clientui.py
git commit -m "feat: OCR-guided move sequence with per-step abort"
```

---

### Task 7: Orchestration — move with verify/retry, and the CLI

**Files:**
- Modify: `sortify/foldermove.py` (append), `pyproject.toml` (scripts)
- Test: `tests/test_foldermove.py` (append)

**Interfaces:**
- Consumes: Tasks 1–2 logic; Task 3 `ClientSession`; Task 6 `move_playlist_ui`; `rootlist.extract_tree`, `rootlist.sync_client`, `rootlist.cache_mtime`.
- Produces: `execute_move(plan, session_cls=None, mover=None, extractor=None) -> None` (raises on failure), `main() -> None`, entry point `spfolders = "sortify.foldermove:main"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_foldermove.py`)

```python
from sortify.foldermove import execute_move


class FakeSession:
    entered = 0
    def __enter__(self):
        FakeSession.entered += 1
        return self
    def __exit__(self, *a):
        return False


def _tree_with(pid_path):
    """Minimal tree putting pl_haze at the given path (or top level)."""
    node = {"type": "playlist", "uri": "spotify:playlist:pl_haze"}
    if pid_path is None:
        return {"type": "folder", "children": [node]}
    return {"type": "folder", "children": [
        {"name": pid_path, "type": "folder", "uri": "spotify:user:u:folder:ff",
         "children": [node]}]}


def test_execute_move_verifies_against_fresh_tree():
    plan = MovePlan("pl_haze", "HAZE", None, "DEST")
    moved = []
    execute_move(
        plan, session_cls=FakeSession,
        mover=lambda s, p: moved.append(p),
        extractor=lambda: _tree_with("DEST"),
    )
    assert moved == [plan]


def test_execute_move_retries_whole_move_once_then_fails():
    plan = MovePlan("pl_haze", "HAZE", None, "DEST")
    attempts = []
    with pytest.raises(RuntimeError, match="not verified"):
        execute_move(
            plan, session_cls=FakeSession,
            mover=lambda s, p: attempts.append(p),
            extractor=lambda: _tree_with(None),   # never lands
            settle_seconds=0,
        )
    assert len(attempts) == 2  # one retry of the whole move


def test_execute_move_skips_ui_if_already_done():
    plan = MovePlan("pl_haze", "HAZE", None, "DEST")
    moved = []
    execute_move(
        plan, session_cls=FakeSession,
        mover=lambda s, p: moved.append(p),
        extractor=lambda: _tree_with("DEST"),
        precheck=True,
    )
    assert moved == []  # slow-flush guard: verified done before re-driving
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_foldermove.py -v`
Expected: FAIL — `execute_move` undefined.

- [ ] **Step 3: Write the implementation** (append to `sortify/foldermove.py`)

```python
import sys
import time as _time


def execute_move(
    plan: MovePlan,
    session_cls=None,
    mover=None,
    extractor=None,
    settle_seconds: float = 3.0,
    precheck: bool = False,
) -> None:
    """Drive the move and verify it against the rootlist. Raises on failure.

    The injectable seams (session_cls/mover/extractor) exist for tests; the
    defaults are the real client session, UI sequence, and cache extraction.
    """
    from . import clientui, rootlist

    session_cls = session_cls or clientui.ClientSession
    mover = mover or clientui.move_playlist_ui
    extractor = extractor or rootlist.extract_tree

    def verified() -> bool:
        # The LevelDB can lag the UI: re-extract a few times before ruling.
        for _ in range(5):
            if verify_move(extractor(), plan.playlist_id, plan.to_path):
                return True
            _time.sleep(settle_seconds)
        return False

    with session_cls() as session:
        for attempt in (1, 2):
            # Slow-flush guard: never re-drive a move that already landed.
            if (precheck or attempt == 2) and verify_move(
                extractor(), plan.playlist_id, plan.to_path
            ):
                return
            mover(session, plan)
            if verified():
                return
    raise RuntimeError(
        f"move not verified: {plan.playlist_name} did not land at "
        f"{plan.to_path or 'top level'} — the tree still shows otherwise. "
        "Nothing further was attempted."
    )


def _load_inputs():
    from .rootlist import extract_tree
    from .store import Store

    entry = Store().cache().get("playlist_list") or {}
    items = entry.get("items") or []
    if not items:
        print("no cached playlist listing — open sortify and press Refresh first")
        sys.exit(2)
    return items, extract_tree()


def _print_tree(tree, indent=0):
    if isinstance(tree, dict):
        name = (tree.get("name") or "").strip()
        if name:
            print("  " * indent + name + "/")
            indent += 1
        for c in tree.get("children") or []:
            _print_tree(c, indent)


def main() -> None:
    from . import rootlist

    args = sys.argv[1:]
    if not args or args[0] not in ("tree", "move"):
        print(__doc__ or "usage: spfolders tree [--sync] | "
              "spfolders move <playlist> (<folder> | --out) [--dry-run]")
        sys.exit(0 if args and args[0] in ("-h", "--help") else 2)

    if args[0] == "tree":
        if "--sync" in args:
            print("waking the client to sync (~45s)…")
            rootlist.sync_client()
        print(f"tree as of {rootlist.cache_mtime() or 'unknown'}")
        _print_tree(rootlist.extract_tree())
        return

    # move
    rest = [a for a in args[1:] if not a.startswith("--")]
    flags = {a for a in args[1:] if a.startswith("--")}
    if not rest or (len(rest) == 1 and "--out" not in flags) or len(rest) > 2:
        print('usage: spfolders move "<playlist>" ("<folder>" | --out) [--dry-run]')
        sys.exit(2)
    dest = None if "--out" in flags else rest[1]
    items, tree = _load_inputs()
    try:
        plan = plan_move(items, tree, rest[0], dest)
    except ResolveError as e:
        print(f"refused: {e}")
        sys.exit(2)
    frm = plan.from_path or "top level"
    to = plan.to_path or "top level"
    print(f"plan: move {plan.playlist_name!r}  {frm}  →  {to}")
    if "--dry-run" in flags:
        print("dry run — nothing done. Zero API calls either way.")
        return
    try:
        execute_move(plan)
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)
    print("verified: the rootlist shows the playlist at its new path.")
    print("note: data/folders.json updates on the next folder re-import.")
```

- [ ] **Step 4: Add the entry point** in `pyproject.toml` `[project.scripts]`:

```toml
spfolders = "sortify.foldermove:main"
```

Then: `.venv/bin/pip install -e '.[clientui]'` (re-links scripts).

- [ ] **Step 5: Run tests, then the CLI smoke checks**

Run: `.venv/bin/pytest tests/test_foldermove.py -v` — all PASS.
Run: `.venv/bin/spfolders tree` — prints the real hierarchy from the cache.
Run: `.venv/bin/spfolders move "no such list" --out` — refuses with candidates, exit 2.
Run: `.venv/bin/spfolders move "<a real playlist>" "<its current folder>"` — refuses ("already in"), exit 2. No client launched for any of these.

- [ ] **Step 6: Commit**

```bash
git add sortify/foldermove.py tests/test_foldermove.py pyproject.toml
git commit -m "feat: spfolders CLI — plan, execute, verify against rootlist"
```

---

### Task 8: INTERACTIVE — scratch-object acceptance test

**Files:**
- Create: `tests/test_clientui_live.py`
- Modify: `pyproject.toml` (pytest markers)

**Interfaces:**
- Consumes: everything above; Task 5's `MENU_CREATE_FOLDER`, `MENU_DELETE`, `CONFIRM_DELETE` constants.
- Produces: the acceptance gate. The tool is "ready" only when this passes.

User-approved policy (2026-08-23): scratch objects only; real-tree moves
only on explicit user command. **Get a fresh go from the user before the
first run** — it creates and deletes a real playlist and folder on the
account (via the client UI; zero Web API calls).

- [ ] **Step 1: Register the marker** in `pyproject.toml` `[tool.pytest.ini_options]`:

```toml
markers = ["clientui: drives the real desktop client (excluded by default)"]
addopts = "-m 'not clientui'"
```

Verify exclusion: `.venv/bin/pytest -q` must not collect the live test; CLAUDE.md's "tests cost zero API calls" stays true.

- [ ] **Step 2: Write the live test**

```python
# tests/test_clientui_live.py
"""The acceptance gate: create scratch objects via the client UI, move a
playlist in and out of a folder, verify each step from the rootlist, and
delete everything created. Zero Web API calls; ~3-4 minutes of client time.

Run deliberately: .venv/bin/pytest -m clientui -v --timeout=600
"""
import time

import pytest

from sortify import clientui, rootlist
from sortify.folders import extract_folder_map
from sortify.foldermove import MovePlan, verify_move

SCRATCH_FOLDER = "zz spfolders scratch"
SCRATCH_LIST = "zz spfolders test list"

pytestmark = [pytest.mark.clientui, pytest.mark.timeout(600)]


def test_scratch_cycle():
    with clientui.ClientSession() as s:
        d = s.display
        # -- create scratch folder + playlist (constants from the Task 5 map)
        clientui.create_folder_ui(s, SCRATCH_FOLDER)
        clientui.create_playlist_ui(s, SCRATCH_LIST)
        time.sleep(5)
        tree = rootlist.extract_tree()
        pid = _find_playlist_id(tree, SCRATCH_LIST)
        assert pid, "scratch playlist did not appear in the rootlist"

        # -- move in, verify from the cache
        plan_in = MovePlan(pid, SCRATCH_LIST, None, SCRATCH_FOLDER)
        clientui.move_playlist_ui(s, plan_in)
        assert _settle(lambda: verify_move(rootlist.extract_tree(), pid, SCRATCH_FOLDER))

        # -- move out, verify again
        plan_out = MovePlan(pid, SCRATCH_LIST, SCRATCH_FOLDER, None)
        clientui.move_playlist_ui(s, plan_out)
        assert _settle(lambda: verify_move(rootlist.extract_tree(), pid, None))

        # -- cleanup through the same UI
        clientui.delete_playlist_ui(s, SCRATCH_LIST)
        clientui.delete_folder_ui(s, SCRATCH_FOLDER)
    tree = rootlist.extract_tree()
    assert not _find_playlist_id(tree, SCRATCH_LIST)


def _settle(check, tries=5, wait=3):
    for _ in range(tries):
        if check():
            return True
        time.sleep(wait)
    return False


def _find_playlist_id(tree, name):
    # The tree has no playlist names; match via the cached listing is not
    # possible for a just-created list. Diff instead: any playlist id in the
    # tree that data/cache.json does not know is the scratch one.
    from sortify.store import Store
    known = {p["id"] for p in (Store().cache().get("playlist_list") or {}).get("items", [])}
    ids = set(extract_folder_map(tree)) | _top_level_ids(tree)
    new = ids - known
    return next(iter(new), None) if len(new) == 1 else None


def _top_level_ids(tree):
    out = set()
    for c in tree.get("children") or []:
        uri = c.get("uri") or ""
        if ":playlist:" in uri:
            out.add(uri.rsplit(":", 1)[-1])
    return out
```

`create_folder_ui`, `create_playlist_ui`, `delete_playlist_ui`,
`delete_folder_ui` are written in this task too (in `clientui.py`), each
following the exact shape of `move_playlist_ui` — OCR-find the mapped menu
label from Task 5, click, assert the next expected state, abort on
surprise. Each is ~10 lines; write them from the Task 5 map, mirroring
`move_playlist_ui`'s structure (screenshot → find_text → click → sleep).

- [ ] **Step 3: Run the acceptance gate** (after user go)

Run: `.venv/bin/pytest -m clientui -v --timeout=600`
Expected: PASS. If a step aborts, read the failure's screenshot context,
fix the Task 5 constant it exposes, re-run. The default suite
(`.venv/bin/pytest -q`) must still pass and still exclude this file.

- [ ] **Step 4: Commit**

```bash
git add tests/test_clientui_live.py sortify/clientui.py pyproject.toml
git commit -m "test: scratch-object acceptance gate for client-UI moves"
```

---

### Task 9: Shared lock in sync_client + docs

**Files:**
- Modify: `sortify/rootlist.py:81-96` (`sync_client`)
- Modify: `CLAUDE.md` (Playlist folders section)
- Test: `tests/test_clientui.py` (append)

**Interfaces:**
- Consumes: Task 3's `client_lock`, `UiStepError`.
- Produces: refresh button and spfolders can never fight over the client.

- [ ] **Step 1: Write the failing test** (append to `tests/test_clientui.py`)

```python
def test_sync_client_respects_ui_lock(tmp_path, monkeypatch):
    from sortify import rootlist
    monkeypatch.setattr(clientui, "LOCK_PATH", str(tmp_path / "ui.lock"))
    with client_lock():
        with pytest.raises(UiStepError, match="another client-UI session"):
            rootlist.sync_client(seconds=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_clientui.py::test_sync_client_respects_ui_lock -v`
Expected: FAIL — sync_client runs (tries to launch; on a box without a free
display it may error differently — the assertion on the message is the point).

- [ ] **Step 3: Wrap sync_client's body** in the lock. In `sortify/rootlist.py`, add at the top of `sync_client` (import at module top: `from .clientui import client_lock`):

```python
def sync_client(seconds: int | None = None) -> None:
    ...existing docstring...
    with client_lock():
        ...entire existing body, indented one level...
```

- [ ] **Step 4: Run the full default suite**

Run: `.venv/bin/pytest -q`
Expected: green, live test not collected.

- [ ] **Step 5: Update CLAUDE.md** — append one bullet to the "Playlist folders" section:

```markdown
- **Moving playlists between folders**: `.venv/bin/spfolders move "<name>"
  ("<folder>" | --out)` drives the box's own client UI (display `:94`,
  OCR-guided, zero API calls) and verifies against the rootlist; `--dry-run`
  to preview. It shares a lock with the refresh button. After moves, re-run
  the folder re-import to update `data/folders.json`.
```

- [ ] **Step 6: Commit**

```bash
git add sortify/rootlist.py sortify/clientui.py tests/test_clientui.py CLAUDE.md
git commit -m "feat: refresh and spfolders share the client-UI lock"
```

---

## Self-Review (done at plan time)

- **Spec coverage:** CLI shape → Task 7; a11y probe + fallback decision → Task 5 (with explicit STOP-and-amend if a11y is populated); step discipline → Tasks 6 (shape + abort test) and 8; verification loop + slow-flush guard → Task 7 (`execute_move`, `precheck`/attempt-2 re-check); lock + sync_client follow-up → Tasks 3 and 9; scratch-object gate + user policy → Task 8; zero-API constraint → Global Constraints + `_load_inputs` failing instead of fetching.
- **Known open point (by design):** Task 5's constants are placeholders *because they are facts about a live UI* — the task exists to fill them, and Tasks 6/8 consume them by name. This is the one place the plan defers to reality.
- **Type consistency check:** `MovePlan(playlist_id, playlist_name, from_path, to_path)` used identically in Tasks 2, 6, 7, 8; `find_text(img, text) -> (x, y)` consistent between 4 and 6; `execute_move` seams match the fakes in Task 7's tests.
