"""Deezer preview clips: the client's parsing, and the /api/playlist_preview
route's cache-only track picking.

Deezer is not under the Spotify budget, but errors still matter: Deezer
reports quota trips as HTTP 200 with an error body, which must raise
(retryable) rather than read as a permanent miss.
"""

import httpx
import pytest

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)
from sortify.deezer import Deezer, DeezerError
from sortify.store import Store


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


# ---- preview clips (picker hold-to-preview) --------------------------------


def test_fetch_preview_returns_url_from_search_alone():
    dz = client_with({
        "/search": {"data": [{"id": 42, "preview": "https://cdn-preview.dzcdn.net/x.mp3"}]},
    })
    rec = dz.fetch_preview("Daft Punk", "Harder Better")
    assert rec == {"url": "https://cdn-preview.dzcdn.net/x.mp3", "deezer_id": 42}


def test_fetch_preview_miss_when_unknown_or_no_preview():
    dz = client_with({"/search": {"data": []}})
    assert dz.fetch_preview("Nobody", "Nothing") == {"miss": True}
    dz2 = client_with({"/search": {"data": [{"id": 42, "preview": ""}]}})
    assert dz2.fetch_preview("Known", "No Clip") == {"miss": True}


def test_fetch_preview_error_payload_raises_not_miss():
    dz = client_with({"/search": {"error": {"code": 4, "message": "quota"}}})
    with pytest.raises(DeezerError):
        dz.fetch_preview("A", "B")


# ---- /api/playlist_preview: cache-only track pick, Deezer-only audio -------


def _seed_preview_playlist(s: Store):
    cache = s.cache()
    cache["playlists"]["pv1"] = {
        "snapshot_id": "snap",
        "tracks": [
            {"uri": f"spotify:track:t{i}", "id": f"t{i}", "type": "track",
             "is_local": False, "name": f"Song {i}",
             "artists": [{"id": f"a{i}", "name": f"Artist {i}"}],
             "added_at": f"2026-08-{10 + i:02d}T10:00:00Z"}
            for i in range(5)
        ],
        "fetched_at": 0.0,
    }
    s.save_cache(cache)
    return cache


class ScriptedPreviews:
    def __init__(self, results):
        self.results = results  # {title: dict | Exception}
        self.calls = []

    def fetch_preview(self, artist, title):
        self.calls.append(title)
        r = self.results.get(title, {"miss": True})
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture()
def preview_env(monkeypatch):
    s = Store()
    original = s.cache()
    _seed_preview_playlist(s)
    appmod._preview_cache.clear()
    yield s
    s.save_cache(original)
    appmod._preview_cache.clear()


def test_preview_endpoint_resolves_newest_tracks_first(preview_env, monkeypatch):
    fake = ScriptedPreviews({
        "Song 4": {"url": "u4", "deezer_id": 4},
        "Song 3": {"url": "u3", "deezer_id": 3},
        "Song 2": {"url": "u2", "deezer_id": 2},
    })
    monkeypatch.setattr(appmod, "_deezer_client", lambda: fake)
    out = appmod.playlist_preview("pv1")
    assert [c["url"] for c in out["clips"]] == ["u4", "u3", "u2"]
    assert out["clips"][0]["name"] == "Song 4"
    assert out["clips"][0]["artist"] == "Artist 4"
    assert fake.calls == ["Song 4", "Song 3", "Song 2"]  # newest first, stops at 3
    assert out["total"] == 5
    assert [t["name"] for t in out["tracks"][:2]] == ["Song 4", "Song 3"]


def test_preview_endpoint_skips_misses_and_survives_deezer_errors(preview_env, monkeypatch):
    fake = ScriptedPreviews({
        "Song 4": {"miss": True},
        "Song 3": {"url": "u3", "deezer_id": 3},
        "Song 2": DeezerError("quota"),
        "Song 1": {"url": "u1", "deezer_id": 1},
        "Song 0": {"url": "u0", "deezer_id": 0},
    })
    monkeypatch.setattr(appmod, "_deezer_client", lambda: fake)
    out = appmod.playlist_preview("pv1")
    # miss skipped, error skipped, next candidates fill the medley
    assert [c["url"] for c in out["clips"]] == ["u3", "u1", "u0"]
    # the text fallback list is always there regardless
    assert len(out["tracks"]) == 5


def test_preview_endpoint_caches_resolution_briefly(preview_env, monkeypatch):
    fake = ScriptedPreviews({"Song 4": {"url": "u4", "deezer_id": 4},
                             "Song 3": {"url": "u3", "deezer_id": 3},
                             "Song 2": {"url": "u2", "deezer_id": 2}})
    monkeypatch.setattr(appmod, "_deezer_client", lambda: fake)
    first = appmod.playlist_preview("pv1")
    second = appmod.playlist_preview("pv1")
    assert second["clips"] == first["clips"]
    assert len(fake.calls) == 3  # the second hold cost zero Deezer requests


def test_preview_endpoint_404s_for_uncached_playlist(preview_env):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        appmod.playlist_preview("nope")
    assert e.value.status_code == 404


