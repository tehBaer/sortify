"""One-shot, read-only Deezer probe (option-2 follow-up, 2026-08-21).

Answers two questions the BPM probe left open, then gets deleted or ignored:
  A. Of the recorded BPM misses, how many tracks does Deezer's search not
     know at all vs know-but-carry-no-BPM? (The latter would mean album
     GENRES might still have coverage worth building on.)
  B. Among tracks Deezer does know (BPM hits + the re-found misses), how
     often does the album actually carry genres?

Writes NOTHING — deezer.json's schema has no genre field and this probe
does not add one. Deezer only (keyless, 50 req/5 s per IP); zero Spotify
calls. Cost ceiling printed before spending: one search per sampled miss,
one /track per sampled hit, one /album per distinct album (deduped).

Usage:
    .venv/bin/python scripts/probe_deezer_genres.py [--n 25] [--seed 7]
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sortify.deezer import Deezer  # noqa: E402
from sortify.store import Store  # noqa: E402

# Reuse the BPM probe's loaders/keying so both probes see the same targets.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_deezer_bpm import DATA_DIR, MIN_INTERVAL, collect_target_tracks, load_home_tracks  # noqa: E402


def album_genres(client: Deezer, album_id: int, cache: dict) -> list[str]:
    if album_id not in cache:
        data = client._get(f"/album/{album_id}")
        cache[album_id] = [g.get("name") for g in (data.get("genres") or {}).get("data") or []]
    return cache[album_id]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=25, help="sample size per part (misses / hits)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    targets = collect_target_tracks(load_home_tracks())
    known = Store(DATA_DIR).deezer_map()
    misses = sorted(k for k, v in known.items() if v.get("miss") and k in targets)
    hits = sorted((k, v["deezer_id"]) for k, v in known.items()
                  if not v.get("miss") and v.get("deezer_id") and k in targets)
    rng = random.Random(args.seed)
    miss_sample = rng.sample(misses, min(args.n, len(misses)))
    hit_sample = rng.sample(hits, min(args.n, len(hits)))
    print(f"recorded: {len(misses)} misses, {len(hits)} hits | sampling {len(miss_sample)} + {len(hit_sample)}")
    print(f"cost ceiling: {2 * len(miss_sample) + 2 * len(hit_sample)} Deezer requests, read-only")

    client = Deezer()
    genre_cache: dict[int, list[str]] = {}
    stats = {"miss_unknown": 0, "miss_found": 0, "with_genres": 0, "known_total": 0}
    genre_names: dict[str, int] = {}

    def note_album(album_id) -> None:
        stats["known_total"] += 1
        genres = album_genres(client, int(album_id), genre_cache) if album_id else []
        if genres:
            stats["with_genres"] += 1
            for g in genres:
                genre_names[g] = genre_names.get(g, 0) + 1
        time.sleep(MIN_INTERVAL)

    for key in miss_sample:
        entry = targets[key]
        found = client._get(
            "/search", {"q": f'artist:"{entry["artist"]}" track:"{entry["title"]}"', "limit": 1}
        ).get("data") or []
        if not found or not found[0].get("id"):
            stats["miss_unknown"] += 1
            time.sleep(MIN_INTERVAL)
            continue
        stats["miss_found"] += 1
        note_album((found[0].get("album") or {}).get("id"))

    for _key, deezer_id in hit_sample:
        detail = client._get(f"/track/{deezer_id}")
        note_album((detail.get("album") or {}).get("id"))

    print(f"miss anatomy: {stats['miss_unknown']} truly unknown to Deezer, "
          f"{stats['miss_found']} known but carrying no BPM "
          f"(of {len(miss_sample)} sampled misses)")
    if stats["known_total"]:
        rate = stats["with_genres"] / stats["known_total"]
        print(f"album genres among Deezer-known tracks: {stats['with_genres']}/{stats['known_total']} = {rate:.1%}")
    top = sorted(genre_names.items(), key=lambda kv: -kv[1])[:10]
    print("top genres seen:", ", ".join(f"{g} ({n})" for g, n in top) or "none")


if __name__ == "__main__":
    main()
