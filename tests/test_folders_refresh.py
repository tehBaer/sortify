"""POST /api/folders/refresh — the on-box folder tree re-import.

The sync and extraction are faked: tests must never launch the real client
(spawns Xvfb + snap) or read a real cache. The contract under test is the
orchestration — sync unless told not to, extraction failures surface as 503
not 500, the mapping flows through the same ingest tail as POST /api/folders,
and the diff/freshness fields the button renders are computed correctly.
Zero Spotify Web API calls throughout, enforced by the fake listing counter.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify import rootlist

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "h1", "name": "HAZE", "owner": "me", "editable": True,
     "total": 4, "snapshot_id": "s1", "image": None},
    {"id": "h2", "name": "DAWN", "owner": "me", "editable": True,
     "total": 9, "snapshot_id": "s2", "image": None},
    {"id": "i1", "name": "[inn]", "owner": "me", "editable": True,
     "total": 2, "snapshot_id": "s3", "image": None},
]

TREE = {
    "type": "folder",
    "children": [
        {"type": "folder", "name": "ROOT", "children": [
            {"type": "playlist", "uri": "spotify:playlist:h1"},
            {"type": "folder", "name": "Late", "children": [
                {"type": "playlist", "uri": "spotify:playlist:h2"},
            ]},
        ]},
    ],
}


@pytest.fixture()
def client(monkeypatch):
    calls = {"listing": 0, "synced": 0}

    def fake_my_playlists(refresh=False):
        calls["listing"] += 1
        assert not refresh, "refresh endpoint must never force a listing re-fetch"
        return [dict(p) for p in LISTING]

    monkeypatch.setattr(appmod.sp, "my_playlists", fake_my_playlists)
    monkeypatch.setattr(rootlist, "sync_client",
                        lambda seconds=None: calls.__setitem__("synced", calls["synced"] + 1))
    monkeypatch.setattr(rootlist, "extract_tree", lambda: TREE)
    monkeypatch.setattr(rootlist, "cache_mtime", lambda: "2026-08-23T10:00:00Z")
    appmod.store.update_config(
        input_ids=["i1"], home_ids=[], sticky_home_ids=[],
        home_folder_prefixes=["ROOT"], home_folder_exclude=["ARCHIVED", "OLD"],
        home_name_exclude_patterns=[], home_exclude_emoji_names=False,
    )
    appmod.store.save_folders({})
    c = TestClient(appmod.app)
    c.calls = calls
    return c


def test_refresh_syncs_extracts_and_marks_homes(client):
    resp = client.post("/api/folders/refresh", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert client.calls["synced"] == 1
    assert data["playlists_in_folders"] == 2
    assert data["homes_marked"] == 2
    assert data["tree_as_of"] == "2026-08-23T10:00:00Z"
    assert data["added"] == 2 and data["moved"] == 0 and data["dropped"] == 0
    assert appmod.store.folders()["h2"]["path"] == "ROOT / Late"
    assert appmod.store.config()["home_ids"] == ["h1", "h2"]


def test_sync_false_skips_the_client_run(client):
    resp = client.post("/api/folders/refresh", json={"sync": False})
    assert resp.status_code == 200
    assert client.calls["synced"] == 0


def test_diff_counts_against_previous_mapping(client):
    appmod.store.save_folders({
        "h1": {"path": "ROOT", "caps": True},          # unchanged
        "h2": {"path": "ROOT / Old", "caps": True},    # moved
        "gone": {"path": "ROOT / Gone", "caps": True},  # dropped
    })
    data = client.post("/api/folders/refresh", json={"sync": False}).json()
    assert (data["added"], data["moved"], data["dropped"]) == (0, 1, 1)


def test_extraction_failure_is_503_and_keeps_old_mapping(client, monkeypatch):
    appmod.store.save_folders({"h1": {"path": "ROOT", "caps": True}})

    def boom():
        raise RuntimeError("no rootlist in cache")

    monkeypatch.setattr(rootlist, "extract_tree", boom)
    resp = client.post("/api/folders/refresh", json={"sync": False})
    assert resp.status_code == 503
    assert "rootlist" in resp.json()["detail"]
    assert appmod.store.folders() == {"h1": {"path": "ROOT", "caps": True}}


def test_empty_tree_is_503_and_keeps_old_mapping(client, monkeypatch):
    appmod.store.save_folders({"h1": {"path": "ROOT", "caps": True}})
    monkeypatch.setattr(rootlist, "extract_tree",
                        lambda: {"type": "folder", "children": []})
    resp = client.post("/api/folders/refresh", json={"sync": False})
    assert resp.status_code == 503
    assert appmod.store.folders() == {"h1": {"path": "ROOT", "caps": True}}


def test_concurrent_refresh_is_409(client):
    assert appmod._folders_refresh_lock.acquire(blocking=False)
    try:
        resp = client.post("/api/folders/refresh", json={"sync": False})
        assert resp.status_code == 409
    finally:
        appmod._folders_refresh_lock.release()


def test_manual_ingest_still_works_through_shared_tail(client):
    resp = client.post("/api/folders", json=TREE)
    assert resp.status_code == 200
    data = resp.json()
    assert data["homes_marked"] == 2
    assert "tree_as_of" not in data  # freshness stamp is refresh-only
