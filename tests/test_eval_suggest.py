"""Unit tests for the hold-one-out evaluation harness (scripts/eval_suggest.py).

Hand-built fixtures only — no reads from data/, no network. The harness's
whole validity rests on hold-one-out: these tests pin that the sampled pairs
are actually re-ranked against a profile that has had the track removed from
EVERY home it sits in, not the trivially-perfect version where it (or a
sibling home) still has it (spec §Evaluation; the "already must not fire for
the held-out pair" rule; fix round 1 finding C3 for the multi-home case).

Fix round 1, finding C2: the original `test_evaluate_pair_holds_track_out_
before_ranking` used a fixture (home with two tracks by the same artist)
where hold-out changes NOTHING about the outcome — "dreamy" wins whether or
not d1 is held out, because d2 alone is enough. A no-op `evaluate_pair` that
never held anything out would have passed that test. The fixtures below are
built so hold-out actually flips the result, and are mutation-checked: see
the comments on `test_evaluate_pair_holds_track_out_before_ranking` and
`test_evaluate_pair_holds_track_out_of_every_home_it_appears_in` for exactly
how (temporarily rank against the untouched profiles and confirm the
assertion fails).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import eval_suggest as ev  # noqa: E402
from sortify.suggest import suggest as raw_suggest  # noqa: E402


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
    # Fixture built so hold-out changes the outcome: "solo" contains ONLY
    # the held-out track, so once it's genuinely removed the profile is
    # empty (no artist overlap, no tags) and nothing should rank it — the
    # decoy home shares no artist or tag with it either, so no suggestion
    # should fire at all.
    #
    # Mutation check performed by hand: ranking against the UNTOUCHED
    # profiles (`raw_suggest(held, profiles, ARTISTS)` — "solo" still
    # containing d1) makes `already=True` win "solo" trivially, which would
    # flip both assertions below to True. That confirms this fixture is
    # sensitive to whether hold-out actually happened, unlike the original
    # two-track fixture (fix round 1, finding C2).
    home_tracks = {
        "solo": [track("spotify:track:d1", "beach-house")],
        "decoy": [track("spotify:track:decoy1", "kvelertak")],
    }
    profiles = ev.build_all_profiles(home_tracks, ARTISTS)
    idx = ev.uri_home_index(home_tracks)
    held = track("spotify:track:d1", "beach-house")

    top1_hit, top3_hit, _ = ev.evaluate_pair("solo", held, home_tracks, profiles, ARTISTS, idx, top_k=3)

    assert top1_hit is False
    assert top3_hit is False


def test_evaluate_pair_holds_track_out_before_ranking_mutation_check():
    # The other half of the mutation check above, executed rather than just
    # described: rank the same fixture against the UNTOUCHED profiles (as a
    # no-op hold-out would) and confirm the "correct" outcome is trivially
    # True — proving the fixture and assertions above are not vacuous.
    home_tracks = {
        "solo": [track("spotify:track:d1", "beach-house")],
        "decoy": [track("spotify:track:decoy1", "kvelertak")],
    }
    profiles = ev.build_all_profiles(home_tracks, ARTISTS)
    held = track("spotify:track:d1", "beach-house")

    results = raw_suggest(held, profiles, ARTISTS)
    ranked_ids = [r["playlist_id"] for r in results]
    assert ranked_ids[:1] == ["solo"]  # already=True wins trivially without hold-out


def test_evaluate_pair_holds_track_out_of_every_home_it_appears_in():
    # Fix round 1, finding C3 (label leak): a multi-home track filed in two
    # homes that BOTH only contain that one track. Without holding it out of
    # every true home, "home2" would keep `already=True` and guarantee a
    # top-1 hit for the pair regardless of any real signal. Both homes are
    # emptied by a correct hold-out, and neither shares anything with a
    # decoy, so nothing should rank.
    shared = track("spotify:track:shared", "beach-house")
    home_tracks = {
        "home1": [shared],
        "home2": [shared],
    }
    profiles = ev.build_all_profiles(home_tracks, ARTISTS)
    idx = ev.uri_home_index(home_tracks)

    top1_hit, top3_hit, _ = ev.evaluate_pair("home1", shared, home_tracks, profiles, ARTISTS, idx, top_k=3)

    assert top1_hit is False
    assert top3_hit is False


def test_evaluate_pair_holds_track_out_of_every_home_it_appears_in_mutation_check():
    # Mutation check for C3: hold the track out of ONLY "home1" (the old,
    # buggy behaviour) and confirm "home2" still wins trivially via
    # `already=True` — proving the fixture is sensitive to the bug this
    # test pins against.
    shared = track("spotify:track:shared", "beach-house")
    home_tracks = {
        "home1": [shared],
        "home2": [shared],
    }
    profiles = ev.build_all_profiles(home_tracks, ARTISTS)
    only_home1_held_out = dict(profiles)
    only_home1_held_out["home1"] = ev.build_profile([], ARTISTS)

    results = raw_suggest(shared, only_home1_held_out, ARTISTS)
    ranked_ids = [r["playlist_id"] for r in results]
    assert ranked_ids[:1] == ["home2"]  # sibling home wins trivially, unfixed


def test_evaluate_pair_flags_artist_absent_pairs():
    # The spec's target case: a track whose artist has no overlap in ANY
    # home once held out. "solo" is emptied by hold-out and no other home
    # shares the artist, so this pair is artist-absent.
    home_tracks = {
        "solo": [track("spotify:track:d1", "beach-house")],
        "decoy": [track("spotify:track:decoy1", "kvelertak")],
    }
    profiles = ev.build_all_profiles(home_tracks, ARTISTS)
    idx = ev.uri_home_index(home_tracks)
    held = track("spotify:track:d1", "beach-house")

    _, _, artist_absent = ev.evaluate_pair("solo", held, home_tracks, profiles, ARTISTS, idx, top_k=3)
    assert artist_absent is True

    # But a track whose artist DOES still overlap elsewhere (a second
    # beach-house track survives in "solo") is not artist-absent.
    home_tracks2 = fake_home_tracks()
    profiles2 = ev.build_all_profiles(home_tracks2, ARTISTS)
    idx2 = ev.uri_home_index(home_tracks2)
    held2 = track("spotify:track:d1", "beach-house")
    _, _, artist_absent2 = ev.evaluate_pair("dreamy", held2, home_tracks2, profiles2, ARTISTS, idx2, top_k=3)
    assert artist_absent2 is False


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
    assert 0.0 <= result["artist_absent_top1"] <= 1.0
    assert 0.0 <= result["artist_absent_top3"] <= 1.0


def test_run_eval_with_zero_tag_weight_is_the_artist_only_baseline():
    home_tracks = fake_home_tracks()
    pairs = ev.collect_pairs(home_tracks)

    with ev.weights(tag_weight=0.0):
        assert ev.suggest_mod.TAG_WEIGHT == 0.0
        baseline = ev.run_eval(home_tracks, ARTISTS, pairs)

    # Weight is restored once the context manager exits.
    assert ev.suggest_mod.TAG_WEIGHT != 0.0
    assert baseline["n"] == 4


def test_grid_search_covers_every_cell_and_restores_weights():
    home_tracks = fake_home_tracks()
    pairs = ev.collect_pairs(home_tracks)
    grid = [2.0, 4.0]
    original = ev.suggest_mod.TAG_WEIGHT

    results = ev.grid_search(home_tracks, ARTISTS, pairs, grid)

    assert set(results.keys()) == set(grid)
    for cell in grid:
        assert "top1" in results[cell] and "top3" in results[cell]
    # weight ends up restored after the search, not left on the last cell
    assert ev.suggest_mod.TAG_WEIGHT == original
