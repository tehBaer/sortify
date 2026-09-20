"""Filing a newly created playlist into a folder, and creating buffers.

Three separable pieces, tested separately:

- `folders.folder_paths` — the list of destinations the UI offers, derived
  from the stored mapping (no tree, no client, no Spotify).
- `Store.set_folder_path` — a filed playlist patches ONE entry into
  data/folders.json. Deliberately not a folder re-import: that re-marks
  every home from the tree, which is a far bigger act than "this new
  playlist now lives here".
- `filing` — the background job. It drives the desktop client, so every
  test here injects fake seams; the real client is never launched.

Zero Spotify calls throughout: the create is faked, and the move is a
client-UI act that costs none by construction.
"""

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from sortify import filing
from sortify.folders import folder_paths
from sortify.store import Store

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "old1", "name": "Existing", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-old1", "image": None, "description": ""},
]

FOLDERS = {
    "old1": {"path": "ROOT / Rock", "caps": True},
    "old2": {"path": "ROOT / Rock", "caps": True},
    "old3": {"path": "THE BOMB", "caps": True},
}


# ---- the destination list -------------------------------------------------

def test_folder_paths_lists_each_folder_once_sorted():
    assert folder_paths(FOLDERS) == ["ROOT / Rock", "THE BOMB"]


def test_folder_paths_of_an_empty_mapping_is_empty():
    assert folder_paths({}) == []


# ---- patching one entry ---------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    s.save_folders(dict(FOLDERS))
    return s


def test_set_folder_path_patches_one_entry_and_leaves_the_rest(store):
    store.set_folder_path("made1", "ROOT / Rock")
    mapping = store.folders()
    assert mapping["made1"] == {"path": "ROOT / Rock", "caps": True}
    assert mapping["old1"] == FOLDERS["old1"]
    assert len(mapping) == len(FOLDERS) + 1


def test_set_folder_path_marks_caps_from_the_path(store):
    store.set_folder_path("made1", "Roots / lower")
    assert store.folders()["made1"]["caps"] is False


def test_set_folder_path_to_top_level_drops_the_entry(store):
    store.set_folder_path("old1", None)
    assert "old1" not in store.folders()


# ---- the filing job -------------------------------------------------------

TREE = {"children": [
    {"type": "folder", "name": "ROOT", "children": [
        {"type": "folder", "name": "Rock", "children": []},
    ]},
    {"type": "folder", "name": "THE BOMB", "children": []},
]}


def _run(name="Late Night", dest="ROOT / Rock", items=None, executor=None):
    """Run one filing job synchronously, returning the plan it executed."""
    seen = {}

    def fake_executor(plan):
        seen["plan"] = plan

    filing.file_playlist(
        "made1", name, dest,
        items=items if items is not None else LISTING + [{"id": "made1", "name": name}],
        tree_extractor=lambda: TREE,
        executor=executor or fake_executor,
    )
    return seen.get("plan")


def test_filing_moves_the_new_playlist_from_top_level_to_the_folder():
    plan = _run()
    assert plan.playlist_id == "made1"
    assert plan.playlist_name == "Late Night"
    assert plan.from_path is None
    assert plan.to_path == "ROOT / Rock"


def test_filing_refuses_when_another_playlist_shares_the_name():
    # The client UI finds the row by filtering on the name, so a duplicate
    # would be a coin flip over which playlist gets moved.
    items = LISTING + [{"id": "made1", "name": "Existing"}]
    with pytest.raises(filing.FilingError) as e:
        _run(name="Existing", items=items)
    assert "name" in str(e.value)


def test_filing_refuses_a_folder_the_tree_does_not_have():
    with pytest.raises(filing.FilingError):
        _run(dest="ROOT / Nowhere")


def test_a_failed_move_surfaces_as_a_filing_error():
    def boom(plan):
        raise RuntimeError("aborted mid-move")

    with pytest.raises(filing.FilingError) as e:
        _run(executor=boom)
    assert "aborted mid-move" in str(e.value)


def test_start_records_success_and_calls_back_with_the_destination():
    filed = []
    filing.start("made1", "Late Night", "ROOT / Rock",
                 on_success=filed.append, runner=lambda *a, **k: None, threaded=False)
    assert filing.status("made1")["state"] == "filed"
    assert filing.status("made1")["folder"] == "ROOT / Rock"
    assert filed == ["ROOT / Rock"]


def test_start_records_failure_and_never_calls_back():
    filed = []

    def boom(*a, **k):
        raise filing.FilingError("the client is not installed")

    filing.start("made2", "Late Night", "ROOT / Rock",
                 on_success=filed.append, runner=boom, threaded=False)
    st = filing.status("made2")
    assert st["state"] == "failed"
    assert "not installed" in st["error"]
    assert filed == []


def test_status_of_a_playlist_that_was_never_filed_is_idle():
    assert filing.status("never-seen")["state"] == "idle"


