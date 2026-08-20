"""The naming endpoints, network faked out.

As with the split API tests, the call budget is part of the contract:
GET /api/naming must be free (cached listing only), and a rename must be
exactly one PUT through the client.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod

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
