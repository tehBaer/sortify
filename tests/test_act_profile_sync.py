"""Filing a track must be visible to the very next suggestion.

The shipped bug: file a song from the now card, reload the page, and the card
came back suggesting the same homes — including the one the song had just been
filed into, offered as a fresh guess with no "already there" badge.

Why a reload exposed it. The ✓ filed card is client state (`filedUris` in
app.js), which a reload wipes; the only signal that survives is `already` on
each suggestion row, and that is read off `_profile_state["profiles"][pid]
["uris"]` — a snapshot rebuilt at most every PROFILE_TTL (10 min). `/api/act`
mirrored its move into `data/cache.json` (`_cache_move`) and into the in-memory
INPUT membership (`_sync_membership`, so the capture chips stay right),
but never into the home profiles, and nothing reset `built_at`. So for up to
ten minutes the server kept answering "no, that song isn't in that home".

The asymmetry is the bug: inputs were kept current and homes were not, from
the same action, in the same request. These tests pin both halves — and the
undo, which has to put the profile back or the badge outlives the move that
earned it.

Zero Spotify calls: fake clients and a hand-primed now-cache throughout.
"""

import time

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
     "total": 12, "snapshot_id": "s-h1", "image": None, "description": ""},
    {"id": "inp", "name": "[Buffer]", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-in", "image": None, "description": ""},
]

# Same artist as the home's one track, so h1 clears MIN_SCORE on artist
# overlap alone — the row is a confident suggestion before the move, which is
# what makes the `already` flag the only thing that changes after it.
PLAYING = {"uri": "spotify:track:z", "id": "z", "name": "Z", "is_local": False,
           "type": "track", "duration_ms": 210_000,
           "artists": [{"id": "ar1", "name": "Ar One"}],
           "added_at": "2026-02-02T00:00:00Z"}
HOME_TRACK = {"uri": "spotify:track:a", "id": "a", "name": "A", "is_local": False,
              "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
              "added_at": "2026-01-01T00:00:00Z"}


@pytest.fixture
def filing(monkeypatch):
    """A now-card mid-filing: track z playing out of input `inp`, home `h1`
    profiled and not yet holding it, and both Spotify writes trapped."""
    store = appmod.store
    original_cache, original_config = store.cache(), store.config()

    cache = store.cache()
    cache["playlist_list"] = {"fetched_at": 0.0, "items": LISTING}
    cache["playlists"] = {
        "h1": {"snapshot_id": "s-h1", "tracks": [HOME_TRACK], "fetched_at": time.time()},
        "inp": {"snapshot_id": "s-in", "tracks": [PLAYING], "fetched_at": time.time()},
    }
    store.save_cache(cache)
    store.save_config({**original_config, "home_ids": ["h1"], "input_ids": ["inp"],
                       "subset_ids": [], "input_name_pattern": r"^\[.+\]$"})

    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", lambda pid, uri: "s-h1-new")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist", lambda pid, uri: "s-in-new")
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)

    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    appmod._now_cache.update(at=time.time(), value={
        "track": PLAYING, "is_playing": True, "progress_ms": 1000,
        "context_playlist_id": "inp",
    }, ttl=appmod.NOW_TTL_MAX)
    appmod.undo_stack.clear()
    # Built up front, from the cache alone, because that is the state a real
    # filing happens in: the card you press was drawn from these profiles. A
    # first build AFTER the move would refetch h1 (its snapshot moved) and
    # hide the staleness behind a Spotify call the poll path never makes.
    appmod._ensure_profiles(force=True)

    try:
        yield TestClient(appmod.app, raise_server_exceptions=False)
    finally:
        store.save_cache(original_cache)
        store.save_config(original_config)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod._now_cache.update(at=0.0, value=None, ttl=appmod.NOW_TTL_IDLE)
        appmod.undo_stack.clear()


def _row(client, pid):
    rows = client.get("/api/now/suggest").json()["suggestions"]
    return next((s for s in rows if s["playlist_id"] == pid), None)


def _file_it(client):
    res = client.post("/api/act", json={
        "action": "move", "uri": PLAYING["uri"], "from_id": "inp", "to_id": "h1"})
    assert res.status_code == 200, res.text
    return res


def test_the_home_is_suggested_before_the_move(filing):
    """The premise: h1 is an ordinary confident suggestion to begin with."""
    row = _row(filing, "h1")
    assert row is not None and row["already"] is False


def test_filing_marks_the_home_already_on_the_next_suggestion(filing):
    """The regression. No rebuild in between — the very next request, which
    is all a reload gets, has to say the song is in there now."""
    _file_it(filing)
    row = _row(filing, "h1")
    assert row is not None, "the home vanished from the list entirely"
    assert row["already"] is True, "reload re-offered the home it was just filed into"


def test_filing_does_not_force_a_profile_rebuild(filing):
    """The fix is a mirror, not an invalidation. Dropping `built_at` would
    make the next poll rebuild every home profile — and a rebuild re-reads
    playlists whose snapshot has moved, which is a Spotify call per filed
    home on the polling path."""
    filing.get("/api/now/suggest")
    built_at = appmod._profile_state["built_at"]
    _file_it(filing)
    assert appmod._profile_state["built_at"] == built_at


def test_undo_takes_the_home_back_out_of_the_profile(filing):
    """A badge that outlived the move it described would be worse than the
    stale one this fixes — it would claim a home holds a song it doesn't."""
    _file_it(filing)
    assert filing.post("/api/undo").status_code == 200
    row = _row(filing, "h1")
    assert row is not None and row["already"] is False


def test_the_input_chip_and_the_home_badge_agree_after_filing(filing):
    """The asymmetry that was the bug: input membership was mirrored and home
    membership was not, so one request left the two halves of the same card
    disagreeing about where the song is."""
    _file_it(filing)
    body = filing.get("/api/now/suggest").json()
    chip = next(l for l in body["inputs"] if l["id"] == "inp")
    home = next(s for s in body["suggestions"] if s["playlist_id"] == "h1")
    assert chip["has_track"] is False
    assert home["already"] is True
