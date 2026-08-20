# Artist-Similar Signal — Design

**Date:** 2026-08-20
**Status:** draft, awaiting user review
**Depends on:** the weak-guesses tier (branch `weak-guesses`, commit e14fb5e) and the
2026-08-20 `--all-cached` track backfill having landed.

## Problem

About a third of ranked tracks have no artist overlap in any home (the eval
harness's "absent" subset: 158/500 at the 2026-08-18 measurement). For them,
only tag cosine and track-level getSimilar neighbours can score — and
track-level coverage is structurally thin: Last.fm often knows the *artist*
but not the specific *track* (memory: getSimilar carries usable signal at
~52% track coverage; tags ~8%). The weak-guesses tier now surfaces whatever
sub-threshold evidence exists, but for many unknown-artist tracks there is
still literally nothing to rank.

`artist.getSimilar` fills that hole: artist-level similar lists exist for
most artists Last.fm knows at all, and "this artist is similar to artists
already living in that home" is exactly the mood/scene evidence a human uses
when filing an unfamiliar artist.

Measured 2026-08-20 (worktree, post-backfill, guesses tier live; 500 pairs,
seed 7, hold-one-out): 26/500 tracks had no confident home. The guesses
tier fills 12 of those lists (all 12 artist-absent), but only 1/12 carries
the true home in its top-3, and 14/500 still return nothing at all — zero
tag, track-neighbour, or artist evidence. Those 26 are this signal's
target population, and hold-one-out flatters coverage: a genuinely new
track (in no home, artists unseen) lands in that population far more often
than 5%.

## Options considered

1. **Fourth additive signal in the confident tier.** Rejected: the
   artist-overlap-primacy pin requires all non-artist evidence to sum below
   `ARTIST_BASE + ARTIST_PER_TRACK = 3.4`, and `TAG_WEIGHT (3.0) +
   NEIGHBOUR_WEIGHT × NEIGHBOUR_SUM_CAP (0.3) = 3.3` already leaves only 0.1
   of headroom — nothing meaningful fits, and renegotiating the pin means
   re-measuring every committed constant.
2. **Guess-tier-only signal (chosen).** The confident tier's math is
   untouched — same constants, same pins, no re-analysis. The new signal
   contributes only when a home has already failed the `MIN_SCORE` gate,
   i.e. exactly when we're assembling labeled guesses. It can strengthen and
   order guesses, and it can pull a zero-score home *into* the guess pool,
   but it can never mint a confident suggestion. This is R2a's philosophy
   applied from the start: similarity evidence ranks guesses; owning the
   artist ranks homes.
3. **Replace track-neighbours with artist-similar.** Rejected: track-level
   evidence is stronger where it exists (it is about this recording);
   artist-level is the fallback, not the replacement.

## Design

### Scoring (`sortify/suggest.py`)

- `build_profile` additionally returns `artist_names: set[str]` — the
  `_norm_name` of every credited artist on the home's tracks. (Names, not
  IDs: Last.fm returns similar *names*; Spotify IDs never enter this
  signal.)
- New `_artist_sim_score(track, prof, artist_map) -> tuple[float, int, list[str]]`:
  for each seed artist's cached record, sum `match` over similar artists
  whose normalized name is in `prof["artist_names"]`, excluding — **binding,
  same pin as `_neighbour_score`** — any similar artist whose normalized
  name matches a seed artist. Each home-present similar artist is counted
  once even if several seed artists list it (a collab must not double-spend
  one neighbour). Returns (raw sum, count, top contributor names by match).
- In `suggest()`, the confident computation is byte-for-byte today's. Only
  in the `elif score > 0` weak-pool branch — and in a new
  `elif artist-sim evidence exists` case for otherwise-zero homes — the term
  `ARTIST_SIM_WEIGHT * min(sum, ARTIST_SIM_CAP)` is added, with reason
  `similar artists: X, Y` (top 2 by match). Weak entries whose *total*
  would cross `MIN_SCORE` stay weak — the flag means "nothing was
  confident", not "score is small". Display consequence (pct can read high
  on a dashed guess button) is accepted; the label is the contract.
