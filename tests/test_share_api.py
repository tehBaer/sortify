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
    def fake_share(title, artist, friend, run=None, sleep=None):
        assert (title, artist, friend) == ("Song", "Artist", "bob99")
        return ["Alice Example", "bob99"]
    monkeypatch.setattr(tabletshare, "share_track", fake_share)

    r = client.post("/api/share/track", json={
        "title": "Song", "artist": "Artist", "friend": "bob99"})

    assert r.status_code == 200
    assert r.json() == {"ok": True, "targets": ["Alice Example", "bob99"]}
    cached = json.loads((appmod.store.dir / "tabletshare.json").read_text())
    assert cached["targets"] == ["Alice Example", "bob99"]


def test_share_surfaces_a_ui_step_failure_as_502(client, monkeypatch):
    def fake_share(*a, **k):
        raise tabletshare.UiStepError("'nobody' is not a share target")
    monkeypatch.setattr(tabletshare, "share_track", fake_share)

    r = client.post("/api/share/track", json={
        "title": "Song", "artist": "Artist", "friend": "nobody"})

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


def test_share_accepts_an_alias_and_drives_the_raw_name(client, monkeypatch):
    (appmod.store.dir / "tabletshare.json").write_text(json.dumps(
        {"targets": ["11161517688"], "updated": 1,
         "aliases": {"11161517688": "Mara"}}))
    seen = {}
    def fake_share(title, artist, friend, run=None, sleep=None):
        seen["friend"] = friend
        return ["11161517688", "soppis96"]
    monkeypatch.setattr(tabletshare, "share_track", fake_share)

    r = client.post("/api/share/track", json={
        "title": "Song", "artist": "Artist", "friend": "Mara"})

    assert r.status_code == 200
    assert seen["friend"] == "11161517688"


def test_a_share_rewrite_keeps_the_aliases(client, monkeypatch):
    (appmod.store.dir / "tabletshare.json").write_text(json.dumps(
        {"targets": ["old"], "updated": 1,
         "aliases": {"11161517688": "Mara"}}))
    monkeypatch.setattr(tabletshare, "share_track",
                        lambda *a, **k: ["11161517688"])

    client.post("/api/share/track", json={
        "title": "Song", "artist": "Artist", "friend": "Mara"})

    cached = json.loads((appmod.store.dir / "tabletshare.json").read_text())
    assert cached["aliases"] == {"11161517688": "Mara"}
    assert cached["targets"] == ["11161517688"]


def test_targets_endpoint_hides_groups_and_shows_aliases(client):
    (appmod.store.dir / "tabletshare.json").write_text(json.dumps(
        {"targets": ["Jonas Sandberg, mariussunde +4", "11161517688",
                     "soppis96"],
         "updated": 5, "aliases": {"11161517688": "Mara"}}))

    r = client.get("/api/share/targets")

    assert r.json() == {"targets": ["Mara", "soppis96"], "updated": 5}


def test_a_failed_share_logs_the_step_for_forensics(client, monkeypatch, caplog):
    # The uvicorn access log alone carries no detail on a 502 (proven by
    # the first live send: the log tail showed only the request line), so
    # the endpoint must record the failing step itself.
    def fake_share(*a, **k):
        raise tabletshare.UiStepError("context sheet for 'X' did not open")
    monkeypatch.setattr(tabletshare, "share_track", fake_share)

    with caplog.at_level("WARNING"):
        client.post("/api/share/track", json={
            "title": "X", "artist": "Y", "friend": "soppis96"})

    assert any("context sheet" in r.message for r in caplog.records)
