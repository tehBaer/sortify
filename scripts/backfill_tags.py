"""One-shot Last.fm tag backfill for home-playlist artists.

Not part of the served app, but unlike `eval_suggest.py` this one DOES make
network calls — to Last.fm only, never Spotify (see `sortify/tags.py`'s
module docstring: it has its own client and its own limiter, so this can
never touch the Spotify budget). Spec §Fetching sanctions exactly this: "a
bounded backfill command" run on explicit user action, paced at the same
`MIN_INTERVAL` the on-demand fetch uses, with no background job behind it.

Collects target artists from cached HOME playlists (mirrors
`eval_suggest.load_home_tracks`'s config.json + cache.json read, since that
script is not a package and importing it would run its own argparse setup
as a side effect) and skips anything already in tags.json — hit or miss,
write-once, same rule `tags.enrich` itself follows.

`--limit N` bounds how many artists this run ATTEMPTS (fetch calls it makes),
not how many it successfully fetches — a run that hits N skips still stops
at N attempts with 0 fetched.

Usage:
    .venv/bin/python scripts/backfill_tags.py [--limit N] [--all-cached]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sortify.store import Store  # noqa: E402
from sortify.tags import LastFm, LastFmError, enrich, load_key  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROGRESS_EVERY = 25
SAVE_EVERY = 50


class BackfillAbort(Exception):
    """Raised when a save must be refused rather than risk the permanent
    tags.json cache. Always means: fetched-but-unsaved artists from this
    invocation of `merge_save` are discarded, but that work is re-runnable
    (another backfill invocation re-fetches them) — the file on disk is not,
    so refusing beats guessing."""


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
    overwrite earlier ones — first name seen wins, matching how `enrich`
    itself treats its `artist_names` argument (a plain id->name map, no
    notion of "more recent")."""
    out: dict[str, str] = {}
    for tracks in track_lists.values():
        for track in tracks:
            for artist in track.get("artists", []):
                aid = artist.get("id")
                name = artist.get("name")
                if aid and aid not in out:
                    out[aid] = name
    return out


def artists_to_fetch(target_artists: dict[str, str], known: dict[str, dict]) -> dict[str, str]:
    """Target artists minus anything already in tags.json — hit or miss,
    write-once, same rule `tags.enrich` applies internally. Kept as its own
    function so the "already known" count in the summary is exact rather
    than inferred from a diff of before/after `enrich` runs."""
    return {aid: name for aid, name in target_artists.items() if aid not in known}


# ---- persistence ---------------------------------------------------------


def merge_save(store: Store, new_entries: dict) -> dict:
    """Re-read tags.json fresh, merge with existing-entries-win, and save.

    This script runs in its OWN process, so `app.py`'s in-process
    `_tags_save_lock` (guarding `_merge_save_tag_artists`) gives it no
    protection at all — a save here and a save from the live server can
    still interleave. The re-read-immediately-before-write pattern mirrored
    here is the mitigation, not a full fix: the server writes at most one
    bounded fetch per 60s (`/api/now`'s force path, rate-limited by
    `NOW_FORCE_MIN_INTERVAL`), so the actual cross-process window this
    leaves open is tiny, and the failure mode inside it is a lost update —
    one artist's freshly-fetched entry silently overwritten — never file
    corruption, and always retryable on the next backfill run since a lost
    entry is just absent, not recorded as a false miss.

    Clobber guard: `store.tag_artists()` degrades a malformed-but-valid-JSON
    envelope to `{}` rather than raising (guard-on-read, so other callers
    fail toward "no tags" instead of crashing) — but that same behaviour
    would make this function treat a ~1400-entry permanent cache as
    correctly empty and overwrite it with just this run's batch. `baseline`
    is captured before the fresh re-read used for the actual merge; if that
    later read comes back with zero artists while the baseline was
    non-empty, that is a malformed re-read, not a real empty file, and the
    save is refused — raises `BackfillAbort` rather than guessing. Ordinary
    concurrent writers (the live server) only ever ADD entries, so a
    legitimate race never collapses the count to zero; only a broken
    envelope does.

    A `json.JSONDecodeError` (the file itself is not valid JSON, not merely
    a malformed envelope) is also refused rather than propagated raw:
    `Store._atomic_write` writes via `mkstemp` + `os.replace`, so the file on
    disk was never left half-written by anything this script did — a bad
    read here means something else touched the file — but this run's
    fetched-but-unsaved batch is still lost and that needs saying plainly.
    """
    try:
        baseline = len(store.tag_artists())
        current = store.tag_artists()
    except json.JSONDecodeError as exc:
        raise BackfillAbort(
            f"data/tags.json is not valid JSON ({exc}); refusing to save. "
            f"{len(new_entries)} fetched-but-unsaved artist(s) from this batch are "
            "discarded (the file itself was never corrupted by this script — Store "
            "writes atomically — but something else made it unreadable). Artists "
            "already flushed by an earlier incremental save in this run are unaffected; "
            "fix data/tags.json and re-run the backfill to retry the discarded batch."
        ) from exc
    if baseline > 0 and not current:
        raise BackfillAbort(
            f"data/tags.json re-read as 0 artists but {baseline} were on disk moments "
            "ago — the envelope is likely malformed (missing/wrong-typed 'artists' key), "
            f"not really empty. Refusing to save: {len(new_entries)} fetched-but-unsaved "
            "artist(s) from this batch are discarded, but that work is re-runnable and "
            "the permanent cache is not — inspect data/tags.json and re-run the backfill."
        )
    merged = {**new_entries, **current}
    store.save_tag_artists(merged)
    return merged