# ---- the endpoint ---------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    appmod.store.save_config({
        "client_id": "x", "input_ids": [], "home_ids": [], "subset_ids": [],
        "home_folder_prefixes": ["ROOT"], "home_folder_exclude": [],
        "input_name_pattern": r"^\[.+\]$",
        "input_sets": [
            {"key": "buffer", "label": "buffer", "pattern": r"^\[.+\]$"},
            {"key": "the-bomb", "label": "THE BOMB", "path_segment": "THE BOMB"},
        ],
        "home_exclude_emoji_names": True,
        "home_name_exclude_patterns": [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"],
    })
    appmod.store.save_folders(dict(FOLDERS))
    appmod.store.save_cache({
        "playlists": {}, "artists": {}, "me": {"id": "me"},
        "playlist_list": {"fetched_at": 1.0, "items": list(LISTING)},
    })
    calls = {"create": 0}
    started = []

    def fake_full(name, description="", bulk=False, spend_reserve=False, public=False):
        calls["create"] += 1
        return "made1", "snap-new"

    monkeypatch.setattr(appmod.sp, "create_playlist_full", fake_full)
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: list(LISTING))
    monkeypatch.setattr(
        appmod.filing, "start",
        lambda pid, name, dest, **kw: started.append((pid, name, dest)))
    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.calls = calls
    c.started = started
    try:
        yield c
    finally:
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0


def _create(client, name, **body):
    return client.post("/api/playlists/create", json={"name": name, **body})


def test_creating_with_a_folder_starts_a_filing_job(client):
    res = _create(client, "Late Night", role="home", folder="ROOT / Rock")
    assert res.status_code == 200
    assert client.started == [("made1", "Late Night", "ROOT / Rock")]
    assert res.json()["playlist"]["folder"] == "ROOT / Rock"
    assert res.json()["filing"] is True


def test_creating_at_top_level_starts_no_job(client):
    res = _create(client, "Late Night", role="home", folder=None)
    assert res.status_code == 200
    assert client.started == []
    assert res.json()["playlist"]["folder"] is None
    assert res.json()["filing"] is False


def test_an_unknown_folder_is_refused_before_a_call_is_spent(client):
    res = _create(client, "Late Night", role="home", folder="ROOT / Nowhere")
    assert res.status_code == 400
    assert "folder" in res.json()["detail"]
    assert client.calls["create"] == 0
    assert client.started == []


def test_the_chosen_folder_becomes_that_roles_default(client):
    _create(client, "Late Night", role="home", folder="ROOT / Rock")
    assert appmod.store.config()["create_folders"]["home"] == "ROOT / Rock"


def test_an_omitted_folder_uses_the_stored_default(client):
    appmod.store.update_config(create_folders={"home": "ROOT / Rock"})
    res = _create(client, "Late Night", role="home")
    assert res.status_code == 200
    assert client.started == [("made1", "Late Night", "ROOT / Rock")]


def test_choosing_top_level_clears_that_roles_default(client):
    appmod.store.update_config(create_folders={"home": "ROOT / Rock"})
    _create(client, "Late Night", role="home", folder=None)
    assert appmod.store.config()["create_folders"]["home"] is None


def test_a_subsets_default_is_separate_from_a_homes(client):
    appmod.store.update_config(create_folders={"home": "ROOT / Rock"})
    _create(client, "best of", role="subset", folder="THE BOMB")
    cfg = appmod.store.config()
    assert cfg["create_folders"] == {"home": "ROOT / Rock", "subset": "THE BOMB"}


# ---- creating buffers -----------------------------------------------------

def test_creating_a_buffer_marks_it_an_input_and_nothing_else(client):
    res = _create(client, "[Hazy]", role="input", folder="ROOT / Rock")
    assert res.status_code == 200
    cfg = appmod.store.config()
    assert cfg["input_ids"] == ["made1"]
    assert "made1" not in cfg["home_ids"]
    assert "made1" not in (cfg.get("sticky_home_ids") or [])
    assert "made1" not in (cfg.get("subset_ids") or [])
    assert res.json()["playlist"]["role"] == "input"


def test_a_buffer_name_matching_no_set_rule_is_refused(client):
    # For a pattern set the NAME is the membership: "Hazy" would be marked
    # an input by id and then sit in no set at all.
    res = _create(client, "Hazy", role="input", folder="ROOT / Rock")
    assert res.status_code == 400
    assert "set" in res.json()["detail"]
    assert client.calls["create"] == 0


def test_a_folder_defined_set_takes_any_name_in_that_folder(client):
    res = _create(client, "Progressive rock · psych", role="input", folder="THE BOMB")
    assert res.status_code == 200
    assert appmod.store.config()["input_ids"] == ["made1"]


def test_the_filing_status_of_a_new_playlist_is_readable(client):
    _create(client, "Late Night", role="home", folder="ROOT / Rock")
    res = client.get("/api/playlists/filing/made1")
    assert res.status_code == 200
    assert res.json()["state"] in ("idle", "filing", "filed", "failed")


def test_the_listing_offers_the_folders_and_the_defaults(client):
    appmod.store.update_config(create_folders={"home": "ROOT / Rock"})
    body = client.get("/api/playlists").json()
    assert body["folder_paths"] == ["ROOT / Rock", "THE BOMB"]
    assert body["create_folders"] == {"home": "ROOT / Rock"}
