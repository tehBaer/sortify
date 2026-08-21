"""Creating home playlists from inside sortify.

Spec: docs/superpowers/specs/2026-08-21-create-home-playlists-design.md.
Everything here is zero-Spotify-call: fake transports and monkeypatched
clients only.
"""

from sortify.folders import creatable_home_name_problem

INPUT_PAT = r"^\[.+\]$"
EXCLUDES = [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"]


def problem(name):
    return creatable_home_name_problem(
        name, input_pattern=INPUT_PAT, exclude_patterns=EXCLUDES, exclude_emoji=True
    )


def test_ordinary_names_are_creatable():
    for name in ("Late Night", "HAZE 2", "Ærlig talt", "  padded  "):
        assert problem(name) is None, name


def test_input_shaped_names_are_refused_as_would_be_inputs():
    # The pattern union in _effective_input_ids beats the home_ids config
    # list, so "[Foo]" would become an input on the very next request.
    msg = problem("[Foo]")
    assert msg and "input" in msg


def test_home_excluded_shapes_are_refused():
    for name in ("{alle sanger}", "<motor>", "__start__", "🐾 subset", "🔈 haze"):
        assert problem(name), name


def test_empty_and_whitespace_names_are_refused():
    assert problem("")
    assert problem("   ")


def test_no_input_pattern_configured_skips_that_check():
    assert creatable_home_name_problem(
        "[Foo]", input_pattern=None, exclude_patterns=EXCLUDES, exclude_emoji=True
    ) is None


# ---- playlist cache mutations (remember, forget) ---------------------------

import pytest

from sortify.spotify import Spotify
from sortify.store import Store


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.status_code = status
        self.content = b"{}"
        self.headers = {}
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def sp(tmp_path, monkeypatch):
    client = Spotify(Store(tmp_path))
    monkeypatch.setattr(client, "_access_token", lambda: "token")
    monkeypatch.setattr(client, "_last_call", 0.0)
    client.store.save_cache({
        "playlists": {}, "artists": {}, "me": {"id": "me"},
        "playlist_list": {"fetched_at": 1.0, "items": [
            {"id": "old1", "name": "Existing", "owner": "me", "editable": True,
             "total": 3, "snapshot_id": "snap-old1", "image": None, "description": ""},
        ]},
    })
    return client


def item(pid, name="New Home"):
    return {"id": pid, "name": name, "owner": "me", "editable": True,
            "total": 0, "snapshot_id": f"created:{pid}", "image": None,
            "description": ""}


def test_remember_playlist_appears_in_listing_with_no_http(sp, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("remember_playlist must never touch the network")
    monkeypatch.setattr(sp.http, "request", boom)
    sp.remember_playlist(item("new1"))
    ids = [p["id"] for p in sp.my_playlists()]
    assert ids == ["new1", "old1"]


def test_remember_then_forget_round_trips(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    sp.remember_playlist(item("new1"))
    sp.forget_playlists({"new1"})
    assert [p["id"] for p in sp.my_playlists()] == ["old1"]


def test_remember_is_idempotent(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    sp.remember_playlist(item("new1"))
    sp.remember_playlist(item("new1"))
    assert [p["id"] for p in sp.my_playlists()].count("new1") == 1


def test_remember_without_a_cached_listing_is_a_noop(tmp_path, monkeypatch):
    client = Spotify(Store(tmp_path))
    client.remember_playlist(item("new1"))  # must not raise, must not fetch
    assert (client.store.cache().get("playlist_list") or {}) in ({}, None) or \
        client.store.cache()["playlist_list"] is None


def test_create_playlist_full_returns_id_and_snapshot(sp, monkeypatch):
    sent = []
    def fake_request(method, url, **kwargs):
        sent.append((method, url))
        return FakeResponse({"id": "fresh", "snapshot_id": "snap-1"}, status=201)
    monkeypatch.setattr(sp.http, "request", fake_request)
    assert sp.create_playlist_full("Late Night") == ("fresh", "snap-1")
    assert sent == [("POST", "https://api.spotify.com/v1/me/playlists")]


def test_create_playlist_full_snapshot_is_none_when_absent(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "request",
                        lambda *a, **k: FakeResponse({"id": "fresh"}, status=201))
    assert sp.create_playlist_full("Late Night") == ("fresh", None)


def test_create_playlist_still_returns_bare_id(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "request",
                        lambda *a, **k: FakeResponse({"id": "fresh"}, status=201))
    assert sp.create_playlist("Late Night") == "fresh"