# ---- CLI -----------------------------------------------------------------


def run_backfill(
    target_artists: dict[str, str],
    store: Store,
    fm: LastFm,
    limit: int | None = None,
    now: str | None = None,
    progress_every: int = PROGRESS_EVERY,
    save_every: int = SAVE_EVERY,
    print_fn=print,
) -> dict:
    """Fetch tags for `target_artists`. `limit` bounds how many artists this
    call ATTEMPTS (fetch calls made), not how many succeed — a run with
    `limit=10` that hits 10 non-code-6 errors makes exactly 10 attempts and
    fetches 0. Saves incrementally every `save_every` fetched artists and
    once more at the end. Returns the summary dict also printed at the end.

    Failure rule (spec §Fetching, binding): only Last.fm error code 6 is a
    genuine miss — `enrich` already records those as `miss: true`. Any other
    `LastFmError` (bad key, suspended, rate limit, service error, malformed
    response) is NOT a miss: the artist is left absent from the cache so a
    later run retries it, and this run counts it as skipped. The exception
    carries `.partial` (`enrich`'s contract) with every artist verified
    before the failure, which this function saves before re-raising-free
    return — an error mid-run must not lose already-fetched progress.

    Lets `BackfillAbort` (from `merge_save`'s clobber guard) propagate
    uncaught: a save that must be refused mid-run means every fetch already
    flushed by an earlier `_save` call in this run is safe, but continuing
    to fetch more when saves are refused just discards more work, so this
    stops rather than looping to the end pointlessly. `main()` is
    responsible for turning that into a clean exit.
    """
    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    known = store.tag_artists()
    to_fetch = artists_to_fetch(target_artists, known)
    already_known = len(target_artists) - len(to_fetch)

    if limit is not None:
        bounded_ids = list(to_fetch)[:limit]
        to_fetch = {aid: to_fetch[aid] for aid in bounded_ids}

    fetched_total = 0
    misses = 0
    skipped = 0
    pending: dict[str, str] = {}  # artists not yet folded into a save

    def _save(entries: dict) -> None:
        if not entries:
            return
        merge_save(store, entries)

    items = list(to_fetch.items())
    processed = 0
    for aid, name in items:
        single = {aid: name}
        try:
            result = enrich(single, {}, fm, now)
        except LastFmError:
            # Any error other than code 6 (already handled inside enrich as
            # a miss) means this artist is left absent, not recorded. enrich
            # raises before writing anything into its own `out` for a
            # request-level failure, so there is nothing to merge for this
            # artist; already-fetched artists earlier in THIS loop are safe
            # because they were already handed to `_save` below.
            skipped += 1
            processed += 1
            if processed % progress_every == 0:
                print_fn(f"progress: {processed}/{len(items)} fetched={fetched_total} "
                          f"misses={misses} skipped={skipped}")
            continue

        entry = result[aid]
        pending[aid] = entry
        fetched_total += 1
        if entry.get("miss"):
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
          f"scope={'all-cached' if args.all_cached else 'home'}")

    try:
        summary = run_backfill(target_artists, store, fm, limit=args.limit)
    except BackfillAbort as exc:
        print(f"backfill aborted: {exc}")
        raise SystemExit(1) from exc
    _print_summary(summary)


if __name__ == "__main__":
    main()
