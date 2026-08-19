"""The pure half of reconciliation: deciding what is a leftover sitting.

Everything here is offline and free. The rule these tests pin exists because
`splits.json` cannot be the authority on what is in the account — creating a
playlist and recording it are not atomic, so three leak classes survive every
per-slot fix (see .superpowers/sdd/2026-08-17-playlist-splitting/progress.md,
Ruling R17). The account is the authority instead, and these functions are how
a cached listing is read as one.
"""

from __future__ import annotations

from sortify.split import (
    SITTING_DESCRIPTION,
    SITTING_PREFIX,
    is_sitting_playlist,
    select_orphans,
)

ME = "bjorntehbear"


def entry(**over):
    base = {"id": "P1", "name": f"{SITTING_PREFIX}dreamy", "owner": ME,
            "editable": True, "description": SITTING_DESCRIPTION}
    base.update(over)
    return base


# ---- identification --------------------------------------------------------


def test_marked_sitting_owned_by_me_is_a_sitting():
    assert is_sitting_playlist(entry(), ME)


def test_name_without_the_prefix_is_never_a_sitting():
    assert not is_sitting_playlist(entry(name="dreamy"), ME)


def test_a_playlist_someone_else_owns_is_never_a_sitting():
    # Unfollowing one of these removes it from the user's library — the sweep
    # must never reach a playlist it did not create.
    assert not is_sitting_playlist(entry(owner="spotify"), ME)


def test_materialised_pile_is_never_a_sitting():
    """The hazard the whole rule exists to avoid.

    A materialised pile is permanent, holds the user's tracks, and its own
    description promises sortify will never delete it. It is excluded twice
    over — it carries no prefix, and its description is not the sitting
    marker — and this test pins both halves independently.
    """
    from sortify.app import MATERIALISE_DESCRIPTION

    assert not is_sitting_playlist(
        entry(name="dreamy", description=MATERIALISE_DESCRIPTION), ME)
    # Even if a pile name itself began with the prefix, the description saves it.
    assert not is_sitting_playlist(
        entry(name=f"{SITTING_PREFIX}dreamy", description=MATERIALISE_DESCRIPTION), ME)


def test_a_user_playlist_that_merely_starts_with_the_prefix_is_not_a_sitting():
    assert not is_sitting_playlist(
        entry(name=f"{SITTING_PREFIX}my favourites", description="mine, hands off"), ME)


def test_entry_cached_before_descriptions_were_kept_falls_back_to_the_prefix():
    """Pre-existing cache entries have no `description` key at all.

    Refusing them would mean finding nothing until the user spends ~21 calls
    on a Refresh; trusting a *wrong* description would be worse. Absent is
    therefore permissive, present-and-different is not — so the rule tightens
    by itself on the next refresh with no migration step.
    """
    stale = entry()
    del stale["description"]
    assert is_sitting_playlist(stale, ME)

    stale_but_not_a_sitting = entry(name="dreamy")
    del stale_but_not_a_sitting["description"]
    assert not is_sitting_playlist(stale_but_not_a_sitting, ME)


def test_unknown_owner_is_not_enough():
    # cache["me"] missing (never authed, or a cleared cache) must not turn
    # every marked playlist into a sweep candidate.
    assert not is_sitting_playlist(entry(), None)


# ---- selection -------------------------------------------------------------


def test_select_orphans_skips_protected_ids():
    listing = [entry(id="P1"), entry(id="P2"), entry(id="P3")]
    found, remaining = select_orphans(listing, ME, protected={"P2"}, cap=10)
    assert [p["id"] for p in found] == ["P1", "P3"]
    assert remaining == 0


def test_select_orphans_caps_the_burst_and_reports_the_rest():
    listing = [entry(id=f"P{i}") for i in range(25)]
    found, remaining = select_orphans(listing, ME, protected=set(), cap=10)
    assert len(found) == 10
    assert remaining == 15


def test_select_orphans_ignores_everything_unmarked():
    listing = [entry(id="P1"), {"id": "H", "name": "HOME", "owner": ME,
                                "editable": True, "description": ""}]
    found, remaining = select_orphans(listing, ME, protected=set(), cap=10)
    assert [p["id"] for p in found] == ["P1"]
    assert remaining == 0