def test_preview_endpoint_pages_older_tracks_with_offset(preview_env, monkeypatch):
    fake = ScriptedPreviews({f"Song {i}": {"url": f"u{i}", "deezer_id": i} for i in range(5)})
    monkeypatch.setattr(appmod, "_deezer_client", lambda: fake)
    first = appmod.playlist_preview("pv1")
    assert [c["url"] for c in first["clips"]] == ["u4", "u3", "u2"]
    assert first["next_offset"] == 3
    second = appmod.playlist_preview("pv1", offset=3)
    assert [c["url"] for c in second["clips"]] == ["u1", "u0"]
    assert second["next_offset"] is None  # exhausted — the medley ends here
    assert fake.calls == ["Song 4", "Song 3", "Song 2", "Song 1", "Song 0"]


def test_preview_endpoint_caches_each_page_separately(preview_env, monkeypatch):
    fake = ScriptedPreviews({f"Song {i}": {"url": f"u{i}", "deezer_id": i} for i in range(5)})
    monkeypatch.setattr(appmod, "_deezer_client", lambda: fake)
    appmod.playlist_preview("pv1")
    appmod.playlist_preview("pv1", offset=3)
    n = len(fake.calls)
    appmod.playlist_preview("pv1")
    appmod.playlist_preview("pv1", offset=3)
    assert len(fake.calls) == n  # both pages served from cache


# ---- /api/preview_resume: one budgeted call to un-pause after a preview ----


def test_preview_resume_spends_one_resume_call(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "resume_playback", lambda: calls.append(1), raising=False)
    appmod._preview_resume_last = -1e9
    out = appmod.preview_resume()
    assert out == {"ok": True}
    assert calls == [1]


def test_preview_resume_floors_bursts_without_spending(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "resume_playback", lambda: calls.append(1), raising=False)
    appmod._preview_resume_last = -1e9
    appmod.preview_resume()
    out = appmod.preview_resume()   # immediately again — must not spend
    assert out["ok"] is False
    assert calls == [1]


def test_preview_resume_reports_spotify_refusal_without_raising(monkeypatch):
    from sortify.spotify import SpotifyError
    def boom():
        raise SpotifyError(403, "no active device")
    monkeypatch.setattr(appmod.sp, "resume_playback", boom, raising=False)
    appmod._preview_resume_last = -1e9
    out = appmod.preview_resume()
    assert out["ok"] is False and "403" in out["error"]


# ---- loose-search fallback --------------------------------------------------
#
# The strict `artist:"X" track:"Y"` form is an exact match: remixes, live
# takes, `feat.` suffixes and punctuation drift all miss it, and every miss
# costs a candidate from the page's attempt budget. A second, free-text
# search recovers most of those — but only when the strict one found nothing,
# so a clean hit still costs exactly one request.


def searching_client(handler):
    dz = Deezer()
    dz._client = httpx.Client(transport=httpx.MockTransport(handler))
    return dz


def test_fetch_preview_strict_hit_still_costs_one_search():
    seen = []

    def handler(request):
        seen.append(request.url.params["q"])
        return httpx.Response(200, json={"data": [{"id": 1, "preview": "https://cdn/a.mp3"}]})

    rec = searching_client(handler).fetch_preview("Can", "Vitamin C")
    assert rec == {"url": "https://cdn/a.mp3", "deezer_id": 1}
    assert len(seen) == 1 and seen[0].startswith("artist:")


def test_fetch_preview_retries_loose_when_strict_finds_nothing():
    seen = []

    def handler(request):
        q = request.url.params["q"]
        seen.append(q)
        if q.startswith("artist:"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [{"id": 7, "preview": "https://cdn/b.mp3"}]})

    rec = searching_client(handler).fetch_preview("Miles Davis", "So What - Live")
    assert rec == {"url": "https://cdn/b.mp3", "deezer_id": 7}
    assert seen == ['artist:"Miles Davis" track:"So What - Live"',
                    "Miles Davis So What - Live"]


def test_fetch_preview_loose_retry_also_covers_a_hit_without_a_clip():
    """A strict hit whose `preview` is empty is a miss too — retry it loose."""
    seen = []

    def handler(request):
        q = request.url.params["q"]
        seen.append(q)
        if q.startswith("artist:"):
            return httpx.Response(200, json={"data": [{"id": 3, "preview": ""}]})
        return httpx.Response(200, json={"data": [{"id": 9, "preview": "https://cdn/c.mp3"}]})

    rec = searching_client(handler).fetch_preview("Neu!", "Hallogallo")
    assert rec == {"url": "https://cdn/c.mp3", "deezer_id": 9}
    assert len(seen) == 2


def test_fetch_preview_miss_after_both_searches():
    seen = []

    def handler(request):
        seen.append(request.url.params["q"])
        return httpx.Response(200, json={"data": []})

    assert searching_client(handler).fetch_preview("Nobody", "Nothing") == {"miss": True}
    assert len(seen) == 2  # tried both forms before giving up
