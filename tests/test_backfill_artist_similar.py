import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backfill_artist_similar import (  # noqa: E402
    BackfillAbort,
    artists_to_fetch,
    collect_target_artists,
    load_all_cached_tracks,
    load_home_tracks,
    merge_save,
    run_backfill,
)
from sortify.store import Store  # noqa: E402
from sortify.tags import LastFmError  # noqa: E402


def track(artist_id, artist_name, uri="spotify:track:x"):
    return {"uri": uri, "artists": [{"id": artist_id, "name": artist_name}]}


def write_cache(data_dir: Path, playlists: dict) -> None:
    (data_dir / "cache.json").write_text(json.dumps({"playlists": playlists}))


def write_config(data_dir: Path, home_ids: list) -> None:
    (data_dir / "config.json").write_text(json.dumps({"home_ids": home_ids}))


class FakeFm:
    """Stands in for sortify.tags.LastFm. `responses` maps artist name to a
    similar-artist list (hit) or None (Last.fm code-6 not-found, per
    `artist_similar`'s own contract). `raise_on` maps artist name to an
    exception `artist_similar` raises instead — used to simulate either a
    non-6 `LastFmError` or a bare transport failure (neither is wrapped by
    `artist_similar`, unlike `enrich`/`top_tags`)."""

    def __init__(self, responses=None, raise_on=None):
        self.responses = responses or {}
        self.raise_on = raise_on or {}
        self.calls = []

    def artist_similar(self, artist_name):
        self.calls.append(artist_name)
        if artist_name in self.raise_on:
            raise self.raise_on[artist_name]
        return self.responses.get(artist_name)


# ---- collection: home vs --all-cached -----------------------------------


def test_load_home_tracks_reads_only_configured_home_ids(tmp_path):
    write_config(tmp_path, home_ids=["home1"])
    write_cache(tmp_path, {
        "home1": {"tracks": [track("a1", "Artist One")]},
        "other": {"tracks": [track("a2", "Artist Two")]},
    })
    home_tracks = load_home_tracks(tmp_path)
    assert set(home_tracks) == {"home1"}


def test_load_all_cached_tracks_reads_every_playlist(tmp_path):
    write_cache(tmp_path, {
        "home1": {"tracks": [track("a1", "Artist One")]},
        "other": {"tracks": [track("a2", "Artist Two")]},
    })
    all_tracks = load_all_cached_tracks(tmp_path)
    assert set(all_tracks) == {"home1", "other"}


def test_collect_target_artists_dedupes_by_id_and_keeps_first_name():
    track_lists = {
        "home1": [track("a1", "Artist One"), track("a1", "Artist One (dup)")],
        "home2": [track("a2", "Artist Two")],
    }
    result = collect_target_artists(track_lists)
    assert result == {"a1": "Artist One", "a2": "Artist Two"}


def test_collect_target_artists_skips_tracks_with_no_artist_id():
    track_lists = {"home1": [{"uri": "u", "artists": [{"name": "No Id"}]}]}
    assert collect_target_artists(track_lists) == {}


# ---- skip-known -------------------------------------------------------


def test_artists_to_fetch_skips_ids_already_known_hit_or_miss():
    target = {"a1": "Artist One", "a2": "Artist Two", "a3": "Artist Three"}
    known = {
        "a1": {"name": "Artist One", "similar": [], "fetched_at": "t", "miss": False},
        "a2": {"name": "Artist Two", "similar": [], "fetched_at": "t", "miss": True},
    }
    assert artists_to_fetch(target, known) == {"a3": "Artist Three"}


def test_refetch_misses_re_attempts_only_the_misses():
    target = {"a1": "Artist One", "a2": "Artist Two", "a3": "Artist Three"}
    known = {
        "a1": {"name": "Artist One", "similar": [], "fetched_at": "t", "miss": False},
        "a2": {"name": "Artist Two", "similar": [], "fetched_at": "t", "miss": True},
    }
    result = artists_to_fetch(target, known, refetch_misses=True)
    assert result == {"a2": "Artist Two", "a3": "Artist Three"}


