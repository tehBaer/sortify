"""Filing or removing a song takes it out of EVERY input, not just one.

A song can sit in several inboxes at once — 29 of 2244 did when this was
written, one of them in three. Before this, filing from `[A]` left the copy in
`[B]` behind, so the same song came round again in a later session and had to
be decided twice. The second decision is the bug: the first one already
answered the question.

The rule is deliberately blind to which set an input belongs to (buffer,
other, the-bomb) and includes the Homeless buffer, which is an input like any
other — the user chose that over an exception when asked.

The one destination that must survive the sweep is the destination itself: the
Homeless button files INTO an input, and a sweep that did not exclude the
target would delete the track it had just added.

Cost: one DELETE per extra input, and there is no batch delete. It is a
no-op for the 98.7% of tracks that live in exactly one input, so the sweep is
almost always free.

Zero Spotify calls: every write is trapped.
"""

import time

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
     "total": 5, "snapshot_id": "s-h1", "image": None, "description": ""},
    {"id": "inA", "name": "[A]", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-inA", "image": None, "description": ""},
    {"id": "inB", "name": "[B]", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-inB", "image": None, "description": ""},
    {"id": "homeless", "name": "[Homeless]", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-hl", "image": None, "description": ""},
    {"id": "sub1", "name": "best of", "owner": "me", "editable": True,
     "total": 2, "snapshot_id": "s-sub", "image": None, "description": ""},
]

TRACK = {"uri": "spotify:track:z", "id": "z", "name": "Z", "is_local": False,
         "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
         "added_at": "2026-02-02T00:00:00Z"}
OTHER = {"uri": "spotify:track:q", "id": "q", "name": "Q", "is_local": False,
         "type": "track", "artists": [{"id": "ar9", "name": "Ar Nine"}],
         "added_at": "2026-01-01T00:00:00Z"}


@pytest.fixture
def swept(monkeypatch):
    """Track z sits in THREE inputs at once — [A], [B] and [Homeless] — with
    every Spotify write trapped and counted."""
    store = appmod.store
    original_cache, original_config = store.cache(), store.config()

    cache = store.cache()
    cache["playlist_list"] = {"fetched_at": time.time(), "items": LISTING}
    cache["playlists"] = {
        "h1": {"snapshot_id": "s-h1", "tracks": [OTHER], "fetched_at": time.time()},
        "inA": {"snapshot_id": "s-inA", "tracks": [TRACK], "fetched_at": time.time()},
        "inB": {"snapshot_id": "s-inB", "tracks": [TRACK, OTHER], "fetched_at": time.time()},
        "homeless": {"snapshot_id": "s-hl", "tracks": [TRACK], "fetched_at": time.time()},
        "sub1": {"snapshot_id": "s-sub", "tracks": [TRACK], "fetched_at": time.time()},
    }
    store.save_cache(cache)
    store.save_config({**original_config,
                       "home_ids": ["h1"], "input_ids": ["inA", "inB", "homeless"],
                       "subset_ids": ["sub1"], "homeless_id": "homeless",
                       "input_name_pattern": r"^\[.+\]$"})

    removed, added = [], []
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri: added.append(pid) or f"s-{pid}-new")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist",
                        lambda pid, uri: removed.append(pid) or f"s-{pid}-new")
    # Filing into a home now writes to the library too (`_like_after_filing`).
    # It swallows its own failures, so an unmocked one does not fail a test —
    # it just spends the rate limiter's backoff in silence, which is how this
    # file once took 38s to assert something unrelated.
    monkeypatch.setattr(appmod.sp, "save_to_liked", lambda uri: None)
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)

    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    appmod.undo_stack.clear()
    appmod._ensure_profiles(force=True)

    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.removed, c.added = removed, added
    try:
        yield c
    finally:
        store.save_cache(original_cache)
        store.save_config(original_config)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod.undo_stack.clear()


def _inputs_holding(uri):
    return {l["id"] for l in appmod._profile_state["inputs"] if uri in l["uris"]}


