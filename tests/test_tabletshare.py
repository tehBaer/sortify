"""tabletshare — parse uiautomator dumps and orchestrate a tablet share.

Everything here runs against fake adb output: tests must never touch the
real tablet (wakes its screen, starts playback, could message a real
person). The dump XML in the fixtures mirrors the real structure captured
2026-08-24 (single-line uiautomator XML, labels in content-desc, bounds as
"[x1,y1][x2,y2]") but with synthetic names.
"""

import pytest

from sortify import tabletshare as ts


def node(label="", text="", rid="", bounds="[0,0][100,100]"):
    return (
        f'<node index="0" text="{text}" resource-id="{rid}" '
        f'class="android.view.View" package="com.spotify.music" '
        f'content-desc="{label}" checkable="false" checked="false" '
        f'clickable="true" enabled="true" focusable="true" focused="false" '
        f'scrollable="false" long-clickable="false" password="false" '
        f'selected="false" bounds="{bounds}" />'
    )


def hierarchy(*nodes):
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
        "<hierarchy rotation=\"0\">" + "".join(nodes) + "</hierarchy>"
    )


# --- node_center -----------------------------------------------------------

def test_node_center_finds_a_content_desc_and_returns_its_middle():
    xml = hierarchy(node(label="Share", bounds="[100,900][500,1010]"))
    assert ts.node_center(xml, "Share") == (300, 955)


def test_node_center_matches_text_when_content_desc_is_empty():
    xml = hierarchy(node(text="Send", bounds="[0,0][200,100]"))
    assert ts.node_center(xml, "Send") == (100, 50)


def test_node_center_raises_ui_step_error_when_absent():
    with pytest.raises(ts.UiStepError, match="Share"):
        ts.node_center(hierarchy(node(label="Close")), "Share")


# --- share_targets ---------------------------------------------------------

SHARE_SHEET = hierarchy(
    node(label="Close", bounds="[560,800][640,894]"),
    node(label="Search", bounds="[150,1550][230,1654]"),
    node(label="Create group", bounds="[267,1560][347,1670]"),
    node(label="Share to Alice Example", bounds="[501,1560][581,1670]"),
    node(label="Share to bob99", bounds="[852,1550][932,1654]"),
    node(label="Copy share link", bounds="[150,1740][230,1848]"),
)


def test_share_targets_lists_friend_names_with_tap_points():
    assert ts.share_targets(SHARE_SHEET) == [
        ("Alice Example", (541, 1615)),
        ("bob99", (892, 1602)),
    ]


def test_share_targets_ignores_search_group_and_link_actions():
    names = [n for n, _ in ts.share_targets(SHARE_SHEET)]
    assert "Search" not in names and "Create group" not in names


# --- track_row_menu_point --------------------------------------------------

ALBUM_PAGE = hierarchy(
    node(text="Other Song", rid="com.spotify.music:id/title_text",
         bounds="[66,300][200,344]"),
    node(text="Found the Way", rid="com.spotify.music:id/title_text",
         bounds="[66,400][200,444]"),
)


def test_track_row_menu_point_is_the_dots_column_at_the_rows_height():
    assert ts.track_row_menu_point(ALBUM_PAGE, "Found the Way") == \
        (ts.ROW_MENU_X, 422)


def test_track_row_menu_point_raises_when_the_track_is_not_on_screen():
    with pytest.raises(ts.UiStepError, match="Missing Song"):
        ts.track_row_menu_point(ALBUM_PAGE, "Missing Song")


# --- share_track orchestration ---------------------------------------------

MENU_SHEET = hierarchy(
    # context-menu header: same text as the album row but NO resource-id —
    # that absence is what proves the sheet is open (album rows carry
    # id/title_text).
    node(text="Found the Way", bounds="[234,820][399,853]"),
)

COMPOSE = hierarchy(
    node(text="Send", bounds="[1000,1700][1160,1780]"),
)

NO_SEND = hierarchy(
    node(text="Say something", bounds="[100,1700][600,1780]"),
)


class FakeAdb:
    """Scripted adb: records every command, serves dumps in sequence."""

    def __init__(self, dumps):
        self.dumps = list(dumps)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args == ("shell", "cat /sdcard/claude_ui.xml"):
            return self.dumps.pop(0)
        return ""

    def taps(self):
        return [c[1] for c in self.calls
                if c[0] == "shell" and c[1].startswith("input tap")]


def share(dumps, friend="bob99", **kw):
    adb = FakeAdb(dumps)
    result = ts.share_track(
        "4zAS1w7phxmMtQNjNFJVou", "Found the Way", friend,
        run=adb, sleep=lambda s: None, **kw)
    return adb, result


def test_share_track_taps_row_menu_share_friend_then_send():
    adb, sent = share([ALBUM_PAGE, MENU_SHEET, SHARE_SHEET, COMPOSE])
    assert sent == ["Alice Example", "bob99"]
    assert adb.taps() == [
        f"input tap {ts.ROW_MENU_X} 422",       # the track row's ⋮
        f"input tap {ts.SHARE_ROW_X} 955",      # "Share" under the header
        "input tap 892 1602",                   # bob99's avatar
        "input tap 1080 1740",                  # Send
    ]


def test_share_track_deep_links_first_and_pauses_playback_after():
    adb, _ = share([ALBUM_PAGE, MENU_SHEET, SHARE_SHEET, COMPOSE])
    shells = [c[1] for c in adb.calls if c[0] == "shell"]
    assert "am start -a android.intent.action.VIEW " \
           "-d spotify:track:4zAS1w7phxmMtQNjNFJVou" in shells
    assert shells.index("input keyevent KEYCODE_MEDIA_PAUSE") > \
        shells.index("input tap 892 1602")


def test_share_track_refuses_an_unknown_friend_and_names_the_targets():
    with pytest.raises(ts.UiStepError, match="Alice Example"):
        share([ALBUM_PAGE, MENU_SHEET, SHARE_SHEET], friend="nobody")


def test_share_track_aborts_when_the_menu_never_opened():
    # second dump still shows the album page (row tap missed): the header
    # check must not be satisfied by the row's own id/title_text node
    with pytest.raises(ts.UiStepError):
        share([ALBUM_PAGE, ALBUM_PAGE])


def test_share_track_aborts_on_an_unmapped_compose_screen():
    with pytest.raises(ts.UiStepError, match="Send"):
        share([ALBUM_PAGE, MENU_SHEET, SHARE_SHEET, NO_SEND])


def test_share_track_pauses_playback_even_when_a_step_fails():
    adb = FakeAdb([ALBUM_PAGE, ALBUM_PAGE])
    with pytest.raises(ts.UiStepError):
        ts.share_track("id", "Found the Way", "bob99",
                       run=adb, sleep=lambda s: None)
    assert ("shell", "input keyevent KEYCODE_MEDIA_PAUSE") in adb.calls
    assert ("shell", "input keyevent KEYCODE_SLEEP") in adb.calls


def test_share_track_defaults_to_the_real_adb_runner(monkeypatch):
    adb = FakeAdb([ALBUM_PAGE, MENU_SHEET, SHARE_SHEET, COMPOSE])
    monkeypatch.setattr(ts, "_adb", adb)
    monkeypatch.setattr(ts.time, "sleep", lambda s: None)
    sent = ts.share_track("t1", "Found the Way", "bob99")
    assert sent == ["Alice Example", "bob99"]
