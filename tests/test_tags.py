import json
import subprocess
import sys
from pathlib import Path

import pytest

from sortify.tags import LastFm, LastFmError, enrich, load_key

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def test_module_never_imports_spotify_source():
    src = (REPO_ROOT / "sortify" / "tags.py").read_text()
    assert "sortify.spotify" not in src
    assert "from .spotify" not in src
    assert "import spotify" not in src


def test_importing_tags_loads_no_spotify_module():
    """The headline invariant, checked at runtime so transitive imports count.

    A fresh interpreter imports sortify.tags and nothing else; if any module
    with "spotify" in its name is loaded afterwards, tag traffic could reach
    the Spotify budget or its limiter.
    """
    probe = (
        "import sys, sortify.tags; "
        "print(','.join(sorted(m for m in sys.modules if 'spotify' in m)))"
    )
    r = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", f"sortify.tags pulled in: {r.stdout.strip()}"


def test_top_tags_returns_raw_unfiltered_tags():
    """top_tags is the transport; clean_tags runs at split time."""
    fm = LastFm("k", sleep=lambda s: None,
                client=FakeClient({"Slowdive": tagset(("shoegaze", 100), ("british", 40))}))
    assert fm.top_tags("Slowdive").tags == [{"name": "shoegaze", "count": 100},
                                            {"name": "british", "count": 40}]


def test_top_tags_returns_none_for_unknown_artist():
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({}))
    assert fm.top_tags("Spherelet") is None


def test_top_tags_reports_the_name_lastfm_matched():
    """autocorrect=1 can match a different artist; the record must show it."""
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(
        {"toptags": {"tag": tagset(("ambient", 100)), "@attr": {"artist": "AIR"}}})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    assert fm.top_tags("Air").matched_name == "AIR"


def test_top_tags_matched_name_is_none_when_absent():
    fm = LastFm("k", sleep=lambda s: None,
                client=FakeClient({"Slowdive": tagset(("shoegaze", 100))}))
    assert fm.top_tags("Slowdive").matched_name is None


def test_rate_limiter_sleeps_between_calls():
    slept = []
    fm = LastFm("k", sleep=slept.append,
                client=FakeClient({"A": tagset(("techno", 50)), "B": tagset(("house", 50))}))
    fm.top_tags("A")
    fm.top_tags("B")
    assert len(slept) == 2
    assert all(s == pytest.approx(0.25) for s in slept)


def test_enrich_stores_raw_tags_unfiltered():
    """Hygiene happens at split time, so nothing may be dropped on the way in.

    `turkish` is on the stoplist and would have been filtered at fetch time;
    keeping it means the stoplist can be retuned without re-fetching.
    """
    fm = LastFm("k", sleep=lambda s: None,
                client=FakeClient({"Altin Gun": tagset(("psychedelic rock", 100),
                                                       ("turkish", 51), ("obscure", 2))}))
    out = enrich({"a1": "Altin Gun"}, {}, fm, now="2026-08-17T16:00:00Z")
    assert out["a1"]["tags"] == [{"name": "psychedelic rock", "count": 100},
                                 {"name": "turkish", "count": 51},
                                 {"name": "obscure", "count": 2}]
    assert out["a1"]["miss"] is False
    assert out["a1"]["fetched_at"] == "2026-08-17T16:00:00Z"


def test_enrich_records_the_matched_lastfm_name():
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(
        {"toptags": {"tag": tagset(("ambient", 100)), "@attr": {"artist": "Altın Gün"}}})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    out = enrich({"a1": "Altin Gun"}, {}, fm, now="2026-08-17T16:00:00Z")
    assert out["a1"]["name"] == "Altin Gun"
    assert out["a1"]["lastfm_name"] == "Altın Gün"


def test_enrich_records_misses_explicitly():
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({}))
    out = enrich({"a1": "Spherelet"}, {}, fm, now="2026-08-17T16:00:00Z")
    assert out["a1"]["miss"] is True
    assert out["a1"]["tags"] == []