- Placeholder `ARTIST_SIM_WEIGHT = 1.0`, `ARTIST_SIM_CAP = 1.0` until
  measured. No primacy analysis needed: the signal cannot reach the
  confident tier by construction, which a test must pin (a maxed
  artist-sim-only home never returns unflagged).

### Storage (`sortify/store.py`)

`data/lastfm_artists.json` — REBUILDABLE cache, envelope
`{"version": 1, "artists": {<spotify_artist_id>: {"name": str,
"similar": [{"artist": str, "match": float}], "fetched_at": float,
"miss": bool}}}` via the existing `_versioned` guard-on-read helper, plus
`Store.lastfm_artist_map() -> dict` ({} on malformed). Keyed by Spotify
artist ID to mirror `tags.json` (seed-side lookups have IDs in hand; the
similar entries themselves are name-matched). Same miss discipline as
`lastfm_tracks.json`: Last.fm error code 6 → `miss: true`; every other
error leaves the key ABSENT so it retries; merge-save with the shrink
guard; `--refetch-misses` permitted (rebuildable file).

### Client (`sortify/tags.py`)

`LastFm.artist_similar(name, limit=20) -> list[dict] | None` — mirrors
`track_similar` exactly: `artist.getSimilar`, reuse `_looks_like_not_found`
(None on code 6, `LastFmError` otherwise), shared `MIN_INTERVAL` pacing,
entries slimmed to `{"artist": name, "match": float}`. No second rate
limiter, no Spotify imports.

### Fetching

- `scripts/backfill_artist_similar.py`, mirroring `backfill_tags.py`'s
  structure and target selection (artists of cached home playlists;
  `--all-cached` widens; `--limit` bounds; progress every 25; incremental
  merge-save every 50 with the shrink guard; summary counts). ~1 call per
  artist; the home-artist set was 1427 at the last count. Runs only with
  the controller's explicit go.
- Force-path piggyback in `app.py`: inside the existing non-blocking lock
  and 60s floor, when the now-playing track's artists lack
  `lastfm_artists` records, fetch them (bounded by the track's artist
  count, in practice ≤3 calls). Never on passive polls. Zero Spotify calls
  anywhere in this feature.

### Evaluation (`scripts/eval_suggest.py`)

1. **Re-baseline first.** The weak-guesses tier changed eval semantics for
   the absent subset (empty results became ranked guesses), and the
   2026-08-20 backfill changed the data. Record the new
   artist-only / tags-only / +neighbours rows before touching weights.
2. Thread `artist_map` through; hold-one-out must rebuild `artist_names`
   without the held-out track (extend the existing mutation-check pin).
3. 1-D sweep of `ARTIST_SIM_WEIGHT` over {0.25, 0.5, 1.0, 1.5, 2.0, 3.0}
   with everything else fixed; headline metric is the absent subset's
   top1/top3.
4. **Self-check:** non-absent rows must be identical across the sweep — the
   confident tier cannot move by construction; if it does, the
   implementation leaked the signal past the gate.
5. Commit the measured winner with numbers, command, and date in the
   constant's comment, as always.

## Testing

Unit pins: same-artist exclusion (incl. the `_norm_name` whitespace-drift
case); collab double-count exclusion; guess-tier-only containment (maxed
signal never yields an unflagged entry, never crosses into a non-empty
confident list); zero-base-score homes enter the pool on artist-sim alone
with the reason string; miss/absent records contribute nothing; envelope
round-trip + guard; client code-6 vs other-error discrimination; backfill
mirror-coverage of `test_backfill_tags.py`; piggyback force/passive/floor
cases. Both pytest orderings + `node tests/ui_harness.mjs` (no UI changes
expected — reasons flow through the existing guess rendering).

## Out of scope (YAGNI)

Confident-tier participation, transitive similarity (similar-of-similar),
genre inference from similar artists, any new UI treatment, Last.fm push,
and any change to `tags.json`'s permanent-file rules.

## Success criteria

Absent-subset top3 improves measurably over the post-guesses baseline at a
committed weight; non-absent metrics unchanged; suite green in both
orderings; zero Spotify calls; Last.fm spend bounded to one backfill run
plus ≤3 calls per explicit force.
