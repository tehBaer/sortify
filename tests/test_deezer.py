"""Deezer BPM: the client's parsing, and the now-playing fetch discipline.

Deezer is not under the Spotify budget, but the same manners apply to the
/api/now path: fetch only on ?force=1, write-once, floored attempts, and a
failure must never break the response it rides on.
"""

import httpx
import pytest

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)
from sortify.deezer import Deezer, DeezerError
from sortify.store import Store
from sortify.tags import track_key


def client_with(routes: dict):
    """A Deezer whose transport answers from a {path_prefix: payload} map."""

    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, payload in routes.items():
            if request.url.path.startswith(prefix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404)

    dz = Deezer()
    dz._client = httpx.Client(transport=httpx.MockTransport(handler))
    return dz


def test_fetch_track_returns_bpm_from_search_then_detail():
    dz = client_with({
        "/search": {"data": [{"id": 3135556}]},
        "/track/3135556": {"id": 3135556, "bpm": 123.4},
    })
    rec = dz.fetch_track("Daft Punk", "Harder Better")
    assert rec["bpm"] == 123.4
    assert rec["deezer_id"] == 3135556
    assert rec["fetched_at"] > 0


def test_no_search_hit_is_a_permanent_miss():
    dz = client_with({"/search": {"data": []}})
    assert dz.fetch_track("Nobody", "Nothing") == {"miss": True}


def test_bpm_zero_means_deezer_does_not_know_and_is_a_miss():
    dz = client_with({
        "/search": {"data": [{"id": 1}]},
        "/track/1": {"id": 1, "bpm": 0},
    })
    assert dz.fetch_track("A", "B") == {"miss": True}


def test_deezer_error_payload_raises_instead_of_recording_a_miss():
    """Deezer reports quota errors as HTTP 200 with an error body. Recording
    that as a miss would make a temporary outage permanent for this track."""
    dz = client_with({"/search": {"error": {"type": "Exception", "code": 4}}})
    with pytest.raises(DeezerError):
        dz.fetch_track("A", "B")


# ---- the now-playing fetch helper ------------------------------------------


TRACK = {"uri": "spotify:track:x", "name": "X", "id": "x", "type": "track",
         "is_local": False, "artists": [{"id": "a1", "name": "A"}]}


@pytest.fixture
def clean_bpm_state(monkeypatch):
    s = Store()
    original = s.deezer_tracks()
    monkeypatch.setattr(appmod, "_now_bpm_last_attempt", 0.0)
    yield s
    s.save_deezer_tracks(original)


def test_fetch_writes_once_and_respects_the_floor(clean_bpm_state, monkeypatch):
    calls = []

    class FakeDz:
        def fetch_track(self, artist, title):
            calls.append((artist, title))
            return {"bpm": 120.0, "deezer_id": 1, "fetched_at": 1.0}

    monkeypatch.setattr(appmod, "_deezer_client", lambda: FakeDz())
    t = [1000.0]

    appmod._fetch_missing_now_bpm(TRACK, clock=lambda: t[0])
    assert calls == [("A", "X")]
    assert clean_bpm_state.deezer_map()[track_key("A", "X")]["bpm"] == 120.0

    # Known track: no second fetch even after the floor passes.
    t[0] += appmod.NOW_BPM_MIN_INTERVAL + 1
    appmod._fetch_missing_now_bpm(TRACK, clock=lambda: t[0])
    assert len(calls) == 1

    # Unknown track inside the floor: attempt suppressed.
    other = {**TRACK, "name": "Y"}
    appmod._fetch_missing_now_bpm(other, clock=lambda: t[0])
    assert len(calls) == 2  # floor had passed, so this one runs...
    appmod._fetch_missing_now_bpm({**TRACK, "name": "Z"}, clock=lambda: t[0])
    assert len(calls) == 2  # ...and this one, 0s later, is floored out.


def test_a_deezer_failure_records_nothing_and_does_not_raise(clean_bpm_state, monkeypatch):
    class BoomDz:
        def fetch_track(self, artist, title):
            raise DeezerError("quota")

    monkeypatch.setattr(appmod, "_deezer_client", lambda: BoomDz())
    appmod._fetch_missing_now_bpm(TRACK, clock=lambda: 1000.0)
    assert track_key("A", "X") not in clean_bpm_state.deezer_map()
