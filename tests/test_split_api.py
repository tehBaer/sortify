"""The split endpoints, with both networks faked out.

The point of these tests is the call budget as much as the payload: a
re-cluster that quietly re-reads 1372 tracks would cost 15 Spotify calls
every time the user nudged a slider.

Five things beyond the original brief, added after a review round changed
the modules these endpoints call:

1. The store's safe accessors (`tag_artists()` / `save_tag_artists()`) are
   used instead of hand-unwrapping the `tags.json` envelope — a mixup would
   silently re-fetch every artist and leave every track untagged.
2. A `tags.json` version guard: a stale/foreign version must 400 clearly,
   not crash inside the splitter.
3. `enrich` can raise `LastFmError` carrying `.partial` — the endpoint must
   persist that partial and surface a clear, resumable error.
4. `SplitParams` carries `tag_floor` and `max_tags_per_artist` through to
   `split_tracks`, and re-clustering with different values costs nothing.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify.store import TAGS_VERSION, Store
from sortify.tags import LastFmError

TAGS = {
    "bh": {"name": "Beach House", "tags": [{"name": "dream pop", "count": 100}], "miss": False},
    "kv": {"name": "Kvelertak", "tags": [{"name": "black metal", "count": 100}], "miss": False},
}


def make_track(uri, tid, name, artist_id, artist_name, album):
    return {"uri": uri, "id": tid, "name": name, "duration_ms": 300000,
            "artists": [{"id": artist_id, "name": artist_name}], "album": album,
            "image": None, "added_at": "2023-01-01T00:00:00Z", "type": "track",
            "is_local": False}


def tracks(n_bh=20, n_kv=20):
    out = []
    for i in range(n_bh):
        out.append(make_track(f"spotify:track:bh{i}", f"bh{i}", f"BH {i}",
                              "bh", "Beach House", "A"))
    for i in range(n_kv):
        out.append(make_track(f"spotify:track:kv{i}", f"kv{i}", f"KV {i}",
                              "kv", "Kvelertak", "B"))
    return out


# The playlist listing the fake `sp.my_playlists()` serves. create_split now
# validates the id and reads snapshot_id from here (the same cached-listing
# pattern `triage` uses), so every playlist id a test posts to must appear.
PLAYLIST_LIST = [
    {"id": "PL1", "name": "PL1", "owner": "me", "editable": True,
     "total": 40, "snapshot_id": "snap-pl1", "image": None},
    {"id": "PL4", "name": "PL4", "owner": "me", "editable": True,
     "total": 21, "snapshot_id": "snap-pl4", "image": None},
]


@pytest.fixture
def client(monkeypatch):
    calls = {"spotify": 0, "lastfm": 0}

    def fake_playlist_tracks(pid):
        calls["spotify"] += 1
        return tracks()

    def fake_my_playlists(refresh=False):
        return PLAYLIST_LIST

    def fake_enrich(artist_names, cached, fm, now):
        calls["lastfm"] += 1
        return {**cached, **{a: TAGS[a] for a in artist_names if a in TAGS}}

    monkeypatch.setattr(appmod.sp, "playlist_tracks", fake_playlist_tracks)
    monkeypatch.setattr(appmod.sp, "my_playlists", fake_my_playlists)
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: object())
    monkeypatch.setattr(appmod, "enrich", fake_enrich)
    c = TestClient(appmod.app)
    c.calls = calls
    return c


def test_split_creates_piles(client):
    r = client.post("/api/split/PL1")
    assert r.status_code == 200
    piles = r.json()["piles"]
    assert len(piles) == 2
    assert sum(len(p["uris"]) for p in piles) == 40


def test_split_persists(client):
    client.post("/api/split/PL1")
    stored = Store().splits()["splits"]["PL1"]
    assert stored["piles"]
    assert stored["params"]["min_pile"] == 15
    assert stored["params"]["tag_floor"] == 10
    assert stored["params"]["max_tags_per_artist"] == 8
    assert stored["decided"] == {}


def test_split_records_the_real_snapshot_id_and_reuses_the_track_cache(client):
    """The cache write create_split does (via _cached_tracks, the same helper
    triage uses) must be the real shape — snapshot_id + fetched_at — or the
    ~15-call read it just paid for can never be served again, and the split's
    own snapshot_id (which a later task needs, to detect "changed since")
    stays permanently None."""
    # data/cache.json is shared for the whole test session (see
    # conftest.py) — clear PL1's entry so the first post here is a genuine
    # cold read, not a hit left over from an earlier test.
    cache = Store().cache()
    cache["playlists"].pop("PL1", None)
    Store().save_cache(cache)

    r = client.post("/api/split/PL1")
    assert r.status_code == 200
    assert client.calls["spotify"] == 1

    stored = Store().splits()["splits"]["PL1"]
    assert stored["snapshot_id"] == "snap-pl1"  # from the fake playlist listing

    # Splitting again with the same snapshot must be a cache hit, not a
    # second ~15-call read.
    client.post("/api/split/PL1")
    assert client.calls["spotify"] == 1


def test_split_stores_the_inner_tag_map_not_the_envelope(client):
    """Correction 1: a mixed-up envelope/inner-map write would leave
    tag_artists() empty (artists nested one level too deep)."""
    client.post("/api/split/PL1")
    artists = Store().tag_artists()
    assert set(artists) == {"bh", "kv"}
    assert artists["bh"]["name"] == "Beach House"
    # And the on-disk envelope is well-formed, not a double-wrapped map.
    envelope = Store().tags()
    assert envelope["version"] == TAGS_VERSION
    assert envelope["artists"] == artists


def test_get_split_returns_stored_piles(client):
    client.post("/api/split/PL1")
    r = client.get("/api/split/PL1")
    assert r.status_code == 200
    assert len(r.json()["piles"]) == 2


def test_get_split_404s_when_absent(client):
    assert client.get("/api/split/NOPE").status_code == 404


def test_split_rejects_an_unknown_playlist_without_spending_a_call(client):
    r = client.post("/api/split/NOT-IN-LISTING")
    assert r.status_code == 404
    assert client.calls["spotify"] == 0


def test_recluster_spends_no_api_calls(client, monkeypatch):
    client.post("/api/split/PL1")
    before = dict(client.calls)

    # Tighter than counting through one method: fail loudly if recluster
    # touches EITHER Spotify entry point this feature uses, not just the one
    # a prior test happened to instrument. Zero-cost reclustering is the
    # feature's headline contract.
    def fail(*a, **kw):
        raise AssertionError("recluster must not touch the Spotify API")

    monkeypatch.setattr(appmod.sp, "playlist_tracks", fail)
    monkeypatch.setattr(appmod.sp, "my_playlists", fail)

    r = client.post("/api/split/PL1/recluster", json={"min_pile": 5, "resolution": 1.0})
    assert r.status_code == 200
    assert client.calls == before


def test_recluster_preserves_decisions(client):
    client.post("/api/split/PL1")
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["decided"] = {
        "spotify:track:bh0": {"action": "keep", "to_id": "H1", "at": "2026-08-17T10:00:00Z"}
    }
    s.save_splits(payload)
    client.post("/api/split/PL1/recluster", json={"min_pile": 5})
    assert "spotify:track:bh0" in Store().splits()["splits"]["PL1"]["decided"]


def test_split_reports_progress(client):
    client.post("/api/split/PL1")
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["decided"] = {
        "spotify:track:bh0": {"action": "reject", "to_id": None, "at": "2026-08-17T10:00:00Z"}
    }
    s.save_splits(payload)
    body = client.get("/api/split/PL1").json()
    assert sum(p["decided"] for p in body["piles"]) == 1


def test_split_without_lastfm_key_errors_clearly(client, monkeypatch):
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)
    r = client.post("/api/split/PL2")
    assert r.status_code == 400
    assert "lastfm" in r.json()["detail"].lower()


# ---- Correction 2: tags.json version guard ---------------------------------


def test_split_rejects_a_stale_tags_version_with_a_clear_error(client):
    # data/tags.json is shared for the whole test session (see conftest.py),
    # so a version-1 file left behind would 400 every later test's split —
    # restore a valid envelope no matter how the assertions turn out.
    original = Store().tags()
    Store().save_tags({"version": 1, "artists": {"bh": {"name": "Beach House"}}})
    try:
        r = client.post("/api/split/PL1")
        assert r.status_code == 400
        detail = r.json()["detail"].lower()
        assert "version" in detail
        assert "tags.json" in detail
        # And it must not have burned the Spotify call getting there.
        assert client.calls["spotify"] == 0
    finally:
        Store().save_tags(original)


def test_recluster_rejects_a_stale_tags_version_with_a_clear_error(client):
    client.post("/api/split/PL1")
    original = Store().tags()
    Store().save_tags({"version": 1, "artists": {}})
    try:
        r = client.post("/api/split/PL1/recluster", json={"min_pile": 5})
        assert r.status_code == 400
        assert "version" in r.json()["detail"].lower()
    finally:
        Store().save_tags(original)


# ---- Correction 3: LastFmError partial handling -----------------------------


def test_split_on_lastfm_failure_persists_the_partial_and_reports_clearly(client, monkeypatch):
    # data/tags.json and data/splits.json are shared for the whole test
    # session (see conftest.py) — start this test from a known state so its
    # assertions describe this test's behaviour, not leftovers from earlier
    # tests. Crucially, the tag cache is warm with artists from OTHER
    # playlists ("other1", "other2") that have nothing to do with this
    # split: a message that reports len(the whole cache) instead of "how many
    # of THIS playlist's artists got through" would pass with an empty cache
    # (0 of 0 coincide) but go nonsensical the moment the cache has history —
    # which, for a real user who has split more than one playlist, is every
    # time.
    warm_cache = {
        "other1": {"name": "Other One", "tags": [], "miss": False},
        "other2": {"name": "Other Two", "tags": [], "miss": False},
    }
    Store().save_tag_artists(warm_cache)
    splits_payload = Store().splits()
    splits_payload["splits"].pop("PL1", None)
    Store().save_splits(splits_payload)

    def failing_enrich(artist_names, cached, fm, now):
        partial = {**cached, "bh": TAGS["bh"]}
        raise LastFmError("Last.fm error 29: rate limited", partial=partial)

    monkeypatch.setattr(appmod, "enrich", failing_enrich)
    r = client.post("/api/split/PL1")
    assert r.status_code == 502
    detail = r.json()["detail"].lower()
    # 1 of this playlist's 2 artists ("bh", "kv") got through — NOT
    # 3 of 2 (the 2 warm cross-playlist entries plus "bh"), which is what a
    # bare len(saved) would report.
    assert "1 of 2" in detail
    assert "3 of" not in detail
    assert "resum" in detail  # "resume"/"resuming" — retrying will pick up where it left off

    # The 689-of-700 promise: verified-good entries are not discarded, and
    # neither is the pre-existing cross-playlist cache.
    saved = Store().tag_artists()
    assert saved == {**warm_cache, "bh": TAGS["bh"]}
    # And no split was persisted from the failed attempt.
    assert "PL1" not in Store().splits()["splits"]


# ---- Correction 4: tag_floor / max_tags_per_artist are split parameters ----


FAINT_TAGS = {
    "bh": {"name": "Beach House", "tags": [{"name": "dream pop", "count": 100}], "miss": False},
    "faint": {"name": "Faint Band", "tags": [{"name": "ambient", "count": 12}], "miss": False},
}


def faint_tracks():
    out = [make_track(f"spotify:track:bh{i}", f"bh{i}", f"BH {i}", "bh", "Beach House", "A")
           for i in range(20)]
    out.append(make_track("spotify:track:faint0", "faint0", "Faint 0",
                          "faint", "Faint Band", "C"))
    return out


def test_recluster_tag_floor_changes_the_result_with_zero_api_calls(client, monkeypatch):
    def fake_playlist_tracks(pid):
        client.calls["spotify"] += 1
        return faint_tracks()

    def fake_enrich(artist_names, cached, fm, now):
        client.calls["lastfm"] += 1
        return {**cached, **{a: FAINT_TAGS[a] for a in artist_names if a in FAINT_TAGS}}

    monkeypatch.setattr(appmod.sp, "playlist_tracks", fake_playlist_tracks)
    monkeypatch.setattr(appmod, "enrich", fake_enrich)

    client.post("/api/split/PL4")
    before = dict(client.calls)

    def pile_of(piles, uri):
        return next(p["id"] for p in piles if uri in p["uris"])

    low = client.post(
        "/api/split/PL4/recluster",
        json={"min_pile": 1, "resolution": 1.0, "tag_floor": 10},
    ).json()
    high = client.post(
        "/api/split/PL4/recluster",
        json={"min_pile": 1, "resolution": 1.0, "tag_floor": 20},
    ).json()

    assert client.calls == before  # zero Spotify or Last.fm calls for either recluster

    assert pile_of(low["piles"], "spotify:track:faint0") != "untagged"
    assert pile_of(high["piles"], "spotify:track:faint0") == "untagged"


@pytest.mark.parametrize("bad_params", [
    {"min_pile": -5},
    {"min_pile": 0},
    {"resolution": -1.0},
    {"resolution": 0},
    {"tag_floor": -3},
    {"max_tags_per_artist": 0},
])
def test_recluster_rejects_meaningless_params(client, bad_params):
    """min_pile: -5, resolution: -1.0, tag_floor: -3, max_tags_per_artist: 0
    all used to return 200 and produce meaningless clustering."""
    client.post("/api/split/PL1")
    r = client.post("/api/split/PL1/recluster", json=bad_params)
    assert r.status_code == 422


def test_recluster_persists_the_new_params(client):
    client.post("/api/split/PL1")
    client.post("/api/split/PL1/recluster",
               json={"min_pile": 5, "resolution": 1.0, "tag_floor": 3, "max_tags_per_artist": 2})
    stored = Store().splits()["splits"]["PL1"]["params"]
    assert stored == {"resolution": 1.0, "min_pile": 5, "tag_floor": 3, "max_tags_per_artist": 2}
