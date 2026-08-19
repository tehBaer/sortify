import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backfill_similar import (  # noqa: E402
    BackfillAbort,
    collect_target_tracks,
    load_all_cached_tracks,
    load_home_tracks,
    merge_save,
    run_backfill,
    tracks_to_fetch,
)
from sortify.store import Store  # noqa: E402
from sortify.tags import LastFmError  # noqa: E402


def track(title, artist_names, uri="spotify:track:x"):
    return {"uri": uri, "name": title,
            "artists": [{"id": f"id-{n}", "name": n} for n in artist_names]}


def write_cache(data_dir: Path, playlists: dict) -> None:
    (data_dir / "cache.json").write_text(json.dumps({"playlists": playlists}))


def write_config(data_dir: Path, home_ids: list) -> None:
    (data_dir / "config.json").write_text(json.dumps({"home_ids": home_ids}))


class FakeFm:
    """Stands in for sortify.tags.LastFm at the fetch_track level.
    `responses` maps (artist, title) to the record `tags.fetch_track` would
    return; `raise_on` maps (artist, title) to an exception raised instead —
    used to simulate a non-"not found" Last.fm failure. `calls` records every
    (artist, title) pair asked about, mirroring `fetch_track`'s own single
    entry point so this fake never needs to know about getSimilar vs
    getTopTags separately."""

    def __init__(self, responses=None, raise_on=None):
        self.responses = responses or {}
        self.raise_on = raise_on or {}
        self.calls = []


@pytest.fixture(autouse=True)
def _patch_fetch_track(monkeypatch):
    import backfill_similar as bs

    def fake_fetch_track(fm, artist, title, now):
        fm.calls.append((artist, title))
        key = (artist, title)
        if key in fm.raise_on:
            raise fm.raise_on[key]
        if key in fm.responses:
            return fm.responses[key]
        return {"similar": [], "tags": [], "fetched_at": now, "miss": True}

    monkeypatch.setattr(bs, "fetch_track", fake_fetch_track)
    yield


def hit_record(now="t"):
    return {"similar": [{"artist": "X", "track": "Y", "match": 0.5}],
            "tags": ["rock"], "fetched_at": now, "miss": False}


def miss_record(now="t"):
    return {"similar": [], "tags": [], "fetched_at": now, "miss": True}


# ---- collection: home vs --all-cached -----------------------------------


def test_load_home_tracks_reads_only_configured_home_ids(tmp_path):
    write_config(tmp_path, home_ids=["home1"])
    write_cache(tmp_path, {
        "home1": {"tracks": [track("Song A", ["Artist One"])]},
        "other": {"tracks": [track("Song B", ["Artist Two"])]},
    })
    home_tracks = load_home_tracks(tmp_path)
    assert set(home_tracks) == {"home1"}


def test_load_all_cached_tracks_reads_every_playlist(tmp_path):
    write_cache(tmp_path, {
        "home1": {"tracks": [track("Song A", ["Artist One"])]},
        "other": {"tracks": [track("Song B", ["Artist Two"])]},
    })
    all_tracks = load_all_cached_tracks(tmp_path)
    assert set(all_tracks) == {"home1", "other"}


def test_collect_target_tracks_keys_by_first_credited_artist():
    track_lists = {"home1": [track("Dream On", ["Aerosmith", "Run-DMC"])]}
    result = collect_target_tracks(track_lists)
    from sortify.tags import track_key
    key = track_key("Aerosmith", "Dream On")
    assert set(result) == {key}
    assert result[key]["artist"] == "Aerosmith"
    assert result[key]["title"] == "Dream On"
    assert result[key]["keys"] == [key, track_key("Run-DMC", "Dream On")]


def test_collect_target_tracks_dedupes_by_fetch_key_first_seen_wins():
    track_lists = {
        "home1": [track("Song", ["Artist One"], uri="spotify:track:a")],
        "home2": [track("Song", ["Artist One"], uri="spotify:track:b")],
    }
    result = collect_target_tracks(track_lists)
    assert len(result) == 1


