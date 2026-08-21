"""The naming endpoints, network faked out.

As with the split API tests, the call budget is part of the contract:
GET /api/naming must be free (cached listing only), and a rename must be
exactly one PUT through the client.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "h1", "name": "beach vibes", "owner": "me", "editable": True,
     "total": 40, "snapshot_id": "s-h1", "image": None},
    {"id": "i1", "name": "new finds", "owner": "me", "editable": True,
     "total": 10, "snapshot_id": "s-i1", "image": None},
    {"id": "ok", "name": "ALREADY FINE", "owner": "me", "editable": True,
     "total": 5, "snapshot_id": "s-ok", "image": None},
]


@pytest.fixture()
def client(monkeypatch):
    calls = {"refreshes": 0}

    def fake_my_playlists(refresh=False):
        if refresh:
            calls["refreshes"] += 1
        return [dict(p) for p in LISTING]

    monkeypatch.setattr(appmod.sp, "my_playlists", fake_my_playlists)
    appmod.store.update_config(input_ids=["i1"], home_ids=["h1", "ok"])
    c = TestClient(appmod.app)
    c.calls = calls
    return c


def test_naming_lists_violations(client):
    resp = client.get("/api/naming")
    assert resp.status_code == 200
    rows = resp.json()["violations"]
    assert {r["playlist_id"]: r["proposed"] for r in rows} == {
        "h1": "BEACH VIBES", "i1": "[new finds]",
    }


def test_naming_never_refreshes_the_listing(client):
    client.get("/api/naming")
    assert client.calls["refreshes"] == 0


def test_rename_applies_the_proposal(client, monkeypatch):
    renamed = []
    monkeypatch.setattr(appmod.sp, "rename_playlist",
                        lambda pid, name: renamed.append((pid, name)))
    resp = client.post("/api/naming/h1/rename")
    assert resp.status_code == 200
    assert renamed == [("h1", "BEACH VIBES")]
    assert resp.json()["renamed"] == {
        "playlist_id": "h1", "from": "beach vibes", "to": "BEACH VIBES"}


def test_rename_conforming_playlist_is_409(client, monkeypatch):
    # A stale tab must not rename something already fixed: the server
    # recomputes from the cached listing and refuses when nothing is wrong.
    monkeypatch.setattr(appmod.sp, "rename_playlist",
                        lambda pid, name: pytest.fail("must not spend a call"))
    assert client.post("/api/naming/ok/rename").status_code == 409
    assert client.post("/api/naming/nonexistent/rename").status_code == 409


def test_rename_playlist_patches_the_cached_listing(monkeypatch):
    # The client method itself: one PUT, then the cached name changes so
    # the UI shows the result without a paid Refresh.
    put = []
    monkeypatch.setattr(
        appmod.sp, "request",
        lambda method, path, **kw: put.append((method, path, kw.get("json"))))
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 1.0, "items": [
        {"id": "h1", "name": "beach vibes", "owner": "me", "editable": True,
         "total": 40, "snapshot_id": "s-h1", "image": None}]}
    appmod.store.save_cache(cache)

    appmod.sp.rename_playlist("h1", "BEACH VIBES")

    assert put == [("PUT", "/playlists/h1", {"name": "BEACH VIBES"})]
    items = appmod.store.cache()["playlist_list"]["items"]
    assert items[0]["name"] == "BEACH VIBES"