def test_run_backfill_skips_artists_already_known_hit_or_miss(tmp_path):
    store = Store(tmp_path)
    store.save_lastfm_artists({"version": 1, "artists": {
        "a1": {"name": "Hit", "similar": [{"artist": "X", "match": 0.5}],
               "fetched_at": "t", "miss": False},
        "a2": {"name": "Miss", "similar": [], "fetched_at": "t", "miss": True},
    }})
    target = {"a1": "Hit", "a2": "Miss", "a3": "New"}
    fm = FakeFm(responses={"New": [{"artist": "Y", "match": 0.7}]})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["already_known"] == 2
    assert summary["attempted"] == 1
    assert fm.calls == ["New"]
    assert "a3" in store.lastfm_artist_map()


def test_run_backfill_with_refetch_misses_re_attempts_the_miss(tmp_path):
    store = Store(tmp_path)
    store.save_lastfm_artists({"version": 1, "artists": {
        "a1": {"name": "Hit", "similar": [], "fetched_at": "t", "miss": False},
        "a2": {"name": "Miss", "similar": [], "fetched_at": "t", "miss": True},
    }})
    target = {"a1": "Hit", "a2": "Miss"}
    fm = FakeFm(responses={"Miss": [{"artist": "Z", "match": 0.3}]})

    summary = run_backfill(target, store, fm, refetch_misses=True, print_fn=lambda *_: None)

    assert summary["attempted"] == 1
    assert fm.calls == ["Miss"]
    saved = store.lastfm_artist_map()
    assert saved["a2"]["miss"] is False
    assert saved["a2"]["similar"] == [{"artist": "Z", "match": 0.3}]


# ---- limit -----------------------------------------------------------------


def test_run_backfill_respects_limit(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "One", "a2": "Two", "a3": "Three"}
    fm = FakeFm(responses={name: [] for name in ("One", "Two", "Three")})

    summary = run_backfill(target, store, fm, limit=2, print_fn=lambda *_: None)

    assert summary["attempted"] == 2
    assert len(fm.calls) == 2
    assert summary["target"] == 3


# ---- code 6 vs other errors --------------------------------------------


def test_code6_not_found_is_recorded_as_miss(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "Ghost Artist"}
    fm = FakeFm(responses={"Ghost Artist": None})  # artist_similar's own not-found contract

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["misses"] == 1
    assert summary["skipped"] == 0
    saved = store.lastfm_artist_map()
    assert saved["a1"]["miss"] is True
    assert saved["a1"]["similar"] == []


def test_other_lastfm_error_is_skipped_and_left_absent(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "Rate Limited Artist"}
    fm = FakeFm(raise_on={"Rate Limited Artist": LastFmError("Last.fm error 29: rate limit exceeded")})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["skipped"] == 1
    assert summary["misses"] == 0
    assert summary["fetched"] == 0
    assert "a1" not in store.lastfm_artist_map()


def test_transport_error_is_also_skipped_not_recorded(tmp_path):
    """`artist_similar`, unlike `enrich`, does NOT wrap a bare transport
    exception into `LastFmError` — a fake raising a plain exception exercises
    that unwrapped path end to end, and the broad `except Exception` in
    `run_backfill` must still catch it."""
    store = Store(tmp_path)
    target = {"a1": "Flaky Artist"}
    fm = FakeFm(raise_on={"Flaky Artist": ConnectionError("boom")})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["skipped"] == 1
    assert "a1" not in store.lastfm_artist_map()


# ---- incremental save preserves progress on a mid-run error --------------