def test_collect_target_tracks_skips_tracks_with_no_title_or_no_artists():
    track_lists = {"home1": [
        {"uri": "u1", "name": "", "artists": [{"id": "a1", "name": "A"}]},
        {"uri": "u2", "name": "Has Title", "artists": []},
        {"uri": "u3", "artists": [{"id": "a1", "name": "A"}]},
    ]}
    assert collect_target_tracks(track_lists) == {}


def test_collect_target_tracks_skips_artists_missing_a_name_for_the_extra_keys():
    track_lists = {"home1": [track("Song", ["Artist One"])]}
    track_lists["home1"][0]["artists"].append({"id": "no-name"})
    from sortify.tags import track_key
    result = collect_target_tracks(track_lists)
    key = track_key("Artist One", "Song")
    assert result[key]["keys"] == [key]


# ---- skip-known, incl. cross-artist collab keys ---------------------------


def test_tracks_to_fetch_skips_a_hit_found_under_any_key():
    from sortify.tags import track_key
    k1 = track_key("Aerosmith", "Dream On")
    k2 = track_key("Run-DMC", "Dream On")
    target = {k1: {"artist": "Aerosmith", "title": "Dream On", "keys": [k1, k2]}}
    # The record is recorded under the co-artist's key, not the fetch key.
    known = {k2: hit_record()}
    assert tracks_to_fetch(target, known) == {}


def test_tracks_to_fetch_skips_a_miss_by_default():
    from sortify.tags import track_key
    k1 = track_key("A", "T")
    target = {k1: {"artist": "A", "title": "T", "keys": [k1]}}
    known = {k1: miss_record()}
    assert tracks_to_fetch(target, known) == {}


def test_tracks_to_fetch_retries_a_miss_with_refetch_misses(tmp_path):
    from sortify.tags import track_key
    k1 = track_key("A", "T")
    target = {k1: {"artist": "A", "title": "T", "keys": [k1]}}
    known = {k1: miss_record()}
    assert tracks_to_fetch(target, known, refetch_misses=True) == target


def test_run_backfill_skips_tracks_already_known_incl_collab_cross_key(tmp_path):
    from sortify.tags import track_key
    store = Store(tmp_path)
    k_hit = track_key("Known Artist", "Known Song")
    k_first = track_key("Aerosmith", "Dream On")
    k_other = track_key("Run-DMC", "Dream On")  # where the collab's record actually lives
    store.save_lastfm_tracks({"version": 1, "tracks": {
        k_hit: hit_record(),
        k_other: hit_record(),
    }})
    target = {
        k_hit: {"artist": "Known Artist", "title": "Known Song", "keys": [k_hit]},
        k_first: {"artist": "Aerosmith", "title": "Dream On", "keys": [k_first, k_other]},
        track_key("New", "Song"): {"artist": "New", "title": "Song",
                                    "keys": [track_key("New", "Song")]},
    }
    fm = FakeFm(responses={("New", "Song"): hit_record()})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["already_known"] == 2
    assert summary["attempted"] == 1
    assert fm.calls == [("New", "Song")]


# ---- limit -----------------------------------------------------------------


def test_run_backfill_respects_limit(tmp_path):
    store = Store(tmp_path)
    target = {
        f"k{i}": {"artist": f"A{i}", "title": "T", "keys": [f"k{i}"]} for i in range(3)
    }
    fm = FakeFm(responses={(f"A{i}", "T"): hit_record() for i in range(3)})

    summary = run_backfill(target, store, fm, limit=2, print_fn=lambda *_: None)

    assert summary["attempted"] == 2
    assert len(fm.calls) == 2
    assert summary["target"] == 3


