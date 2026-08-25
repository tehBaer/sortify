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

SEARCH_PAGE = hierarchy(
    # search results: rows carry id/title + id/subtitle. The remix row
    # exists to prove matching is exact, not substring.
    node(text="Found the Way", rid="com.spotify.music:id/title",
         bounds="[66,190][1130,234]"),
    node(text="Song • Lab's Cloud", rid="com.spotify.music:id/subtitle",
         bounds="[66,234][1104,258]"),
    node(text="Found the Way - Abstract Seeds Remix",
         rid="com.spotify.music:id/title", bounds="[66,286][1104,330]"),
)


def test_search_row_point_finds_the_exact_title():
    assert ts.search_row_point(SEARCH_PAGE, "Found the Way") == (598, 212)


def test_search_row_point_raises_when_the_track_is_not_in_results():
    with pytest.raises(ts.UiStepError, match="Missing Song"):
        ts.search_row_point(SEARCH_PAGE, "Missing Song")


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
        "Found the Way", "Lab's Cloud", friend,
        run=adb, sleep=lambda s: None, **kw)
    return adb, result


def test_share_track_long_presses_the_row_then_share_friend_send():
    adb, sent = share([SEARCH_PAGE, MENU_SHEET, SHARE_SHEET, COMPOSE])
    assert sent == ["Alice Example", "bob99"]
    shells = [c[1] for c in adb.calls if c[0] == "shell"]
    assert "input swipe 598 212 598 212 800" in shells   # long-press the row
    assert adb.taps() == [
        f"input tap {ts.SHARE_ROW_X} 955",      # "Share" under the header
        "input tap 892 1602",                   # bob99's avatar
        "input tap 1080 1740",                  # Send
    ]


def test_share_track_opens_search_not_a_track_link():
    # A track VIEW intent autoplays and steals playback via Connect
    # (measured live 2026-08-24, both spotify: and https forms); the
    # search link is the only entry that leaves playback alone.
    adb, _ = share([SEARCH_PAGE, MENU_SHEET, SHARE_SHEET, COMPOSE])
    shells = [c[1] for c in adb.calls if c[0] == "shell"]
    assert "am start -a android.intent.action.VIEW " \
           "-d spotify:search:Found%20the%20Way%20Lab%27s%20Cloud" in shells
    assert not any("spotify:track" in s for s in shells)


def test_share_track_refuses_an_unknown_friend_and_names_the_targets():
    with pytest.raises(ts.UiStepError, match="Alice Example"):
        share([SEARCH_PAGE, MENU_SHEET, SHARE_SHEET], friend="nobody")


def test_share_track_aborts_when_the_menu_never_opened():
    # second dump still shows the results page (long-press missed): the
    # header check must not be satisfied by the row's own id/title node
    with pytest.raises(ts.UiStepError):
        share([SEARCH_PAGE, SEARCH_PAGE])


def test_share_track_aborts_on_an_unmapped_compose_screen():
    with pytest.raises(ts.UiStepError, match="Send"):
        share([SEARCH_PAGE, MENU_SHEET, SHARE_SHEET, NO_SEND])


def test_share_track_sends_no_media_key_at_all():
    # A media key sent to the tablet reaches Spotify — the only active
    # media session and the registered MediaButtonReceiver — which is a
    # Connect controller for whatever the user is really listening on
    # (volumeType=2 REMOTE, measured 2026-08-25). A cleanup pause left
    # over from the track-link era therefore paused the USER's music.
    # Search entry autoplays nothing, so no media key is ever warranted.
    adb, _ = share([SEARCH_PAGE, MENU_SHEET, SHARE_SHEET, COMPOSE])
    keys = [c[1] for c in adb.calls if "keyevent" in c[1]]
    assert not any("MEDIA" in k for k in keys), keys


def test_share_track_sends_no_media_key_when_a_step_fails_either():
    adb = FakeAdb([SEARCH_PAGE, SEARCH_PAGE])
    with pytest.raises(ts.UiStepError):
        ts.share_track("Found the Way", "Lab's Cloud", "bob99",
                       run=adb, sleep=lambda s: None)
    keys = [c[1] for c in adb.calls if "keyevent" in c[1]]
    assert not any("MEDIA" in k for k in keys), keys
    # the screen still goes back to sleep — that touches no playback
    assert ("shell", "input keyevent KEYCODE_SLEEP") in adb.calls


