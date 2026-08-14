"""The background genre enricher — the job that earned the 2026-08-13 ban.

These drive the real loop with the HTTP boundary stubbed, so the budget
accounting and pacing under test are the ones that actually ship.
"""

import time

import pytest

from sortify import app as appmod
from sortify.app import ENRICH_IDLE_SLEEP, ENRICH_INTERVAL, _next_missing_artist
from sortify.spotify import BACKGROUND_DAILY_CAP, QUIET_AFTER_COOLDOWN, Spotify
from sortify.store import Store


class Stop(BaseException):
    """Breaks the enricher's `while True` — BaseException dodges its own
    `except Exception` backstop."""


class FakeResp:
    status_code = 200
    content = b"{}"

    @staticmethod
    def json():
        return {"name": "Artist", "genres": ["rock"]}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        return FakeResp()


def _seeded_store(tmp_path, artist_count):
    store = Store(tmp_path)
    tracks = [
        {"uri": f"spotify:track:{i}", "artists": [{"id": f"a{i}", "name": None}]}
        for i in range(artist_count)
    ]
    store.save_cache({"playlists": {"p1": {"tracks": tracks}}, "artists": {}, "me": None})
    return store


@pytest.fixture
def enricher(tmp_path, monkeypatch):
    """The app's enricher wired to a temp store and a stubbed transport."""
    store = _seeded_store(tmp_path, artist_count=BACKGROUND_DAILY_CAP * 3)
    sp = Spotify(store)
    http = FakeHttp()
    sp.http = http
    monkeypatch.setattr(sp, "_access_token", lambda: "token")
    monkeypatch.setattr(appmod, "store", store)
    monkeypatch.setattr(appmod, "sp", sp)

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        # Once it has idled twice it is parked for good; stop the loop.
        if sleeps.count(ENRICH_IDLE_SLEEP) >= 2:
            raise Stop()

    # app.py and spotify.py share the one `time` module, so this also disarms
    # the pacing sleeps inside artists_genres and the rolling-window wait.
    monkeypatch.setattr(time, "sleep", fake_sleep)
    return sp, http, sleeps


def _run(enricher):
    with pytest.raises(Stop):
        appmod._genre_enricher()


def test_enricher_stops_at_the_background_cap(enricher):
    sp, http, sleeps = enricher
    _run(enricher)
    assert len(http.calls) == BACKGROUND_DAILY_CAP
    assert sp.background_spent() == BACKGROUND_DAILY_CAP


def test_enricher_paces_in_minutes_between_fetches(enricher):
    sp, http, sleeps = enricher
    _run(enricher)
    # Every successful fetch is followed by the long pace interval. The old
    # loop slept 30s and fetched 5 at a time — ~580 calls/hour.
    assert sleeps.count(ENRICH_INTERVAL) == BACKGROUND_DAILY_CAP
    assert 3600 / ENRICH_INTERVAL <= 12  # peak rate, calls/hour
    assert BACKGROUND_DAILY_CAP <= 3600 / ENRICH_INTERVAL * 4  # ~4h to exhaust, then parked


def test_enricher_makes_no_calls_during_quiet_period(tmp_path, monkeypatch, enricher):
    """The 2026-08-13 regression: a cooldown that just lifted must not restart
    proactive traffic."""
    sp, http, sleeps = enricher
    sp.store.save_tokens({"cooldown_until": time.time() - 60})
    sp.cooldown_until = time.time() - 60
    _run(enricher)
    assert http.calls == []


def test_enricher_resumes_once_the_quiet_period_has_passed(enricher):
    sp, http, sleeps = enricher
    sp.store.save_tokens({"cooldown_until": time.time() - QUIET_AFTER_COOLDOWN - 60})
    _run(enricher)
    assert len(http.calls) == BACKGROUND_DAILY_CAP


def test_next_missing_artist_skips_known_and_returns_none_when_done(tmp_path, monkeypatch):
    store = _seeded_store(tmp_path, artist_count=3)
    monkeypatch.setattr(appmod, "store", store)
    assert _next_missing_artist() == "a0"

    cache = store.cache()
    cache["artists"] = {"a0": {}, "a1": {}}
    store.save_cache(cache)
    assert _next_missing_artist() == "a2"

    cache = store.cache()
    cache["artists"]["a2"] = {}
    store.save_cache(cache)
    assert _next_missing_artist() is None
