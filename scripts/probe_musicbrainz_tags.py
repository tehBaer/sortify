"""One-shot, read-only MusicBrainz probe (2026-08-21).

Question: does MusicBrainz add tag/genre vocabulary WHERE LAST.FM IS WEAK?
Last.fm artist tags already cover ~99% of home artists, so raw MB coverage
is the wrong metric — this samples the weak spots (Last.fm miss/absent and
thin-tagged artists) plus a control group of well-tagged artists, and
reports MB's hit rate and vocabulary on each group separately.

Writes nothing. MusicBrainz only: keyless, 1 request/second hard etiquette
limit, mandatory User-Agent. Two requests per artist (search, then lookup
with inc=genres+tags). Zero Spotify calls.

Usage:
    .venv/bin/python scripts/probe_musicbrainz_tags.py [--weak 30] [--control 15] [--seed 7]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MB = "https://musicbrainz.org/ws/2"
UA = "sortify/0.1 (personal playlist sorter; klooomp@gmail.com)"
MIN_INTERVAL = 1.1  # MB etiquette: 1 req/s, hard


BACKOFF_503 = 10.0
CONSECUTIVE_FAILURE_LIMIT = 5


def mb_get(client: httpx.Client, path: str, params: dict) -> dict:
    """One paced request. The sleep runs on EVERY outcome (finally) — the
    first run of this probe skipped it on errors, which turned two timeouts
    into an unpaced burst and earned a 503 spiral for everything after.
    A 503 additionally backs off BACKOFF_503 before the caller continues."""
    try:
        resp = client.get(f"{MB}{path}", params={**params, "fmt": "json"})
        if resp.status_code == 503:
            time.sleep(BACKOFF_503)
        resp.raise_for_status()
        return resp.json()
    finally:
        time.sleep(MIN_INTERVAL)


def probe_artist(client: httpx.Client, name: str) -> dict:
    hits = mb_get(client, "/artist", {"query": f'artist:"{name}"', "limit": 1}).get("artists") or []
    if not hits or hits[0].get("score", 0) < 90:
        return {"found": False}
    mbid = hits[0]["id"]
    detail = mb_get(client, f"/artist/{mbid}", {"inc": "genres+tags"})
    genres = sorted(g["name"] for g in detail.get("genres") or [] if g.get("count", 0) > 0)
    tags = sorted(t["name"] for t in detail.get("tags") or [] if t.get("count", 0) > 0)
    return {"found": True, "genres": genres, "tags": tags}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weak", type=int, default=30)
    parser.add_argument("--control", type=int, default=15)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    tags = json.load(open(DATA_DIR / "tags.json"))["artists"]
    cache = json.load(open(DATA_DIR / "cache.json"))
    cfg = json.load(open(DATA_DIR / "config.json"))
    homes = set(cfg.get("home_ids") or [])
    home_artists: dict[str, str] = {}
    for pid, p in cache["playlists"].items():
        if pid not in homes:
            continue
        for t in p["tracks"]:
            for a in t.get("artists") or []:
                if a.get("id") and a.get("name"):
                    home_artists.setdefault(a["id"], a["name"])

    def n_raw_tags(aid: str) -> int:
        return len((tags.get(aid) or {}).get("tags") or [])

    weak = sorted(
        aid for aid in home_artists
        if aid not in tags or tags[aid].get("miss") or n_raw_tags(aid) <= 2
    )
    strong = sorted(
        aid for aid in home_artists
        if aid in tags and not tags[aid].get("miss") and n_raw_tags(aid) >= 8
    )
    rng = random.Random(args.seed)
    weak_sample = rng.sample(weak, min(args.weak, len(weak)))
    control_sample = rng.sample(strong, min(args.control, len(strong)))
    total = len(weak_sample) + len(control_sample)
    print(f"weak pool={len(weak)} control pool={len(strong)} | sampling {len(weak_sample)}+{len(control_sample)}")
    print(f"cost ceiling: {2 * total} MusicBrainz requests at 1/s (~{2 * total * MIN_INTERVAL:.0f}s), read-only")

    with httpx.Client(headers={"User-Agent": UA}, timeout=10.0) as client:
        for label, sample in (("WEAK", weak_sample), ("CONTROL", control_sample)):
            found = tagged = attempted = 0
            consecutive_failures = 0
            vocab: dict[str, int] = {}
            examples = []
            for aid in sample:
                name = home_artists[aid]
                attempted += 1
                try:
                    r = probe_artist(client, name)
                except Exception as e:
                    print(f"  error for {name!r}: {e}")
                    consecutive_failures += 1
                    if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                        print(f"  aborting {label}: {consecutive_failures} consecutive failures")
                        break
                    continue
                consecutive_failures = 0
                if not r["found"]:
                    continue
                found += 1
                words = r["genres"] or r["tags"]
                if words:
                    tagged += 1
                    for w in words:
                        vocab[w] = vocab.get(w, 0) + 1
                    if len(examples) < 5:
                        examples.append(f"{name}: {', '.join(words[:5])}")
            print(f"{label}: n={len(sample)} attempted={attempted} found={found} with_genres_or_tags={tagged}")
            top = sorted(vocab.items(), key=lambda kv: -kv[1])[:12]
            print("  vocab:", ", ".join(f"{g} ({n})" for g, n in top) or "none")
            for ex in examples:
                print("  e.g.", ex)


if __name__ == "__main__":
    main()
