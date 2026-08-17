"""The playlist list is cached on disk and only re-read when asked.

Listing ~1000 playlists costs ~21 paginated calls, and WINDOW_CAP paces those
into a ~60s stall. That was being paid by the Playlists view *and* by every
profile rebuild (PROFILE_TTL, 10 min), which meant an afternoon of listening
re-read the whole list several times an hour. The list almost never changes,
so it is fetched once and then refreshed only on explicit request.
"""

import json

import pytest

from sortify.spotify import Spotify
from sortify.store import Store


class FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self.content = b"{}"
        self.headers = {}
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def page(ids: list[str], nxt: str | None = None) -> dict:
    return {
        "items": [
            {"id": i, "name": f"list {i}", "owner": {"id": "me"},
             "snapshot_id": f"snap-{i}", "tracks": {"total": 3}, "images": []}
            for i in ids
        ],
        "next": nxt,
    }


@pytest.fixture
def sp(tmp_path, monkeypatch):
    """A client whose transport is fake but whose budget/pagination is real."""
    client = Spotify(Store(tmp_path))
    monkeypatch.setattr(client, "_access_token", lambda: "token")
    monkeypatch.setattr(client, "_last_call", 0.0)
    client.store.save_cache({"playlists": {}, "artists": {}, "me": {"id": "me"}})
    return client


def transport(sp, monkeypatch, pages: list[dict]):
    """Serve `pages` in order; count how many upstream requests happen."""
    calls = {"n": 0}
    queue = list(pages)

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        return FakeResponse(queue.pop(0) if queue else page([]))

    monkeypatch.setattr(sp.http, "request", fake_request)
    return calls


def test_the_list_is_fetched_once_and_then_costs_no_budget(sp, monkeypatch):
    calls = transport(sp, monkeypatch, [page(["a", "b"], nxt="http://next"), page(["c"])])

    first = sp.my_playlists()
    spent_after_first = sp.budget_spent()

    second = sp.my_playlists()

    assert [p["id"] for p in first] == ["a", "b", "c"]
    assert second == first
    assert calls["n"] == 2, "the second call must not touch the API"
    assert sp.budget_spent() == spent_after_first, "a cache hit must spend no budget"


def test_refresh_re_reads_the_list(sp, monkeypatch):
    calls = transport(
        sp, monkeypatch,
        [page(["a"]), page(["a", "b"])],
    )
    sp.my_playlists()

    refreshed = sp.my_playlists(refresh=True)

    assert [p["id"] for p in refreshed] == ["a", "b"]
    assert calls["n"] == 2
    assert [p["id"] for p in sp.my_playlists()] == ["a", "b"], "refresh must rewrite the cache"


def test_a_cache_written_before_this_feature_is_a_miss_not_a_crash(sp, monkeypatch):
    (sp.store.dir / "cache.json").write_text(
        json.dumps({"playlists": {}, "artists": {}, "me": {"id": "me"}})
    )
    transport(sp, monkeypatch, [page(["a"])])

    assert [p["id"] for p in sp.my_playlists()] == ["a"]


def test_the_cache_records_when_it_was_fetched(sp, monkeypatch):
    transport(sp, monkeypatch, [page(["a"])])

    sp.my_playlists()

    entry = sp.store.cache()["playlist_list"]
    assert entry["fetched_at"] > 0


# ---- the refresh button's endpoint -----------------------------------------


def test_api_refresh_re_reads_the_list(monkeypatch):
    """The Refresh button is the only thing that re-reads the listing, so
    /api/refresh must invalidate — rebuilding profiles off the cached list
    would leave the button doing nothing visible."""
    from sortify import app as appmod

    asked = []
    monkeypatch.setattr(
        appmod.sp, "my_playlists",
        lambda refresh=False: asked.append(refresh) or [],
    )
    monkeypatch.setattr(appmod, "_ensure_profiles", lambda force=False: {"homes": []})

    appmod.refresh_profiles()

    assert asked == [True]


def test_api_playlists_reports_when_the_list_was_last_read(monkeypatch):
    """Button-only refresh means the list can be arbitrarily old, so the view
    has to be able to say how old rather than quietly present stale data."""
    from sortify import app as appmod

    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: [])
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 1_700_000_000.0, "items": []}
    appmod.store.save_cache(cache)

    assert appmod.playlists()["fetched_at"] == 1_700_000_000.0


def test_api_playlists_spends_no_api_calls_when_the_list_is_cached(monkeypatch):
    """The Playlists view is opened constantly — nav-lists, ‹ Back from
    triage, ‹ Back from a split, and once more after every Refresh — so
    /api/playlists costing 0 Spotify calls is load-bearing, not incidental.
    Nothing pinned it: adding `refresh=True` to the `sp.my_playlists()` call
    in the endpoint left the whole suite green, and that is ~21 paginated
    calls (a ~60s WINDOW_CAP stall) on every single one of those opens.

    Unlike the other tests here, this one runs the REAL my_playlists so the
    cache-hit path is what is under test, and guards Spotify.request() — the
    chokepoint every upstream call funnels through — so any call at all, by
    any method, current or future, fails the test rather than only the ones
    this endpoint happens to make today.
    """
    from sortify import app as appmod

    cache = appmod.store.cache()
    cache["playlist_list"] = {
        "fetched_at": 1_700_000_000.0,
        "items": [{"id": "PLX", "name": "PLX", "owner": "me", "editable": True,
                   "total": 3, "snapshot_id": "snap-x", "image": None}],
    }
    appmod.store.save_cache(cache)

    def fail(*a, **kw):
        raise AssertionError("GET /api/playlists must not touch the Spotify API")

    monkeypatch.setattr(appmod.sp, "request", fail)

    out = appmod.playlists()

    assert [p["id"] for p in out["playlists"]] == ["liked", "PLX"]
    assert out["fetched_at"] == 1_700_000_000.0


def test_api_refresh_reports_what_it_spent(monkeypatch):
    """Refresh is the one user action that can burst — the listing plus a
    re-read of every home whose snapshot moved. CLAUDE.md's rule is that the
    budget gets stated, so the endpoint reports the calls it cost."""
    from sortify import app as appmod

    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: [])
    monkeypatch.setattr(appmod.sp, "budget_spent", lambda: 100)
    monkeypatch.setattr(appmod, "_ensure_profiles", lambda force=False: {"homes": []})

    assert appmod.refresh_profiles()["calls_spent"] == 0
