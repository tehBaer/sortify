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
        sent.update(method=method, url=url, json=kwargs.get("json"),
                    params=kwargs.get("params"))
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


def test_seek_moves_the_play_head_within_the_track(sp, monkeypatch):
    sent = record(sp, monkeypatch)

    sp.seek(42_000)

    assert sent["method"] == "PUT"
    assert sent["url"].endswith("/me/player/seek")
    assert sent["params"] == {"position_ms": 42_000}


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


def test_seek_endpoint_moves_the_head(appmod, monkeypatch):
    called = []
    monkeypatch.setattr(appmod.sp, "seek", lambda ms: called.append(ms))

    assert appmod.player_seek(appmod.SeekIn(position_ms=42_000))["ok"] is True
    assert called == [42_000]


def test_seeking_keeps_the_now_cache_instead_of_dropping_it(appmod, monkeypatch):
    """The one playback call whose result we can predict exactly.

    Skipping drops the cache because nobody knows what plays next. A seek
    changes one number, and it is a number we chose — so the cached answer is
    put back with the new position rather than thrown away, and the seek costs
    the one call it makes instead of one plus the poll behind it.
    """
    monkeypatch.setattr(appmod.sp, "seek", lambda ms: None)
    appmod._now_cache.update(
        at=9_999_999.0, ttl=300,
        value={"is_playing": True, "progress_ms": 1_000,
               "track": {"uri": "spotify:track:s1", "duration_ms": 200_000}})

    appmod.player_seek(appmod.SeekIn(position_ms=120_000))

    assert appmod._now_cache["at"] > 0.0, "the answer was dropped, not repositioned"
    assert appmod._now_cache["value"]["progress_ms"] == 120_000


def test_seeking_re_times_the_poll_schedule(appmod, monkeypatch):
    """The TTL *is* the track's remaining runtime, so moving the head moves
    the moment the client should come back. Seek to 40s before the end and the
    next poll must be due in ~40s, not at the old position's ~199s."""
    monkeypatch.setattr(appmod.sp, "seek", lambda ms: None)
    appmod._now_cache.update(
        at=9_999_999.0, ttl=300,
        value={"is_playing": True, "progress_ms": 1_000,
               "track": {"uri": "spotify:track:s1", "duration_ms": 200_000}})

    appmod.player_seek(appmod.SeekIn(position_ms=160_000))

    assert appmod._now_cache["ttl"] == pytest.approx(41.0)


def test_seeking_with_nothing_cached_does_not_invent_an_answer(appmod, monkeypatch):
    """No cached track means nothing to reposition — and a fabricated cache
    entry would be worse than the extra poll it saves."""
    monkeypatch.setattr(appmod.sp, "seek", lambda ms: None)
    appmod._now_cache.update(at=9_999_999.0, ttl=300, value=None)

    appmod.player_seek(appmod.SeekIn(position_ms=5_000))

    assert appmod._now_cache["value"] is None
    assert appmod._now_cache["at"] == 0.0   # _playback_call's drop stands


def test_a_negative_seek_is_refused_before_it_costs_a_call(appmod, monkeypatch):
    called = []
    monkeypatch.setattr(appmod.sp, "seek", lambda ms: called.append(ms))

    with pytest.raises(appmod.HTTPException) as e:
        appmod.player_seek(appmod.SeekIn(position_ms=-1))
    assert e.value.status_code == 400
    assert called == []


# ---- repeat, and the endpoint that can see it ------------------------------
#
# Repeat is the one playback fact /me/player/currently-playing does not
# report. /me/player does, for the same single call — behind a scope the app
# only started asking for in Aug 2026. So the endpoint follows the TOKEN, not
# the constant: every check below is about degrading to the old behaviour
# rather than to a 403 on a polling path.


def test_the_token_asks_to_read_playback_state():
    assert "user-read-playback-state" in SCOPES


def test_a_login_records_what_spotify_actually_granted(sp):
    """SCOPES is what we asked for; the token carries what we got. Only the
    second one can gate anything."""
    sp._store_token_response({"access_token": "a", "expires_in": 3600,
                              "scope": "user-modify-playback-state"})

    assert sp.has_scope("user-modify-playback-state")
    assert not sp.has_scope("user-read-playback-state")


def test_an_unrecorded_scope_reads_as_absent(sp):
    """A token from before the scope string was saved must degrade quietly,
    not claim permissions it may not have."""
    sp.store.save_tokens({"access_token": "a", "expires_at": 9e9})

    assert not sp.has_scope("user-read-playback-state")


def test_without_the_scope_we_poll_exactly_what_we_always_did(sp):
    sp.store.save_tokens({"scope": "user-modify-playback-state"})

    assert sp._now_playing_path() == "/me/player/currently-playing"


def test_with_the_scope_the_richer_endpoint_answers_instead(sp):
    """Same one call, and it carries repeat_state — there is no cheaper way
    to know the loop state, and no more expensive one either."""
    sp.store.save_tokens({"scope": "user-read-playback-state"})

    assert sp._now_playing_path() == "/me/player"


