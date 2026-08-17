import json
from pathlib import Path

import pytest

from sortify.tags import LastFm, enrich, load_key


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.Client. Records every call it is asked to make."""

    def __init__(self, by_artist):
        self.by_artist = by_artist
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params["artist"])
        payload = self.by_artist.get(params["artist"])
        if payload is None:
            return FakeResponse({"error": 6, "message": "The artist you supplied could not be found"})
        return FakeResponse({"toptags": {"tag": payload}})


def tagset(*pairs):
    return [{"name": n, "count": c} for n, c in pairs]


def test_module_never_imports_spotify():
    src = Path("sortify/tags.py").read_text()
    assert "sortify.spotify" not in src
    assert "from .spotify" not in src
    assert "import spotify" not in src


def test_top_tags_returns_raw_unfiltered_tags():
    """top_tags is the transport; clean_tags does the filtering in enrich."""
    fm = LastFm("k", sleep=lambda s: None,
                client=FakeClient({"Slowdive": tagset(("shoegaze", 100), ("british", 40))}))
    assert fm.top_tags("Slowdive") == [{"name": "shoegaze", "count": 100},
                                       {"name": "british", "count": 40}]


def test_top_tags_returns_none_for_unknown_artist():
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({}))
    assert fm.top_tags("Spherelet") is None


def test_rate_limiter_sleeps_between_calls():
    slept = []
    fm = LastFm("k", sleep=slept.append,
                client=FakeClient({"A": tagset(("techno", 50)), "B": tagset(("house", 50))}))
    fm.top_tags("A")
    fm.top_tags("B")
    assert len(slept) == 2
    assert all(s == pytest.approx(0.25) for s in slept)


def test_enrich_stores_cleaned_tags():
    fm = LastFm("k", sleep=lambda s: None,
                client=FakeClient({"Altin Gun": tagset(("psychedelic rock", 100), ("turkish", 51))}))
    out = enrich({"a1": "Altin Gun"}, {}, fm, now="2026-08-17T16:00:00Z")
    assert out["a1"]["tags"] == [["psychedelic rock", 100]]
    assert out["a1"]["miss"] is False
    assert out["a1"]["fetched_at"] == "2026-08-17T16:00:00Z"


def test_enrich_records_misses_explicitly():
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({}))
    out = enrich({"a1": "Spherelet"}, {}, fm, now="2026-08-17T16:00:00Z")
    assert out["a1"]["miss"] is True
    assert out["a1"]["tags"] == []


def test_enrich_never_refetches_cached_artists():
    client = FakeClient({"A": tagset(("techno", 50))})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    cached = {"a1": {"name": "A", "tags": [["techno", 50]], "miss": False,
                     "fetched_at": "2026-08-01T00:00:00Z"}}
    enrich({"a1": "A"}, cached, fm, now="2026-08-17T16:00:00Z")
    assert client.calls == []


def test_enrich_never_refetches_known_misses():
    client = FakeClient({})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    cached = {"a1": {"name": "Gone", "tags": [], "miss": True,
                     "fetched_at": "2026-08-01T00:00:00Z"}}
    enrich({"a1": "Gone"}, cached, fm, now="2026-08-17T16:00:00Z")
    assert client.calls == []


def test_load_key_reads_state_file(tmp_path):
    p = tmp_path / "lastfm.json"
    p.write_text(json.dumps({"api_key": "abc123"}))
    assert load_key(p) == "abc123"


def test_load_key_returns_none_when_absent(tmp_path):
    assert load_key(tmp_path / "nope.json") is None


def test_store_round_trips_tags():
    from sortify.store import Store
    s = Store()
    assert s.tags() == {"version": 1, "artists": {}}
    s.save_tags({"version": 1, "artists": {"a1": {"name": "A", "tags": [], "miss": True}}})
    assert s.tags()["artists"]["a1"]["name"] == "A"
