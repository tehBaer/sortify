from sortify.folders import extract_folder_map
from sortify.spotify import Spotify

TREE = {
    "type": "folder",
    "children": [
        {"type": "playlist", "uri": "spotify:playlist:rootloose"},
        {
            "type": "folder",
            "name": "ELEKTRONISK",
            "children": [
                {"type": "playlist", "uri": "spotify:playlist:house1"},
                {
                    "type": "folder",
                    "name": "Dyp",
                    "children": [{"type": "playlist", "uri": "spotify:playlist:deep1"}],
                },
            ],
        },
        {
            "type": "folder",
            "name": "Turer 2026",
            "children": [{"type": "playlist", "uri": "spotify:playlist:trip1"}],
        },
    ],
}


def test_root_level_playlists_are_not_in_folders():
    assert "rootloose" not in extract_folder_map(TREE)


def test_caps_folder_marks_home_including_subfolders():
    m = extract_folder_map(TREE)
    assert m["house1"] == {"path": "ELEKTRONISK", "caps": True}
    assert m["deep1"]["path"] == "ELEKTRONISK / Dyp"
    assert m["deep1"]["caps"] is True


def test_normal_case_folder_is_not_home():
    m = extract_folder_map(TREE)
    assert m["trip1"]["caps"] is False


def test_select_home_ids_prefix_and_exclusion():
    from sortify.folders import select_home_ids

    mapping = {
        "a": {"path": "ROOT / Hazy", "caps": True},
        "b": {"path": "ROOT EX / DAENS", "caps": True},
        "c": {"path": "ROOT / Hominin / ARCHIVED", "caps": True},
        "d": {"path": "SUBSET / hybel", "caps": True},
        "e": {"path": "ROOT", "caps": True},
    }
    chosen = select_home_ids(mapping, ["ROOT"], ["ARCHIVED", "OLD"])
    assert chosen == {"a", "e"}  # not ROOT EX, not ARCHIVED, not SUBSET


def test_home_name_excluded_markers_and_emoji():
    from sortify.folders import home_name_excluded

    patterns = [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"]
    for name in ("__start__", "__stop__", "{alle sanger}", "<motor>", "🐾 subset"):
        assert home_name_excluded(name, patterns, emoji=True), name
    for name in ("THROTTLE BACK PSY", "start", "_understrek", "Y'NO, HAZE"):
        assert not home_name_excluded(name, patterns, emoji=True), name


def test_starts_with_emoji_marks_derived_playlists():
    from sortify.folders import starts_with_emoji

    assert starts_with_emoji("🔥 alt som lugger")
    assert starts_with_emoji("✨sammensatt")
    assert starts_with_emoji("⭐ topp")
    assert not starts_with_emoji("THROTTLE BACK PSY")
    assert not starts_with_emoji("Ærlig talt")  # Norwegian letters are not emoji
    assert not starts_with_emoji("[Hazy]")
    assert not starts_with_emoji("")


def test_slim_track_reads_both_item_and_track_keys():
    payload = {"uri": "spotify:track:x", "id": "x", "name": "N", "artists": [], "album": {}}
    via_item = Spotify._slim_track({"item": dict(payload), "added_at": "t"})
    via_track = Spotify._slim_track({"track": dict(payload), "added_at": "t"})
    assert via_item["uri"] == via_track["uri"] == "spotify:track:x"


def test_slim_track_keeps_album_release_date():
    # Era groundwork (2026-08-21): release_date rides the same playlist
    # fetches that already happen on snapshot change, so era coverage
    # accrues at zero extra API cost. Absent (old cache entries, local
    # files) degrades to None.
    payload = {"uri": "spotify:track:x", "id": "x", "name": "N", "artists": [],
               "album": {"name": "A", "release_date": "2019-05-03"}}
    slim = Spotify._slim_track({"item": dict(payload), "added_at": "t"})
    assert slim["release_date"] == "2019-05-03"
    bare = Spotify._slim_track({"item": {"uri": "u", "album": {}}, "added_at": "t"})
    assert bare["release_date"] is None


def test_item_fields_filter_requests_release_date():
    from sortify.spotify import ITEM_FIELDS
    assert "release_date" in ITEM_FIELDS