def test_error_mid_run_does_not_lose_artists_fetched_before_it(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "First", "a2": "Second", "a3": "Third", "a4": "Fourth"}
    fm = FakeFm(
        responses={"First": [], "Second": [], "Fourth": []},
        raise_on={"Third": LastFmError("Last.fm error 11: service offline")},
    )

    # save_every=1 forces an on-disk save after each successful fetch, so
    # "First" and "Second" (fetched before "Third" raises) must be on disk
    # regardless of what happens to "Third" or "Fourth" after it.
    summary = run_backfill(target, store, fm, save_every=1, print_fn=lambda *_: None)

    saved = store.lastfm_artist_map()
    assert "a1" in saved
    assert "a2" in saved
    assert "a3" not in saved  # the failing artist: left absent, not recorded
    assert "a4" in saved  # the loop continues past the failure
    assert summary["fetched"] == 3
    assert summary["skipped"] == 1


# ---- merge never overwrites an existing entry -----------------------------


def test_merge_save_never_overwrites_an_existing_entry(tmp_path):
    store = Store(tmp_path)
    store.save_lastfm_artists({"version": 1, "artists": {
        "a1": {"name": "Original", "similar": [{"artist": "X", "match": 0.9}],
               "fetched_at": "original-time", "miss": False},
    }})

    # Simulate this script's own snapshot being stale relative to what is
    # now on disk (e.g. another process wrote a1 in the meantime) by handing
    # merge_save a conflicting value for the same key.
    merged = merge_save(store, {
        "a1": {"name": "Stale Overwrite Attempt", "similar": [], "fetched_at": "stale-time",
               "miss": True},
        "a2": {"name": "Brand New", "similar": [], "fetched_at": "t", "miss": False},
    })

    assert merged["a1"]["name"] == "Original"
    assert merged["a1"]["fetched_at"] == "original-time"
    assert merged["a2"]["name"] == "Brand New"

    on_disk = store.lastfm_artist_map()
    assert on_disk["a1"]["name"] == "Original"
    assert on_disk["a2"]["name"] == "Brand New"


def test_run_backfill_end_to_end_saves_final_summary_matches_disk(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "One", "a2": "Two", "a3": "Ghost"}
    fm = FakeFm(responses={
        "One": [{"artist": "X", "match": 0.4}],
        "Two": [{"artist": "Y", "match": 0.4}],
        "Ghost": None,
    })

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    saved = store.lastfm_artist_map()
    assert set(saved) == {"a1", "a2", "a3"}
    assert saved["a3"]["miss"] is True
    assert summary["fetched"] == 3
    assert summary["misses"] == 1
    assert summary["already_known"] == 0


# ---- clobber guard: mirrors backfill_tags.py's ----------------------------


def test_merge_save_refuses_when_a_malformed_reread_would_clobber_the_cache(tmp_path, monkeypatch):
    """`store.lastfm_artist_map()` degrades a malformed-but-valid-JSON
    envelope to `{}` rather than raising (guard-on-read, same as every other
    reader) — without a check here, a malformed re-read would make
    `merge_save` treat a real cache as legitimately empty and overwrite it
    with just this batch. Simulated by making the baseline read (first call)
    see real seeded data and every read after that see `{}`, the same shape
    a malformed envelope produces."""
    store = Store(tmp_path)
    store.save_lastfm_artists({"version": 1, "artists": {
        "a1": {"name": "Real", "similar": [{"artist": "X", "match": 0.2}],
               "fetched_at": "t0", "miss": False},
    }})

    calls = {"n": 0}
    real_artist_map = store.lastfm_artist_map

    def flaky_artist_map():
        calls["n"] += 1
        return real_artist_map() if calls["n"] == 1 else {}

    monkeypatch.setattr(store, "lastfm_artist_map", flaky_artist_map)

    with pytest.raises(BackfillAbort, match="malformed"):
        merge_save(store, {"a2": {"name": "New", "similar": [], "fetched_at": "t1", "miss": False}})

    # The file on disk is read directly, bypassing the now-patched
    # lastfm_artist_map, and still holds only the real pre-existing entry.
    on_disk = json.loads((tmp_path / "lastfm_artists.json").read_text())["artists"]
    assert on_disk == {
        "a1": {"name": "Real", "similar": [{"artist": "X", "match": 0.2}],
               "fetched_at": "t0", "miss": False},
    }