def test_enrich_never_refetches_cached_artists():
    client = FakeClient({"A": tagset(("techno", 50))})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    cached = {"a1": {"name": "A", "tags": [{"name": "techno", "count": 50}], "miss": False,
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


def test_store_round_trips_tags(tmp_path):
    """Isolated data dir: a shared one would break the day another test
    writes tags.json into it."""
    from sortify.store import TAGS_VERSION, Store
    s = Store(tmp_path)
    assert s.tags() == {"version": TAGS_VERSION, "artists": {}}
    assert s.tag_artists() == {}
    s.save_tags({"version": TAGS_VERSION,
                 "artists": {"a1": {"name": "A", "tags": [], "miss": True}}})
    assert s.tags()["artists"]["a1"]["name"] == "A"
    assert s.tag_artists()["a1"]["name"] == "A"


def test_store_tag_artists_unwraps_the_envelope(tmp_path):
    """enrich and split_tracks both want the inner map, never the envelope."""
    from sortify.store import TAGS_VERSION, Store
    s = Store(tmp_path)
    s.save_tag_artists({"a1": {"name": "A", "tags": [], "miss": True}})
    assert s.tags() == {"version": TAGS_VERSION,
                        "artists": {"a1": {"name": "A", "tags": [], "miss": True}}}
    assert list(s.tag_artists()) == ["a1"]


def test_store_tag_artists_survives_a_malformed_file(tmp_path):
    from sortify.store import Store
    s = Store(tmp_path)
    (tmp_path / "tags.json").write_text(json.dumps({"version": 2}))
    assert s.tag_artists() == {}


def test_error_10_raises():
    """Error code 10 (invalid API key) raises so batch aborts loudly."""
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(
        {"error": 10, "message": "Invalid API key"})
    fm = LastFm("bad_key", sleep=lambda s: None, client=client)
    with pytest.raises(LastFmError, match="error 10"):
        fm.top_tags("A")


def test_error_29_raises():
    """Error code 29 (rate limit) raises so batch aborts loudly."""
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(
        {"error": 29, "message": "Rate limit exceeded"})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    with pytest.raises(LastFmError, match="error 29"):
        fm.top_tags("A")


def test_error_6_with_a_non_missing_artist_message_raises():
    """Code 6 also means "invalid parameters". Only the not-found wording is
    a miss — otherwise one malformed request writes miss:true for everyone."""
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(
        {"error": 6, "message": "Invalid parameters - your request is missing a required parameter"})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    with pytest.raises(LastFmError, match="error 6"):
        fm.top_tags("A")


def test_error_code_as_string_is_still_a_miss():
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(
        {"error": "6", "message": "The artist you supplied could not be found"})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    assert fm.top_tags("A") is None


def test_blank_api_key_is_rejected_at_construction():
    """load_key returns None when the state file is renamed; that must not
    become a library-wide run of false misses."""
    for bad in (None, "", "   "):
        with pytest.raises(LastFmError, match="API key"):
            LastFm(bad, sleep=lambda s: None, client=FakeClient({}))


def test_blank_artist_name_is_rejected():
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({}))
    for bad in ("", "   ", None):
        with pytest.raises(LastFmError, match="artist name"):
            fm.top_tags(bad)


def test_missing_toptags_key_raises_rather_than_recording_no_tags():
    """A 200 maintenance body would otherwise mark every artist tagless
    forever, indistinguishable from a genuine empty tag list."""
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse({"ok": True})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    with pytest.raises(LastFmError, match="toptags"):
        fm.top_tags("A")


def test_empty_tag_list_is_a_real_answer():
    """An artist Last.fm knows but nobody tagged: not an error, not a miss."""
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({"A": []}))
    got = fm.top_tags("A")
    assert got is not None and got.tags == []


def test_non_dict_body_raises_lastfm_error():
    client = FakeClient({})
    client.get = lambda url, params=None, timeout=None: FakeResponse(["not", "an", "object"])
    fm = LastFm("k", sleep=lambda s: None, client=client)
    with pytest.raises(LastFmError, match="non-object"):
        fm.top_tags("A")


def test_enrich_aborts_on_error_not_miss():
    """A failure partway through must not be recorded as miss: true."""
    class HalfBroken(FakeClient):
        def get(self, url, params=None, timeout=None):
            self.calls.append(params["artist"])
            if params["artist"] == "B":
                return FakeResponse({"error": 10, "message": "Invalid API key"})
            return FakeResponse({"toptags": {"tag": tagset(("techno", 50))}})

    fm = LastFm("k", sleep=lambda s: None, client=HalfBroken({}))
    with pytest.raises(LastFmError) as excinfo:
        enrich({"a1": "A", "a2": "B"}, {}, fm, now="2026-08-17T16:00:00Z")
    partial = excinfo.value.partial
    assert partial["a1"]["miss"] is False
    assert partial["a1"]["tags"] == [{"name": "techno", "count": 50}]
    assert "a2" not in partial


def test_enrich_wraps_transport_errors_and_keeps_the_partial_map():
    """A network blip 600 artists in must not cost 600 answered requests."""
    class Flaky(FakeClient):
        def get(self, url, params=None, timeout=None):
            self.calls.append(params["artist"])
            if params["artist"] == "B":
                raise RuntimeError("connection reset")
            return FakeResponse({"toptags": {"tag": tagset(("techno", 50))}})

    fm = LastFm("k", sleep=lambda s: None, client=Flaky({}))
    with pytest.raises(LastFmError) as excinfo:
        enrich({"a1": "A", "a2": "B"}, {}, fm, now="2026-08-17T16:00:00Z")
    assert list(excinfo.value.partial) == ["a1"]