def test_run_backfill_limit_counts_failed_attempts_too(tmp_path):
    """M4: `limit` bounds ATTEMPTS (fetch_track calls made), not successes —
    a run with `limit=2` that hits 2 failures stops at 2 attempts with 0
    fetched, exactly like the module docstring claims, not silently
    continuing past the limit looking for a success."""
    store = Store(tmp_path)
    target = {
        f"k{i}": {"artist": f"A{i}", "title": "T", "keys": [f"k{i}"]} for i in range(3)
    }
    fm = FakeFm(raise_on={
        (f"A{i}", "T"): LastFmError("Last.fm error 29: rate limited") for i in range(3)
    })

    summary = run_backfill(target, store, fm, limit=2, print_fn=lambda *_: None)

    assert summary["attempted"] == 2
    assert summary["fetched"] == 0
    assert summary["skipped"] == 2
    assert len(fm.calls) == 2  # the third target track was never even attempted


# ---- code-6 (miss) vs other errors -----------------------------------------


def test_a_both_not_found_track_is_recorded_as_a_miss(tmp_path):
    store = Store(tmp_path)
    target = {"k1": {"artist": "Ghost", "title": "Song", "keys": ["k1"]}}
    fm = FakeFm(responses={("Ghost", "Song"): miss_record()})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["misses"] == 1
    assert summary["skipped"] == 0
    saved = store.lastfm_track_map()
    assert saved["k1"]["miss"] is True


def test_other_lastfm_error_is_skipped_and_left_absent(tmp_path):
    store = Store(tmp_path)
    target = {"k1": {"artist": "Rate", "title": "Limited", "keys": ["k1"]}}
    fm = FakeFm(raise_on={("Rate", "Limited"): LastFmError("Last.fm error 29: rate limit exceeded")})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["skipped"] == 1
    assert summary["misses"] == 0
    assert summary["fetched"] == 0
    assert "k1" not in store.lastfm_track_map()


def test_transport_error_is_also_skipped_not_recorded(tmp_path):
    store = Store(tmp_path)
    target = {"k1": {"artist": "Flaky", "title": "Artist", "keys": ["k1"]}}
    fm = FakeFm(raise_on={("Flaky", "Artist"): ConnectionError("boom")})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["skipped"] == 1
    assert "k1" not in store.lastfm_track_map()


# ---- incremental save preserves progress on a mid-run error --------------


def test_error_mid_run_does_not_lose_tracks_fetched_before_it(tmp_path):
    store = Store(tmp_path)
    target = {
        "k1": {"artist": "First", "title": "T", "keys": ["k1"]},
        "k2": {"artist": "Second", "title": "T", "keys": ["k2"]},
        "k3": {"artist": "Third", "title": "T", "keys": ["k3"]},
        "k4": {"artist": "Fourth", "title": "T", "keys": ["k4"]},
    }
    fm = FakeFm(
        responses={
            ("First", "T"): hit_record(),
            ("Second", "T"): hit_record(),
            ("Fourth", "T"): hit_record(),
        },
        raise_on={("Third", "T"): LastFmError("Last.fm error 11: service offline")},
    )

    summary = run_backfill(target, store, fm, save_every=1, print_fn=lambda *_: None)

    saved = store.lastfm_track_map()
    assert "k1" in saved
    assert "k2" in saved
    assert "k3" not in saved  # the failing track: left absent, not recorded
    assert "k4" in saved  # the loop continues past the failure
    assert summary["fetched"] == 3
    assert summary["skipped"] == 1


def test_incremental_save_fires_at_the_default_save_every_cadence(tmp_path, monkeypatch):
    """M4: every other incremental-save test overrides `save_every=1` for a
    tight assertion window — this one exercises the real default (50)
    end to end, confirming a batch of 60 fetched tracks saves exactly twice
    (once at 50, once for the trailing 10) rather than once at the end."""
    import backfill_similar as bs

    store = Store(tmp_path)
    target = {
        f"k{i}": {"artist": f"A{i}", "title": "T", "keys": [f"k{i}"]} for i in range(60)
    }
    fm = FakeFm(responses={(f"A{i}", "T"): hit_record() for i in range(60)})

    save_calls: list[int] = []
    real_merge_save = bs.merge_save

    def counting_merge_save(store_, new_entries, replace_keys=None):
        save_calls.append(len(new_entries))
        return real_merge_save(store_, new_entries, replace_keys=replace_keys)

    monkeypatch.setattr(bs, "merge_save", counting_merge_save)
    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["fetched"] == 60
    assert save_calls == [50, 10]  # default SAVE_EVERY=50, then the trailing batch
    assert len(store.lastfm_track_map()) == 60


