"""The Feb-2026 dev-mode API accepts up to 100 uris per playlist-add POST
(probed 2026-08-23 — docs/superpowers/specs/2026-08-23-faster-splits-design.md).
Pins the client's batch form and the bulk-accounting passthrough on the
paged read, at the request() seam — zero live calls."""

import pytest

import sortify.app as appmod
from liveguard import assert_not_live_data
assert_not_live_data(appmod.store.dir)

from sortify.spotify import Spotify
from sortify.store import Store


@pytest.fixture
def client(monkeypatch):
    sp = Spotify(Store())
    calls = []

    def fake_request(method, path, background=False, bulk=False,
                     spend_reserve=False, **kw):
        calls.append({"method": method, "path": path, "bulk": bulk,
                      "spend_reserve": spend_reserve, "json": kw.get("json"),
                      "params": kw.get("params")})
        if method == "GET" and path.endswith("/items"):
            return {"items": [
                {"item": {"uri": f"spotify:track:t{i}", "name": f"t{i}",
                          "artists": []}}
                for i in range(3)]}
        return {"snapshot_id": "snap"}

    monkeypatch.setattr(sp, "request", fake_request)
    sp.calls = calls
    return sp


def test_add_single_uri_still_sends_a_list(client):
    client.add_to_playlist("P1", "spotify:track:a")
    assert client.calls[-1]["json"] == {"uris": ["spotify:track:a"]}


def test_add_batch_sends_all_uris_in_one_call(client):
    uris = [f"spotify:track:b{i}" for i in range(100)]
    client.add_to_playlist("P1", uris, bulk=True, spend_reserve=True)
    assert len(client.calls) == 1
    assert client.calls[0]["json"] == {"uris": uris}
    assert client.calls[0]["bulk"] is True
    assert client.calls[0]["spend_reserve"] is True


def test_add_batch_over_100_refuses_before_spending(client):
    with pytest.raises(ValueError):
        client.add_to_playlist("P1", [f"spotify:track:c{i}" for i in range(101)])
    assert client.calls == []


def test_playlist_tracks_passes_bulk_through_to_request(client):
    tracks = client.playlist_tracks("P1", bulk=True, spend_reserve=True)
    assert [t["uri"] for t in tracks] == [f"spotify:track:t{i}" for i in range(3)]
    assert all(c["bulk"] for c in client.calls)
    assert all(c["spend_reserve"] for c in client.calls)


def test_playlist_tracks_default_stays_interactive(client):
    client.playlist_tracks("P1")
    assert all(not c["bulk"] for c in client.calls)
