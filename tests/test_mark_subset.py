"""Marking a subset is a rename (1 call), and unmarking is too."""

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)


@pytest.fixture
def renames(monkeypatch):
    calls = []
    def fake(pid, name):
        calls.append((pid, name))
        cache = appmod.store.cache()
        for p in cache["playlist_list"]["items"]:
            if p["id"] == pid:
                p["name"] = name
        appmod.store.save_cache(cache)
    monkeypatch.setattr(appmod.sp, "rename_playlist", fake)
    return calls


def seed(playlists, **cfg):
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 0, "items": playlists}
    appmod.store.save_cache(cache)
    base = {"home_ids": [], "input_ids": [], "input_sets": []}
    base.update(cfg)
    appmod.store.update_config(**base)


def pl(pid, name, editable=True):
    return {"id": pid, "name": name, "editable": editable, "total": 0, "snapshot_id": "s",
            "owner": "me", "image": None, "description": ""}


def post(pid, on):
    return TestClient(appmod.app).post(f"/api/playlists/{pid}/subset", json={"on": on})


def test_marking_puts_a_paw_on_the_name(renames):
    seed([pl("P1", "tabletop")])
    r = post("P1", True)
    assert r.status_code == 200 and r.json() == {"playlist_id": "P1", "name": "🐾 tabletop", "role": "subset"}
    assert renames == [("P1", "🐾 tabletop")]


def test_unmarking_takes_the_emoji_off(renames):
    seed([pl("P1", "🐾 tabletop")])
    assert post("P1", False).json()["name"] == "tabletop"


def test_marking_an_archived_list_unarchives_it(renames):
    seed([pl("A1", "🗄️ 🐾 eksotisk")])
    assert post("A1", True).json() == {"playlist_id": "A1", "name": "🐾 eksotisk", "role": "subset"}


def test_unmarking_an_archived_list_changes_nothing(renames):
    # unmark_subset_name("🗄️ x") would return "x" — a silent un-archive.
    seed([pl("A1", "🗄️ 🐾 old")])
    r = post("A1", False)
    assert r.status_code == 200 and r.json()["name"] == "🗄️ 🐾 old"
    assert renames == []


def test_unmarking_a_plain_name_changes_nothing(renames):
    seed([pl("P1", "tabletop")])
    r = post("P1", False)
    assert r.status_code == 200 and r.json()["name"] == "tabletop" and renames == []


def test_already_marked_costs_nothing(renames):
    seed([pl("P1", "🧸 fett")])
    assert post("P1", True).status_code == 200 and renames == []


def test_an_emoji_only_name_cannot_be_unmarked(renames):
    seed([pl("P1", "👼")])
    r = post("P1", False)
    assert r.status_code == 400 and renames == []


def test_not_ours_is_refused(renames):
    seed([pl("X1", "plain", editable=False)])
    assert post("X1", True).status_code == 400 and renames == []


def test_a_home_or_input_is_refused(renames):
    seed([pl("H1", "DARK SOAR"), pl("I1", "[Hazy]")], home_ids=["H1"],
         input_sets=[{"key": "buffer", "pattern": r"^\[.+\]$"}])
    assert post("H1", True).status_code == 409
    assert post("I1", True).status_code == 409
    assert renames == []


def test_unknown_playlist_is_404(renames):
    seed([])
    assert post("NOPE", True).status_code == 404


def test_a_stale_tab_posting_subset_ids_is_ignored():
    seed([pl("P1", "plain")])
    r = TestClient(appmod.app).post("/api/config", json={
        "input_ids": [], "home_ids": [], "home_hints": {}, "subset_ids": ["P1"]})
    assert r.status_code == 200
    assert "subset_ids" not in appmod.store.config() or appmod.store.config()["subset_ids"] != ["P1"]


def test_an_archived_playlist_in_home_ids_is_not_a_home():
    listing = [pl("H1", "DARK SOAR"), pl("H2", "🗄️ OLD HOME")]
    cfg = {"home_ids": ["H1", "H2"]}
    assert [p["id"] for p in appmod._resolve_homes(cfg, listing, "", set())] == ["H1"]


def test_an_archived_playlist_is_not_a_fallback_home_either():
    listing = [pl("H1", "DARK SOAR"), pl("H2", "🗄️ OLD HOME")]
    assert [p["id"] for p in appmod._resolve_homes({}, listing, "", set())] == ["H1"]