# ---- fix round 1, I2: consecutive-failure circuit breaker ------------------


def test_run_backfill_aborts_after_consecutive_failure_limit(tmp_path):
    store = Store(tmp_path)
    target = {
        f"k{i}": {"artist": f"A{i}", "title": "T", "keys": [f"k{i}"]} for i in range(12)
    }
    fm = FakeFm(raise_on={
        (f"A{i}", "T"): LastFmError("Last.fm error 29: rate limited") for i in range(12)
    })

    with pytest.raises(BackfillAbort, match="10 consecutive"):
        run_backfill(target, store, fm, consecutive_failure_limit=10, print_fn=lambda *_: None)

    # Only the first 10 attempts happened — the run stopped rather than
    # walking the remaining 2 targets blind.
    assert len(fm.calls) == 10


def test_run_backfill_preserves_progress_flushed_before_the_abort(tmp_path):
    """The consecutive-failure abort must not lose tracks this run already
    fetched — `save_every=1` forces "k0" onto disk before the failure run
    begins."""
    store = Store(tmp_path)
    target = {"k0": {"artist": "Good", "title": "T", "keys": ["k0"]}}
    target.update({
        f"k{i}": {"artist": f"A{i}", "title": "T", "keys": [f"k{i}"]} for i in range(1, 11)
    })
    fm = FakeFm(
        responses={("Good", "T"): hit_record()},
        raise_on={(f"A{i}", "T"): LastFmError("Last.fm error 29: rate limited")
                  for i in range(1, 11)},
    )

    with pytest.raises(BackfillAbort):
        run_backfill(target, store, fm, save_every=1, consecutive_failure_limit=10,
                     print_fn=lambda *_: None)

    assert "k0" in store.lastfm_track_map()  # flushed before the failure streak began


def test_a_success_resets_the_consecutive_failure_counter(tmp_path):
    """9 failures, then a success, then 9 more failures must NOT abort —
    the counter resets on the success, so it never reaches 10 in a row."""
    store = Store(tmp_path)
    fail_names = [f"F{i}" for i in range(1, 10)] + [f"G{i}" for i in range(1, 10)]
    target = {f"k{n}": {"artist": n, "title": "T", "keys": [f"k{n}"]} for n in fail_names}
    target["k-good"] = {"artist": "Good", "title": "T", "keys": ["k-good"]}
    # Order matters: 9 failures, the good one, then 9 more failures.
    ordered = {}
    for n in fail_names[:9]:
        ordered[f"k{n}"] = target[f"k{n}"]
    ordered["k-good"] = target["k-good"]
    for n in fail_names[9:]:
        ordered[f"k{n}"] = target[f"k{n}"]

    fm = FakeFm(
        responses={("Good", "T"): hit_record()},
        raise_on={(n, "T"): LastFmError("Last.fm error 29: rate limited") for n in fail_names},
    )

    summary = run_backfill(ordered, store, fm, consecutive_failure_limit=10,
                            print_fn=lambda *_: None)  # must not raise

    assert summary["attempted"] == 19
    assert summary["fetched"] == 1
    assert summary["skipped"] == 18


# ---- fix round 1, I3: actual request count in the summary ------------------
#
# This test file's own autouse fixture (`_patch_fetch_track`) replaces
# `backfill_similar.fetch_track` outright — necessary for the rest of this
# file's fine-grained control over hits/misses/raises, but it means
# `_CountingFm`'s wrapped `track_similar`/`track_top_tags` are never actually
# called through `run_backfill` in THIS file (the fake calls neither), so
# `summary["requests"]` is untestable at that level here. `_CountingFm` is
# exercised directly instead, against the REAL `tags.fetch_track`.


