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


def test_remember_without_a_cached_listing_still_seeds_the_track_cache(tmp_path, monkeypatch):
    client = Spotify(Store(tmp_path))
    monkeypatch.setattr(client.http, "request",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    client.remember_playlist(item("new1"))  # must not raise, must not fetch
    # The listing half stays a no-op: nothing cached to keep current.
    assert (client.store.cache().get("playlist_list") or {}).get("items") is None
    # The track-cache seed always runs — that's what keeps profile rebuilds
    # at zero calls whether or not a listing was cached (spec §3).
    entry = client.store.cache()["playlists"]["new1"]
    assert entry["tracks"] == [] and entry["snapshot_id"] == "created:new1"


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


# ---- sticky home ids (Task 3) ----------------------------------------------

from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)


@pytest.fixture
def client(monkeypatch):
    return TestClient(appmod.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _clean_profile_state():
    """The leak-guard and profile-clear tests mutate module globals
    (_profile_state) directly; reset them after every test so a run order
    can't leak stale profile state into an unrelated test."""
    yield
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0


LISTING = [
    {"id": "tree1", "name": "Hazy", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s1", "image": None, "description": ""},
    {"id": "made1", "name": "Late Night", "owner": "me", "editable": True,
     "total": 0, "snapshot_id": "created:made1", "image": None, "description": ""},
]

TREE = {"type": "folder", "children": [
    {"type": "folder", "name": "ROOT", "children": [
        {"type": "playlist", "uri": "spotify:playlist:tree1"}]},
]}


def _seed_config(**extra):
    appmod.store.save_config({
        "client_id": "x", "input_ids": [], "home_ids": [],
        "home_folder_prefixes": ["ROOT"], "home_folder_exclude": [],
        "input_name_pattern": r"^\[.+\]$",
        "home_exclude_emoji_names": True,
        "home_name_exclude_patterns": [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"],
        **extra,
    })


def test_folder_ingest_keeps_sticky_homes_the_tree_never_saw(client, monkeypatch):
    _seed_config(home_ids=["made1"], sticky_home_ids=["made1"])
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    res = client.post("/api/folders", json=TREE)
    assert res.status_code == 200
    assert sorted(appmod.store.config()["home_ids"]) == ["made1", "tree1"]


def test_folder_ingest_still_filters_sticky_by_editable_and_inputs(client, monkeypatch):
    _seed_config(sticky_home_ids=["ghost", "made1"], input_ids=["made1"])
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    client.post("/api/folders", json=TREE)
    # "ghost" is not in the listing (not editable), "made1" is an input now.
    assert appmod.store.config()["home_ids"] == ["tree1"]


def test_switching_home_off_also_drops_sticky_so_ingest_cannot_resurrect(client, monkeypatch):
    _seed_config(home_ids=["made1", "tree1"], sticky_home_ids=["made1"])
    res = client.post("/api/config", json={
        "input_ids": [], "home_ids": ["tree1"], "home_hints": {}})
    assert res.status_code == 200
    assert appmod.store.config()["sticky_home_ids"] == []
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    client.post("/api/folders", json=TREE)
    assert appmod.store.config()["home_ids"] == ["tree1"]


# ---- POST /api/playlists/create (Task 4) -----------------------------------


def _wire_create(monkeypatch, snapshot="snap-new"):
    """Fake the two client methods the endpoint spends/uses; count creates."""
    calls = {"create": 0}
    def fake_full(name, description="", bulk=False, spend_reserve=False):
        calls["create"] += 1
        return "made1", snapshot
    monkeypatch.setattr(appmod.sp, "create_playlist_full", fake_full)
    monkeypatch.setattr(appmod.sp, "my_playlists",
                        lambda refresh=False: list(LISTING[:1]))
    return calls


def _seed_cache_with_listing():
    appmod.store.save_cache({
        "playlists": {}, "artists": {}, "me": {"id": "me"},
        "playlist_list": {"fetched_at": 1.0, "items": list(LISTING[:1])},
    })


def test_create_refuses_bad_names_before_spending(client, monkeypatch):
    _seed_config()
    calls = _wire_create(monkeypatch)
    for bad in ("[Foo]", "{x}", "🐾 sub", "  "):
        res = client.post("/api/playlists/create", json={"name": bad, "role": "home"})
        assert res.status_code == 400, bad
    assert calls["create"] == 0


def test_create_refuses_non_home_roles(client, monkeypatch):
    _seed_config()
    calls = _wire_create(monkeypatch)
    res = client.post("/api/playlists/create", json={"name": "Ok", "role": "input"})
    assert res.status_code == 400
    assert calls["create"] == 0


def test_create_marks_home_and_sticky_and_seeds_the_track_cache(client, monkeypatch):
    _seed_config()
    _seed_cache_with_listing()
    _wire_create(monkeypatch, snapshot="snap-new")
    res = client.post("/api/playlists/create", json={"name": "Late Night", "role": "home"})
    assert res.status_code == 200
    cfg = appmod.store.config()
    assert "made1" in cfg["home_ids"] and "made1" in cfg["sticky_home_ids"]
    entry = appmod.store.cache()["playlists"]["made1"]
    assert entry["tracks"] == [] and entry["snapshot_id"] == "snap-new"
    # And the listing entry carries the same snapshot — the equality is the
    # whole point (spec §3).
    listed = next(p for p in appmod.store.cache()["playlist_list"]["items"]
                  if p["id"] == "made1")
    assert listed["snapshot_id"] == "snap-new"
    assert res.json()["playlist"]["role"] == "home"


def test_create_without_a_response_snapshot_seeds_the_sentinel(client, monkeypatch):
    _seed_config()
    _seed_cache_with_listing()
    _wire_create(monkeypatch, snapshot=None)
    client.post("/api/playlists/create", json={"name": "Late Night", "role": "home"})
    entry = appmod.store.cache()["playlists"]["made1"]
    listed = next(p for p in appmod.store.cache()["playlist_list"]["items"]
                  if p["id"] == "made1")
    assert entry["snapshot_id"] == listed["snapshot_id"] == "created:made1"


def test_duplicate_names_are_allowed_with_a_note(client, monkeypatch):
    _seed_config()
    _seed_cache_with_listing()
    _wire_create(monkeypatch)
    res = client.post("/api/playlists/create", json={"name": "Hazy", "role": "home"})
    assert res.status_code == 200
    assert res.json()["note"]  # "already exists" note, creation not refused


def test_create_clears_the_profile_cache(client, monkeypatch):
    _seed_config()
    _seed_cache_with_listing()
    _wire_create(monkeypatch)
    appmod._profile_state.update(built_at=9e12, profiles={"stale": None})
    client.post("/api/playlists/create", json={"name": "Late Night", "role": "home"})
    assert appmod._profile_state.get("built_at") == 0.0
    assert "profiles" not in appmod._profile_state


def test_leak_guard_created_home_costs_exactly_one_call_ever(client, monkeypatch):
    """THE test for the snapshot trap (spec §3, §6): create a home, then
    build profiles twice; total upstream spend is exactly the 1 create call.
    A falsy or mismatched seeded snapshot makes _cached_tracks refetch the
    empty playlist on every rebuild — 1 call per 10 minutes, forever."""
    _seed_config()
    _seed_cache_with_listing()
    calls = {"n": 0}

    class Resp:
        status_code = 201
        content = b"{}"
        headers = {}
        text = ""
        @staticmethod
        def json():
            return {"id": "made1"}  # deliberately NO snapshot_id → sentinel path

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        assert method == "POST" and url.endswith("/me/playlists"), (
            f"unexpected upstream call: {method} {url}")
        return Resp()

    monkeypatch.setattr(appmod.sp, "_access_token", lambda: "token")
    monkeypatch.setattr(appmod.sp, "_last_call", 0.0)
    monkeypatch.setattr(appmod.sp.http, "request", fake_request)

    res = client.post("/api/playlists/create", json={"name": "Late Night", "role": "home"})
    assert res.status_code == 200
    appmod._ensure_profiles(force=True)
    appmod._ensure_profiles(force=True)
    assert calls["n"] == 1
