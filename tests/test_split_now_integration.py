"""The reload gap, closed: after a page reload, the client's pointer to an
active sitting used to live only in memory, so the Now view had no way to
tell a sitting's disposable playlist apart from any other. Filing a track
then went through /api/act instead of /api/split/.../decide — a Spotify call
spent, nothing written to `decided`, and `pick_sitting` would serve the exact
same track again in a later sitting: paying to decide it twice.

`/api/now` now reports which split (if any) owns the currently-playing
context, from a local `store.splits()` read piggybacked on the poll that
already exists — zero Spotify calls, and it survives reload by construction
since there is no client state to lose. `/api/playlists` similarly grows a
`split` summary (pile count, remaining) per playlist, so one already split
doesn't look untouched in the picker — also a local read.
"""

import pytest

import sortify.app as appmod
from sortify.store import Store


def _splits_payload(pile_id="p1", uris=("spotify:track:a", "spotify:track:b", "spotify:track:c"),
                     sitting_playlist_id="sit1", decided=None):
    return {"version": 1, "splits": {"PL_NOW": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None, "params": {},
        "piles": [{"id": pile_id, "name": "dream pop", "tags": ["dream pop"],
                   "uris": list(uris)}],
        "decided": decided or {},
        "active_sitting": {"playlist_id": sitting_playlist_id, "pile_id": pile_id,
                            "uris": list(uris), "started_at": "2026-08-17T10:05:00Z",
                            "claim": "c1"},
    }}}


@pytest.fixture
def splits_store():
    """splits.json is one file shared for the whole test session (see
    conftest.py) — same discipline as test_split_decisions.py: capture
    whatever was there, restore it after, regardless of pass/fail."""
    original = Store().splits()
    try:
        yield
    finally:
        Store().save_splits(original)


# ---- _sitting_for_context ----------------------------------------------------


def test_no_context_means_no_sitting(splits_store):
    Store().save_splits(_splits_payload())
    assert appmod._sitting_for_context(None) is None


def test_a_context_that_matches_no_sitting_is_not_one(splits_store):
    Store().save_splits(_splits_payload())
    assert appmod._sitting_for_context("some-other-playlist") is None


def test_the_sitting_playlist_resolves_to_its_split_and_pile(splits_store):
    Store().save_splits(_splits_payload())

    result = appmod._sitting_for_context("sit1")

    assert result["split_id"] == "PL_NOW"
    assert result["pile_id"] == "p1"
    assert result["pile_name"] == "dream pop"
    assert result["uris"] == ["spotify:track:a", "spotify:track:b", "spotify:track:c"]


def test_decided_is_restricted_to_this_sittings_own_uris(splits_store):
    """A uri decided under some other pile/sitting must not leak into what
    this sitting's card thinks is already decided."""
    decided = {
        "spotify:track:a": {"action": "keep", "to_id": "home1", "at": "t"},
        "spotify:track:zzz": {"action": "reject", "at": "t"},  # not in this sitting
    }
    Store().save_splits(_splits_payload(decided=decided))

    result = appmod._sitting_for_context("sit1")

    assert result["decided"] == {"spotify:track:a": decided["spotify:track:a"]}


def test_no_active_sitting_at_all_is_not_one(splits_store):
    payload = _splits_payload()
    payload["splits"]["PL_NOW"]["active_sitting"] = None
    Store().save_splits(payload)

    assert appmod._sitting_for_context("sit1") is None


# ---- _split_summary -----------------------------------------------------------


def test_an_unsplit_playlist_has_no_summary(splits_store):
    Store().save_splits(_splits_payload())
    assert appmod._split_summary("some-other-playlist") is None


def test_split_summary_counts_piles_and_what_remains(splits_store):
    decided = {"spotify:track:a": {"action": "reject", "at": "t"}}
    Store().save_splits(_splits_payload(decided=decided))

    summary = appmod._split_summary("PL_NOW")

    assert summary == {"piles": 1, "remaining": 2}  # 3 uris, 1 decided


# ---- wired into the endpoints --------------------------------------------------

MINIMAL_STATE = {"playlists": [], "input_ids": set(), "artist_info": {},
                  "profiles": {}, "homes": [], "inputs": []}


def _now_payload(uri="spotify:track:a", ctx_id="sit1"):
    return {
        "track": {"uri": uri, "id": "a", "name": "A Song", "type": "track",
                  "is_local": False, "duration_ms": 200000, "artists": [],
                  "album": None, "image": None},
        "is_playing": True, "progress_ms": 1000, "context_playlist_id": ctx_id,
    }


@pytest.fixture
def now_client(monkeypatch):
    """Bypasses the real profile-building machinery (Spotify/home-playlist
    reads) the same way test_playlist_cache.py does — this is testing the
    `sitting` field, not profile suggestion."""
    monkeypatch.setattr(appmod, "_ensure_profiles", lambda force=False: MINIMAL_STATE)
    monkeypatch.setattr(appmod.sugg, "suggest", lambda *a, **k: [])
    appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)


def test_now_reports_the_sitting_for_a_sitting_playlist(splits_store, now_client, monkeypatch):
    Store().save_splits(_splits_payload())
    monkeypatch.setattr(appmod.sp, "currently_playing", lambda: _now_payload())

    resp = appmod.now_playing()

    assert resp["sitting"]["split_id"] == "PL_NOW"
    assert resp["sitting"]["pile_name"] == "dream pop"


def test_now_reports_no_sitting_for_an_ordinary_playlist(splits_store, now_client, monkeypatch):
    Store().save_splits(_splits_payload())
    monkeypatch.setattr(appmod.sp, "currently_playing",
                        lambda: _now_payload(ctx_id="some-ordinary-playlist"))

    resp = appmod.now_playing()

    assert resp["sitting"] is None


def test_playlists_endpoint_carries_the_split_summary(splits_store, monkeypatch):
    Store().save_splits(
        _splits_payload(decided={"spotify:track:a": {"action": "reject", "at": "t"}}))
    monkeypatch.setattr(
        appmod.sp, "my_playlists",
        lambda refresh=False: [
            {"id": "PL_NOW", "name": "Big One", "owner": "me", "editable": True,
             "total": 3, "snapshot_id": "s1", "image": None},
        ],
    )

    out = {p["id"]: p for p in appmod.playlists()["playlists"]}

    assert out["PL_NOW"]["split"] == {"piles": 1, "remaining": 2}
    assert out[appmod.LIKED_ID]["split"] is None
