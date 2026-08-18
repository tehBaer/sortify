"""Hold-one-out evaluation harness for sortify/suggest.py.

Not part of the served app: reads cached data on disk and never imports
`sortify.spotify`, so it makes zero network calls of any kind. Zero writes
either — `data/` here is the live tree's data, read-only, the one sanctioned
use of that symlink (see task-3 brief).

The user's own home playlists are labelled training data: every track that
sits in a home is an example of a track the user decided belongs there. The
harness samples (home, track) pairs and asks "if we rebuild that home's
profile with this one track removed, does ranking still put it back?"

Hold-one-out is the whole validity of this file. If the held-out track were
left in the profile used to rank it, `already` and the artist-overlap count
would see it and every pair would score as a trivial, meaningless 100% — see
`test_removing_track_from_profile_changes_its_score` in tests/test_suggest.py
and the harness's own tests in tests/test_eval_suggest.py.

Usage:
    python scripts/eval_suggest.py [--n 500] [--seed 7] [--baseline] [--search]
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import random
import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sortify.suggest as suggest_mod  # noqa: E402
from sortify.suggest import build_profile, suggest  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_N = 500
DEFAULT_SEED = 7
DEFAULT_TOP_K = 3
SEARCH_TAG_WEIGHTS = (2.0, 3.0, 4.0, 6.0)
SEARCH_DILUTIONS = (0.3, 0.5, 0.7, 1.0)


# ---- loading (the only place that touches disk) -----------------------------


def load_home_tracks(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    """Read-only: config.json's `home_ids` + cache.json's cached track lists.

    Mirrors `app.py`'s `_resolve_homes` + `_cached_tracks`, minus the live
    Spotify calls that function makes when the cache is stale — this harness
    only ever sees what is already on disk. Home ids missing from the cache
    (never fetched) are silently skipped rather than treated as empty homes.
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
    track filed into two homes yields two pairs, one per home."""
    return [(hid, t) for hid, tracks in home_tracks.items() for t in tracks]


def sample_pairs(
    pairs: list[tuple[str, dict]], n: int, seed: int
) -> list[tuple[str, dict]]:
    """Seeded, repeatable sample of at most `n` pairs (order-stable input,
    order-shuffled output) — same seed always yields the same sample."""
    rng = random.Random(seed)
    if n >= len(pairs):
        sampled = list(pairs)
        rng.shuffle(sampled)
        return sampled
    return rng.sample(pairs, n)


def uri_home_index(home_tracks: dict[str, list[dict]]) -> dict[str, set[str]]:
    """track uri -> every home it currently sits in. Used to score
    multi-home tracks as correct if ANY of their homes lands in top-k."""
    idx: dict[str, set[str]] = {}
    for hid, tracks in home_tracks.items():
        for t in tracks:
            idx.setdefault(t["uri"], set()).add(hid)
    return idx


def build_all_profiles(
    home_tracks: dict[str, list[dict]], tag_artists: dict[str, dict]
) -> dict[str, dict]:
    return {hid: build_profile(tracks, tag_artists) for hid, tracks in home_tracks.items()}


def evaluate_pair(
    home_id: str,
    track: dict,
    home_tracks: dict[str, list[dict]],
    profiles: dict[str, dict],
    tag_artists: dict[str, dict],
    uri_homes: dict[str, set[str]],
    top_k: int = DEFAULT_TOP_K,
) -> tuple[bool, bool]:
    """Rank `track` with `home_id`'s profile rebuilt minus `track` itself —
    every other home's profile is passed through untouched. Returns
    (top1_hit, top3_hit): whether any of the track's true homes (which may
    be more than just `home_id`) lands in the top-1 / top-k ranking.

    Does not mutate `profiles` — a fresh dict is built per call so later
    pairs in the same run never see a corrupted home profile.
    """
    held_out_tracks = [t for t in home_tracks[home_id] if t["uri"] != track["uri"]]
    rebuilt = build_profile(held_out_tracks, tag_artists)
    profiles_for_pair = dict(profiles)
    profiles_for_pair[home_id] = rebuilt

    results = suggest(track, profiles_for_pair, tag_artists)
    ranked_ids = [r["playlist_id"] for r in results]
    true_homes = uri_homes.get(track["uri"], {home_id})

    top1_hit = bool(true_homes & set(ranked_ids[:1]))
    top3_hit = bool(true_homes & set(ranked_ids[:top_k]))
    return top1_hit, top3_hit


def run_eval(
    home_tracks: dict[str, list[dict]],
    tag_artists: dict[str, dict],
    sampled_pairs: list[tuple[str, dict]],
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    """Evaluate a fixed, pre-sampled set of pairs under whatever weights
    `sortify.suggest` currently holds. Takes the sample as an argument
    (rather than sampling itself) so a baseline run, a full grid search, and
    the committed-weights run all score the identical pairs."""
    profiles = build_all_profiles(home_tracks, tag_artists)
    uri_homes = uri_home_index(home_tracks)

    top1 = top3 = 0
    for home_id, track in sampled_pairs:
        hit1, hit3 = evaluate_pair(home_id, track, home_tracks, profiles, tag_artists, uri_homes, top_k)
        top1 += hit1
        top3 += hit3

    total = len(sampled_pairs)
    return {
        "n": total,
        "top1": top1 / total if total else 0.0,
        "top3": top3 / total if total else 0.0,
    }


@contextlib.contextmanager
def weights(tag_weight: float, dilution: float):
    """Temporarily override sortify.suggest's module-level weight constants.
    `suggest()` reads them at call time (not as bound defaults), so mutating
    the module and restoring it afterwards is sufficient — no monkeypatch
    fixture needed outside of pytest."""
    orig_weight = suggest_mod.TAG_WEIGHT
    orig_dilution = suggest_mod.ARTIST_TAG_DILUTION
    suggest_mod.TAG_WEIGHT = tag_weight
    suggest_mod.ARTIST_TAG_DILUTION = dilution
    try:
        yield
    finally:
        suggest_mod.TAG_WEIGHT = orig_weight
        suggest_mod.ARTIST_TAG_DILUTION = orig_dilution


def grid_search(
    home_tracks: dict[str, list[dict]],
    tag_artists: dict[str, dict],
    sampled_pairs: list[tuple[str, dict]],
    grid: Iterable[tuple[float, float]],
    top_k: int = DEFAULT_TOP_K,
) -> dict[tuple[float, float], dict]:
    """Run `run_eval` once per (tag_weight, dilution) cell, restoring the
    module's weights to whatever they were before the search once done."""
    results = {}
    for tag_weight, dilution in grid:
        with weights(tag_weight, dilution):
            results[(tag_weight, dilution)] = run_eval(home_tracks, tag_artists, sampled_pairs, top_k)
    return results


