"""Hold-one-out evaluation harness for sortify/suggest.py.

Not part of the served app: reads cached data on disk and never imports
`sortify.spotify`, so it makes zero network calls of any kind. Zero writes
either — `data/` here is the live tree's data, read-only, the one sanctioned
use of that symlink (see task-3 brief).

The user's own home playlists are labelled training data: every track that
sits in a home is an example of a track the user decided belongs there. The
harness samples (home, track) pairs and asks "if we rebuild the profile of
EVERY home this track sits in, with this track removed, does ranking still
put it back?"

Hold-one-out is the whole validity of this file, and it must be applied to
every true home of the track, not just the one pair happens to name — a
multi-home track whose sibling home is left untouched keeps `already=True`
there and trivially wins the ranking regardless of any real signal (fix
round 1, finding C3). See `test_removing_track_from_profile_changes_its_score`
in tests/test_suggest.py and the harness's own tests in
tests/test_eval_suggest.py, in particular
`test_evaluate_pair_holds_track_out_before_ranking` (a fixture where holding
out actually flips the outcome — a no-op hold-out fails it, mutation-verified)
and `test_evaluate_pair_holds_track_out_of_every_home_it_appears_in` (the
multi-home regression pin for C3).

Usage:
    python scripts/eval_suggest.py [--n 500] [--seed 7] [--baseline] [--search]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sortify.suggest as suggest_mod  # noqa: E402
from sortify.suggest import build_profile, suggest  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_N = 500
DEFAULT_SEED = 7
DEFAULT_TOP_K = 3
# 1-D sweep over TAG_WEIGHT (fix round 1, ruling R3a: ARTIST_TAG_DILUTION was
# removed because it and TAG_WEIGHT only ever appeared as a product — the
# original 4x4 grid was this same 1-D search wearing a 2-D costume, with
# duplicate cells wherever two (weight, dilution) pairs shared a product).
# These values are the deduplicated products of that original grid, so the
# rerun is comparable to the numbers task-3-report.md already recorded.
SEARCH_TAG_WEIGHTS = (0.6, 0.9, 1.0, 1.2, 1.4, 1.5, 1.8, 2.0, 2.1, 2.8, 3.0, 4.0, 4.2, 6.0)


# ---- loading (the only place that touches disk) -----------------------------


def load_home_tracks(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    """Read-only: config.json's `home_ids` + cache.json's cached track lists.

    Mirrors `app.py`'s `_resolve_homes` + `_cached_tracks`, minus the live
    Spotify calls that function makes when the cache is stale — this harness
    only ever sees what is already on disk. Home ids missing from the cache
    (never fetched) are silently skipped rather than treated as empty homes.

    Unlike `_resolve_homes`, this mirrors `config.json`'s `home_ids` only —
    an empty (or unset) `home_ids` yields zero homes here, not app.py's
    live fallback to every editable playlist. That fallback exists so a
    fresh install has *something* to build profiles from before the user
    marks any Home; the harness has no such need (a config with no homes
    named simply has nothing to evaluate) and duplicating the live-listing
    fallback here would need a Spotify call this file is not allowed to make.
    """
    with open(data_dir / "config.json") as f:
        cfg = json.load(f)
    with open(data_dir / "cache.json") as f:
        cache = json.load(f)
    home_ids = cfg.get("home_ids") or []
    playlists = cache.get("playlists", {})
    return {hid: playlists[hid]["tracks"] for hid in home_ids if hid in playlists}


def load_tag_artists(data_dir: Path = DATA_DIR) -> dict[str, dict]:
    """Guard-on-read, same as `store.tag_artists()`: a bad/absent envelope
    degrades to no tags rather than raising, since a suggestion tool should
    fail toward "artist-overlap-only", not toward a crash."""
    try:
        with open(data_dir / "tags.json") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    artists = payload.get("artists") if isinstance(payload, dict) else None
    return artists if isinstance(artists, dict) else {}


# ---- pure harness logic (unit-tested against hand-built fixtures) ----------


def collect_pairs(home_tracks: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    """One (home_id, track) pair per occurrence of a track in a home. A
    track filed into two homes yields two pairs, one per home.

    A track sitting in N homes is therefore sampled up to N times, each
    hold-one-out eval run with an identical outcome (same track, same
    artists, same tags — only which home is being held out differs, and
    hold-one-out already applies to every home the track sits in, not just
    the sampled pair's — see the module docstring's C3 note). Mild
    over-weighting of multi-home tracks in the aggregate accuracy, accepted
    rather than deduplicated: multi-home tracks are rare enough in practice
    that this hasn't been worth the extra bookkeeping to fix.
    """
    return [(hid, t) for hid, tracks in home_tracks.items() for t in tracks]


def sample_pairs(
    pairs: list[tuple[str, dict]], n: int, seed: int
) -> list[tuple[str, dict]]:
    """Seeded, repeatable sample of at most `n` pairs (order-stable input,
    order-shuffled output) — same seed always yields the same sample.

    Not nested across `n`: `sample_pairs(pairs, 100, 7)` is not a subset of
    `sample_pairs(pairs, 500, 7)`. `random.Random(seed).sample` consumes the
    RNG stream differently depending on how many items are drawn, so two
    calls with the same seed but different `n` are independent draws, not
    one growing out of the other. Comparing runs at different `n` is
    therefore comparing two different samples, not a bigger vs. a smaller
    look at the same one.
    """
    rng = random.Random(seed)
    if n >= len(pairs):
        sampled = list(pairs)
        rng.shuffle(sampled)
        return sampled
    return rng.sample(pairs, n)


def uri_home_index(home_tracks: dict[str, list[dict]]) -> dict[str, set[str]]:
    """track uri -> every home it currently sits in. Used to score
    multi-home tracks as correct if ANY of their homes lands in top-k, and to
    know every home a held-out track must be removed from (not just the
    pair's own home — see the module docstring, finding C3)."""
    idx: dict[str, set[str]] = {}
    for hid, tracks in home_tracks.items():
        for t in tracks:
            idx.setdefault(t["uri"], set()).add(hid)
    return idx


def build_all_profiles(
    home_tracks: dict[str, list[dict]], tag_artists: dict[str, dict]
) -> dict[str, dict]:
    return {hid: build_profile(tracks, tag_artists) for hid, tracks in home_tracks.items()}


def _artist_absent(track: dict, profiles_for_pair: dict[str, dict]) -> bool:
    """True when none of the track's artists have any overlap in ANY home
    profile (after hold-out). This is the spec's original target case — "a
    sad ballad by an artist you own is offered playlists of sad, slow music,
    not merely playlists containing that artist" — the subset where artist
    overlap contributes nothing and only the tag signal (or nothing) can
    produce a correct ranking. A track with no artist id at all counts as
    artist-absent too, since overlap can never fire for it either way."""
    artist_ids = {a.get("id") for a in track["artists"] if a.get("id")}
    if not artist_ids:
        return True
    return not any(
        prof["artist_counts"].get(aid, 0) for prof in profiles_for_pair.values() for aid in artist_ids
    )


def evaluate_pair(
    home_id: str,
    track: dict,
    home_tracks: dict[str, list[dict]],
    profiles: dict[str, dict],
    tag_artists: dict[str, dict],
    uri_homes: dict[str, set[str]],
    top_k: int = DEFAULT_TOP_K,
) -> tuple[bool, bool, bool]:
    """Rank `track` with EVERY true home's profile rebuilt minus `track`
    itself — not just `home_id`. A multi-home track's sibling homes must
    lose the track too, or `already=True` there trivially wins the ranking
    no matter what the scorer does (fix round 1, finding C3: this used to
    rebuild only `home_id`'s profile, which handed a multi-home track a free
    top-1 via whichever sibling home was left untouched).

    Returns (top1_hit, top3_hit, artist_absent). `artist_absent` flags the
    spec's target case (see `_artist_absent`) so callers can report accuracy
    on that subset separately.

    Does not mutate `profiles` — a fresh dict is built per call so later
    pairs in the same run never see a corrupted home profile.
    """
    true_homes = uri_homes.get(track["uri"], {home_id})

    profiles_for_pair = dict(profiles)
    for hid in true_homes:
        held_out_tracks = [t for t in home_tracks[hid] if t["uri"] != track["uri"]]
        profiles_for_pair[hid] = build_profile(held_out_tracks, tag_artists)

    # 3-arg call — track_map deliberately not wired until Task 4's measurement.
    results = suggest(track, profiles_for_pair, tag_artists)
    ranked_ids = [r["playlist_id"] for r in results]

    top1_hit = bool(true_homes & set(ranked_ids[:1]))
    top3_hit = bool(true_homes & set(ranked_ids[:top_k]))
    return top1_hit, top3_hit, _artist_absent(track, profiles_for_pair)


def run_eval(
    home_tracks: dict[str, list[dict]],
    tag_artists: dict[str, dict],
    sampled_pairs: list[tuple[str, dict]],
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    """Evaluate a fixed, pre-sampled set of pairs under whatever weight
    `sortify.suggest` currently holds. Takes the sample as an argument
    (rather than sampling itself) so a baseline run, a full grid search, and
    the committed-weight run all score the identical pairs.

    Reports both the overall top1/top3 and the artist-absent subset's
    top1/top3 — the subset where artist overlap cannot fire at all and only
    the tag signal could produce a correct ranking (the spec's target case).
    """
    profiles = build_all_profiles(home_tracks, tag_artists)
    uri_homes = uri_home_index(home_tracks)

    top1 = top3 = 0
    absent_n = absent_top1 = absent_top3 = 0
    for home_id, track in sampled_pairs:
        hit1, hit3, absent = evaluate_pair(home_id, track, home_tracks, profiles, tag_artists, uri_homes, top_k)
        top1 += hit1
        top3 += hit3
        if absent:
            absent_n += 1
            absent_top1 += hit1
            absent_top3 += hit3

    total = len(sampled_pairs)
    return {
        "n": total,
        "top1": top1 / total if total else 0.0,
        "top3": top3 / total if total else 0.0,
        "artist_absent_n": absent_n,
        "artist_absent_top1": absent_top1 / absent_n if absent_n else 0.0,
        "artist_absent_top3": absent_top3 / absent_n if absent_n else 0.0,
    }


@contextlib.contextmanager
def weights(tag_weight: float):
    """Temporarily override sortify.suggest's TAG_WEIGHT constant.
    `suggest()` reads it as a module-level lookup at call time (not a bound
    default), so mutating the module and restoring it afterwards is
    sufficient — no monkeypatch fixture needed outside of pytest."""
    orig_weight = suggest_mod.TAG_WEIGHT
    suggest_mod.TAG_WEIGHT = tag_weight
    try:
        yield
    finally:
        suggest_mod.TAG_WEIGHT = orig_weight


def grid_search(
    home_tracks: dict[str, list[dict]],
    tag_artists: dict[str, dict],
    sampled_pairs: list[tuple[str, dict]],
    tag_weights: list[float],
    top_k: int = DEFAULT_TOP_K,
) -> dict[float, dict]:
    """Run `run_eval` once per TAG_WEIGHT value, restoring the module's
    weight to whatever it was before the search once done."""
    results = {}
    for tag_weight in tag_weights:
        with weights(tag_weight):
            results[tag_weight] = run_eval(home_tracks, tag_artists, sampled_pairs, top_k)
    return results


# ---- CLI ---------------------------------------------------------------


def _print_result(label: str, result: dict) -> None:
    print(
        f"{label}: n={result['n']} top1={result['top1']:.3f} top3={result['top3']:.3f} | "
        f"artist-absent n={result['artist_absent_n']} "
        f"top1={result['artist_absent_top1']:.3f} top3={result['artist_absent_top3']:.3f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=DEFAULT_N, help="number of (home, track) pairs to sample")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="sampling seed, for repeatability")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--baseline", action="store_true", help="force TAG_WEIGHT=0 (artist-only)")
    parser.add_argument("--search", action="store_true", help="1-D grid search over TAG_WEIGHT")
    args = parser.parse_args()

    top_k = args.top_k
    if top_k > suggest_mod.TOP_N:
        print(f"note: --top-k {top_k} > suggest.TOP_N ({suggest_mod.TOP_N}); "
              f"suggest() never returns more than TOP_N results, clamping to {suggest_mod.TOP_N}")
        top_k = suggest_mod.TOP_N

    home_tracks = load_home_tracks()
    tag_artists = load_tag_artists()
    pairs = collect_pairs(home_tracks)
    sampled = sample_pairs(pairs, args.n, args.seed)

    print(f"homes={len(home_tracks)} pairs_available={len(pairs)} sampled={len(sampled)} seed={args.seed}")

    if args.baseline:
        with weights(0.0):
            _print_result("artist-only baseline (TAG_WEIGHT=0)", run_eval(home_tracks, tag_artists, sampled, top_k))
        return

    if args.search:
        with weights(0.0):
            baseline = run_eval(home_tracks, tag_artists, sampled, top_k)
        _print_result("artist-only baseline (TAG_WEIGHT=0)", baseline)

        results = grid_search(home_tracks, tag_artists, sampled, list(SEARCH_TAG_WEIGHTS), top_k)
        for tag_weight, result in results.items():
            _print_result(f"TAG_WEIGHT={tag_weight}", result)

        best_weight = max(results, key=lambda w: (results[w]["top3"], results[w]["top1"]))
        print(f"best: TAG_WEIGHT={best_weight} -> {results[best_weight]}")
        return

    _print_result(
        f"current weight (TAG_WEIGHT={suggest_mod.TAG_WEIGHT})",
        run_eval(home_tracks, tag_artists, sampled, top_k),
    )


if __name__ == "__main__":
    main()