def test_repeat_state_comes_back_with_the_track(sp, monkeypatch):
    sp.store.save_tokens({"scope": "user-read-playback-state"})
    payload = {
        "item": {"uri": "spotify:track:x", "id": "x", "name": "X", "type": "track",
                 "artists": [], "duration_ms": 1000, "album": {"name": "A", "images": []}},
        "is_playing": True, "progress_ms": 0, "repeat_state": "context",
    }
    record(sp, monkeypatch, FakeResponse(200, payload))

    assert sp.currently_playing()["repeat"] == "context"


def test_the_old_endpoint_reports_repeat_as_unknown(sp, monkeypatch):
    """None is "we cannot see it", which the UI draws differently from "off" —
    a toggle that shows off when it means unknown is a toggle that lies."""
    sp.store.save_tokens({"scope": "user-modify-playback-state"})
    payload = {
        "item": {"uri": "spotify:track:x", "id": "x", "name": "X", "type": "track",
                 "artists": [], "duration_ms": 1000, "album": {"name": "A", "images": []}},
        "is_playing": True, "progress_ms": 0,
    }
    record(sp, monkeypatch, FakeResponse(200, payload))

    assert sp.currently_playing()["repeat"] is None


def test_an_unusable_player_endpoint_degrades_once_and_stays_degraded(sp, monkeypatch):
    """If /me/player cannot be used on this account — dev mode not serving it,
    or Spotify disagreeing about the scope — every poll must not spend a call
    rediscovering that. One no, remembered."""
    sp.store.save_tokens({"scope": "user-read-playback-state"})
    record(sp, monkeypatch, FakeResponse(403, text="Forbidden"))

    assert sp.currently_playing() is None
    assert sp._now_playing_path() == "/me/player/currently-playing"


def test_logging_in_again_gives_the_richer_endpoint_another_chance(sp):
    """The degrade is one-way per token, not forever: a fresh login is exactly
    the event that could change the answer."""
    sp.store.save_tokens({"scope": "user-read-playback-state", "no_me_player": True})
    assert sp._now_playing_path() == "/me/player/currently-playing"

    sp._store_token_response({"access_token": "a", "expires_in": 3600,
                              "scope": "user-read-playback-state"})

    assert sp._now_playing_path() == "/me/player"


def test_a_real_failure_is_not_swallowed_as_a_degrade(sp, monkeypatch):
    """Only the three "you cannot use this" statuses degrade. Anything else —
    a 500, a rate limit — must reach the caller: turning it into "nothing is
    playing" would hide a real outage behind an empty card, and would give up
    the richer endpoint over a blip."""
    sp.store.save_tokens({"scope": "user-read-playback-state"})
    record(sp, monkeypatch, FakeResponse(500, text="upstream"))

    with pytest.raises(SpotifyError):
        sp.currently_playing()
    assert not sp.store.tokens().get("no_me_player")
    assert sp._now_playing_path() == "/me/player"


def test_set_repeat_asks_for_the_mode_by_name(sp, monkeypatch):
    sent = record(sp, monkeypatch)

    sp.set_repeat("context")

    assert sent["method"] == "PUT"
    assert sent["url"].endswith("/me/player/repeat")
    assert sent["params"] == {"state": "context"}


def test_repeat_endpoint_sets_the_mode(appmod, monkeypatch):
    called = []
    monkeypatch.setattr(appmod.sp, "set_repeat", lambda st: called.append(st))

    assert appmod.player_repeat(appmod.RepeatIn(state="context"))["ok"] is True
    assert called == ["context"]


def test_an_unknown_repeat_mode_is_refused_before_it_costs_a_call(appmod, monkeypatch):
    called = []
    monkeypatch.setattr(appmod.sp, "set_repeat", lambda st: called.append(st))

    with pytest.raises(appmod.HTTPException) as e:
        appmod.player_repeat(appmod.RepeatIn(state="sideways"))
    assert e.value.status_code == 400
    assert called == []


def test_setting_repeat_repositions_the_now_cache_rather_than_dropping_it(appmod, monkeypatch):
    """We are the ones setting it, so the cached answer can be corrected in
    place. Dropping it would make the press cost the poll behind it too — and
    that poll would only report what we just said."""
    monkeypatch.setattr(appmod.sp, "set_repeat", lambda st: None)
    appmod._now_cache.update(
        at=9_999_999.0, ttl=123.0,
        value={"is_playing": True, "progress_ms": 1_000, "repeat": "off",
               "track": {"uri": "spotify:track:r1", "duration_ms": 200_000}})

    appmod.player_repeat(appmod.RepeatIn(state="context"))

    assert appmod._now_cache["value"]["repeat"] == "context"
    assert appmod._now_cache["at"] == 9_999_999.0
    # Looping changes nothing about how long the rest of the answer stays true.
    assert appmod._now_cache["ttl"] == 123.0