def test_merge_save_refuses_a_partial_shrink_not_just_total_collapse(tmp_path, monkeypatch):
    """Re-review, residual: a total collapse (baseline non-empty, re-read
    empty) is only the extreme case. lastfm_artists.json is append-only — no
    code path anywhere ever deletes an entry — so ANY shrink between two
    reads taken moments apart is anomalous, not a legitimate race. A
    truncated re-read (5 seeded entries down to 2, neither empty) must be
    refused exactly like the total-collapse case."""
    store = Store(tmp_path)
    seed = {
        f"a{i}": {"name": f"Artist {i}", "similar": [], "fetched_at": "t0", "miss": False}
        for i in range(1, 6)
    }
    store.save_lastfm_artists({"version": 1, "artists": seed})

    calls = {"n": 0}
    real_artist_map = store.lastfm_artist_map

    def flaky_artist_map():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_artist_map()  # baseline: all 5
        truncated = real_artist_map()
        return {k: truncated[k] for k in list(truncated)[:2]}  # re-read: only 2

    monkeypatch.setattr(store, "lastfm_artist_map", flaky_artist_map)

    with pytest.raises(BackfillAbort, match="shrank from 5 to 2"):
        merge_save(store, {"a6": {"name": "New", "similar": [], "fetched_at": "t1", "miss": False}})

    on_disk = json.loads((tmp_path / "lastfm_artists.json").read_text())["artists"]
    assert on_disk == seed  # untouched — the guard tripped before any save


def test_merge_save_refuses_on_unreadable_json_and_reports_discarded_count(tmp_path):
    """A `json.JSONDecodeError` (the file itself is not valid JSON, not
    merely a malformed envelope) is refused with a clean message rather than
    propagated raw, and states how many fetched-but-unsaved entries were
    discarded."""
    store = Store(tmp_path)
    (tmp_path / "lastfm_artists.json").write_text("{not valid json")

    with pytest.raises(BackfillAbort, match=r"2 fetched-but-unsaved"):
        merge_save(store, {
            "a1": {"name": "One", "similar": [], "fetched_at": "t", "miss": False},
            "a2": {"name": "Two", "similar": [], "fetched_at": "t", "miss": False},
        })

    # Untouched — merge_save never got as far as writing.
    assert (tmp_path / "lastfm_artists.json").read_text() == "{not valid json"


def test_run_backfill_propagates_backfill_abort_without_losing_earlier_saves(tmp_path, monkeypatch):
    """A clobber-guard trip partway through a run must not erase progress an
    earlier incremental save already flushed to disk. `save_every=1` flushes
    "First" via a real `merge_save` call before "Second" is even fetched;
    only the SECOND call to `merge_save` is made to raise, so the win from
    the first save must still be on disk when `BackfillAbort` propagates."""
    import backfill_artist_similar as bas

    store = Store(tmp_path)
    target = {"a1": "First", "a2": "Second"}
    fm = FakeFm(responses={"First": [], "Second": []})

    real_merge_save = bas.merge_save
    calls = {"n": 0}

    def flaky_merge_save(store_, new_entries):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_merge_save(store_, new_entries)
        raise BackfillAbort("simulated clobber guard trip")

    monkeypatch.setattr(bas, "merge_save", flaky_merge_save)

    with pytest.raises(BackfillAbort):
        run_backfill(target, store, fm, save_every=1, print_fn=lambda *_: None)

    saved = store.lastfm_artist_map()
    assert "a1" in saved  # the earlier, already-flushed save survives
    assert "a2" not in saved
