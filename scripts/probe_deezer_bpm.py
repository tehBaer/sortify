"""Deezer BPM coverage probe — the option-2 gate, not the backfill.

Fetches BPM for a seeded sample of home tracks and reports the hit rate, so
the "is Deezer BPM worth building a signal on?" question is answered for
~200 keyless Deezer requests instead of ~4,500. The decision rule agreed
with the user (2026-08-21): coverage under ~40% means BPM scoring is not
worth building.

Deezer is not Spotify: none of the api.spotify.com budget ledger applies
(see sortify/deezer.py). Its own limit is 50 requests per 5 s per IP; one
track costs two requests (search + track detail), and MIN_INTERVAL paces
attempts far below that ceiling anyway.

Results are merged write-once into data/deezer.json (existing entries win),
so whatever the probe learns is already banked for the real backfill —
hits AND misses are permanent answers per sortify/deezer.py's contract,
while transport failures and Deezer error payloads are never recorded
(they are retryable; recording them would make a temporary outage
permanent). The same consecutive-failure circuit breaker as the Last.fm
backfills stops an outage from walking the whole sample.

Sampling, key conventions (fetch under the FIRST credited artist's
`track_key`, recognise "known" under ANY credited key) and the
collect/known/merge shapes all mirror scripts/backfill_similar.py —
duplicated rather than imported for the same reason that file duplicates
its own loaders: scripts/ is not a package.

Usage:
    .venv/bin/python scripts/probe_deezer_bpm.py [--n 100] [--seed 7]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sortify.deezer import Deezer, DeezerError  # noqa: E402
from sortify.store import Store  # noqa: E402
from sortify.tags import track_key  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_N = 100
DEFAULT_SEED = 7
MIN_INTERVAL = 0.4  # seconds between tracks: 2 requests / 0.4 s = 5 req/s < 10/s ceiling
PROGRESS_EVERY = 10
SAVE_EVERY = 25
CONSECUTIVE_FAILURE_LIMIT = 10


# ---- loading (read-only) ---------------------------------------------------


def load_home_tracks(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    with open(data_dir / "config.json") as f:
        cfg = json.load(f)
    with open(data_dir / "cache.json") as f:
        cache = json.load(f)
    home_ids = cfg.get("home_ids") or []
    playlists = cache.get("playlists", {})
    return {hid: playlists[hid]["tracks"] for hid in home_ids if hid in playlists}


# ---- pure logic (unit-tested against hand-built fixtures) ------------------


def collect_target_tracks(track_lists: dict[str, list[dict]]) -> dict[str, dict]:
    """Distinct tracks keyed by first-credited-artist `track_key`, each
    carrying `keys` under every credited artist — same convention and
    first-wins rule as `backfill_similar.collect_target_tracks`."""
    out: dict[str, dict] = {}
    for tracks in track_lists.values():
        for tr in tracks:
            title = tr.get("name")
            artists = tr.get("artists") or []
            if not title or not artists:
                continue
            first_name = artists[0].get("name")
            if not first_name:
                continue
            fetch_key = track_key(first_name, title)
            if fetch_key in out:
                continue
            keys = [fetch_key]
            for a in artists[1:]:
                aname = a.get("name")
                if aname:
                    k = track_key(aname, title)
                    if k not in keys:
                        keys.append(k)
            out[fetch_key] = {"artist": first_name, "title": title, "keys": keys}
    return out


def sample_targets(
    targets: dict[str, dict], known: dict[str, dict], n: int, seed: int
) -> list[str]:
    """Seeded, repeatable sample of at most `n` fetch keys not already
    answered (hit OR miss, under any credited key) in deezer.json."""
    unknown = [
        k for k, entry in sorted(targets.items())
        if not any(isinstance(known.get(key), dict) for key in entry["keys"])
    ]
    rng = random.Random(seed)
    if n >= len(unknown):
        rng.shuffle(unknown)
        return unknown
    return rng.sample(unknown, n)


def run_probe(
    client: Deezer,
    sample: list[str],
    targets: dict[str, dict],
    record,
    sleep=time.sleep,
    progress=lambda msg: None,
) -> dict:
    """Fetch each sampled track; `record(key, value)` only hits and misses
    (both permanent answers), never errors (retryable). Aborts after
    CONSECUTIVE_FAILURE_LIMIT raises in a row — an outage or a quota trip
    fails every attempt, not just most of them."""
    stats = {"attempted": 0, "hits": 0, "misses": 0, "errors": 0}
    consecutive_failures = 0
    for i, key in enumerate(sample):
        entry = targets[key]
        stats["attempted"] += 1
        try:
            result = client.fetch_track(entry["artist"], entry["title"])
        except Exception as e:  # DeezerError and transport errors alike
            stats["errors"] += 1
            consecutive_failures += 1
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                progress(f"aborting: {consecutive_failures} consecutive failures ({e})")
                break
        else:
            consecutive_failures = 0
            record(key, result)
            stats["misses" if result.get("miss") else "hits"] += 1
        if (i + 1) % PROGRESS_EVERY == 0:
            progress(f"{i + 1}/{len(sample)} attempted "
                     f"({stats['hits']} hits, {stats['misses']} misses, {stats['errors']} errors)")
        sleep(MIN_INTERVAL)
    return stats


# ---- persistence -----------------------------------------------------------


class _Recorder:
    """Buffers new records and merges them into deezer.json every
    SAVE_EVERY entries (re-read fresh, existing entries win — the same
    write-once merge discipline as the Last.fm backfills)."""

    def __init__(self, store: Store):
        self.store = store
        self.pending: dict[str, dict] = {}

    def record(self, key: str, value: dict) -> None:
        self.pending[key] = value
        if len(self.pending) >= SAVE_EVERY:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        envelope = self.store.deezer_tracks()
        tracks = envelope.setdefault("tracks", {})
        for k, v in self.pending.items():
            tracks.setdefault(k, v)
        self.store.save_deezer_tracks(envelope)
        self.pending.clear()


# ---- CLI -------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    store = Store(DATA_DIR)
    targets = collect_target_tracks(load_home_tracks())
    known = store.deezer_map()
    sample = sample_targets(targets, known, args.n, args.seed)
    known_hits = sum(1 for v in known.values() if isinstance(v, dict) and not v.get("miss"))
    print(f"targets={len(targets)} already_known={len(known)} (hits={known_hits}) "
          f"sampling={len(sample)} seed={args.seed}")
    print(f"cost: at most {2 * len(sample)} Deezer requests (keyless, not Spotify), "
          f"paced at 1 track / {MIN_INTERVAL}s")

    recorder = _Recorder(store)
    try:
        stats = run_probe(Deezer(), sample, targets, recorder.record, progress=print)
    finally:
        recorder.flush()

    answered = stats["hits"] + stats["misses"]
    rate = stats["hits"] / answered if answered else 0.0
    print(f"probe: attempted={stats['attempted']} hits={stats['hits']} "
          f"misses={stats['misses']} errors={stats['errors']}")
    print(f"coverage among answered: {rate:.1%} "
          f"({'worth a backfill' if rate >= 0.4 else 'below the 40% bar — not worth building'})")


if __name__ == "__main__":
    main()
