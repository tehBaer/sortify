"""One-shot Last.fm track-level backfill (getSimilar + track top tags) for
home-playlist tracks.

Sibling to `backfill_tags.py`, same shape and same reasoning throughout —
see that module's docstring for the network/pacing/write-once rationale this
one shares. The one deliberate difference: `lastfm_tracks.json` is
REBUILDABLE (unlike `tags.json`, it is never the sole record of anything —
see `sortify/store.py`'s `LASTFM_TRACKS_DEFAULT` docstring: "safe to delete
at any time"), so this script alone gets a `--refetch-misses` flag that
treats a cached `miss: true` record as fetchable again. `backfill_tags.py`
deliberately has no equivalent.

Collects target tracks from cached HOME playlists (mirrors
`eval_suggest.load_home_tracks`'s config.json + cache.json read; duplicated
here rather than imported for the same reason `backfill_tags.py` duplicates
it — neither `eval_suggest.py` nor `backfill_tags.py` is a package, and
importing either would run its own argparse setup as a side effect).

Each target track is fetched under the `tags.track_key` of its FIRST
credited artist (matching `sortify.suggest._track_record`'s own convention
for where a collab's record lives), but "already known" is checked against
ALL of the track's credited-artist keys — a collab already fetched under a
co-artist's credit must not be re-fetched just because this run reaches it
from a different artist first.

Two Last.fm calls per track (`tags.fetch_track` makes one `track.getSimilar`
and one `track.getTopTags` request, paced by the same `tags.MIN_INTERVAL`
each) — a run that ATTEMPTS N tracks costs up to 2N Last.fm requests, half
that only when a track lands as a miss on a call that itself 404s a whole
call short (still counted as one attempt).

`--limit N` bounds how many tracks this run ATTEMPTS (fetch_track calls it
makes), not how many it successfully fetches — a run that hits N tracks that
all raise still stops at N attempts with 0 fetched.

Usage:
    .venv/bin/python scripts/backfill_similar.py [--limit N] [--all-cached] [--refetch-misses]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sortify.store import Store  # noqa: E402
from sortify.tags import LastFm, fetch_track, load_key, track_key  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROGRESS_EVERY = 25
SAVE_EVERY = 50


class BackfillAbort(Exception):
    """Raised when a save must be refused rather than risk the permanent
    tags.json cache. Named to match `backfill_tags.BackfillAbort` exactly —
    same meaning here: fetched-but-unsaved tracks from this invocation of
    `merge_save` are discarded, but that work is re-runnable (another
    backfill invocation re-fetches them), so refusing beats guessing. Unlike
    `backfill_tags.py`, the file this guards is rebuildable — but a spurious
    overwrite mid-run is still wasted work worth refusing rather than
    silently eating."""


# ---- loading (read-only) -----------------------------------------------


def load_home_tracks(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    """Mirrors `backfill_tags.load_home_tracks` exactly — see that
    function's docstring for why a missing home_id is silently skipped
    rather than treated as empty."""
    with open(data_dir / "config.json") as f:
        cfg = json.load(f)
    with open(data_dir / "cache.json") as f:
        cache = json.load(f)
    home_ids = cfg.get("home_ids") or []
    playlists = cache.get("playlists", {})
    return {hid: playlists[hid]["tracks"] for hid in home_ids if hid in playlists}


def load_all_cached_tracks(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    """Every cached playlist, not just homes — the `--all-cached` widening."""
    with open(data_dir / "cache.json") as f:
        cache = json.load(f)
    playlists = cache.get("playlists", {})
    return {pid: entry["tracks"] for pid, entry in playlists.items()}


# ---- pure logic (unit-tested against hand-built fixtures) --------------


def collect_target_tracks(track_lists: dict[str, list[dict]]) -> dict[str, dict]:
    """Every distinct track seen across the given playlists' tracks, keyed
    by `track_key` of its FIRST credited artist (the key this script fetches
    under) — matching `sortify.suggest._track_record`'s own convention for
    where a collab's fetched record is expected to live.

    A track with no title or no credited artists at all is skipped: there is
    nothing sensible to fetch (`LastFm.track_similar`/`track_top_tags`
    reject a blank artist or title outright). Each entry also carries
    `keys`: `track_key` under EVERY credited artist, not just the first —
    `tracks_to_fetch` uses the full list to recognise a collab already
    fetched under a co-artist's credit. Later tracks that land on the same
    fetch key (same first-artist name + same title, e.g. re-added to a
    second home playlist) do not overwrite the first occurrence — same
    first-wins rule `backfill_tags.collect_target_artists` follows.
    """
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


def tracks_to_fetch(
    target_tracks: dict[str, dict], known: dict[str, dict], refetch_misses: bool = False
) -> dict[str, dict]:
    """Target tracks minus anything already recorded in `lastfm_tracks.json`
    under ANY of their credited-artist keys.

    A hit (`miss: False`) found under any key always counts as known,
    regardless of `refetch_misses` — there is real data there already, and
    nothing about `--refetch-misses` is about discarding a hit. A miss found
    under every checked key counts as known UNLESS `refetch_misses` is True,
    the one behaviour `backfill_tags.artists_to_fetch` has no equivalent of,
    since `lastfm_tracks.json` (unlike `tags.json`) is rebuildable.
    """
    out: dict[str, dict] = {}
    for fetch_key, entry in target_tracks.items():
        saw_miss = False
        is_hit = False
        for k in entry["keys"]:
            rec = known.get(k)
            if not isinstance(rec, dict):
                continue
            if not rec.get("miss"):
                is_hit = True
                break
            saw_miss = True
        if is_hit:
            continue
        if saw_miss and not refetch_misses:
            continue
        out[fetch_key] = entry
    return out


# ---- persistence ---------------------------------------------------------


def merge_save(store: Store, new_entries: dict) -> dict:
    """Re-read lastfm_tracks.json fresh, merge with existing-entries-win, and
    save. Mirrors `backfill_tags.merge_save` exactly — see that function's
    docstring for the full reasoning on the re-read-immediately-before-write
    pattern, the cross-process race it does (and does not) protect against,
    and why a shrink of ANY size between two reads is treated as a malformed
    or truncated re-read rather than a real one.

    The one structural difference: `Store.save_lastfm_tracks` writes the
    WHOLE envelope (unlike `save_tag_artists`, which wraps the inner map for
    the caller), so this wraps `merged` in `{"version": 1, "tracks": ...}`
    itself before saving.
    """
    try:
        baseline = len(store.lastfm_track_map())
        current = store.lastfm_track_map()
    except json.JSONDecodeError as exc:
        raise BackfillAbort(
            f"data/lastfm_tracks.json is not valid JSON ({exc}); refusing to save. "
            f"{len(new_entries)} fetched-but-unsaved track(s) from this batch are "
            "discarded (the file itself was never corrupted by this script — Store "
            "writes atomically — but something else made it unreadable). Artists "
            "already flushed by an earlier incremental save in this run are unaffected; "
            "fix data/lastfm_tracks.json and re-run the backfill to retry the discarded "
            "batch (this file is rebuildable, so re-running is always safe)."
        ) from exc
    if len(current) < baseline:
        raise BackfillAbort(
            f"data/lastfm_tracks.json shrank from {baseline} to {len(current)} tracks "
            "between two reads moments apart — refusing to save. A genuine concurrent "
            "writer (the /api/now force-path piggyback) only ever ADDS entries, so any "
            f"shrink is a malformed or truncated re-read, not a real one. "
            f"{len(new_entries)} fetched-but-unsaved track(s) from this batch are "
            "discarded, but that work is re-runnable — inspect data/lastfm_tracks.json "
            "and re-run the backfill."
        )
    merged = {**new_entries, **current}
    store.save_lastfm_tracks({"version": 1, "tracks": merged})
    return merged


# ---- CLI -----------------------------------------------------------------


def run_backfill(
    target_tracks: dict[str, dict],
    store: Store,
    fm: LastFm,
    limit: int | None = None,
    now: float | None = None,
    refetch_misses: bool = False,
    progress_every: int = PROGRESS_EVERY,
    save_every: int = SAVE_EVERY,
    print_fn=print,
) -> dict:
    """Fetch (getSimilar + track top tags) for `target_tracks`. `limit`
    bounds how many tracks this call ATTEMPTS (`fetch_track` calls made),
    not how many succeed. Saves incrementally every `save_every` fetched
    tracks and once more at the end. Returns the summary dict also printed
    at the end.

    Failure rule: `tags.fetch_track` already folds a genuine "not found" on
    EITHER or BOTH Last.fm calls into the record itself (`miss: True` only
    when both come back not-found — see its docstring). Anything else
    `fetch_track` raises instead means this track is left absent from the
    cache so a later run retries it, and this run counts it as skipped.
    `fetch_track`'s own docstring is explicit that this includes non-
    `LastFmError` transport failures too (unlike `top_tags`/`enrich`,
    neither `track_similar` nor `track_top_tags` wraps a raw transport
    exception into `LastFmError` before it reaches here), so the catch below
    is deliberately broad rather than `LastFmError`-only. Unlike
    `backfill_tags.enrich`, `fetch_track` handles exactly one track per
    call, so there is no partial batch to salvage: a raise here means
    literally nothing was learned about this track, not "some of a larger
    batch."

    Lets `BackfillAbort` (from `merge_save`'s clobber guard) propagate
    uncaught, exactly like `backfill_tags.run_backfill` — see that
    function's docstring. `main()` turns it into a clean exit.
    """
    now = now if now is not None else time.time()
    known = store.lastfm_track_map()
    to_fetch = tracks_to_fetch(target_tracks, known, refetch_misses=refetch_misses)
    already_known = len(target_tracks) - len(to_fetch)

    if limit is not None:
        bounded_keys = list(to_fetch)[:limit]
        to_fetch = {k: to_fetch[k] for k in bounded_keys}

    fetched_total = 0
    misses = 0
    skipped = 0
    pending: dict[str, dict] = {}  # tracks not yet folded into a save

    def _save(entries: dict) -> None:
        if not entries:
            return
        merge_save(store, entries)

    items = list(to_fetch.items())
    processed = 0
    for fetch_key, entry in items:
        try:
            record = fetch_track(fm, entry["artist"], entry["title"], now)
        except Exception:
            # Left absent, not recorded — see the docstring above: unlike
            # enrich()'s batch, there is no partial progress to salvage for
            # a single track, and `fetch_track` does not wrap a bare
            # transport exception into `LastFmError` the way `enrich` does,
            # so this catches both deliberately.
            skipped += 1
            processed += 1
            if processed % progress_every == 0:
                print_fn(f"progress: {processed}/{len(items)} fetched={fetched_total} "
                          f"misses={misses} skipped={skipped}")
            continue

        pending[fetch_key] = record
        fetched_total += 1
        if record.get("miss"):
            misses += 1
        processed += 1

        if len(pending) >= save_every:
            _save(pending)
            pending = {}

        if processed % progress_every == 0:
            print_fn(f"progress: {processed}/{len(items)} fetched={fetched_total} "
                      f"misses={misses} skipped={skipped}")

    _save(pending)

    return {
        "target": len(target_tracks),
        "already_known": already_known,
        "attempted": len(items),
        "fetched": fetched_total,
        "misses": misses,
        "skipped": skipped,
    }


def _print_summary(summary: dict, print_fn=print) -> None:
    print_fn(
        f"done: target={summary['target']} already_known={summary['already_known']} "
        f"attempted={summary['attempted']} fetched={summary['fetched']} "
        f"misses={summary['misses']} skipped={summary['skipped']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                         help="max number of tracks to ATTEMPT this run (not fetched-"
                              "successfully; default: unlimited)")
    parser.add_argument("--all-cached", action="store_true",
                         help="widen target tracks to every cached playlist, not just homes")
    parser.add_argument("--refetch-misses", action="store_true",
                         help="treat a cached miss:true record as fetchable again "
                              "(lastfm_tracks.json is rebuildable, unlike tags.json)")
    args = parser.parse_args()

    store = Store(DATA_DIR)
    key = load_key()
    if not key:
        print(f"no Last.fm API key found (expected {load_key.__module__}.KEY_PATH); aborting")
        raise SystemExit(1)
    fm = LastFm(key)

    track_lists = load_all_cached_tracks() if args.all_cached else load_home_tracks()
    target_tracks = collect_target_tracks(track_lists)
    print(f"playlists={len(track_lists)} target_tracks={len(target_tracks)} "
          f"limit={args.limit if args.limit is not None else 'unbounded'} "
          f"scope={'all-cached' if args.all_cached else 'home'} "
          f"refetch_misses={args.refetch_misses}")

    try:
        summary = run_backfill(
            target_tracks, store, fm, limit=args.limit, refetch_misses=args.refetch_misses
        )
    except BackfillAbort as exc:
        print(f"backfill aborted: {exc}")
        raise SystemExit(1) from exc
    _print_summary(summary)


if __name__ == "__main__":
    main()
