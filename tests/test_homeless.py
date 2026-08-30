"""The "Homeless" destination: a buffer for songs no home fits.

The button that files there is ordinary /api/act — a move out of the input
into another input — so nothing new happens server-side except naming the
destination in the now payload. What these tests pin is that naming: which
id the client is told to aim at, and when it is told nothing at all.

`homeless_id` is a config key, not a name convention. The playlist is
called `[Homeless]` in the live library, which makes it a buffer input
for free — but the button must not depend on that name, because renaming it
would silently move the destination.

Zero-Spotify-call throughout: fake listings and monkeypatched clients only.
"""

import pytest

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

LISTING = [
    {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
     "total": 12, "snapshot_id": "s-h1", "image": None, "description": ""},
    {"id": "buf", "name": "[Hazy]", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-buf", "image": None, "description": ""},
    {"id": "nh", "name": "[Homeless]", "owner": "me", "editable": True,
     "total": 0, "snapshot_id": "s-nh", "image": None, "description": ""},
]

TRACKS = {
    "h1": [{"uri": "spotify:track:a", "id": "a", "name": "A", "is_local": False,
            "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
            "added_at": "2026-01-01T00:00:00Z"}],
    "buf": [{"uri": "spotify:track:z", "id": "z", "name": "Z", "is_local": False,
             "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
             "added_at": "2026-01-01T00:00:00Z"}],
}

PLAYING = {"uri": "spotify:track:z", "id": "z", "name": "Z", "type": "track",
           "is_local": False, "duration_ms": 210_000,
           "artists": [{"id": "ar1", "name": "Ar One"}],
           "album": {"name": "Alb", "images": [{"url": "http://img/1"}]}}


def _cfg(**over):
    base = {"input_ids": [], "home_ids": ["h1"], "subset_ids": [],
            "input_name_pattern": r"^\[.+\]$"}
    base.update(over)
    return base


@pytest.fixture
def wired(monkeypatch):
    def build(**cfg_over):
        appmod.store.save_config(_cfg(**cfg_over))
        monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
        monkeypatch.setattr(appmod, "_cached_tracks", lambda pid, snap: TRACKS.get(pid, []))
        appmod._profile_state.clear()
        appmod._profile_state["built_at"] = 0.0
        appmod._ensure_profiles(force=True)
        return appmod._suggestion_payload(
            {"track": PLAYING, "context_playlist_id": "buf"})
    original_cache, original_config = appmod.store.cache(), appmod.store.config()
    yield build
    appmod.store.save_cache(original_cache)
    appmod.store.save_config(original_config)
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0


def test_the_now_payload_names_the_configured_destination(wired):
    assert wired(homeless_id="nh")["homeless_id"] == "nh"


def test_no_destination_configured_means_no_button(wired):
    """Absent config is the ordinary state of a fresh install, not an error.

    The client renders the button only when this is truthy, so None here is
    what keeps the feature invisible until the destination exists.
    """
    assert wired()["homeless_id"] is None


def test_a_destination_that_is_not_a_resolved_input_is_withheld(wired):
    """A stale id — playlist deleted, or renamed out of the buffer pattern —
    must not be offered. Clicking it would 404 on the add, and the config key
    is the only thing standing between the button and a dead destination."""
    assert wired(homeless_id="ghost")["homeless_id"] is None
    assert wired(homeless_id="h1")["homeless_id"] is None


def test_a_config_written_before_the_rename_still_names_the_destination(wired):
    """The concept was renamed Needs a home -> Homeless and the config key
    followed it. A config file written before that names the same playlist by
    the same id under the old key, and nothing about the destination changed —
    so the old key is still read rather than silently resolving to no button."""
    assert wired(needs_home_id="nh")["homeless_id"] == "nh"


def test_the_destination_is_still_an_ordinary_input(wired):
    """It carries `has_track` like any other input, which is how the client
    knows to hide the button for a song already sitting there."""
    inputs = {l["id"]: l for l in wired(homeless_id="nh")["inputs"]}
    assert inputs["nh"]["has_track"] is False
    assert inputs["buf"]["has_track"] is True