# ---- blank artist names: a data condition, not a service failure ----------
#
# Spotify's own placeholder for a removed/unavailable track carries a blank
# artist name (and a blank track name — see the module docstring in
# sortify/tags.py). One such track used to abort tagging for every artist
# still waiting behind it in the batch: `enrich` passed the blank name
# straight to `top_tags`, which rightly refuses to send it over the wire and
# raises, and `enrich` let that raise propagate like any other failure. But
# a blank name can never produce a different answer no matter how many times
# it's retried — it belongs with "artist not found", not with a rate limit
# or a bad API key.


def test_enrich_records_a_blank_artist_name_as_a_miss_and_keeps_going():
    client = FakeClient({"Real Artist": tagset(("rock", 50))})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    out = enrich(
        {"blank": "", "real": "Real Artist"}, {}, fm, now="2026-08-17T16:00:00Z"
    )
    assert out["blank"] == {"name": "", "lastfm_name": None, "tags": [],
                            "fetched_at": "2026-08-17T16:00:00Z", "miss": True}
    assert out["real"]["miss"] is False
    assert out["real"]["tags"] == [{"name": "rock", "count": 50}]
    # The blank name never reached the network — top_tags' own guard would
    # have raised on it, and there was never anything to ask Last.fm anyway.
    assert client.calls == ["Real Artist"]


def test_enrich_records_a_none_artist_name_as_a_miss():
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({}))
    out = enrich({"a1": None}, {}, fm, now="2026-08-17T16:00:00Z")
    assert out["a1"]["miss"] is True
    assert out["a1"]["tags"] == []
    assert out["a1"]["name"] == ""  # never store None — downstream expects a string


def test_enrich_records_a_whitespace_only_artist_name_as_a_miss():
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({}))
    out = enrich({"a1": "   "}, {}, fm, now="2026-08-17T16:00:00Z")
    assert out["a1"]["miss"] is True
    assert out["a1"]["tags"] == []


def test_enrich_tags_the_artists_around_a_blank_name_and_completes():
    """The end-to-end shape of the real incident: several good artists, one
    dead track's blank-named artist in the middle, more good artists after
    it — the whole batch must finish, not just avoid raising on the one bad
    entry."""
    client = FakeClient({"A": tagset(("techno", 50)), "C": tagset(("house", 40))})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    out = enrich(
        {"a1": "A", "blank": "", "a3": "C"}, {}, fm, now="2026-08-17T16:00:00Z"
    )
    assert out["a1"]["miss"] is False
    assert out["blank"]["miss"] is True
    assert out["a3"]["miss"] is False
    assert set(out) == {"a1", "blank", "a3"}


def test_enrich_still_aborts_on_a_real_lastfm_failure_with_partial_intact():
    """Regression guard: the blank-name-is-a-miss fix must not blunt the
    existing rule that a genuine service failure (error 10: invalid API key)
    aborts the batch and still hands back everything verified so far."""
    class HalfBroken(FakeClient):
        def get(self, url, params=None, timeout=None):
            self.calls.append(params["artist"])
            if params["artist"] == "B":
                return FakeResponse({"error": 10, "message": "Invalid API key"})
            return FakeResponse({"toptags": {"tag": tagset(("techno", 50))}})

    fm = LastFm("k", sleep=lambda s: None, client=HalfBroken({}))
    with pytest.raises(LastFmError, match="error 10") as excinfo:
        enrich({"a1": "A", "blank": "", "a2": "B"}, {}, fm, now="2026-08-17T16:00:00Z")
    partial = excinfo.value.partial
    assert partial["a1"]["miss"] is False
    assert partial["blank"]["miss"] is True  # the blank one was already resolved, for free
    assert "a2" not in partial  # never reached — B is where the real failure hit


def test_load_key_rejects_null(tmp_path):
    p = tmp_path / "lastfm.json"
    p.write_text("null")
    assert load_key(p) is None


def test_load_key_rejects_list(tmp_path):
    p = tmp_path / "lastfm.json"
    p.write_text("[1, 2, 3]")
    assert load_key(p) is None


def test_load_key_rejects_string(tmp_path):
    p = tmp_path / "lastfm.json"
    p.write_text('"just a string"')
    assert load_key(p) is None


def test_load_key_rejects_empty_key(tmp_path):
    p = tmp_path / "lastfm.json"
    p.write_text('{"api_key": ""}')
    assert load_key(p) is None
