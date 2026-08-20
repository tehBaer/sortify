"""One-shot Last.fm artist-similar backfill for home-playlist artists.

Not part of the served app, but unlike `eval_suggest.py` this one DOES make
network calls — to Last.fm only, never Spotify (see `sortify/tags.py`'s
module docstring: it has its own client and its own limiter, so this can
never touch the Spotify budget). Spec §Fetching sanctions exactly this: "a
bounded backfill command" run on explicit user action, paced at the same
`MIN_INTERVAL` the on-demand fetch uses, with no background job behind it.

Skeleton copied from `scripts/backfill_tags.py` — same target collection
(cached HOME playlists' artists, `--all-cached` widening), same `--limit`,
same progress/incremental-save/shrink-guard discipline — with the tags-
specific pieces swapped for `lastfm_artists.json` and `LastFm.artist_similar`:

    similar = fm.artist_similar(name)          # LastFmError propagates -> key stays absent
    record = {
        "name": name,
        "similar": similar or [],
        "fetched_at": now,
        "miss": similar is None,               # code 6 only — the ONLY path to a miss
    }

Unlike `enrich()` (which `backfill_tags.py` calls), `LastFm.artist_similar`
does NOT wrap a non-Last.fm transport failure into `LastFmError` — its own
docstring mirrors `track_similar`'s, and neither wraps. So the per-artist
catch below is `except Exception`, not `except LastFmError`: any raised
error, Last.fm-flavoured or a bare transport failure, leaves the key absent
and retryable on a later run — exactly like a non-code-6 `LastFmError` does.
Only a genuine Last.fm code-6 (`artist_similar` returning `None`) is ever a
recorded miss.

Collects target artists from cached HOME playlists (mirrors
`eval_suggest.load_home_tracks`'s config.json + cache.json read, since that
script is not a package and importing it would run its own argparse setup
as a side effect) and skips anything already in lastfm_artists.json — hit or
miss, write-once, same rule the on-demand piggyback follows — unless
`--refetch-misses` is given, which narrows that skip to hits only.

`--limit N` bounds how many artists this run ATTEMPTS (fetch calls it makes),
not how many it successfully fetches — a run that hits N skips still stops
at N attempts with 0 fetched.

Usage:
    .venv/bin/python scripts/backfill_artist_similar.py [--limit N] [--all-cached] [--refetch-misses]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sortify.store import Store  # noqa: E402
from sortify.tags import LastFm, load_key  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROGRESS_EVERY = 25
SAVE_EVERY = 50


class BackfillAbort(Exception):
    """Raised when a save must be refused rather than risk the permanent
    lastfm_artists.json cache. Always means: fetched-but-unsaved artists from
    this invocation of `merge_save` are discarded, but that work is
    re-runnable (another backfill invocation re-fetches them) — the file on
    disk is not, so refusing beats guessing."""


# ---- loading (read-only) -----------------------------------------------


def load_home_tracks(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    """Mirrors `eval_suggest.load_home_tracks`: config.json's `home_ids` +
    cache.json's cached track lists, read-only. See that module's docstring
    for why a missing home_id (never fetched) is silently skipped rather
    than treated as empty, and why this does not reproduce app.py's live
    fallback to every editable playlist when home_ids is unset."""
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


def collect_target_artists(track_lists: dict[str, list[dict]]) -> dict[str, str]:
    """Every distinct (artist id, artist name) seen across the given
    playlists' tracks, keyed by id. Later occurrences of the same id do not
    overwrite earlier ones — first name seen wins, matching
    `backfill_tags.collect_target_artists`."""
    out: dict[str, str] = {}
    for tracks in track_lists.values():
        for track in tracks:
            for artist in track.get("artists", []):
                aid = artist.get("id")
                name = artist.get("name")
                if aid and aid not in out:
                    out[aid] = name
    return out


def artists_to_fetch(target_artists: dict[str, str], known: dict[str, dict],
                      refetch_misses: bool = False) -> dict[str, str]:
    """Target artists minus anything already in lastfm_artists.json — hit or
    miss, write-once by default, same rule the on-demand piggyback follows.
    `--refetch-misses` narrows the skip to hits only, so a previously
    recorded `miss: true` (a genuine Last.fm code-6 "no such artist" — a
    transport failure or other non-code-6 error never gets recorded at all,
    so it is already retryable without this flag) is attempted again. Kept
    as its own function so the "already known" count in the summary is exact
    rather than inferred from a diff of before/after run counts."""
    out: dict[str, str] = {}
    for aid, name in target_artists.items():
        entry = known.get(aid)
        if entry is None:
            out[aid] = name
        elif refetch_misses and entry.get("miss"):
            out[aid] = name
    return out


# ---- persistence ---------------------------------------------------------


def merge_save(store: Store, new_entries: dict) -> dict:
    """Re-read lastfm_artists.json fresh, merge with existing-entries-win,
    and save.

    This script runs in its OWN process, so `app.py`'s in-process
    `_lastfm_artists_save_lock` (guarding `_merge_save_lastfm_artists`) gives
    it no protection at all — a save here and a save from the live server
    can still interleave. The re-read-immediately-before-write pattern
    mirrored here is the mitigation, not a full fix: mirrors
    `backfill_tags.merge_save`'s reasoning exactly, substituted for
    lastfm_artists.json (rebuildable, unlike tags.json, but still worth
    refusing a clobber rather than guessing).

    Clobber guard: `store.lastfm_artist_map()` degrades a malformed-but-
    valid-JSON envelope to `{}` rather than raising (guard-on-read, so other
    callers fail toward "no artists" instead of crashing) — but that same
    behaviour would make this function treat a real cache as correctly empty
    (or truncated) and overwrite it with just this run's batch. `baseline` is
    captured before the fresh re-read used for the actual merge; if that
    later read comes back with FEWER artists than the baseline — not just
    zero — that is a malformed or truncated re-read, not a real shrink, and
    the save is refused. lastfm_artists.json is append-only by design: no
    code path anywhere ever deletes an entry from it (write-once, the same
    rule the piggyback follows), so a shrink of ANY size between two reads
    taken moments apart is anomalous, not a legitimate race — a real
    concurrent writer only ever ADDS entries, so a genuine race can grow the
    count between the two reads but never shrink it.

    A `json.JSONDecodeError` (the file itself is not valid JSON, not merely
    a malformed envelope) is also refused rather than propagated raw:
    `Store._atomic_write` writes via `mkstemp` + `os.replace`, so the file on
    disk was never left half-written by anything this script did — a bad
    read here means something else touched the file — but this run's
    fetched-but-unsaved batch is still lost and that needs saying plainly.
    """
    try:
        baseline = len(store.lastfm_artist_map())
        current = store.lastfm_artist_map()
    except json.JSONDecodeError as exc:
        raise BackfillAbort(
            f"data/lastfm_artists.json is not valid JSON ({exc}); refusing to save. "
            f"{len(new_entries)} fetched-but-unsaved artist(s) from this batch are "
            "discarded (the file itself was never corrupted by this script — Store "
            "writes atomically — but something else made it unreadable). Artists "
            "already flushed by an earlier incremental save in this run are unaffected; "
            "fix data/lastfm_artists.json and re-run the backfill to retry the discarded batch."
        ) from exc
    if len(current) < baseline:
        raise BackfillAbort(
            f"data/lastfm_artists.json shrank from {baseline} to {len(current)} artists "
            "between two reads moments apart — refusing to save. lastfm_artists.json is "
            "append-only (nothing ever deletes an entry from it), so any shrink is a "
            f"malformed or truncated re-read, not a real one. {len(new_entries)} "
            "fetched-but-unsaved artist(s) from this batch are discarded, but that work "
            "is re-runnable and the permanent cache is not — inspect "
            "data/lastfm_artists.json and re-run the backfill."
        )
    # Existing-wins EXCEPT when the existing entry on disk is itself a
    # `miss: true` — a hit is precious and permanently write-once (protects
    # a genuine concurrent writer's fresher save from being clobbered by
    # this run's possibly-stale batch), but a miss is not: `--refetch-misses`
    # exists specifically to let a fresh fetch upgrade one, and a stale miss
    # sitting in `current` must not out-rank the very re-fetch that targeted
    # it. A miss overwritten by another miss (a re-attempt that failed again)
    # is a no-op in practice — same shape, new `fetched_at`.
    merged = dict(current)
    for aid, entry in new_entries.items():
        existing = current.get(aid)
        if existing is None or existing.get("miss"):
            merged[aid] = entry
    store.save_lastfm_artists({"version": 1, "artists": merged})
    return merged


# ---- CLI -----------------------------------------------------------------


def run_backfill(
    target_artists: dict[str, str],
    store: Store,
    fm: LastFm,
    limit: int | None = None,
    refetch_misses: bool = False,
    now: str | None = None,
    progress_every: int = PROGRESS_EVERY,
    save_every: int = SAVE_EVERY,
    print_fn=print,
) -> dict:
    """Fetch similar-artist lists for `target_artists`. `limit` bounds how
    many artists this call ATTEMPTS (fetch calls made), not how many succeed
    — a run with `limit=10` that hits 10 non-code-6 errors makes exactly 10
    attempts and fetches 0. Saves incrementally every `save_every` fetched
    artists and once more at the end. Returns the summary dict also printed
    at the end.

    Failure rule (spec §Fetching, binding): only Last.fm error code 6 —
    `fm.artist_similar` returning `None` — is a genuine miss, recorded as
    `miss: true`. Any other error (a raised `LastFmError` OR a bare
    transport exception — `artist_similar`, unlike `enrich`, does not wrap
    the latter into the former) leaves the artist absent from the cache so a
    later run retries it, and this run counts it as skipped.

    Lets `BackfillAbort` (from `merge_save`'s clobber guard) propagate
    uncaught: a save that must be refused mid-run means every fetch already
    flushed by an earlier `_save` call in this run is safe, but continuing
    to fetch more when saves are refused just discards more work, so this
    stops rather than looping to the end pointlessly. `main()` is
    responsible for turning that into a clean exit.
    """
    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    known = store.lastfm_artist_map()
    to_fetch = artists_to_fetch(target_artists, known, refetch_misses=refetch_misses)
    already_known = len(target_artists) - len(to_fetch)

    if limit is not None:
        bounded_ids = list(to_fetch)[:limit]
        to_fetch = {aid: to_fetch[aid] for aid in bounded_ids}

    fetched_total = 0
    misses = 0
    skipped = 0
    pending: dict[str, dict] = {}  # artists not yet folded into a save

    def _save(entries: dict) -> None:
        if not entries:
            return
        merge_save(store, entries)

    items = list(to_fetch.items())
    processed = 0
    for aid, name in items:
        try:
            similar = fm.artist_similar(name)  # LastFmError propagates -> key stays absent
        except Exception:
            # Any error — Last.fm-flavoured (anything other than code 6,
            # already handled below as a miss) or a bare transport failure
            # (`artist_similar` does not wrap those the way `enrich` does) —
            # means this artist is left absent, not recorded. Already-
            # fetched artists earlier in THIS loop are safe because they
            # were already handed to `_save` below.
            skipped += 1
            processed += 1
            if processed % progress_every == 0:
                print_fn(f"progress: {processed}/{len(items)} fetched={fetched_total} "
                          f"misses={misses} skipped={skipped}")
            continue

        record = {
            "name": name,
            "similar": similar or [],
            "fetched_at": now,
            "miss": similar is None,  # code 6 only — the ONLY path to a miss
        }
        pending[aid] = record
        fetched_total += 1
        if record["miss"]:
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
        "target": len(target_artists),
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
                         help="max number of artists to ATTEMPT this run (not fetched-"
                              "successfully; default: unlimited)")
    parser.add_argument("--all-cached", action="store_true",
                         help="widen target artists to every cached playlist, not just homes")
    parser.add_argument("--refetch-misses", action="store_true",
                         help="also re-attempt artists already recorded as a Last.fm miss "
                              "(hits are still skipped, write-once)")
    args = parser.parse_args()

    store = Store(DATA_DIR)
    key = load_key()
    if not key:
        print(f"no Last.fm API key found (expected {load_key.__module__}.KEY_PATH); aborting")
        raise SystemExit(1)
    fm = LastFm(key)

    track_lists = load_all_cached_tracks() if args.all_cached else load_home_tracks()
    target_artists = collect_target_artists(track_lists)
    print(f"playlists={len(track_lists)} target_artists={len(target_artists)} "
          f"limit={args.limit if args.limit is not None else 'unbounded'} "
          f"scope={'all-cached' if args.all_cached else 'home'} "
          f"refetch_misses={args.refetch_misses}")

    try:
        summary = run_backfill(target_artists, store, fm, limit=args.limit,
                                refetch_misses=args.refetch_misses)
    except BackfillAbort as exc:
        print(f"backfill aborted: {exc}")
        raise SystemExit(1) from exc
    _print_summary(summary)


if __name__ == "__main__":
    main()
