"""Renaming a split's materialised playlists to `{source} · pile` form —
explicit, priced (one rename call each), never automatic (design §3)."""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from liveguard import assert_not_live_data
assert_not_live_data(appmod.store.dir)

from sortify.store import Store

URIS = [f"spotify:track:r{i}" for i in range(3)]


def _seed(source_name="{src}"):
    s = Store()
    payload = s.splits()
    payload["splits"]["PLR"] = {
        "created_at": "x", "snapshot_id": None,
        "params": {"resolution": 1.0, "min_pile": 15, "tag_floor": 10,
                   "max_tags_per_artist": 8},
        "piles": [{"id": "p1", "name": "jazz · funk", "tags": ["jazz"],
                   "uris": URIS}],
        "decided": {}, "active_sitting": None,
        "materialised": {
            "p1": {"playlist_id": "SP1", "pile_id": "p1", "name": "jazz · funk",
                   "fingerprint": "f", "track_count": 3, "added": list(URIS),
                   "claim": "c1", "created_at": "x", "updated_at": "x"},
        }}
    s.save_splits(payload)
    cache = s.cache()
    if source_name is not None:
        cache["playlist_list"] = {"items": [{"id": "PLR", "name": source_name}]}
    else:
        cache["playlist_list"] = {"items": []}
    s.save_cache(cache)


@pytest.fixture
def client(monkeypatch):
    renames = []
    monkeypatch.setattr(appmod.sp, "rename_playlist",
                        lambda pid, name: renames.append((pid, name)))
    s = Store()
    original_splits = s.splits()
    original_cache = s.cache()
    c = TestClient(appmod.app)
    c.renames = renames
    try:
        yield c
    finally:
        Store().save_splits(original_splits)
        Store().save_cache(original_cache)


def test_rename_prefixes_and_records(client):
    _seed()
    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 1})
    assert r.status_code == 200
    assert client.renames == [("SP1", "{src} · jazz · funk")]
    rec = Store().splits()["splits"]["PLR"]["materialised"]["p1"]
    assert rec["name"] == "{src} · jazz · funk"


def test_rename_skips_already_prefixed_outputs(client):
    _seed()
    s = Store()
    payload = s.splits()
    payload["splits"]["PLR"]["materialised"]["p1"]["name"] = "{src} · jazz · funk"
    s.save_splits(payload)
    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 0})
    assert r.status_code == 200
    assert r.json()["renamed"] == []
    assert client.renames == []


def test_rename_refuses_a_stale_price_without_spending(client):
    _seed()
    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 3})
    assert r.status_code == 409
    assert client.renames == []


def test_rename_refuses_when_the_source_name_is_unknown(client):
    _seed(source_name=None)
    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 1})
    assert r.status_code == 409
    assert client.renames == []


def test_an_orphan_record_is_neither_priced_nor_renamed(client):
    """Review I2: /recluster carries materialisation records forward without
    sweeping the ones whose pile id is gone (only create_split sweeps). The
    client prices from `data.piles`, so an orphan it cannot see must not be
    counted here either — otherwise the button quotes N, the server computes
    N+1, and the rename 409s forever with no route out of the UI."""
    _seed()
    s = Store()
    payload = s.splits()
    payload["splits"]["PLR"]["materialised"]["gone"] = {
        "playlist_id": "SPX", "pile_id": "gone", "name": "orphaned pile",
        "fingerprint": "f", "track_count": 1, "added": [], "claim": "cx",
        "created_at": "x", "updated_at": "x"}
    s.save_splits(payload)

    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 1})
    assert r.status_code == 200                       # the price the client saw
    assert client.renames == [("SP1", "{src} · jazz · funk")]   # SPX untouched


def test_a_renamed_source_does_not_compound_the_output_name(client):
    """Design §3: a source rename doesn't ripple into existing outputs. This
    action is the user asking for the ripple — but composing from the record's
    current name would give `newsrc · oldsrc · pile`, growing another segment
    on every later source rename."""
    _seed()
    s = Store()
    payload = s.splits()
    payload["splits"]["PLR"]["materialised"]["p1"]["name"] = "{oldsrc} · jazz · funk"
    s.save_splits(payload)
    cache = s.cache()
    cache["playlist_list"] = {"items": [{"id": "PLR", "name": "{newsrc}"}]}
    s.save_cache(cache)

    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 1})
    assert r.status_code == 200
    assert client.renames == [("SP1", "{newsrc} · jazz · funk")]

    # And a second run finds nothing left to do — no call, no cost.
    client.renames.clear()
    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 0})
    assert r.status_code == 200
    assert client.renames == []


def test_rename_refuses_more_outputs_than_it_can_spend_without_stalling(client):
    """Minor 4: N interactive calls in one request means the client sleeps
    holding `_budget_lock` past WINDOW_CAP, stalling every other Spotify call
    — the now-playing poll included. Refuse above the cap, spend nothing."""
    _seed()
    s = Store()
    payload = s.splits()
    split = payload["splits"]["PLR"]
    n = appmod.RENAME_OUTPUTS_MAX + 1
    split["piles"] = [{"id": f"p{i}", "name": f"pile {i}", "tags": [], "uris": URIS}
                      for i in range(n)]
    split["materialised"] = {
        f"p{i}": {"playlist_id": f"SP{i}", "pile_id": f"p{i}", "name": f"pile {i}",
                  "fingerprint": "f", "track_count": 3, "added": list(URIS),
                  "claim": f"c{i}", "created_at": "x", "updated_at": "x"}
        for i in range(n)}
    s.save_splits(payload)

    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": n})
    assert r.status_code == 409
    assert "Nothing was spent" in r.json()["detail"]
    assert client.renames == []
