# Queued materialiser — "save all piles" as one paced, unattended job

Design, 2026-08-18. Regenerated from the decisions approved in the
2026-08-17/18 session (see `.superpowers/sdd/HANDOFF-2026-08-18.md` and the
SDD ledger in `.superpowers/sdd/2026-08-17-playlist-splitting/progress.md`).
Every decision below was explicitly approved by the user; this document
records them, it does not reopen them.

## Problem

The user's 1231-track playlist `{teh bomb}` is split into 9 piles
(`data/splits.json`). They want all 9 as permanent Spotify playlists. The
Feb-2026 dev-mode API has no batch add, so that is **~1240 calls** — one
POST per track plus playlist creation overhead. The pile-materialise branch
(`pile-materialise` @ `eecc85b`) built the machinery — claim tokens,
record-before-create, per-track idempotent resume, repricing, 284 green
tests — and it passed adversarial review. Its **delivery** did not: a
one-shot materialise measured **12.4 calls/min for 25 minutes**, above the
9.7/min that earned the 2026-08-13 ~23h ban. The machinery is accepted; the
blocking burst is rejected; that branch stays unmerged until this design
replaces its delivery.

The user wants two things at once:

1. **One click.** Enqueue all piles once, then walk away; the job finishes
   on its own, taking as long as it takes (~3 days by arithmetic).
2. **Carefully push the limits.** The band between 1.8/min (1208 calls,
   fine) and 9.7/min (678 calls, ~23h ban) is unmeasured. This job is ~1240
   calls of real, wanted traffic — the one legitimate chance to measure it.
   The output of that measurement is a deliverable, not a side effect.

## Evidence base (why these numbers)

- 678 calls @ ~9.7/min → ~23h quota ban (2026-08-13). **Known bad.**
- 1208 calls @ ~1.8/min → no penalty (spotify-autoqueuer, 2026-08-14).
  **Known good.** It is the rate, not the day's total.
- Sittings of ≤42-call bursts → fine. The band between is **unmeasured**.
- Rate-limit 429s are per client ID — contained to sortify.
  `QUOTA_EXCEEDED` propagates through the shared account ledger and parks
  every app on the account — **not** contained. `classify_429` already
  distinguishes the two.

## Design

### 1. Queue

`POST /api/split/{id}/queue` enqueues piles — a subset or all of them.
Enqueueing is free (zero Spotify calls) and persists to `data/queue.json`.
A worker drains **one track per tick**, driving the existing
pile-materialise machinery unchanged (claim tokens, record-before-create,
per-track idempotent resume). Smallest piles first, so finished playlists
appear early. Pause and cancel are instant and free; a partially
materialised pile stays resumable at the price of what's left — that
resume logic already exists on the branch.

### 2. Governor (the measuring instrument)

- **Start at 1.8/min** (33s tick) — the known-good rate.
- After **15 clean minutes** at a rate, shrink the interval **15%**:
  ≈1.8 → 2.1 → 2.5 → 2.9 → 3.4 → 4.0 → 4.7 → 5.5 → 6.5 → 7.0.
- **Ceiling 7.0/min** — 28% under known-bad. A probe allowed to touch the
  boundary has learned nothing and paid full price; the point is to map
  the safe side of the band, not to find the ban again.
- **Rate-limit 429**: honour Retry-After, halve the rate, record
  `{when, rate, retry_after}`, keep going.
- **QUOTA_EXCEEDED**: permanent stop. Publish via `note_cooldown` (so the
  other apps on the account see it), human-only resume — no code path
  restarts the worker after a quota trip.
- Persist `data/pacing.json`: current rate, the full 429 history, and
  `max_clean_rate` — the highest rate sustained for 15 clean minutes.
  **This file is a deliverable**: it converts CLAUDE.md's guess band into
  a measured number.
- `WINDOW_CAP` (12/60s) stays underneath as the backstop; the governor
  never asks it to move.

### 3. Budget class "bulk"

A new spend class for user-initiated, unattended work:

- Counts toward `DAILY_CAP` (600), with an **interactive reserve of 150**:
  the worker never spends past `DAILY_CAP − 150`. When it hits the
  reserve line it sleeps until local midnight and continues — that is
  where the ~3-day arithmetic comes from.
