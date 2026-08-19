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

Two Last.fm calls per attempted track (`tags.fetch_track` makes one
`track.getSimilar` request, THEN one `track.getTopTags` request, each paced
by `tags.MIN_INTERVAL`) — a run that ATTEMPTS N tracks costs exactly 2N
Last.fm requests, with exactly one exception: if the FIRST call
(`getSimilar`) itself raises, `fetch_track` never reaches the second call at
all, so that one attempt costs 1 request, not 2. A "not found" response
(Last.fm error 6) is not a raise — it costs its call same as a hit and the
second call still runs. `main()`'s startup line prints the projected request
ceiling (`2 * attempted`) before spending anything, and the summary prints
the actual count `_CountingFm` measured, so the two can be compared after a
run.

Fix round 1, Important (I2): a Last.fm outage or a dead API key doesn't
raise ONE loud error, it raises the SAME error on every single subsequent
attempt — without a circuit breaker this would walk every remaining target
track "learning" nothing, at 2 wasted requests apiece. `CONSECUTIVE_FAILURE_LIMIT`
aborts the run (via `BackfillAbort`, after flushing whatever this run has
already fetched) once that many attempts in a row have raised; any
intervening success resets the counter to 0, since a broken key or outage
fails EVERY attempt, not just most of them.

`--limit N` bounds how many tracks this run ATTEMPTS (fetch_track calls it
makes), not how many it successfully fetches — a run that hits N tracks that
all raise still stops at N attempts with 0 fetched (unless the consecutive-
failure abort above triggers first, at K=10).

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
# Fix round 1, I2: a dead API key or a Last.fm outage fails EVERY attempt,
# not just most of them — 10 in a row is not "unlucky", it's "stop".
CONSECUTIVE_FAILURE_LIMIT = 10


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


def merge_save(store: Store, new_entries: dict, replace_keys: frozenset | set | None = None) -> dict:
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

    Fix round 1, I1: plain existing-wins made `--refetch-misses` a pure call
    burner — it spent 2N requests re-fetching every stale miss, then
    `{**new_entries, **current}` threw every one of those fresh records away
    because `current` (the stale miss) still won. `replace_keys` is the
    subset of `new_entries`' keys `run_backfill` marks as a DELIBERATE
    refetch-of-a-known-miss; for exactly those keys — never any other key in
    `new_entries` — the fresh record replaces what's on disk, but ONLY if
    what's on disk is STILL a miss at merge time. That last check guards a
    race window this function's own docstring already talks about elsewhere:
    if a concurrent writer (the `/api/now` force-path piggyback) landed a
    REAL hit for that key between `run_backfill`'s stale snapshot and this
    save, a deliberate refetch must not clobber it — existing-wins is still
    the right rule for a hit, refetch-misses is only ever meant to beat a
    miss.
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
    for k in (replace_keys or ()):
        if k in new_entries and isinstance(current.get(k), dict) and current[k].get("miss"):
            merged[k] = new_entries[k]
    store.save_lastfm_tracks({"version": 1, "tracks": merged})
    return merged


# ---- CLI -----------------------------------------------------------------


class _CountingFm:
    """Wraps an `fm` (real `LastFm` or a test double) to count actual
    `track_similar`/`track_top_tags` requests made, regardless of whether
    each one succeeded or raised. Fix round 1, I3: the summary's `requests`
    count needs to be measured, not guessed from `fetched`/`skipped` — a
    skip can mean either 1 request (getSimilar raised) or 2 (getTopTags
    raised after getSimilar succeeded), so only counting at the call site
    is accurate. `fetch_track` only ever calls these two methods, so
    nothing else needs wrapping; any other attribute access falls through
    to the wrapped object unchanged."""

    def __init__(self, inner):
        self._inner = inner
        self.requests = 0

    def track_similar(self, artist, title):
        self.requests += 1
        return self._inner.track_similar(artist, title)

    def track_top_tags(self, artist, title):
        self.requests += 1
        return self._inner.track_top_tags(artist, title)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _was_known_as_miss(entry: dict, known: dict[str, dict]) -> bool:
    """True if any of `entry`'s credited-artist keys held a `miss: true`
    record in `known` (the pre-run snapshot). Used only to mark which
    `--refetch-misses` fetches are DELIBERATE miss retries, so `merge_save`
    knows which keys are allowed to replace a stale on-disk miss (see I1)."""
    return any(isinstance(known.get(k), dict) and known[k].get("miss") for k in entry["keys"])