def test_select_orphans_tolerates_a_listing_with_holes():
    # my_playlists() already drops null entries, but a cached listing is disk
    # state and this must not raise on one.
    listing = [{"id": "X"}, entry(id="P1")]
    found, _ = select_orphans(listing, ME, protected=set(), cap=10)
    assert [p["id"] for p in found] == ["P1"]


# ---- the endpoints ---------------------------------------------------------

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import sortify.app as appmod  # noqa: E402
from sortify.spotify import SpotifyError  # noqa: E402
from sortify.store import Store  # noqa: E402

FIVE_MIN = 300000


def listing_entry(pid, name, description=SITTING_DESCRIPTION, owner=ME):
    return {"id": pid, "name": name, "owner": owner, "editable": True,
            "total": 0, "snapshot_id": "s", "image": None, "description": description}


@pytest.fixture
def client(monkeypatch):
    """A split with one pile, plus a cached listing holding two stray sittings.

    The strays stand in for leak classes (a) and (b): playlists that really
    exist in the account with nothing in splits.json pointing at them.
    """
    calls = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="", **kw: calls.append(("create", name)) or "NEW1")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, **kw: calls.append(("add", uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "unfollow_playlist",
                        lambda pid, **kw: calls.append(("unfollow", pid)))
    s = Store()
    original_splits, original_cache = s.splits(), s.cache()
    s.save_splits({"version": 1, "splits": {"PL1": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None,
        "params": {"resolution": 1.0, "min_pile": 15},
        "piles": [{"id": "p1", "name": "dream pop", "tags": ["dream pop"],
                   "uris": [f"spotify:track:x{i}" for i in range(30)]}],
        "decided": {}, "active_sitting": None}}})
    cache = s.cache()
    cache["me"] = {"id": ME}
    cache["playlists"]["PL1"] = {"tracks": [
        {"uri": f"spotify:track:x{i}", "duration_ms": FIVE_MIN,
         "artists": [{"id": "a", "name": "A"}]} for i in range(30)]}
    cache["playlist_list"] = {"fetched_at": 0, "items": [
        listing_entry("STRAY1", f"{SITTING_PREFIX}dream pop"),
        listing_entry("STRAY2", f"{SITTING_PREFIX}ambient"),
        listing_entry("KEEP", "dream pop", description=appmod.MATERIALISE_DESCRIPTION),
        listing_entry("MINE", "my mixtape", description="hands off"),
    ]}
    s.save_cache(cache)
    c = TestClient(appmod.app)
    c.calls = calls
    try:
        yield c
    finally:
        Store().save_splits(original_splits)
        Store().save_cache(original_cache)


def cached_ids():
    return [p["id"] for p in Store().cache()["playlist_list"]["items"]]


def test_playlists_view_surfaces_orphans(client):
    body = client.get("/api/playlists").json()
    assert [o["id"] for o in body["sitting_orphans"]] == ["STRAY1", "STRAY2"]


def test_playlists_view_costs_no_spotify_calls(client):
    before = appmod.sp.budget_spent()
    client.get("/api/playlists")
    assert appmod.sp.budget_spent() == before
    assert client.calls == []


def test_cleanup_unfollows_each_orphan_once(client):
    body = client.post("/api/sittings/cleanup").json()
    assert sorted(body["removed"]) == ["STRAY1", "STRAY2"]
    assert client.calls == [("unfollow", "STRAY1"), ("unfollow", "STRAY2")]


def test_cleanup_prunes_the_cached_listing(client):
    """Otherwise the same orphans are re-offered forever and Remove looks broken."""
    client.post("/api/sittings/cleanup")
    assert cached_ids() == ["KEEP", "MINE"]


def test_cleanup_never_touches_a_materialised_pile(client):
    """A materialised pile is permanent and its description says sortify will
    never delete it. This is the one mistake that would destroy user data."""
    client.post("/api/sittings/cleanup")
    assert ("unfollow", "KEEP") not in client.calls
    assert "KEEP" in cached_ids()


def test_cleanup_protects_a_live_sitting(client):
    """A sitting that is active right now is not an orphan — even though it
    carries exactly the same marker."""
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    s = Store()
    cache = s.cache()
    cache["playlist_list"]["items"].append(listing_entry("NEW1", f"{SITTING_PREFIX}dream pop"))
    s.save_cache(cache)
    client.calls.clear()
    client.post("/api/sittings/cleanup")
    assert ("unfollow", "NEW1") not in client.calls


