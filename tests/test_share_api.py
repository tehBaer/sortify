"""The tablet-share endpoints.

share_track is faked throughout: hitting the real thing wakes the tablet
and messages a real person. Under test: the endpoint→module contract, the
targets cache (share succeeds → data/tabletshare.json remembers the friend
list for the picker), and UI-step failures surfacing as 502, not 500.
Zero Spotify Web API calls anywhere in the flow.
"""

import json

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify import tabletshare

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)


@pytest.fixture()
def client():
    return TestClient(appmod.app)


def test_share_returns_ok_and_caches_the_targets(client, monkeypatch):
    def fake_share(track_id, title, friend, run=None, sleep=None):
        assert (track_id, title, friend) == ("t1", "Song", "bob99")
        return ["Alice Example", "bob99"]
    monkeypatch.setattr(tabletshare, "share_track", fake_share)

    r = client.post("/api/share/track", json={
        "track_id": "t1", "title": "Song", "friend": "bob99"})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "targets": ["Alice Example", "bob99"]}
    cached = json.loads((appmod.store.dir / "tabletshare.json").read_text())
    assert cached["targets"] == ["Alice Example", "bob99"]


def test_share_surfaces_a_ui_step_failure_as_502(client, monkeypatch):
    def fake_share(*a, **k):
        raise tabletshare.UiStepError("'nobody' is not a share target")
    monkeypatch.setattr(tabletshare, "share_track", fake_share)

    r = client.post("/api/share/track", json={
        "track_id": "t1", "title": "Song", "friend": "nobody"})

    assert r.status_code == 502
    assert "not a share target" in r.json()["detail"]


def test_targets_come_from_the_cache_without_touching_the_tablet(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GET /api/share/targets must not drive adb")
    monkeypatch.setattr(tabletshare, "share_track", boom)
    (appmod.store.dir / "tabletshare.json").write_text(
        json.dumps({"targets": ["bob99"], "updated": 123}))

    r = client.get("/api/share/targets")

    assert r.status_code == 200
    assert r.json() == {"targets": ["bob99"], "updated": 123}


def test_targets_are_empty_before_any_share_has_run(client):
    (appmod.store.dir / "tabletshare.json").unlink(missing_ok=True)
    r = client.get("/api/share/targets")
    assert r.status_code == 200
    assert r.json() == {"targets": [], "updated": None}