def test_share_track_defaults_to_the_real_adb_runner(monkeypatch):
    adb = FakeAdb([SEARCH_PAGE, MENU_SHEET, SHARE_SHEET, COMPOSE])
    monkeypatch.setattr(ts, "_adb", adb)
    monkeypatch.setattr(ts.time, "sleep", lambda s: None)
    sent = ts.share_track("Found the Way", "Lab's Cloud", "bob99")
    assert sent == ["Alice Example", "bob99"]


def test_share_track_retries_the_first_dump_while_the_app_cold_starts():
    # Spotify cold-starting: two dumps of nothing before the album page
    # renders. One fixed sleep was measured too short on the real tablet
    # (2026-08-24, first live send) — the flow must poll, not hope.
    blank = hierarchy(node(text="Loading", bounds="[0,0][10,10]"))
    adb, sent = share([blank, blank, SEARCH_PAGE, MENU_SHEET, SHARE_SHEET, COMPOSE])
    assert sent == ["Alice Example", "bob99"]


def test_share_track_still_aborts_when_the_track_never_appears():
    blank = hierarchy(node(text="Loading", bounds="[0,0][10,10]"))
    with pytest.raises(ts.UiStepError, match="Found the Way"):
        share([blank] * (ts.OPEN_TRIES + 1))


# --- picker presentation: groups hidden, aliases applied --------------------

def test_a_multi_person_target_counts_as_a_group():
    assert ts.is_group("Jonas Sandberg, mariussunde +4")
    assert ts.is_group("Alice Example, bob99")


def test_a_single_person_or_number_is_not_a_group():
    assert not ts.is_group("Michael Moen Allport")
    assert not ts.is_group("11161517688")


def test_display_targets_hides_groups_and_applies_aliases():
    raw = ["Jonas Sandberg, mariussunde +4", "11161517688", "soppis96"]
    assert ts.display_targets(raw, {"11161517688": "Mara"}) == \
        ["Mara", "soppis96"]


def test_resolve_target_maps_a_display_name_back_to_the_raw_name():
    aliases = {"11161517688": "Mara"}
    assert ts.resolve_target("Mara", aliases) == "11161517688"
    assert ts.resolve_target("soppis96", aliases) == "soppis96"


# --- adb reconnect ---------------------------------------------------------
# The tablet's adb link is not durable: Wi-Fi doze drops it and every
# command then fails "device '...' not found" until someone reconnects by
# hand (seen live 2026-08-25). The runner heals that itself.

class FlakyAdb:
    """adb that reports the device missing until `connect` is called."""

    def __init__(self):
        self.connected = False
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:1] == ("connect",):
            self.connected = True
            return "connected to " + ts.TABLET
        if not self.connected:
            raise ts.DeviceOffline(
                f"adb: device '{ts.TABLET}' not found")
        return "ok"


def test_adb_reconnects_once_then_retries_the_command():
    flaky = FlakyAdb()
    out = ts._adb_with_reconnect("shell", "input keyevent KEYCODE_WAKEUP",
                                 attempt=flaky)
    assert out == "ok"
    assert flaky.calls == [
        ("shell", "input keyevent KEYCODE_WAKEUP"),   # fails, device gone
        ("connect", ts.TABLET),                       # heal
        ("shell", "input keyevent KEYCODE_WAKEUP"),   # succeeds
    ]


def test_adb_gives_up_with_a_useful_message_when_the_tablet_is_really_off():
    def always_offline(args):
        raise ts.DeviceOffline(f"adb: device '{ts.TABLET}' not found")
    with pytest.raises(ts.UiStepError, match="tablet is not reachable"):
        ts._adb_with_reconnect("shell", "true", attempt=always_offline)


def test_a_reachable_tablet_is_not_reconnected_needlessly():
    calls = []
    def fine(args):
        calls.append(args)
        return "ok"
    ts._adb_with_reconnect("shell", "true", attempt=fine)
    assert calls == [("shell", "true")]