def test_filing_sweeps_the_song_out_of_every_input(swept):
    res = swept.post("/api/act", json={
        "action": "move", "uri": TRACK["uri"], "from_id": "inA", "to_id": "h1",
        "sweep_inputs": True})
    assert res.status_code == 200, res.text
    assert set(swept.removed) == {"inA", "inB", "homeless"}
    assert swept.added == ["h1"]


def test_removing_sweeps_the_song_out_of_every_input(swept):
    res = swept.post("/api/act", json={
        "action": "remove", "uri": TRACK["uri"], "from_id": "inA",
        "sweep_inputs": True})
    assert res.status_code == 200, res.text
    assert set(swept.removed) == {"inA", "inB", "homeless"}


def test_the_destination_survives_its_own_sweep(swept):
    """The Homeless button files INTO an input. Sweeping the destination would
    delete the track the same request had just added."""
    res = swept.post("/api/act", json={
        "action": "move", "uri": TRACK["uri"], "from_id": "inA", "to_id": "homeless",
        "sweep_inputs": True})
    assert res.status_code == 200, res.text
    assert "homeless" not in swept.removed
    assert set(swept.removed) == {"inA", "inB"}


def test_a_subset_copy_is_not_an_input_and_is_left_alone(swept):
    """Subsets are selections, not inboxes — a song in a best-of is not
    waiting to be filed, so the sweep has no business there."""
    swept.post("/api/act", json={
        "action": "move", "uri": TRACK["uri"], "from_id": "inA", "to_id": "h1",
        "sweep_inputs": True})
    assert "sub1" not in swept.removed


def test_without_the_flag_nothing_but_the_named_input_is_touched(swept):
    """The sweep is opt-in. Everything that files through /api/act without
    asking for it — the split decide path most of all — keeps its old shape."""
    swept.post("/api/act", json={
        "action": "move", "uri": TRACK["uri"], "from_id": "inA", "to_id": "h1"})
    assert swept.removed == ["inA"]


def test_the_sweep_costs_nothing_when_the_song_has_one_home_only(swept):
    """The common case by a wide margin — 98.7% of tracks measured. One
    DELETE, exactly as before the sweep existed."""
    swept.post("/api/act", json={
        "action": "move", "uri": OTHER["uri"], "from_id": "inB", "to_id": "h1",
        "sweep_inputs": True})
    assert swept.removed == ["inB"]


def test_the_membership_the_card_reads_is_current_for_every_swept_input(swept):
    """Same invariant as the filing fix: what the server just did has to be
    visible to the very next request, for all three lists and not merely the
    one named."""
    swept.post("/api/act", json={
        "action": "move", "uri": TRACK["uri"], "from_id": "inA", "to_id": "h1",
        "sweep_inputs": True})
    assert _inputs_holding(TRACK["uri"]) == set()


def test_one_undo_puts_the_song_back_in_all_of_them(swept):
    """The sweep is one decision, so it must cost one undo — not three, and
    not one that restores a third of it."""
    swept.post("/api/act", json={
        "action": "move", "uri": TRACK["uri"], "from_id": "inA", "to_id": "h1",
        "sweep_inputs": True})
    swept.added.clear()
    assert swept.post("/api/undo").status_code == 200
    assert set(swept.added) == {"inA", "inB", "homeless"}
    assert _inputs_holding(TRACK["uri"]) == {"inA", "inB", "homeless"}
    assert swept.removed[-1] == "h1"   # the filing itself is undone too


def test_the_response_reports_what_the_sweep_reached(swept):
    """The client names the extra lists in its toast, so a write to a playlist
    the user was not looking at is never silent."""
    res = swept.post("/api/act", json={
        "action": "move", "uri": TRACK["uri"], "from_id": "inA", "to_id": "h1",
        "sweep_inputs": True}).json()
    assert sorted(res["swept"]) == ["[B]", "[Homeless]"]
