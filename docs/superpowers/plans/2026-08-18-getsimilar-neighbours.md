# getSimilar Neighbour Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the spec's `neighbour` feature — track.getSimilar cross-artist neighbours already present in a home — plus the track-level tag layer, fed by a new rebuildable `data/lastfm_tracks.json`, with a bounded backfill and a measured `NEIGHBOUR_WEIGHT`.

**Architecture:** `lastfm_tracks.json` is REBUILDABLE cache (unlike permanent tags.json), keyed `artist\x1ftitle` lowercased/whitespace-collapsed. `suggest()` gains a fourth argument (the track map); profiles gain a `track_keys` set; the neighbour score sums Last.fm `match` values of cross-artist neighbours found in the home. Track tags REPLACE artist tags when present (resolution, not mixing — reason strings distinguish `tags:` from `artist tags:`). Fetching mirrors the tags pattern: a bounded backfill script + a force-path piggyback.

**Spec:** ~/kode/spotify/sortify-lastfm/docs/superpowers/specs/2026-08-17-suggestion-signals-design.md — authoritative; read §Storage, §Scoring (neighbour), §Fetching, §Evaluation. Still deferred: user tags, descriptions, Last.fm push.

## Global Constraints

- ZERO Spotify calls anywhere. Last.fm calls only in the fetch paths, faked in every test. `data/` is a LIVE symlink — tests never write it (conftest isolates); backfill runs only with the controller's go.
- Worktree ~/kode/spotify/sortify-suggest, branch `lastfm-similar`, base master bd90353. Baseline 383 tests green. Both orderings after every task + `node tests/ui_harness.mjs` (62) when app.py/frontend touched. Known flake: test_queue_worker.py::test_rate_429_halves_and_keeps_going — rerun once, never modify.
- tags.json rules unchanged (permanent, write-once, code-6-only miss). lastfm_tracks.json is rebuildable but follows the SAME miss discipline (code 6 → miss:true; other errors → absent/retryable) and the SAME shrink-guard merge-save pattern both writers use.
- **The correctness requirement (spec, binding): the neighbour feature EXCLUDES same-artist neighbours entirely** — a neighbour whose artist name (case-insensitive) matches ANY of the seed track's artists scores nothing. Without this the feature re-derives artist overlap and reproduces the original complaint. This is the regression test of the whole plan.
- Artist overlap stays primary; TAG_WEIGHT=3.0 and its pin unchanged. New NEIGHBOUR_WEIGHT placeholder 2.0, measured in T4 (1-D sweep), capped by the primacy pin like TAG_WEIGHT.
- No new runtime deps; house style; commit per task, Co-Authored-By trailer.

---

### Task 1: Storage + client — `lastfm_tracks.json`, `track_key`, two fetch methods

**Files:** Modify `sortify/store.py`, `sortify/tags.py`. Tests: `tests/test_store_and_auth.py`, `tests/test_tags.py` (append).

**Produces (exact interfaces):**
- `tags.track_key(artist: str, title: str) -> str` — lowercase, whitespace-collapsed, joined with `"\x1f"` (unit separator; spec §Storage says why).
- `Store.lastfm_tracks() -> dict` / `save_lastfm_tracks(payload)` — envelope `{"version": 1, "tracks": {key: {"similar": [{"artist","track","match"}], "tags": [str,...], "fetched_at": float, "miss": bool}}}`, guard-on-read via the existing `_versioned` helper; plus `Store.lastfm_track_map() -> dict` returning the inner `tracks` dict ({} on anything malformed) — the analog of `tag_artists()`.
- `LastFm.track_similar(artist, title) -> list[dict] | None` (None = code 6 not-found; raises LastFmError otherwise) and `LastFm.track_top_tags(artist, title) -> list[str] | None` — mirror `top_tags`' request/error/pacing structure exactly (`track.getSimilar` params artist/track/limit=20; `track.getTopTags` same params; reuse `_looks_like_not_found`). Similar entries slim to `{"artist": name, "track": name, "match": float}`.
- `tags.fetch_track(fm, artist, title, now: float) -> dict` — one record from both calls: code-6 on EITHER call → `{"miss": True, ...}` only if BOTH miss? No: miss means "Last.fm doesn't know this track" — if getSimilar 404s but tags succeed, record what succeeded with empty similar; `miss: True` only when BOTH return None. Any raised error propagates (caller leaves the key absent).