def test_cleanup_caps_the_burst(client, monkeypatch):
    monkeypatch.setattr(appmod, "SITTING_SWEEP_CAP", 1)
    body = client.post("/api/sittings/cleanup").json()
    assert len(body["removed"]) == 1
    assert body["remaining"] == 1
    assert len([c for c in client.calls if c[0] == "unfollow"]) == 1


def test_cleanup_keeps_an_orphan_it_failed_to_unfollow(client, monkeypatch):
    """A 429 or 5xx must leave the playlist findable — the next refresh sees
    it again. Pruning it from the cache would hide a playlist still in the
    account, which is the leak this whole change exists to end."""
    def boom(pid, **kw):
        if pid == "STRAY1":
            raise SpotifyError(429, "rate limited")
        client.calls.append(("unfollow", pid))
    monkeypatch.setattr(appmod.sp, "unfollow_playlist", boom)
    body = client.post("/api/sittings/cleanup").json()
    assert body["removed"] == ["STRAY2"]
    assert "STRAY1" in cached_ids()


def test_a_404_on_unfollow_still_counts_as_gone(client, monkeypatch):
    monkeypatch.setattr(appmod.sp, "unfollow_playlist",
                        lambda pid, **kw: (_ for _ in ()).throw(SpotifyError(404, "gone")))
    body = client.post("/api/sittings/cleanup").json()
    assert sorted(body["removed"]) == ["STRAY1", "STRAY2"]
    assert cached_ids() == ["KEEP", "MINE"]


def test_finish_sweeps_orphans_as_well_as_its_own_sitting(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    client.calls.clear()
    body = client.post("/api/split/PL1/sitting/finish").json()
    assert ("unfollow", "NEW1") in client.calls          # its own record
    assert sorted(body["swept"]) == ["STRAY1", "STRAY2"]  # and the strays
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


def test_finish_with_no_record_but_orphans_present_still_cleans_up(client):
    """The record-authoritative version 404s here — and 404 is exactly the
    answer that leaves a real playlist stranded in the account."""
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 200
    assert sorted(r.json()["swept"]) == ["STRAY1", "STRAY2"]


def test_finish_with_neither_record_nor_orphans_still_404s(client):
    s = Store()
    cache = s.cache()
    cache["playlist_list"]["items"] = [
        p for p in cache["playlist_list"]["items"] if not p["id"].startswith("STRAY")]
    s.save_cache(cache)
    assert client.post("/api/split/PL1/sitting/finish").status_code == 404


def test_cleanup_never_fetches_the_listing(client, monkeypatch):
    """The near-miss this test exists to keep closed.

    The first implementation read the listing through `sp.my_playlists()`,
    which falls back to a paginated fetch when the cache is cold — turning a
    cleanup on a fresh install into ~21 calls the user never asked for. A cold
    cache means "no orphans are known", never "go find out".
    """
    boom = lambda *a, **kw: pytest.fail("cleanup fetched the playlist listing")
    monkeypatch.setattr(appmod.sp, "_fetch_my_playlists", boom)
    s = Store()
    cache = s.cache()
    cache["playlist_list"] = None
    s.save_cache(cache)

    body = client.post("/api/sittings/cleanup").json()
    assert body["removed"] == []
    # The orphan *finder* is held to the same rule. (/api/playlists as a whole
    # does fetch on a cold cache — that is its existing contract and predates
    # reconciliation; the rule here is that looking for orphans adds nothing.)
    assert appmod._find_sitting_orphans() == []


def test_a_sitting_being_started_defers_the_sweep(client, monkeypatch):
    """While a create is in flight, its playlist may already be in the account
    under an id nobody knows yet — including this process. Nothing can be
    excluded by id in that window, so the sweep declines rather than risk
    deleting a sitting the user just asked for."""
    swept = {}

    def create_while_sweeping(name, description="", **kw):
        # Runs at the exact moment _materialising is raised.
        swept["during"] = client.post("/api/sittings/cleanup").json()
        return "NEW1"

    monkeypatch.setattr(appmod.sp, "create_playlist", create_while_sweeping)
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert swept["during"]["deferred"] is True
    assert swept["during"]["removed"] == []
    # And once it is over, the same orphans are swept normally.
    assert sorted(client.post("/api/sittings/cleanup").json()["removed"]) == ["STRAY1", "STRAY2"]