def test_counting_fm_counts_a_hit_as_two_requests_and_a_first_call_raise_as_one():
    import backfill_similar as bs
    from sortify.tags import fetch_track as real_fetch_track

    class RealShapedFm:
        """Exercises the ACTUAL `fetch_track` (not the patched fixture
        fake), so `_CountingFm` is proven against the real two-call
        sequence: `track_similar` then `track_top_tags`."""

        def __init__(self, raise_similar=False):
            self.raise_similar = raise_similar

        def track_similar(self, artist, title):
            if self.raise_similar:
                raise LastFmError("Last.fm error 29: rate limited")
            return []

        def track_top_tags(self, artist, title):
            return []

    hit_fm = bs._CountingFm(RealShapedFm())
    real_fetch_track(hit_fm, "A", "B", now=1.0)
    assert hit_fm.requests == 2  # getSimilar + getTopTags, both attempted

    raising_fm = bs._CountingFm(RealShapedFm(raise_similar=True))
    with pytest.raises(LastFmError):
        real_fetch_track(raising_fm, "A", "B", now=1.0)
    assert raising_fm.requests == 1  # getSimilar raised; getTopTags never ran


# ---- merge never overwrites an existing entry -----------------------------


def test_merge_save_never_overwrites_an_existing_entry(tmp_path):
    store = Store(tmp_path)
    store.save_lastfm_tracks({"version": 1, "tracks": {
        "k1": {"similar": [{"artist": "Orig", "track": "T", "match": 0.9}],
               "tags": ["jazz"], "fetched_at": "original-time", "miss": False},
    }})

    merged = merge_save(store, {
        "k1": {"similar": [], "tags": [], "fetched_at": "stale-time", "miss": True},
        "k2": {"similar": [], "tags": ["pop"], "fetched_at": "t", "miss": False},
    })

    assert merged["k1"]["fetched_at"] == "original-time"
    assert merged["k2"]["fetched_at"] == "t"

    on_disk = store.lastfm_track_map()
    assert on_disk["k1"]["fetched_at"] == "original-time"
    assert on_disk["k2"]["fetched_at"] == "t"


def test_run_backfill_end_to_end_saves_final_summary_matches_disk(tmp_path):
    store = Store(tmp_path)
    target = {
        "k1": {"artist": "One", "title": "T", "keys": ["k1"]},
        "k2": {"artist": "Two", "title": "T", "keys": ["k2"]},
        "k3": {"artist": "Ghost", "title": "T", "keys": ["k3"]},
    }
    fm = FakeFm(responses={
        ("One", "T"): hit_record(),
        ("Two", "T"): hit_record(),
        ("Ghost", "T"): miss_record(),
    })

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    saved = store.lastfm_track_map()
    assert set(saved) == {"k1", "k2", "k3"}
    assert saved["k3"]["miss"] is True
    assert summary["fetched"] == 3
    assert summary["misses"] == 1
    assert summary["already_known"] == 0


# ---- --refetch-misses end to end -------------------------------------------


def test_run_backfill_leaves_misses_untouched_by_default(tmp_path):
    store = Store(tmp_path)
    store.save_lastfm_tracks({"version": 1, "tracks": {"k1": miss_record("original")}})
    target = {"k1": {"artist": "A", "title": "T", "keys": ["k1"]}}
    fm = FakeFm(responses={("A", "T"): hit_record("new")})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["already_known"] == 1
    assert summary["attempted"] == 0
    assert fm.calls == []
    assert store.lastfm_track_map()["k1"]["fetched_at"] == "original"


def test_run_backfill_retries_misses_with_refetch_misses(tmp_path):
    store = Store(tmp_path)
    store.save_lastfm_tracks({"version": 1, "tracks": {"k1": miss_record("original")}})
    target = {"k1": {"artist": "A", "title": "T", "keys": ["k1"]}}
    fm = FakeFm(responses={("A", "T"): hit_record("new")})

    summary = run_backfill(target, store, fm, refetch_misses=True, print_fn=lambda *_: None)

    assert summary["already_known"] == 0
    assert summary["attempted"] == 1
    assert fm.calls == [("A", "T")]
    # Fix round 1, I1: a deliberate --refetch-misses fetch must actually
    # land — plain existing-wins used to make this flag a pure call burner
    # (2N requests spent to change nothing). `run_backfill` marks "k1" as a
    # deliberate miss-retry key, and `merge_save` replaces the stale on-disk
    # miss with the freshly-fetched hit for exactly that key.
    assert store.lastfm_track_map()["k1"]["fetched_at"] == "new"
    assert store.lastfm_track_map()["k1"]["miss"] is False