# ---- CLI ---------------------------------------------------------------


def _print_result(label: str, result: dict) -> None:
    print(f"{label}: n={result['n']} top1={result['top1']:.3f} top3={result['top3']:.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=DEFAULT_N, help="number of (home, track) pairs to sample")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="sampling seed, for repeatability")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--baseline", action="store_true", help="force TAG_WEIGHT=0 (artist-only)")
    parser.add_argument("--search", action="store_true", help="coarse grid search over TAG_WEIGHT x ARTIST_TAG_DILUTION")
    args = parser.parse_args()

    home_tracks = load_home_tracks()
    tag_artists = load_tag_artists()
    pairs = collect_pairs(home_tracks)
    sampled = sample_pairs(pairs, args.n, args.seed)

    print(f"homes={len(home_tracks)} pairs_available={len(pairs)} sampled={len(sampled)} seed={args.seed}")

    if args.baseline:
        with weights(0.0, suggest_mod.ARTIST_TAG_DILUTION):
            _print_result("artist-only baseline (TAG_WEIGHT=0)", run_eval(home_tracks, tag_artists, sampled, args.top_k))
        return

    if args.search:
        with weights(0.0, suggest_mod.ARTIST_TAG_DILUTION):
            baseline = run_eval(home_tracks, tag_artists, sampled, args.top_k)
        _print_result("artist-only baseline (TAG_WEIGHT=0)", baseline)

        grid = list(itertools.product(SEARCH_TAG_WEIGHTS, SEARCH_DILUTIONS))
        results = grid_search(home_tracks, tag_artists, sampled, grid, args.top_k)
        for (tag_weight, dilution), result in results.items():
            _print_result(f"TAG_WEIGHT={tag_weight} ARTIST_TAG_DILUTION={dilution}", result)

        best_cell = max(results, key=lambda cell: (results[cell]["top3"], results[cell]["top1"]))
        print(f"best: TAG_WEIGHT={best_cell[0]} ARTIST_TAG_DILUTION={best_cell[1]} -> {results[best_cell]}")
        return

    _print_result(
        f"current weights (TAG_WEIGHT={suggest_mod.TAG_WEIGHT} ARTIST_TAG_DILUTION={suggest_mod.ARTIST_TAG_DILUTION})",
        run_eval(home_tracks, tag_artists, sampled, args.top_k),
    )


if __name__ == "__main__":
    main()
