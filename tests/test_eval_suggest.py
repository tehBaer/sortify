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
from sortify.suggest import build_profile as raw_build_profile  # noqa: E402
from sortify.suggest import suggest as raw_suggest  # noqa: E402
from sortify.tags import track_key  # noqa: E402


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


def test_evaluate_pair_removes_held_out_track_from_its_own_track_keys():
    # Task 4's neighbour signal keys off `track_keys` in each home's
    # profile: `suggest._neighbour_score` counts a home as containing one of
    # a track's Last.fm neighbours when that neighbour's (artist, title) key
    # is present in `prof["track_keys"]`. Hold-one-out therefore isn't only
    # about `uris`/`artist_counts`/`tag_counts` (the pre-Task-4 signals) —
    # if the held-out track's OWN key survived in `track_keys`, a neighbour
    # list that happens to reference it back (its own record, or a data
    # quirk) could let the held-out track match its own hold-out home via
    # the neighbour path, the exact kind of leak hold-out exists to close
    # for the artist/tag signals (module docstring, C3).
    #
    # `evaluate_pair` doesn't build a bespoke `track_keys` — it reuses
    # `build_profile(held_out_tracks, tag_artists)` for every signal at
    # once, so this pins that nothing about wiring `track_map` through
    # introduced a second, divergent rebuild path that keeps the held
    # track's key around.
    d1 = {"uri": "spotify:track:d1", "name": "Dreams", "artists": [{"id": "beach-house", "name": "Beach House"}]}
    decoy1 = {
        "uri": "spotify:track:decoy1",
        "name": "Kvelertak",
        "artists": [{"id": "kvelertak", "name": "Kvelertak"}],
    }
    home_tracks = {"solo": [d1], "decoy": [decoy1]}
    profiles = ev.build_all_profiles(home_tracks, ARTISTS)
    idx = ev.uri_home_index(home_tracks)
    held_key = track_key("Beach House", "Dreams")

    # A self-referencing neighbour entry, standing in for "the held-out
    # track's own key surviving in track_keys" — real Last.fm data doesn't
    # list a track as its own neighbour, but the same-artist exclusion in
    # `_neighbour_score` would ALSO suppress a genuine self-reference, so a
    # bare end-to-end run here wouldn't isolate whether `track_keys` itself
    # (as opposed to the same-artist filter) is doing the excluding. The
    # direct assertion below checks the layer this test is about.
    track_map = {
        held_key: {"similar": [{"artist": "Beach House", "track": "Dreams", "match": 1.0}]},
    }

    ev.evaluate_pair("solo", d1, home_tracks, profiles, ARTISTS, idx, top_k=3, track_map=track_map)

    # evaluate_pair must not mutate the passed-in `profiles` (separately
    # pinned by test_evaluate_pair_does_not_mutate_shared_profiles below);
    # rebuild the same way evaluate_pair does internally to inspect the
    # per-pair profile it actually scored against.
    held_out_tracks = [t for t in home_tracks["solo"] if t["uri"] != d1["uri"]]
    rebuilt = raw_build_profile(held_out_tracks, ARTISTS)
    assert held_key not in rebuilt["track_keys"]


def test_evaluate_pair_removes_held_out_track_from_its_own_track_keys_mutation_check():
    # Mutation check: build "solo"'s profile WITHOUT holding d1 out (the
    # no-op hold-out this test exists to catch) and confirm the key IS
    # present — proving the assertion above is sensitive to hold-out
    # actually happening, not vacuously true regardless.
    d1 = {"uri": "spotify:track:d1", "name": "Dreams", "artists": [{"id": "beach-house", "name": "Beach House"}]}
    home_tracks = {"solo": [d1]}
    held_key = track_key("Beach House", "Dreams")

    untouched = raw_build_profile(home_tracks["solo"], ARTISTS)
    assert held_key in untouched["track_keys"]


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


def test_evaluate_pair_rebuilds_artist_names_without_the_held_out_track(monkeypatch):
    # Mutation pin, same family as the track_keys hold-out pin: if the
    # held-out track's artist were still in its home's artist_names, the
    # track's own artist-similar list could match it back to itself.
    held = {"uri": "u1", "name": "T1", "artists": [{"id": "a1", "name": "Lone Artist"}]}
    other = {"uri": "u2", "name": "T2", "artists": [{"id": "a2", "name": "Other"}]}
    home_tracks = {"H": [held, other]}
    profiles = ev.build_all_profiles(home_tracks, {})
    assert "lone artist" in profiles["H"]["artist_names"]  # present before hold-out
    seen = {}
    monkeypatch.setattr(ev, "suggest", lambda t, profs, *a, **k: seen.update(profs) or [])
    ev.evaluate_pair("H", held, home_tracks, profiles, {}, ev.uri_home_index(home_tracks))
    assert "lone artist" not in seen["H"]["artist_names"]
    assert "other" in seen["H"]["artist_names"]


def test_weights_can_vary_artist_sim_weight():
    with ev.weights(artist_sim_weight=2.0):
        assert ev.suggest_mod.ARTIST_SIM_WEIGHT == 2.0
    assert ev.suggest_mod.ARTIST_SIM_WEIGHT == 1.0  # restored
