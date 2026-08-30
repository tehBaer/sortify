"""Landing a song in a home also saves it to the library.

Filing into a home is the verdict "this is mine and it belongs here", and the
library is where that verdict is legible outside sortify. So every path into a
home likes the song — the now card, the triage view, the split's Keep — and
only those. The Homeless buffer and the capture chips are inboxes, a subset is
a selection; neither is a judgement that the song is worth keeping.

Two deliberate asymmetries, both chosen by the user when asked:

  - Undo does NOT unlike. A song can have been liked for years before it was
    ever filed, and telling that apart costs a /me/library/contains call on
    every single filing. Rather than spend that on all of them — or strip an
    old like off the one song someone misfiled and undid — undo leaves the
    library alone. The wart is real and known: undoing a misfile leaves a like
    behind, to be cleared in Spotify.
  - A failed like never fails the filing. It is the second, softer write of
    the pair, and by the time it runs the filing has already landed. In the
    split path it must not even be visible, or it would trip a rollback
    contract that exists to stop a successful add being un-decided.

Zero Spotify calls: every write is trapped.
"""

import time

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)
from sortify.spotify import LIKED_ID
from sortify.store import Store

LISTING = [
    {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
     "total": 5, "snapshot_id": "s-h1", "image": None, "description": ""},
    {"id": "inA", "name": "[A]", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-inA", "image": None, "description": ""},
    {"id": "homeless", "name": "[Homeless]", "owner": "me", "editable": True,
     "total": 1, "snapshot_id": "s-hl", "image": None, "description": ""},
    {"id": "sub1", "name": "best of", "owner": "me", "editable": True,
     "total": 2, "snapshot_id": "s-sub", "image": None, "description": ""},
]

TRACK = {"uri": "spotify:track:z", "id": "z", "name": "Z", "is_local": False,
         "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
         "added_at": "2026-02-02T00:00:00Z"}


@pytest.fixture
def filing(monkeypatch):
    store = appmod.store
    original_cache, original_config = store.cache(), store.config()

    cache = store.cache()
    cache["playlist_list"] = {"fetched_at": time.time(), "items": LISTING}
    cache["playlists"] = {
        "h1": {"snapshot_id": "s-h1", "tracks": [], "fetched_at": time.time()},
        "inA": {"snapshot_id": "s-inA", "tracks": [TRACK], "fetched_at": time.time()},
        "homeless": {"snapshot_id": "s-hl", "tracks": [], "fetched_at": time.time()},
        "sub1": {"snapshot_id": "s-sub", "tracks": [], "fetched_at": time.time()},
    }
    store.save_cache(cache)
    store.save_config({**original_config, "home_ids": ["h1"], "input_ids": ["inA", "homeless"],
                       "subset_ids": ["sub1"], "homeless_id": "homeless",
                       "input_name_pattern": r"^\[.+\]$",
                       # Opt in explicitly: the feature ships OFF (see
                       # `test_it_is_off_unless_the_config_asks_for_it`), so
                       # every assertion below is about what it does WHEN
                       # enabled, not about what happens by default.
                       "like_on_filing": True})

    liked, unliked = [], []
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", lambda pid, uri: f"s-{pid}-new")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist",
                        lambda pid, uri: (unliked.append(uri) if pid == LIKED_ID else None)
                        or f"s-{pid}-new")
    monkeypatch.setattr(appmod.sp, "save_to_liked", lambda uri: liked.append(uri))
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)

    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    appmod.undo_stack.clear()
    appmod._ensure_profiles(force=True)

    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.liked, c.unliked = liked, unliked
    try:
        yield c
    finally:
        store.save_cache(original_cache)
        store.save_config(original_config)
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod.undo_stack.clear()


def _file(client, to_id, **over):
    body = {"action": "move", "uri": TRACK["uri"], "from_id": "inA", "to_id": to_id}
    body.update(over)
    return client.post("/api/act", json=body)


