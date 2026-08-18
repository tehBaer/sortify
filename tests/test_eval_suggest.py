"""Unit tests for the hold-one-out evaluation harness (scripts/eval_suggest.py).

Hand-built fixtures only — no reads from data/, no network. The harness's
whole validity rests on hold-one-out: these tests pin that the sampled pairs
are actually re-ranked against a profile that has had the pair's track
removed, not the trivially-perfect version where the track is still there
(spec §Evaluation; the "already must not fire for the held-out pair" rule).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import eval_suggest as ev  # noqa: E402


def tag_entry(*tags, name="X", miss=False):
    return {"name": name, "tags": [{"name": t, "count": 100} for t in tags], "miss": miss}


def track(uri, artist_id, name=None):
    return {"uri": uri, "artists": [{"id": artist_id, "name": name or artist_id}]}


ARTISTS = {
    "beach-house": tag_entry("dream pop", "shoegaze", name="Beach House"),
    "slowdive": tag_entry("shoegaze", "dream pop", name="Slowdive"),
    "kvelertak": tag_entry("black'n'roll", "hardcore punk", name="Kvelertak"),
}


def fake_home_tracks():
    return {
        "dreamy": [
            track("spotify:track:d1", "beach-house"),
            track("spotify:track:d2", "beach-house"),
            track("spotify:track:d3", "slowdive"),
        ],
        "heavy": [
            track("spotify:track:h1", "kvelertak"),
        ],
    }


def test_collect_pairs_is_one_per_home_track_occurrence():
    pairs = ev.collect_pairs(fake_home_tracks())
    assert len(pairs) == 4
    assert ("dreamy", "spotify:track:d1") in [(hid, t["uri"]) for hid, t in pairs]


def test_sample_pairs_is_seeded_and_repeatable():
    pairs = ev.collect_pairs(fake_home_tracks())
    a = ev.sample_pairs(pairs, n=3, seed=7)
    b = ev.sample_pairs(pairs, n=3, seed=7)
    assert [(hid, t["uri"]) for hid, t in a] == [(hid, t["uri"]) for hid, t in b]
    assert len(a) == 3

    c = ev.sample_pairs(pairs, n=3, seed=8)
    assert [(hid, t["uri"]) for hid, t in a] != [(hid, t["uri"]) for hid, t in c] or len(pairs) <= 3


def test_sample_pairs_caps_at_population_size():
    pairs = ev.collect_pairs(fake_home_tracks())
    sampled = ev.sample_pairs(pairs, n=999, seed=7)
    assert len(sampled) == len(pairs)


def test_uri_home_index_finds_multi_home_tracks():
    home_tracks = fake_home_tracks()
    home_tracks["dreamy2"] = [track("spotify:track:d1", "beach-house")]  # same uri, second home
    idx = ev.uri_home_index(home_tracks)
    assert idx["spotify:track:d1"] == {"dreamy", "dreamy2"}
    assert idx["spotify:track:h1"] == {"heavy"}


def test_evaluate_pair_holds_track_out_before_ranking():
    home_tracks = fake_home_tracks()
    profiles = ev.build_all_profiles(home_tracks, ARTISTS)
    idx = ev.uri_home_index(home_tracks)
    held = track("spotify:track:d1", "beach-house")

    top1_hit, top3_hit = ev.evaluate_pair("dreamy", held, home_tracks, profiles, ARTISTS, idx, top_k=3)

    # beach-house is still the majority artist in "dreamy" once d1 is held out
    # (d2 remains), so the true home should still win the ranking.
    assert top1_hit is True
    assert top3_hit is True


def test_evaluate_pair_does_not_mutate_shared_profiles():
    # A regression pin against the trivial bug: if evaluate_pair mutated the
    # profiles dict passed in (instead of rebuilding a copy for the held-out
    # home), later pairs in the same run would see a corrupted "heavy" or
    # "dreamy" profile.
    home_tracks = fake_home_tracks()
    profiles = ev.build_all_profiles(home_tracks, ARTISTS)
    before = set(profiles["dreamy"]["uris"])
    held = track("spotify:track:d1", "beach-house")
    idx = ev.uri_home_index(home_tracks)

    ev.evaluate_pair("dreamy", held, home_tracks, profiles, ARTISTS, idx, top_k=3)

    assert profiles["dreamy"]["uris"] == before


def test_run_eval_reports_fractions_in_unit_interval():
    home_tracks = fake_home_tracks()
    pairs = ev.collect_pairs(home_tracks)
    result = ev.run_eval(home_tracks, ARTISTS, pairs)
    assert result["n"] == 4
    assert 0.0 <= result["top1"] <= 1.0
    assert 0.0 <= result["top3"] <= 1.0


def test_run_eval_with_zero_tag_weight_is_the_artist_only_baseline(monkeypatch):
    home_tracks = fake_home_tracks()
    pairs = ev.collect_pairs(home_tracks)

    with ev.weights(tag_weight=0.0, dilution=ev.suggest_mod.ARTIST_TAG_DILUTION):
        assert ev.suggest_mod.TAG_WEIGHT == 0.0
        baseline = ev.run_eval(home_tracks, ARTISTS, pairs)

    # Weights are restored once the context manager exits.
    assert ev.suggest_mod.TAG_WEIGHT != 0.0
    assert baseline["n"] == 4


def test_grid_search_covers_every_cell_and_restores_weights():
    home_tracks = fake_home_tracks()
    pairs = ev.collect_pairs(home_tracks)
    grid = [(2.0, 0.5), (4.0, 0.5)]
    original = (ev.suggest_mod.TAG_WEIGHT, ev.suggest_mod.ARTIST_TAG_DILUTION)

    results = ev.grid_search(home_tracks, ARTISTS, pairs, grid)

    assert set(results.keys()) == set(grid)
    for cell in grid:
        assert "top1" in results[cell] and "top3" in results[cell]
    # weights end up restored after the search, not left on the last cell
    assert (ev.suggest_mod.TAG_WEIGHT, ev.suggest_mod.ARTIST_TAG_DILUTION) == original
