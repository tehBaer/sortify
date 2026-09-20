"""Creating a subset playlist from the Now card.

A subset is a non-exclusive selection: never a home, never an input, and a
song put in one still needs its home. So creation writes `subset_ids` and
nothing else — marking it home or sticky would make the picker offer it as a
filing destination on the very next request.

Names: subsets have NO name convention (data/config.json's `subset_ids` is
the whole definition), so the home name rules — `{}`/`<>`/`__x__`, emoji
prefixes — must not apply here. The ONE rule that survives is the input
pattern, because `_effective_input_ids` unions pattern matches over the
config list: a subset called "[Foo]" would come back as an input.

Zero Spotify calls: the create is faked.
"""

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "old1", "name": "Existing", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-old1", "image": None, "description": ""},
]


@pytest.fixture
def client(monkeypatch):
    appmod.store.save_config({
        "client_id": "x", "input_ids": [], "home_ids": [], "subset_ids": [],
        "home_folder_prefixes": ["ROOT"], "home_folder_exclude": [],
        "input_name_pattern": r"^\[.+\]$",
        "home_exclude_emoji_names": True,
        "home_name_exclude_patterns": [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"],
    })
    appmod.store.save_cache({
        "playlists": {}, "artists": {}, "me": {"id": "me"},
        "playlist_list": {"fetched_at": 1.0, "items": list(LISTING)},
    })
    calls = {"create": 0}

    def fake_full(name, description="", bulk=False, spend_reserve=False, public=False):
        calls["create"] += 1
        return "made1", "snap-new"

    monkeypatch.setattr(appmod.sp, "create_playlist_full", fake_full)
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: list(LISTING))
    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.calls = calls
    try:
        yield c
    finally:
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0


def _create(client, name):
    return client.post("/api/playlists/create", json={"name": name, "role": "subset"})


def test_creating_a_subset_marks_it_a_subset_and_nothing_else(client):
    res = _create(client, "best of the bomb")
    assert res.status_code == 200
    cfg = appmod.store.config()
    assert cfg["subset_ids"] == ["made1"]
    assert "made1" not in cfg["home_ids"]
    assert "made1" not in (cfg.get("sticky_home_ids") or [])
    assert res.json()["playlist"]["role"] == "subset"


def test_the_track_cache_is_seeded_so_no_rebuild_refetches_it(client):
    _create(client, "best of the bomb")
    entry = appmod.store.cache()["playlists"]["made1"]
    listed = next(p for p in appmod.store.cache()["playlist_list"]["items"]
                  if p["id"] == "made1")
    assert entry["tracks"] == []
    assert entry["snapshot_id"] == listed["snapshot_id"] == "snap-new"


def test_home_name_rules_do_not_apply_to_subsets(client):
    # Every one of these is refused as a HOME name and must be fine here.
    for name in ("{alle sanger}", "<motor>", "__start__", "🐾 derived"):
        appmod.store.update_config(subset_ids=[])
        assert _create(client, name).status_code == 200, name


def test_input_shaped_names_are_still_refused(client):
    res = _create(client, "[Foo]")
    assert res.status_code == 400
    assert "input" in res.json()["detail"]
    assert client.calls["create"] == 0


def test_blank_names_are_refused_before_spending(client):
    for bad in ("", "   "):
        assert _create(client, bad).status_code == 400, repr(bad)
    assert client.calls["create"] == 0


def test_unknown_roles_are_still_refused(client):
    # Inputs became creatable when filing arrived (they carry their own set
    # rules); anything else is still not a role this endpoint writes.
    res = client.post("/api/playlists/create", json={"name": "Ok", "role": "banana"})
    assert res.status_code == 400
    assert client.calls["create"] == 0


def test_creating_a_subset_clears_the_profile_cache(client):
    # The picker reads `state["playlists"]`, so the new subset is only
    # offerable once the next request rebuilds from the listing.
    appmod._profile_state.update(built_at=9e12, profiles={"stale": None})
    _create(client, "best of the bomb")
    assert appmod._profile_state.get("built_at") == 0.0