def run_backfill(
    target_tracks: dict[str, dict],
    store: Store,
    fm: LastFm,
    limit: int | None = None,
    now: float | None = None,
    refetch_misses: bool = False,
    progress_every: int = PROGRESS_EVERY,
    save_every: int = SAVE_EVERY,
    consecutive_failure_limit: int = CONSECUTIVE_FAILURE_LIMIT,
    print_fn=print,
) -> dict:
    """Fetch (getSimilar + track top tags) for `target_tracks`. `limit`
    bounds how many tracks this call ATTEMPTS (`fetch_track` calls made),
    not how many succeed. Saves incrementally every `save_every` fetched
    tracks and once more at the end. Returns the summary dict also printed
    at the end, including `requests` — the actual Last.fm request count
    `_CountingFm` measured (see module docstring: exactly 2 per attempted
    track, 1 only when `getSimilar` itself raises).

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

    Fix round 1, I2: each skip is printed with the failing key and the
    exception's own one-liner (`print_fn`, not a silent `skipped += 1`) — a
    blind skip loop taught nothing when it later turned out the whole run
    was walking a dead API key. `consecutive_failure_limit` (K=10 by
    default) counts consecutive raises; ANY success resets it to 0, since a
    broken key or a Last.fm outage fails every attempt, not just most —
    hitting K in a row means "stop trusting individual attempts," not "bad
    luck." Hitting the limit flushes whatever `pending` has accumulated,
    THEN raises `BackfillAbort` so nothing already fetched is lost.

    Lets `BackfillAbort` (from `merge_save`'s clobber guard, or the
    consecutive-failure limit above) propagate uncaught, exactly like
    `backfill_tags.run_backfill` — see that function's docstring. `main()`
    turns it into a clean exit.
    """
    now = now if now is not None else time.time()
    known = store.lastfm_track_map()
    to_fetch = tracks_to_fetch(target_tracks, known, refetch_misses=refetch_misses)
    already_known = len(target_tracks) - len(to_fetch)

    if limit is not None:
        bounded_keys = list(to_fetch)[:limit]
        to_fetch = {k: to_fetch[k] for k in bounded_keys}

    counting_fm = _CountingFm(fm)
    fetched_total = 0
    misses = 0
    skipped = 0
    consecutive_failures = 0
    pending: dict[str, dict] = {}  # tracks not yet folded into a save
    retry_keys: set[str] = set()  # I1: deliberate --refetch-misses replacements

    def _save(entries: dict) -> None:
        if not entries:
            return
        merge_save(store, entries, replace_keys=retry_keys)

    items = list(to_fetch.items())
    processed = 0
    for fetch_key, entry in items:
        try:
            record = fetch_track(counting_fm, entry["artist"], entry["title"], now)
        except Exception as exc:
            # Left absent, not recorded — see the docstring above: unlike
            # enrich()'s batch, there is no partial progress to salvage for
            # a single track, and `fetch_track` does not wrap a bare
            # transport exception into `LastFmError` the way `enrich` does,
            # so this catches both deliberately.
            skipped += 1
            processed += 1
            consecutive_failures += 1
            print_fn(f"skip: {fetch_key} ({entry['artist']!r}/{entry['title']!r}): {exc}")
            if consecutive_failures >= consecutive_failure_limit:
                _save(pending)
                raise BackfillAbort(
                    f"{consecutive_failures} consecutive Last.fm failures (most recent: "
                    f"{fetch_key} - {exc}); aborting rather than walking the remaining "
                    f"{len(items) - processed} attempt(s) blind — a dead API key or a "
                    "persistent Last.fm outage looks exactly like this. Fetched-but-"
                    "unsaved progress from this run's last incremental save is safe; fix "
                    "the underlying issue and re-run the backfill to retry everything "
                    "after that point."
                )
            if processed % progress_every == 0:
                print_fn(f"progress: {processed}/{len(items)} fetched={fetched_total} "
                          f"misses={misses} skipped={skipped}")
            continue

        consecutive_failures = 0
        if refetch_misses and _was_known_as_miss(entry, known):
            retry_keys.add(fetch_key)
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
        "requests": counting_fm.requests,
    }


def _print_summary(summary: dict, print_fn=print) -> None:
    print_fn(
        f"done: target={summary['target']} already_known={summary['already_known']} "
        f"attempted={summary['attempted']} fetched={summary['fetched']} "
        f"misses={summary['misses']} skipped={summary['skipped']} "
        f"requests={summary['requests']}"
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

    # Fix round 1, I3: projected BEFORE spending anything, so the operator
    # can compare it against the actual `requests=` the summary prints —
    # this replicates run_backfill's own to_fetch/limit bounding but purely
    # locally (no network), the same skip-known logic `tracks_to_fetch`
    # applies. The ceiling is exact except when a `getSimilar` call itself
    # raises (1 request, not 2, for that one attempt — see the module
    # docstring), so this is a `<=`, not an exact prediction.
    preview = tracks_to_fetch(target_tracks, store.lastfm_track_map(),
                               refetch_misses=args.refetch_misses)
    if args.limit is not None:
        preview = dict(list(preview.items())[:args.limit])
    projected_requests = 2 * len(preview)

    print(f"playlists={len(track_lists)} target_tracks={len(target_tracks)} "
          f"limit={args.limit if args.limit is not None else 'unbounded'} "
          f"scope={'all-cached' if args.all_cached else 'home'} "
          f"refetch_misses={args.refetch_misses} "
          f"projected_requests<={projected_requests} "
          "(2 per attempted track, fewer only if getSimilar raises before getTopTags runs)")

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
