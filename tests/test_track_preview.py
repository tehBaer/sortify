"""Tapping a song in the split plays its 30 s Deezer preview. Zero Spotify
calls; the Deezer client is faked (conftest blocks the real one)."""

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

TRACK = {"uri": "spotify:track:t1", "name": "Song One",
         "artists": [{"id": "a1", "name": "Artist One"}], "is_local": False}


@pytest.fixture
def client(monkeypatch):
    appmod.store.save_splitparts({"version": 1, "drafts": {"H1": {
        "playlist_id": "H1", "tracks": [TRACK], "parts": [], "songs": {}}}})
    appmod.store.save_preview_rejects({"spotify:track:t1": {"rejected": [77]}})
    asked = []

    class FakeDeezer:
        def fetch_preview(self, artist, title, exclude=()):
            asked.append((artist, title, list(exclude)))
            return {"url": "https://cdn/clip.mp3", "deezer_id": 5, "miss": False}

    monkeypatch.setattr(appmod, "_deezer_client", lambda: FakeDeezer())
    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.asked = asked
    return c


def test_a_song_resolves_to_its_clip(client):
    res = client.get("/api/track_preview", params={"pid": "H1", "uri": "spotify:track:t1"})

    assert res.json() == {"url": "https://cdn/clip.mp3", "uri": "spotify:track:t1", "deezer_id": 5}
    assert client.asked == [("Artist One", "Song One", [77])]


def test_no_clip_is_a_404(client, monkeypatch):
    class Miss:
        def fetch_preview(self, artist, title, exclude=()):
            return {"miss": True}

    monkeypatch.setattr(appmod, "_deezer_client", lambda: Miss())

    res = client.get("/api/track_preview", params={"pid": "H1", "uri": "spotify:track:t1"})

    assert res.status_code == 404
    assert res.json()["detail"] == "no preview"


def test_a_deezer_error_is_a_404_not_a_500(client, monkeypatch):
    class Broken:
        def fetch_preview(self, artist, title, exclude=()):
            raise RuntimeError("deezer down")

    monkeypatch.setattr(appmod, "_deezer_client", lambda: Broken())

    assert client.get("/api/track_preview",
                      params={"pid": "H1", "uri": "spotify:track:t1"}).status_code == 404


def test_a_song_outside_the_split_is_refused(client):
    res = client.get("/api/track_preview", params={"pid": "H1", "uri": "spotify:track:zz"})

    assert res.status_code == 404
    assert client.asked == []
