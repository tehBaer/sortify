"""Marking a preview clip as the wrong recording.

Deezer is matched by artist+title text search, and the free-text fallback
that rescues remixes and `feat.` suffixes is the same one that returns a
remix when you wanted the original. There is no way to tell from the search
result which happened, so the user marks it — and the mark has to do more
than mute the clip: the rejected Deezer id is skipped on the next search, so
a wrong-but-close match usually heals into the right recording by itself.

Zero Spotify calls: Deezer is faked, and track picking is a pure cache read.
"""

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)
from sortify.deezer import Deezer

TRACK = {"uri": "spotify:track:z", "id": "z", "name": "Song", "is_local": False,
         "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
         "added_at": "2026-02-02T00:00:00Z"}
OTHER = {"uri": "spotify:track:q", "id": "q", "name": "Other", "is_local": False,
         "type": "track", "artists": [{"id": "ar9", "name": "Ar Nine"}],
         "added_at": "2026-01-01T00:00:00Z"}


def client_with(routes: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, payload in routes.items():
            if request.url.path.startswith(prefix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404)

    dz = Deezer()
    dz._client = httpx.Client(transport=httpx.MockTransport(handler))
    return dz


def hits(*ids):
    return {"data": [{"id": i, "preview": f"https://cdn/{i}.mp3"} for i in ids]}


# ---- the client's own skipping ---------------------------------------------


def test_an_excluded_id_is_skipped_for_the_next_hit():
    dz = client_with({"/search": hits(11, 22, 33)})
    assert dz.fetch_preview("Ar One", "Song", exclude={11}) == {
        "url": "https://cdn/22.mp3", "deezer_id": 22}


def test_several_rejects_walk_further_down_the_results():
    dz = client_with({"/search": hits(11, 22, 33)})
    assert dz.fetch_preview("Ar One", "Song", exclude={11, 22})["deezer_id"] == 33


def test_every_candidate_rejected_is_a_miss_not_a_loop():
    dz = client_with({"/search": hits(11, 22)})
    assert dz.fetch_preview("Ar One", "Song", exclude={11, 22}) == {"miss": True}


def test_excluding_nothing_is_the_old_behaviour():
    dz = client_with({"/search": hits(11, 22)})
    assert dz.fetch_preview("Ar One", "Song")["deezer_id"] == 11


def test_a_hit_without_a_clip_is_passed_over_like_an_excluded_one():
    def handler(request):
        return httpx.Response(200, json={"data": [
            {"id": 11, "preview": ""}, {"id": 22, "preview": "https://cdn/22.mp3"}]})
    dz = Deezer()
    dz._client = httpx.Client(transport=httpx.MockTransport(handler))
    assert dz.fetch_preview("Ar One", "Song")["deezer_id"] == 22


# ---- the endpoint and the store --------------------------------------------


@pytest.fixture
def previewing(monkeypatch):
    store = appmod.store
    original_cache, original_config = store.cache(), store.config()
    cache = store.cache()
    cache["playlists"] = {
        "p1": {"snapshot_id": "s1", "tracks": [TRACK, OTHER], "fetched_at": time.time()},
    }
    store.save_cache(cache)
    store.save_preview_rejects({})

    seen = []

    class FakeDeezer:
        def fetch_preview(self, artist, title, exclude=()):
            seen.append({"artist": artist, "title": title, "exclude": set(exclude)})
            for did in (11, 22, 33):
                if did not in set(exclude):
                    return {"url": f"https://cdn/{did}.mp3", "deezer_id": did}
            return {"miss": True}

    monkeypatch.setattr(appmod, "_deezer_client", lambda: FakeDeezer())
    appmod._preview_cache.clear()
    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.seen = seen
    try:
        yield c
    finally:
        store.save_cache(original_cache)
        store.save_config(original_config)
        store.save_preview_rejects({})
        appmod._preview_cache.clear()


def test_clips_carry_the_uri_and_the_deezer_id_that_answered(previewing):
    clips = previewing.get("/api/playlist_preview/p1").json()["clips"]
    assert clips, "no clips resolved"
    assert clips[0]["uri"] == TRACK["uri"]
    assert clips[0]["deezer_id"] == 11


def test_rejecting_records_the_id_against_the_track(previewing):
    res = previewing.post("/api/preview_reject", json={
        "uri": TRACK["uri"], "deezer_id": 11, "artist": "Ar One", "title": "Song"})
    assert res.status_code == 200
    entry = appmod.store.preview_rejects()[TRACK["uri"]]
    assert entry["rejected"] == [11]
    # Readable by a human who opens the file to fix it by hand.
    assert entry["artist"] == "Ar One" and entry["title"] == "Song"


def test_a_second_reject_appends_rather_than_replaces(previewing):
    for did in (11, 22):
        previewing.post("/api/preview_reject", json={
            "uri": TRACK["uri"], "deezer_id": did, "artist": "Ar One", "title": "Song"})
    assert appmod.store.preview_rejects()[TRACK["uri"]]["rejected"] == [11, 22]


def test_rejecting_the_same_id_twice_does_not_duplicate_it(previewing):
    for _ in range(2):
        previewing.post("/api/preview_reject", json={
            "uri": TRACK["uri"], "deezer_id": 11, "artist": "Ar One", "title": "Song"})
    assert appmod.store.preview_rejects()[TRACK["uri"]]["rejected"] == [11]


def test_the_next_resolve_skips_what_was_rejected(previewing):
    previewing.get("/api/playlist_preview/p1")
    previewing.post("/api/preview_reject", json={
        "uri": TRACK["uri"], "deezer_id": 11, "artist": "Ar One", "title": "Song"})
    clips = previewing.get("/api/playlist_preview/p1").json()["clips"]
    assert clips[0]["deezer_id"] == 22
    # The exclusion reached the client rather than being filtered afterwards:
    # filtering the answer would spend the search and still return nothing.
    assert {"exclude": {11}} == {"exclude": previewing.seen[-2]["exclude"]}


def test_the_cached_page_is_dropped_so_the_reject_takes_effect_now(previewing):
    """Without this the rejected clip keeps playing for PREVIEW_TTL — ten
    minutes of the app ignoring the thing the user just told it."""
    previewing.get("/api/playlist_preview/p1")
    assert appmod._preview_cache, "nothing cached to invalidate"
    previewing.post("/api/preview_reject", json={
        "uri": TRACK["uri"], "deezer_id": 11, "artist": "Ar One", "title": "Song"})
    assert not [k for k in appmod._preview_cache if k[0] == "p1"]


def test_rejects_for_one_track_do_not_touch_another(previewing):
    previewing.post("/api/preview_reject", json={
        "uri": TRACK["uri"], "deezer_id": 11, "artist": "Ar One", "title": "Song"})
    clips = previewing.get("/api/playlist_preview/p1").json()["clips"]
    by_uri = {c["uri"]: c for c in clips}
    assert by_uri[OTHER["uri"]]["deezer_id"] == 11