TDD: key normalization cases (case, whitespace, the dash-in-title collision the separator prevents); envelope round-trip + guard; client methods against fake transports incl. code-6 vs code-29 discrimination; fetch_track's both-miss/half-miss/error matrix. Both orderings; commit.

---

### Task 2: Scoring — neighbour feature + track-tag resolution

**Files:** Modify `sortify/suggest.py`, `sortify/app.py` (three call sites pass the track map). Tests: `tests/test_suggest.py` (append), integration files as touched.

**Produces:**
- `build_profile(tracks, tag_artists)` additionally returns `"track_keys": set[str]` — every `track_key(artist.name, track.name)` combination of the home's tracks.
- `_resolve_tags(track, tag_artists, track_map) -> tuple[Counter, str]` — returns (tags, level) where level ∈ {"track", "artist"}: track-level tags from the track's lastfm_tracks record (cleaned via `clean_tags` with the first artist's name) REPLACE artist tags when non-empty; artist fallback unchanged. Reason strings: `tags: …` for track level, `artist tags: …` for fallback (the wording contract from phase 1).
- `_neighbour_score(track, prof, track_map) -> tuple[float, int]` — look up the track's record under any of its (artist, title) keys; sum `match` over neighbours whose `track_key(n_artist, n_track)` ∈ `prof["track_keys"]` AND whose artist is NOT any seed-track artist (case-insensitive); return (sum, count). Score contribution `NEIGHBOUR_WEIGHT * sum`; reason `f"{count} similar track{s} already here"`.
- `suggest(track, profiles, tag_artists, track_map=None)` — track_map optional ({} default) so existing callers stay valid until app.py passes it; app.py passes a fresh `store.lastfm_track_map()` alongside the fresh tag_artists read (same freshness trade, same comment).

Binding tests: same-artist-only neighbours score ZERO (the spec's regression pin — a home whose matching neighbours are all by the seed artist gets no neighbour score or reason); cross-artist neighbour present scores and reasons; match-weighting (0.9 neighbour beats 0.2); track tags replace (not mix with) artist tags and switch the reason wording; missing/miss records contribute nothing; artist overlap still outranks max tag+neighbour at placeholder weights (extend the primacy pin). Both orderings + harness; commit.

---

### Task 3: Fetching — backfill script + force-path piggyback

**Files:** Create `scripts/backfill_similar.py` (mirror backfill_tags.py's structure: target = tracks of cached HOME playlists, `--all-cached` widens, `--limit` bounds attempted, progress every 25, incremental merge-save every 50 with the SHRINK GUARD, JSONDecodeError handling, summary counts). Modify `sortify/app.py`: the existing force-path fetch additionally fetches the now-playing track's lastfm_tracks record when absent (inside the same non-blocking lock and 60s floor; 2 extra Last.fm calls max; same 5s timeout client; never on passive polls). Tests: `tests/test_backfill_similar.py` (new, mirror test_backfill_tags.py's coverage list), `tests/test_now_tag_fetch.py` (append piggyback cases: absent record fetched on force, present/miss skipped, failure leaves absent + 200, floor shared).

lastfm_tracks.json is rebuildable, so `--refetch-misses` flag is allowed here (unlike tags.json) — default off. Both orderings; commit.

---

### Task 4: Harness + measurement + verification

**Files:** Modify `scripts/eval_suggest.py` (thread track_map through; hold-one-out must also rebuild `track_keys` without the held-out track; `--search` sweeps NEIGHBOUR_WEIGHT {0.5, 1, 1.5, 2, 3, 4, 6} 1-D with TAG_WEIGHT fixed at 3.0; baseline now = both weights 0 AND a tags-only row for attribution), `sortify/suggest.py` (committed measured NEIGHBOUR_WEIGHT + comment with numbers/command/date). Tests: extend test_eval_suggest.py (track_keys hold-out pin with its own mutation check).

Steps: TDD the harness changes → CONTROLLER RUNS THE LIVE BACKFILL (not you — stop and report when ready) → measure: artist-only baseline, tags-only, each neighbour weight; artist-absent subset is the headline; commit the winner respecting the primacy pin (cap and record both cells if the search wants more). Full verification: both orderings, harness 62, eval reproducibility. Commit.
