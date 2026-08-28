"""Playback control: skip, and switching which input playlist is playing.

These are the first calls sortify makes that *change* playback rather than
read it, so they need a scope the existing token does not carry. The failure
modes that actually happen in practice — a token predating the scope, and no
active device — have to arrive as something the UI can explain, not a raw 403.
"""

import pytest

from sortify.spotify import SCOPES, Spotify, SpotifyError
from sortify.store import Store


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.content = b"{}" if payload is not None else b""
        self.headers = {}
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def sp(tmp_path, monkeypatch):
    client = Spotify(Store(tmp_path))
    monkeypatch.setattr(client, "_access_token", lambda: "token")
    return client


def record(sp, monkeypatch, response=None):
    """Capture the outgoing request instead of sending it."""
    sent = {}

    def fake_request(method, url, **kwargs):
        sent.update(method=method, url=url, json=kwargs.get("json"))
        return response or FakeResponse()

    monkeypatch.setattr(sp.http, "request", fake_request)
    return sent


def test_the_token_asks_for_permission_to_change_playback():
    """Without this scope every playback call comes back 403, and the only
    cure is a fresh login — so the scope has to be in the auth URL."""
    assert "user-modify-playback-state" in SCOPES


def test_skip_next_asks_spotify_for_the_next_track(sp, monkeypatch):
    sent = record(sp, monkeypatch)

    sp.skip_next()

    assert sent["method"] == "POST"
    assert sent["url"].endswith("/me/player/next")


def test_skip_previous_asks_spotify_for_the_previous_track(sp, monkeypatch):
    sent = record(sp, monkeypatch)

    sp.skip_previous()

    assert sent["method"] == "POST"
    assert sent["url"].endswith("/me/player/previous")


def test_pause_stops_playback(sp, monkeypatch):
    sent = record(sp, monkeypatch)

    sp.pause_playback()

    assert sent["method"] == "PUT"
    assert sent["url"].endswith("/me/player/pause")


def test_resume_continues_without_restarting_the_context(sp, monkeypatch):
    """PUT /me/player/play with no body resumes where playback stopped; a body
    with a context_uri would restart the playlist from the top instead."""
    sent = record(sp, monkeypatch)

    sp.resume_playback()

    assert sent["method"] == "PUT"
    assert sent["url"].endswith("/me/player/play")
    assert sent["json"] is None


def test_playing_an_input_starts_that_playlist(sp, monkeypatch):
    sent = record(sp, monkeypatch)

    sp.play_context("37i9dQZF1DX")

    assert sent["method"] == "PUT"
    assert sent["url"].endswith("/me/player/play")
    assert sent["json"] == {"context_uri": "spotify:playlist:37i9dQZF1DX"}


def test_a_stale_token_is_reported_as_missing_permission(sp, monkeypatch):
    """Spotify answers 401 'Permissions missing' for a token that predates the
    scope. Retrying cannot fix it, so it must surface as its own error."""
    record(sp, monkeypatch, FakeResponse(401, text="Permissions missing"))

    with pytest.raises(SpotifyError, match="Permissions missing"):
        sp.skip_next()


def test_now_playing_art_is_the_mid_size_image(sp, monkeypatch):
    """Spotify lists album images largest-first ([640, 300, 64]). The now card
    displays art at up to ~240px, so the 64px thumbnail upscales to a blur and
    the 640px original is wasted bytes — the middle one is the right fetch.
    Same URL cost either way; this is a quality choice, not a budget one."""
    payload = {
        "item": {
            "uri": "spotify:track:x", "id": "x", "name": "X", "type": "track",
            "artists": [], "duration_ms": 1000,
            "album": {"name": "Alb", "images": [
                {"url": "http://img/640"}, {"url": "http://img/300"}, {"url": "http://img/64"},
            ]},
        },
        "is_playing": True, "progress_ms": 0,
    }
    record(sp, monkeypatch, FakeResponse(200, payload))

    assert sp.currently_playing()["track"]["image"] == "http://img/300"


def test_now_playing_art_survives_a_single_image(sp, monkeypatch):
    payload = {
        "item": {
            "uri": "spotify:track:x", "id": "x", "name": "X", "type": "track",
            "artists": [], "duration_ms": 1000,
            "album": {"name": "Alb", "images": [{"url": "http://img/only"}]},
        },
        "is_playing": True, "progress_ms": 0,
    }
    record(sp, monkeypatch, FakeResponse(200, payload))

    assert sp.currently_playing()["track"]["image"] == "http://img/only"


# ---- endpoints -------------------------------------------------------------


@pytest.fixture
def appmod(monkeypatch):
    from sortify import app as mod
    return mod


def test_next_endpoint_skips(appmod, monkeypatch):
    called = []
    monkeypatch.setattr(appmod.sp, "skip_next", lambda: called.append(True))

    assert appmod.player_next()["ok"] is True
    assert called == [True]


def test_previous_endpoint_goes_back(appmod, monkeypatch):
    called = []
    monkeypatch.setattr(appmod.sp, "skip_previous", lambda: called.append(True))

    assert appmod.player_previous()["ok"] is True
    assert called == [True]