- **`QUIET_AFTER_COOLDOWN` applies.** That rail was deliberately kept when
  the genre enricher died, named for "the next proactive job" — this is
  that job. Six quiet hours past a cooldown's end, no exceptions.
- `BACKGROUND_DAILY_CAP` (40) is untouched — bulk is its own bucket in
  `usage.json`, not background.

### 4. No self-start

The worker thread is created **only** by an enqueue or resume request.
After a server restart the queue loads **paused**, with a Resume button in
the UI. There is no code path from boot to Spotify traffic — pinned with a
test in the style of `test_no_background_jobs`. Within one process
lifetime the worker may sleep across midnight and continue; that is the
one-click promise, and it is not a self-start because the click happened.

### 5. boxdash surfacing (read-only)

boxdash (`~/kode/boxdash`, port 8090) follows its house pattern: read
other apps' state **files** directly, probe HTTP only for liveness.

Contract on sortify's side: `data/pacing.json` and `data/queue.json` are
written atomically, mode 0600, versioned (`"version": 1`) with
guard-on-read like `tags.json`. Because the files outlive the process, the
card keeps working while sortify is down.

The boxdash card shows: current rate vs ceiling, spend vs cap+reserve,
pile i/n and track j/m, the last 429 (kind, rate, when), `max_clean_rate`,
and state (running / sleeping / paused / quiet / stopped). **Read-only** —
no spend controls live on the dashboard.

### 6. Sequencing

1. **Stoplist fix first.** Split it off `pile-materialise` onto its own
   branch and merge it before anything else, then run a free re-cluster so
   the piles are final before any playlist is created. Materialising then
   re-clustering strands materialisation records (review finding I3);
   bring that finding's sweep-to-history fix along. After the fix: 8
   piles; untagged grows 36 → 55, which is correct-by-design — the 14
   artists losing signal had geography-only tags.
2. **Queue + governor** go behind the same review gate as everything else
   (spec → plan → subagent execution → adversarial review).
3. **First real run is attended**, with the dashboard open.

## Review findings carried into this design

From the pile-materialise adversarial review:

- **I1** (no confirmation/cancel on big spends): the queue's confirm gate
  plus instant pause/cancel largely resolves it. Keep the single-chunk
  action one-click.
- **I2** (displayed price is a floor, not a ceiling): `Spotify.request`
  retries a transient 429 up to 3×, and each attempt hits `_spend_budget`
  — measured 11 confirmed → 16 charged. **Disclose** this wherever a
  price is shown; do **not** change the retry behaviour.
- **I3** (orphaned materialisation records): records keyed by positional
  pile id become invisible after a re-cluster shrinks piles — sweep
  orphans to `materialised_history` when carrying forward.
- **Stoplist verdicts** (independently confirmed, keep as-is): geography
  terms added; `world` / `world music` / `ethnic` KEPT — the only
  surviving tag for 10 artists; removing them moves 21 tracks to untagged
  and fixes nothing. `bollywood` kept deliberately (place-derived but a
  real genre). Five unevidenced city terms are harmless — matching is
  exact, so `detroit techno` survives.

## Non-goals

- No change to `Spotify.request` retry behaviour (I2 is disclosure-only).
- No raising of `WINDOW_CAP`, `DAILY_CAP`, or `BACKGROUND_DAILY_CAP`.
- No spend controls in boxdash.
- No automatic resume after a quota trip, ever.
- No merging of `pile-materialise`'s one-shot delivery path.

## Acceptance sketch

- Full suite green in **both orderings** (`pytest -q` and
  `pytest -q -p no:randomly $(ls -r tests/test_*.py)`) plus
  `node tests/ui_harness.mjs`.
- A boot-to-traffic test proves no self-start.
- Governor behaviour (escalation ladder, 429 halving, quota stop,
  midnight sleep, reserve line) pinned by tests at the `Spotify.request`
  wire level — no method-level fakes; verdicts by execution, not reading.
- Zero live Spotify calls during development; the first attended run is
  the integration test, and `spx budget` is stated before and after it.
