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


def test_error_6_returns_none():
    """Error code 6 (artist not found) returns None — the normal miss path."""
    fm = LastFm("k", sleep=lambda s: None,
                client=FakeClient({}))
    assert fm.top_tags("Unknown") is None


def test_error_10_raises():
    """Error code 10 (invalid API key) raises so batch aborts loudly."""
    from sortify.tags import LastFmError
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(
        {"error": 10, "message": "Invalid API key"})
    fm = LastFm("bad_key", sleep=lambda s: None, client=client)
    with pytest.raises(LastFmError, match="error 10"):
        fm.top_tags("A")


def test_error_29_raises():
    """Error code 29 (rate limit) raises so batch aborts loudly."""
    from sortify.tags import LastFmError
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(
        {"error": 29, "message": "Rate limit exceeded"})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    with pytest.raises(LastFmError, match="error 29"):
        fm.top_tags("A")


def test_enrich_aborts_on_error_not_miss():
    """If top_tags raises partway through, enrich aborts and does not write miss."""
    from sortify.tags import LastFmError
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(
        {"error": 10, "message": "Invalid API key"})
    fm = LastFm("bad_key", sleep=lambda s: None, client=client)
    with pytest.raises(LastFmError):
        enrich({"a1": "A", "a2": "B"}, {}, fm, now="2026-08-17T16:00:00Z")


def test_load_key_rejects_null():
    """load_key returns None if the file contains null."""
    from sortify.tags import load_key
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write("null")
        f.flush()
        result = load_key(Path(f.name))
        assert result is None


def test_load_key_rejects_list():
    """load_key returns None if the file contains a list."""
    from sortify.tags import load_key
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write('[1, 2, 3]')
        f.flush()
        result = load_key(Path(f.name))
        assert result is None


def test_load_key_rejects_string():
    """load_key returns None if the file contains a bare string."""
    from sortify.tags import load_key
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write('"just a string"')
        f.flush()
        result = load_key(Path(f.name))
        assert result is None


def test_load_key_rejects_empty_key():
    """load_key returns None if api_key is empty string."""
    from sortify.tags import load_key
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write('{"api_key": ""}')
        f.flush()
        result = load_key(Path(f.name))
        assert result is None