def test_going_back_invalidates_the_now_cache(appmod, monkeypatch):
    """Same reasoning as skipping forward: the cache predicts the track that
    was playing, and going back just made that prediction wrong."""
    monkeypatch.setattr(appmod.sp, "skip_previous", lambda: None)
    appmod._now_cache.update(at=9_999_999.0, value={"is_playing": True}, ttl=300)

    appmod.player_previous()

    assert appmod._now_cache["at"] == 0.0


def test_going_back_arms_the_skip_settle_window(appmod, monkeypatch):
    """Spotify can still report the pre-press track for a moment after
    previous, exactly as after next — the settle guard has to cover both."""
    monkeypatch.setattr(appmod.sp, "skip_previous", lambda: None)
    appmod._now_cache.update(
        at=9_999_999.0, value={"track": {"uri": "spotify:track:before"}}, ttl=300)

    appmod.player_previous()

    assert appmod._skip_settle["uri"] == "spotify:track:before"


def test_pause_and_resume_endpoints(appmod, monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "pause_playback", lambda: calls.append("pause"))
    monkeypatch.setattr(appmod.sp, "resume_playback", lambda: calls.append("resume"))

    assert appmod.player_pause()["ok"] is True
    assert appmod.player_resume()["ok"] is True
    assert calls == ["pause", "resume"]


def test_pause_invalidates_the_now_cache(appmod, monkeypatch):
    """Same reasoning as skip: the cache now predicts is_playing wrongly, and
    the client's optimistic flip needs the next poll to fetch the truth."""
    monkeypatch.setattr(appmod.sp, "pause_playback", lambda: None)
    appmod._now_cache.update(at=9_999_999.0, value={"is_playing": True}, ttl=300)

    appmod.player_pause()

    assert appmod._now_cache["at"] == 0.0


def test_play_endpoint_starts_the_requested_input(appmod, monkeypatch):
    started = []
    monkeypatch.setattr(appmod.sp, "play_context", lambda pid: started.append(pid))

    appmod.player_play(appmod.PlayIn(input_id="abc123"))

    assert started == ["abc123"]


def test_missing_scope_is_explained_not_dumped(appmod, monkeypatch):
    """A token predating the scope is fixed by logging in again, so the message
    has to say that rather than surface 'Spotify API 401: ...'."""
    def boom():
        raise SpotifyError(401, "Permissions missing (token lacks a scope)")

    monkeypatch.setattr(appmod.sp, "skip_next", boom)

    with pytest.raises(appmod.HTTPException) as e:
        appmod.player_next()
    assert e.value.status_code == 400
    assert "log in again" in e.value.detail.lower()


def test_no_active_device_is_explained_not_dumped(appmod, monkeypatch):
    """Spotify 404s playback calls when nothing is playing anywhere. That is a
    normal state, not a fault, and the user needs to know what to do about it."""
    def boom():
        raise SpotifyError(404, '{"error":{"reason":"NO_ACTIVE_DEVICE"}}')

    monkeypatch.setattr(appmod.sp, "skip_next", boom)

    with pytest.raises(appmod.HTTPException) as e:
        appmod.player_next()
    assert e.value.status_code == 400
    assert "no active" in e.value.detail.lower()


def test_changing_playback_invalidates_the_now_cache(appmod, monkeypatch):
    """The now-cache is a prediction of what Spotify would say. Skipping makes
    that prediction known-wrong, and force=1 alone will not save us — it still
    serves the cache inside NOW_FORCE_MIN_INTERVAL, so the card would show the
    previous track for up to 10s after the press."""
    monkeypatch.setattr(appmod.sp, "skip_next", lambda: None)
    appmod._now_cache.update(at=9_999_999.0, value={"is_playing": True}, ttl=300)

    appmod.player_next()

    assert appmod._now_cache["at"] == 0.0


def test_liked_songs_cannot_be_started_as_a_playlist(appmod, monkeypatch):
    """Liked Songs is a pseudo-playlist with no real id, so
    spotify:playlist:liked is not a thing. Guard it rather than let Spotify
    answer with something unreadable."""
    called = []
    monkeypatch.setattr(appmod.sp, "play_context", lambda pid: called.append(pid))

    with pytest.raises(appmod.HTTPException) as e:
        appmod.player_play(appmod.PlayIn(input_id=appmod.LIKED_ID))
    assert e.value.status_code == 400
    assert called == []


def test_a_success_with_no_json_body_is_not_a_crash(sp, monkeypatch):
    """Spotify's playback endpoints answer a successful skip with a body that
    is not JSON. Parsing it unconditionally turned a working skip into a 500 —
    the track changed, the user saw an error, and because JSONDecodeError is
    not a SpotifyError the caller's cache invalidation was skipped as well."""
    record(sp, monkeypatch, FakeResponse(200, text=""))
    sp.http.request = lambda *a, **k: type(
        "R", (), {"status_code": 200, "content": b"\n", "headers": {}, "text": "",
                  "json": lambda self: (_ for _ in ()).throw(ValueError("no json"))}
    )()

    assert sp.skip_next() is None