# ---- clobber guard ----------------------------------------------------------


def test_merge_save_refuses_when_a_malformed_reread_would_clobber_the_cache(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save_lastfm_tracks({"version": 1, "tracks": {
        "k1": {"similar": [], "tags": ["jazz"], "fetched_at": "t0", "miss": False},
    }})

    calls = {"n": 0}
    real_track_map = store.lastfm_track_map

    def flaky_track_map():
        calls["n"] += 1
        return real_track_map() if calls["n"] == 1 else {}

    monkeypatch.setattr(store, "lastfm_track_map", flaky_track_map)

    with pytest.raises(BackfillAbort, match="malformed"):
        merge_save(store, {"k2": {"similar": [], "tags": [], "fetched_at": "t1", "miss": False}})

    on_disk = json.loads((tmp_path / "lastfm_tracks.json").read_text())["tracks"]
    assert on_disk == {
        "k1": {"similar": [], "tags": ["jazz"], "fetched_at": "t0", "miss": False},
    }


def test_merge_save_refuses_a_partial_shrink_not_just_total_collapse(tmp_path, monkeypatch):
    store = Store(tmp_path)
    seed = {
        f"k{i}": {"similar": [], "tags": [], "fetched_at": "t0", "miss": False}
        for i in range(1, 6)
    }
    store.save_lastfm_tracks({"version": 1, "tracks": seed})

    calls = {"n": 0}
    real_track_map = store.lastfm_track_map

    def flaky_track_map():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_track_map()
        truncated = real_track_map()
        return {k: truncated[k] for k in list(truncated)[:2]}

    monkeypatch.setattr(store, "lastfm_track_map", flaky_track_map)

    with pytest.raises(BackfillAbort, match="shrank from 5 to 2"):
        merge_save(store, {"k6": {"similar": [], "tags": [], "fetched_at": "t1", "miss": False}})

    on_disk = json.loads((tmp_path / "lastfm_tracks.json").read_text())["tracks"]
    assert on_disk == seed


def test_merge_save_refuses_on_unreadable_json_and_reports_discarded_count(tmp_path):
    store = Store(tmp_path)
    (tmp_path / "lastfm_tracks.json").write_text("{not valid json")

    with pytest.raises(BackfillAbort, match=r"2 fetched-but-unsaved"):
        merge_save(store, {
            "k1": {"similar": [], "tags": [], "fetched_at": "t", "miss": False},
            "k2": {"similar": [], "tags": [], "fetched_at": "t", "miss": False},
        })

    assert (tmp_path / "lastfm_tracks.json").read_text() == "{not valid json"


def test_run_backfill_propagates_backfill_abort_without_losing_earlier_saves(tmp_path, monkeypatch):
    import backfill_similar as bs

    store = Store(tmp_path)
    target = {
        "k1": {"artist": "First", "title": "T", "keys": ["k1"]},
        "k2": {"artist": "Second", "title": "T", "keys": ["k2"]},
    }
    fm = FakeFm(responses={("First", "T"): hit_record(), ("Second", "T"): hit_record()})

    real_merge_save = bs.merge_save
    calls = {"n": 0}

    def flaky_merge_save(store_, new_entries, replace_keys=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_merge_save(store_, new_entries, replace_keys=replace_keys)
        raise BackfillAbort("simulated clobber guard trip")

    monkeypatch.setattr(bs, "merge_save", flaky_merge_save)

    with pytest.raises(BackfillAbort):
        run_backfill(target, store, fm, save_every=1, print_fn=lambda *_: None)

    saved = store.lastfm_track_map()
    assert "k1" in saved  # the earlier, already-flushed save survives
    assert "k2" not in saved
