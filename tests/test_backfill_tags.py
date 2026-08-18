import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backfill_tags import (  # noqa: E402
    artists_to_fetch,
    collect_target_artists,
    load_all_cached_tracks,
    load_home_tracks,
    merge_save,
    run_backfill,
)
from sortify.store import Store  # noqa: E402
from sortify.tags import ArtistTags, LastFmError  # noqa: E402


def track(artist_id, artist_name, uri="spotify:track:x"):
    return {"uri": uri, "artists": [{"id": artist_id, "name": artist_name}]}


def write_cache(data_dir: Path, playlists: dict) -> None:
    (data_dir / "cache.json").write_text(json.dumps({"playlists": playlists}))


def write_config(data_dir: Path, home_ids: list) -> None:
    (data_dir / "config.json").write_text(json.dumps({"home_ids": home_ids}))


class FakeFm:
    """Stands in for sortify.tags.LastFm. `responses` maps artist name to an
    ArtistTags (hit) or None (Last.fm code-6 not-found, per `top_tags`'s own
    contract). `raise_on` maps artist name to an exception `top_tags` raises
    instead — used to simulate a non-6 Last.fm failure."""

    def __init__(self, responses=None, raise_on=None):
        self.responses = responses or {}
        self.raise_on = raise_on or {}
        self.calls = []

    def top_tags(self, artist_name):
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


# ---- skip-known -----------------------------------------------------------


def test_artists_to_fetch_skips_ids_already_in_tags_json():
    target = {"a1": "Artist One", "a2": "Artist Two"}
    known = {"a1": {"name": "Artist One", "tags": [], "miss": False}}
    assert artists_to_fetch(target, known) == {"a2": "Artist Two"}


def test_run_backfill_skips_artists_already_known_hit_or_miss(tmp_path):
    store = Store(tmp_path)
    store.save_tag_artists({
        "a1": {"name": "Hit", "lastfm_name": "Hit", "tags": [], "fetched_at": "t", "miss": False},
        "a2": {"name": "Miss", "lastfm_name": None, "tags": [], "fetched_at": "t", "miss": True},
    })
    target = {"a1": "Hit", "a2": "Miss", "a3": "New"}
    fm = FakeFm(responses={"New": ArtistTags(matched_name="New", tags=[{"name": "rock", "count": 50}])})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["already_known"] == 2
    assert summary["attempted"] == 1
    assert fm.calls == ["New"]
    assert "a3" in store.tag_artists()


# ---- limit -----------------------------------------------------------------


def test_run_backfill_respects_limit(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "One", "a2": "Two", "a3": "Three"}
    fm = FakeFm(responses={
        name: ArtistTags(matched_name=name, tags=[]) for name in ("One", "Two", "Three")
    })

    summary = run_backfill(target, store, fm, limit=2, print_fn=lambda *_: None)

    assert summary["attempted"] == 2
    assert len(fm.calls) == 2
    assert summary["target"] == 3


# ---- code 6 vs other errors --------------------------------------------


def test_code6_not_found_is_recorded_as_miss(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "Ghost Artist"}
    fm = FakeFm(responses={"Ghost Artist": None})  # top_tags' own not-found contract

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["misses"] == 1
    assert summary["skipped"] == 0
    saved = store.tag_artists()
    assert saved["a1"]["miss"] is True


def test_other_lastfm_error_is_skipped_and_left_absent(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "Rate Limited Artist"}
    fm = FakeFm(raise_on={"Rate Limited Artist": LastFmError("Last.fm error 29: rate limit exceeded")})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["skipped"] == 1
    assert summary["misses"] == 0
    assert summary["fetched"] == 0
    assert "a1" not in store.tag_artists()


def test_transport_error_is_also_skipped_not_recorded(tmp_path):
    """enrich() wraps any non-LastFmError exception (bad JSON, network
    failure) into a LastFmError; a fake client raising a plain exception
    exercises that wrapping path end to end."""
    store = Store(tmp_path)
    target = {"a1": "Flaky Artist"}
    fm = FakeFm(raise_on={"Flaky Artist": ConnectionError("boom")})

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    assert summary["skipped"] == 1
    assert "a1" not in store.tag_artists()


# ---- incremental save preserves progress on a mid-run error --------------


def test_error_mid_run_does_not_lose_artists_fetched_before_it(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "First", "a2": "Second", "a3": "Third", "a4": "Fourth"}
    fm = FakeFm(
        responses={
            "First": ArtistTags(matched_name="First", tags=[]),
            "Second": ArtistTags(matched_name="Second", tags=[]),
            "Fourth": ArtistTags(matched_name="Fourth", tags=[]),
        },
        raise_on={"Third": LastFmError("Last.fm error 11: service offline")},
    )

    # save_every=1 forces an on-disk save after each successful fetch, so
    # "First" and "Second" (fetched before "Third" raises) must be on disk
    # regardless of what happens to "Third" or "Fourth" after it.
    summary = run_backfill(target, store, fm, save_every=1, print_fn=lambda *_: None)

    saved = store.tag_artists()
    assert "a1" in saved
    assert "a2" in saved
    assert "a3" not in saved  # the failing artist: left absent, not recorded
    assert "a4" in saved  # the loop continues past the failure
    assert summary["fetched"] == 3
    assert summary["skipped"] == 1


# ---- merge never overwrites an existing entry -----------------------------


def test_merge_save_never_overwrites_an_existing_entry(tmp_path):
    store = Store(tmp_path)
    store.save_tag_artists({
        "a1": {"name": "Original", "lastfm_name": "Original", "tags": [{"name": "jazz", "count": 90}],
               "fetched_at": "original-time", "miss": False},
    })

    # Simulate this script's own snapshot being stale relative to what is
    # now on disk (e.g. another process wrote a1 in the meantime) by handing
    # merge_save a conflicting value for the same key.
    merged = merge_save(store, {
        "a1": {"name": "Stale Overwrite Attempt", "lastfm_name": None, "tags": [],
               "fetched_at": "stale-time", "miss": True},
        "a2": {"name": "Brand New", "lastfm_name": "Brand New", "tags": [], "fetched_at": "t", "miss": False},
    })

    assert merged["a1"]["name"] == "Original"
    assert merged["a1"]["fetched_at"] == "original-time"
    assert merged["a2"]["name"] == "Brand New"

    on_disk = store.tag_artists()
    assert on_disk["a1"]["name"] == "Original"
    assert on_disk["a2"]["name"] == "Brand New"


def test_run_backfill_end_to_end_saves_final_summary_matches_disk(tmp_path):
    store = Store(tmp_path)
    target = {"a1": "One", "a2": "Two", "a3": "Ghost"}
    fm = FakeFm(responses={
        "One": ArtistTags(matched_name="One", tags=[{"name": "rock", "count": 40}]),
        "Two": ArtistTags(matched_name="Two", tags=[{"name": "pop", "count": 40}]),
        "Ghost": None,
    })

    summary = run_backfill(target, store, fm, print_fn=lambda *_: None)

    saved = store.tag_artists()
    assert set(saved) == {"a1", "a2", "a3"}
    assert saved["a3"]["miss"] is True
    assert summary["fetched"] == 3
    assert summary["misses"] == 1
    assert summary["already_known"] == 0
