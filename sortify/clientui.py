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


def _norm(s: str) -> str:
    """Lowercase, alphanumerics only — the shape OCR reliably preserves."""
    return "".join(c for c in s.lower() if c.isalnum())


def _scan(data, target: str, below: int | None, scale: int) -> tuple[int, int] | None:
    """Run-join matcher over one OCR pass; see find_text for the rules."""
    n = len(data["text"])
    # Group words into visual rows by y-proximity, not tesseract's
    # block/par/line ids — an icon glyph next to a word can split one
    # visual row into separate OCR blocks, which id-grouping can't
    # bridge. Words that normalize to nothing (icons, punctuation) drop.
    found = [i for i in range(n) if _norm(data["text"][i])]
    found.sort(key=lambda i: data["top"][i] + data["height"][i] // 2)
    rows: list[list[int]] = []
    for i in found:
        cy = data["top"][i] + data["height"][i] // 2
        if rows and abs(
            cy - (data["top"][rows[-1][-1]] + data["height"][rows[-1][-1]] // 2)
        ) <= 8:
            rows[-1].append(i)
        else:
            rows.append([i])
    for row in rows:
        row.sort(key=lambda i: data["left"][i])
    for idxs in rows:
        for a in range(len(idxs)):
            joined = ""
            for b in range(a, len(idxs)):
                joined += _norm(data["text"][idxs[b]])
                if len(joined) > len(target):
                    break
                if joined == target:
                    run = idxs[a : b + 1]
                    ys = [data["top"][j] for j in run]
                    y2 = [data["top"][j] + data["height"][j] for j in run]
                    cy = (min(ys) + max(y2)) // (2 * scale)
                    if below is not None and cy <= below:
                        continue
                    xs = [data["left"][j] for j in run]
                    x2 = [data["left"][j] + data["width"][j] for j in run]
                    return (min(xs) + max(x2)) // (2 * scale), cy
    return None


def find_text(img, text: str, below: int | None = None) -> tuple[int, int]:
    """Center of `text` in the image, tolerant of OCR word mangling.

    Tesseract merges adjacent words at the sidebar's font size ("New
    Folder" reads as "NewFolder") and glues icon glyphs onto words
    ("{Your Library") — both seen live 2026-08-23. Matching therefore
    normalizes to alphanumerics and compares contiguous word-runs within
    an OCR line, joined, for exact equality against the normalized
    target. Exact equality keeps "Playlist" from matching "Playlists".

    `below` restricts matches to y > below — used to keep a row search
    from matching the query text sitting in the filter box itself.
    """
    import pytesseract
    from PIL import Image

    # 2x upscale before OCR: tesseract is blind to some rows at the
    # client's native font size (a whole sidebar row OCR'd as nothing,
    # live 2026-08-23); doubled, it reads them. Coordinates halve back.
    scale = 2
    big = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    target = _norm(text)
    # Three OCR passes (all live findings, 2026-08-23): default
    # segmentation; sparse-text (--psm 11), which reads dialog BUTTON rows
    # the default mode drops ("Cancel"/"Delete" pills); and an
    # inverted-binarized pass, the only one that reads text on a blue
    # SELECTION highlight (a selected sidebar row is invisible to the
    # other two).
    from PIL import ImageOps

    inverted = ImageOps.grayscale(big).point(lambda p: 0 if p > 150 else 255)
    for image, config in ((big, ""), (big, "--psm 11"), (inverted, "")):
        data = pytesseract.image_to_data(
            image, output_type=pytesseract.Output.DICT, config=config
        )
        hit = _scan(data, target, below, scale)
        if hit is not None:
            return hit
    # Save what was actually searched — the abort screenshot is the only
    # forensics a headless UI failure leaves behind.
    fail_path = os.path.expanduser(
        os.environ.get("SORTIFY_CLIENTUI_FAIL_SHOT", "~/.cache/sortify-clientui-fail.png")
    )
    try:
        img.save(fail_path)
        hint = f" (screen saved to {fail_path})"
    except Exception:
        hint = ""
    raise UiStepError(f"text {text!r} not found on screen{hint}")


def _xdo(display: str, *args: str) -> None:
    env = dict(os.environ, DISPLAY=display)
    r = subprocess.run(["xdotool", *args], env=env, capture_output=True)
    if r.returncode != 0:
        raise UiStepError(f"xdotool {' '.join(args)} failed: {r.stderr[:200]!r}")


def key(display: str, keys: str) -> None:
    _xdo(display, "key", "--clearmodifiers", keys)


def type_text(display: str, s: str) -> None:
    # 100ms/key: at 60ms the laggy CEF drops characters (a space vanished
    # from a typed query, live 2026-08-23) — callers verify outcomes too.
    _xdo(display, "type", "--delay", "100", s)


def click(display: str, x: int, y: int) -> None:
    _xdo(display, "mousemove", str(x), str(y))
    _xdo(display, "click", "1")


def right_click(display: str, x: int, y: int) -> None:
    _xdo(display, "mousemove", str(x), str(y))
    _xdo(display, "click", "3")


LIBRARY_TOGGLE_POS = (284, 199)  # collapsed sidebar's expand toggle (t5-01/02)
UI_READY_TIMEOUT = 45            # cold-start paint can trail the window by ~20s


def wait_for_text(
    display: str, text: str, tries: int = 4, wait: float = 3.0,
    below: int | None = None,
) -> tuple[int, int]:
    """Poll screenshots until `text` is OCR-visible; abort after `tries`.

    Also the antidote to plain OCR flakiness: tesseract occasionally drops
    a clearly-visible word from one frame (seen live in the Task 8 runs),
    so even "it must already be on screen" lookups go through here — a
    fresh screenshot and re-OCR is exactly the retry that fixes a dropped
    word, while a genuinely absent element still aborts.
    """
    last: UiStepError | None = None
    for i in range(tries):
        try:
            img = screenshot(display)
            # below is passed positionally only when set, so tests that
            # monkeypatch find_text with a two-arg fake keep working.
            return find_text(img, text, below) if below is not None else find_text(img, text)
        except UiStepError as e:
            last = e
            if i < tries - 1:
                time.sleep(wait)
    raise UiStepError(f"gave up after {tries} looks: {last}")


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
            # A window is not a painted UI: wait for first paint, then
            # normalize to the library root — the client remembers its
            # last view (collapsed sidebar, inside a folder) across runs.
            time.sleep(5)
            back_to_library(self.display)
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


def move_playlist_ui(session: "ClientSession", plan) -> None:
    """Drive one move through the client UI. Verification is the caller's
    job (rootlist extraction) — this function only acts and asserts UI
    state step by step, aborting on the first surprise."""
    d = session.display

    # 1-4. Filter to the playlist row, open its context menu (TOCTOU
    # retry lives in _row_menu), and click "Move to folder".
    click(d, *_row_menu(session, plan.playlist_name, MENU_MOVE_TO_FOLDER))
    time.sleep(SETTLE)

    if plan.to_path is None:
        click(d, *wait_for_text(d, MENU_REMOVE_FROM_FOLDER))
    else:
        fx, fy = wait_for_text(d, FOLDER_SEARCH_HINT)
        leaf = plan.to_path.split(" / ")[-1]
        for _ in range(3):
            click(d, fx, fy)
            _clear_and_type(d, leaf)
            try:
                click(d, *wait_for_text(d, leaf, tries=2, below=fy + 10))
                break
            except UiStepError:
                continue
        else:
            raise UiStepError(f"folder {leaf!r} never appeared in the submenu search")

    time.sleep(SETTLE)  # let the client commit before the caller extracts


# ---- scratch-object helpers (Task 8 acceptance test) -----------------------
# Live-mapped 2026-08-23 (t8-01..t8-15 screenshots). Each follows
# move_playlist_ui's discipline: screenshot -> find_text -> act -> settle,
# aborting on the first missing anchor. The delete-confirm dialogs title
# themselves with the word "Delete", which OCR would find before the button,
# so the button is clicked at a fixed offset right of "Cancel" instead.

PLUS_OFFSET = (129, 0)              # sidebar "+" button, right of "Your
                                    # Library" (t8-c7: opens the create menu)
# "+" menu options, anchored by their full DESCRIPTIONS: the bare words
# "Playlist"/"Folder" also appear in row sublabels ("Playlist • owner"),
# and a missed + click once sent the search into a real playlist's row
# (live finding, cold gate run — the abort discipline caught it). The
# descriptions exist only inside the open + menu, so matching them is
# also the menu-opened assertion.
PLUS_MENU_FOLDER = "Organize your playlists"
PLUS_MENU_PLAYLIST = "Create a playlist with songs or episodes"
FOLDER_MENU_RENAME = "Rename"       # folder rows rename inline (ctrl+a, Return)
DEFAULT_FOLDER_NAME = "New Folder"  # what "Create folder" births
NAME_DETAILS = "Name & details"     # button on a fresh playlist's view
DIALOG_EDIT_DETAILS = "Edit details"
DIALOG_SAVE = "Save"
DEFAULT_PLAYLIST_PREFIX = "My Playlist"  # fresh playlists are "My Playlist #N"
CONFIRM_CANCEL = "Cancel"
CONFIRM_DELETE_OFFSET = (97, 0)     # "Delete" button sits right of "Cancel"


def _clear_and_type(display: str, s: str) -> None:
    key(display, "ctrl+a")
    key(display, "BackSpace")
    type_text(display, s)
    time.sleep(SETTLE)


SIDEBAR_REGION = (248, 160, 528, 620)  # crop for render-stability checks


def _wait_sidebar_stable(display: str, tries: int = 10) -> None:
    """Wait until the sidebar stops re-rendering.

    The library filter applies on a debounce: a screenshot taken right
    after typing shows rows at their OLD positions, and a click computed
    from it lands where a row no longer is (live finding — the empty-area
    create popup opened instead of a row menu). Compare consecutive
    sidebar crops until two in a row are identical; if never stable,
    proceed — the callers' retry loops are the net.
    """
    prev = None
    for _ in range(tries):
        cur = screenshot(display).crop(SIDEBAR_REGION).tobytes()
        if prev is not None and cur == prev:
            return
        prev = cur
        time.sleep(1.0)


BACK_CHEVRON_POS = (271, 198)  # sidebar back arrow when inside a folder view


def back_to_library(session_or_display) -> None:
    """Return the sidebar to the library root, wherever it is.

    The client REMEMBERS its last sidebar view across restarts (a fresh
    session can open inside a folder, live finding 2026-08-23), and
    creating a folder navigates into it. Click the back chevron until
    "Your Library" is visible; expand the sidebar if it's collapsed.
    """
    d = getattr(session_or_display, "display", session_or_display)
    # Dismiss any leftover modal/menu first — an abandoned dialog eats
    # every later click (live finding); Escape is harmless at root.
    key(d, "Escape")
    time.sleep(0.5)
    for attempt in range(4):
        try:
            wait_for_text(d, LIBRARY_HINT, tries=1)
            return
        except UiStepError:
            pass
        if attempt == 0:
            # Maybe just collapsed — the toggle and the chevron share a
            # corner; try the expand toggle first, it is harmless inside
            # a folder view too.
            click(d, *LIBRARY_TOGGLE_POS)
        else:
            click(d, *BACK_CHEVRON_POS)
        time.sleep(SETTLE)
    wait_for_text(d, LIBRARY_HINT)  # final check; aborts with forensics


def _clear_library_filter(session: "ClientSession") -> None:
    d = session.display
    back_to_library(d)
    ax, ay = wait_for_text(d, LIBRARY_FILTER_ANCHOR)
    ox, oy = LIBRARY_FILTER_OFFSET
    click(d, ax + ox, ay + oy)
    key(d, "ctrl+a")
    key(d, "BackSpace")
    time.sleep(SETTLE)


def _plus_menu(session: "ClientSession", option: str) -> None:
    """Open the sidebar "+" create menu and click one of its options.

    Creates at TOP LEVEL — never use a row context menu's Create entries:
    a folder row's menu creates *inside* that folder (learned the hard way,
    t8 run B nested two strays in ROOT).
    """
    d = session.display
    back_to_library(d)
    hx, hy = wait_for_text(d, LIBRARY_HINT)
    click(d, hx + PLUS_OFFSET[0], hy + PLUS_OFFSET[1])
    x, y = wait_for_text(d, option)
    click(d, x, y)
    time.sleep(SETTLE)


def _filter_to_row(session: "ClientSession", name: str) -> tuple[int, int]:
    """Type `name` into the library filter; return the matched row's center."""
    d = session.display
    back_to_library(d)
    ax, ay = wait_for_text(d, LIBRARY_FILTER_ANCHOR)
    ox, oy = LIBRARY_FILTER_OFFSET
    # Outcome-verified typing: dropped keystrokes make the query wrong and
    # the row never appears — clear and retype rather than trust the box.
    # below: the typed query is visible in the filter box itself — only a
    # match under the box is a row (live findings, gate runs 7 and 9).
    for _ in range(3):
        click(d, ax + ox, ay + oy)
        _clear_and_type(d, name)
        _wait_sidebar_stable(d)  # let the filter debounce apply first
        try:
            return wait_for_text(d, name, tries=2, below=ay + oy + 15)
        except UiStepError:
            continue
    raise UiStepError(f"row {name!r} never appeared after 3 typed searches")


def _confirm_delete(session: "ClientSession") -> None:
    """Click the confirm dialog's Delete button (offset from Cancel)."""
    d = session.display
    x, y = wait_for_text(d, CONFIRM_CANCEL)
    click(d, x + CONFIRM_DELETE_OFFSET[0], y + CONFIRM_DELETE_OFFSET[1])
    time.sleep(SETTLE)


def create_folder_ui(session: "ClientSession", name: str) -> None:
    d = session.display
    _clear_library_filter(session)
    _plus_menu(session, PLUS_MENU_FOLDER)
    # Creating navigates INTO the newborn "New Folder" (live finding).
    # Rename opens a DIALOG (title "Rename", Save button) — wait for it
    # before typing, or the keystrokes evaporate (live finding, gate 8).
    x, y = wait_for_text(d, DEFAULT_FOLDER_NAME, tries=6)
    right_click(d, x, y)
    time.sleep(SETTLE)
    click(d, *wait_for_text(d, FOLDER_MENU_RENAME))
    wait_for_text(d, DIALOG_SAVE, tries=6)  # dialog is up before we type
    _clear_and_type(d, name)
    click(d, *wait_for_text(d, DIALOG_SAVE))
    time.sleep(SETTLE)
    back_to_library(d)


def create_playlist_ui(session: "ClientSession", name: str) -> None:
    d = session.display
    _plus_menu(session, PLUS_MENU_PLAYLIST)
    # The new playlist's view opens; rename via "Name & details".
    click(d, *wait_for_text(d, NAME_DETAILS, tries=6))
    wait_for_text(d, DIALOG_EDIT_DETAILS)  # dialog-open assertion
    click(d, *wait_for_text(d, DEFAULT_PLAYLIST_PREFIX))  # name field
    _clear_and_type(d, name)
    click(d, *wait_for_text(d, DIALOG_SAVE))
    time.sleep(SETTLE)
    back_to_library(d)


def _row_menu(session: "ClientSession", name: str, expect_item: str) -> tuple[int, int]:
    """Open `name`'s row context menu; return `expect_item`'s position.

    The filtered list re-renders asynchronously, so a right-click can land
    where a row USED to be (live finding: it opened the empty-area create
    popup instead). Verify the expected menu item actually appeared; on a
    miss, Escape and re-locate the row from a fresh screenshot.
    """
    d = session.display
    for _ in range(3):
        x, y = _filter_to_row(session, name)
        right_click(d, x, y)
        time.sleep(SETTLE)
        try:
            return wait_for_text(d, expect_item, tries=2)
        except UiStepError:
            key(d, "Escape")
            time.sleep(0.5)
    raise UiStepError(f"{expect_item!r} never appeared in {name!r}'s row menu")


def delete_playlist_ui(session: "ClientSession", name: str) -> None:
    d = session.display
    click(d, *_row_menu(session, name, MENU_DELETE))
    time.sleep(SETTLE)
    _confirm_delete(session)


def delete_folder_ui(session: "ClientSession", name: str) -> None:
    # Same flow as playlists: the library filter matches folders too, and a
    # folder row's menu carries its own Delete + confirm dialog.
    delete_playlist_ui(session, name)