def test_it_is_off_unless_the_config_asks_for_it(filing):
    """The default. Liking on every filing costs a call per filing, and the
    plan that replaces it is a weekly batch over what the homes gained — so
    the per-filing write stays behind a flag that is off."""
    cfg = appmod.store.config()
    appmod.store.save_config({**cfg, "like_on_filing": False})
    res = _file(filing, "h1")
    assert res.status_code == 200, res.text
    assert filing.liked == []
    assert res.json()["liked"] is False


def test_filing_into_a_home_likes_the_song(filing):
    res = _file(filing, "h1")
    assert res.status_code == 200, res.text
    assert filing.liked == [TRACK["uri"]]
    assert res.json()["liked"] is True


def test_the_homeless_buffer_is_an_inbox_and_does_not_like(filing):
    """"No home fits this" is the opposite of a verdict that it is worth
    keeping — and Homeless is an input, not a home."""
    assert _file(filing, "homeless", from_id="inA").status_code == 200
    assert filing.liked == []


def test_a_subset_add_does_not_like(filing):
    """A song in a best-of has not been sorted; the selection says nothing
    about whether the song has a home."""
    assert _file(filing, "sub1", from_id=None).status_code == 200
    assert filing.liked == []


def test_capturing_into_an_input_does_not_like(filing):
    assert _file(filing, "inA", from_id=None).status_code == 200
    assert filing.liked == []


def test_undo_leaves_the_library_alone(filing):
    """The chosen asymmetry: undo cannot know whether the like was ours, and
    finding out costs a call on every filing. It leaves it."""
    _file(filing, "h1")
    filing.liked.clear()
    assert filing.post("/api/undo").status_code == 200
    assert filing.unliked == []


def test_a_song_already_in_the_destination_is_not_liked_again(filing):
    """Nothing was added, so nothing was filed — pressing the same home twice
    must not spend a second library write."""
    cache = appmod.store.cache()
    cache["playlists"]["h1"]["tracks"] = [TRACK]
    appmod.store.save_cache(cache)
    res = _file(filing, "h1")
    assert res.status_code == 200
    assert filing.liked == []
    assert res.json()["liked"] is False


def test_a_failed_like_never_fails_the_filing(filing, monkeypatch):
    """The filing has already landed by then. Losing the user's place in the
    queue over the softer of the two writes would be the worse failure."""
    def boom(uri):
        raise RuntimeError("library unavailable")
    monkeypatch.setattr(appmod.sp, "save_to_liked", boom)
    res = _file(filing, "h1")
    assert res.status_code == 200, res.text
    assert res.json()["liked"] is False


def test_filing_to_liked_itself_is_still_one_write(filing):
    """Liked Songs as a destination already IS the library write — it must not
    be followed by a second, redundant one."""
    res = _file(filing, LIKED_ID)
    assert res.status_code == 200, res.text
    assert filing.liked == [TRACK["uri"]]


# ---- the split's Keep, the third path into a home --------------------------


def _split_payload():
    return {"version": 1, "splits": {"PLL": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None, "params": {},
        "piles": [{"id": "p1", "name": "pile", "tags": [], "uris": [TRACK["uri"]]}],
        "decided": {}, "active_sitting": None}}}


def test_a_split_keep_likes_the_song_too(filing):
    """Same verdict reached by a different screen — the rule follows the
    destination, not the button."""
    s = Store()
    original = s.splits()
    s.save_splits(_split_payload())
    appmod._pending_keeps.clear()
    try:
        res = filing.post("/api/split/PLL/decide",
                          json={"uri": TRACK["uri"], "action": "keep", "to_id": "h1"})
        assert res.status_code == 200, res.text
        assert filing.liked == [TRACK["uri"]]
    finally:
        s.save_splits(original)
        appmod._pending_keeps.clear()


def test_a_split_reject_likes_nothing(filing):
    s = Store()
    original = s.splits()
    s.save_splits(_split_payload())
    appmod._pending_keeps.clear()
    try:
        res = filing.post("/api/split/PLL/decide",
                          json={"uri": TRACK["uri"], "action": "reject"})
        assert res.status_code == 200, res.text
        assert filing.liked == []
    finally:
        s.save_splits(original)
        appmod._pending_keeps.clear()
