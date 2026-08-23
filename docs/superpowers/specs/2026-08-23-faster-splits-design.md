# Faster splits — batched materialisation, a Split tab, split-output naming

Design, 2026-08-23. Written from the decisions agreed section by section in
the 2026-08-23 session (handoff: `2026-08-23-faster-splits-handoff.md`).
Every decision below was explicitly agreed with the user; this document
records them, it does not reopen them. All code references verified against
the working tree on 2026-08-23.

## Problem — the cost model was wrong by 50x

`2026-08-18-queued-materialiser-design.md` was built on the premise that the
Feb-2026 dev-mode API has no batch add, pricing the `{teh bomb}` split at
~1240 calls (one POST per track). Probed empirically on 2026-08-23 against a
scratch playlist (created and deleted; ~9 calls total, spent through
`Spotify(store)` so the shared ledger counted them):

```
POST   2 uris -> ACCEPTED, response keys: ['snapshot_id'], both landed
POST 100 uris -> ACCEPTED, all 100 landed
POST 150 uris -> REFUSED: 400 {"message": "Too many ids requested"}
```

**The batch limit is 100** — the ordinary Spotify limit, available all
along. The `{teh bomb}` split (1,231 tracks, 8 piles) cost 1,239 calls and
four days; batched it would have been 24 calls (16 add-batches + 8 creates)
and a couple of minutes.

Consequences this design addresses:

1. The materialiser should add up to 100 tracks per call.
2. With splits taking minutes instead of days, splitting becomes a routine
   activity and deserves a top-level tab, not a button buried in Playlists.
3. Split outputs multiply, so their names must carry the source playlist's
   name (folders are impossible — see Non-goals).
4. The pacing governor's "measure the ban band" ambition dies with the
   traffic that measurement needed, and should be retired honestly.

## Design

### 1. Batched adds (approach A — reconcile against the playlist)

Three approaches were weighed; **A** was chosen. Rejected: **B**
(write-ahead in-flight record — same correctness, more state, another
half-written case to handle) and **C** (batches of 10 — bounds the blast
radius to 9 tracks but gives up most of the win and solves nothing in
principle).

**The change.** `_materialise_tick` (`app.py:1794`) currently takes
`plan["missing"][0]` and spends one call on one track. It takes
`plan["missing"][:100]` and spends one call on up to a hundred:

- `Spotify.add_to_playlist` (`spotify.py:735`) grows a list form — it
  already sends `json={"uris": [uri]}`, so the wire shape is unchanged;
  only the singular parameter is.
- `_claim_materialisation` (`app.py:1723`) grows `added_uris: list[str]`
  beside the existing `added_uri`, appending each landed uri not already in
  `record["added"]`. Note `_readopt_materialisation` (`app.py:1911`)
  **already takes a list** (`added_here`), so the lost-record path is
  batch-ready as it stands.
- `_materialise_plan` (`app.py:1687`) cost becomes
  `ceil(len(missing)/100) + (1 if need_create)` — it is the single place
  cost is computed, shared by the price shown and the price echoed back, so
  nothing else needs repricing.
- The tick's return contract (`{"spent", "done", "gone"}`) is unchanged;
  `spent` stays the number of Spotify calls, not tracks.

**Everything guarding the current loop stays unchanged**: the record
written *before* the create call (the comment at `app.py:1843` — the
create/record gap is where this project's stray playlists came from), the
claim CAS, `_pending_materialise` single-writer, `_readopt_materialisation`,
`_abandon_unrecorded_playlist`, the fingerprint staleness check.

**Reconciliation is the point of approach A.** The one-track loop could
trust `record["added"]` across an interruption because at most one track
was ever in flight, and the CAS wrote it down the moment it landed. A
100-track batch cannot: if the process dies between the POST returning and
the CAS, up to 100 landed tracks are unrecorded — and Spotify permits
duplicate tracks, so a naive retry really would double them up. The
mitigation: when the worker picks up a pile whose record has a
`playlist_id` that **this process has not itself verified**, its first act
is to read the destination playlist (`Spotify.playlist_tracks`, `spotify.py:666` —
paginated at 100, so `ceil(n/100)` calls) and set `added` to what is
*actually there*. Only then is `missing` computed. A crash mid-batch can
then neither duplicate nor skip: the record is never trusted across an
interruption.

Details of the reconcile step:

- It runs once per worker pickup of a resumable pile, not once per tick.
  Within one uninterrupted run the record is trustworthy (single-writer,
  CAS), so ticks after the first proceed on the record alone.
- Its reads are budgeted calls like any other (`bulk=True`), and the
  displayed price for resuming a partial pile includes them.
- A fresh pile (no `playlist_id` at all) skips it — nothing has been
  created, so there is nothing to reconcile and nothing to duplicate.
- **An empty `added` does NOT exempt a record that has a `playlist_id`.**
  An earlier draft of this section said the trigger was a `playlist_id`
  *and* a non-empty `added`; that was wrong, and wrong on exactly the crash
  this whole section exists for. The create CAS records `playlist_id` with
  `added: []`; the very next tick POSTs up to 100 tracks; a death between
  that POST returning and its CAS leaves a record that reads as empty while
  100 tracks are already on Spotify. Skipping the read there re-posts the
  whole batch and doubles it. The rule has no exception: any record with a
  `playlist_id` this process has not verified is read before anything is
  added. (Corrected 2026-08-23, fix round 1 of Task 3; the implementation
  in `_materialise_plan` follows this text, not the earlier one.)
- Reconciliation intersects with the pile's uris: tracks in the destination
  that the pile never contained (the user edited the playlist by hand) are
  left alone and not counted as `added`.

### 2. Split as a top-level tab

`view-split` already exists as a full view (progress, queue panel, pile
list, re-cluster controls). Today it is reachable only from the per-row
**Split** button in Playlists (`app.js` `openSplit` → `show("split")`),
with `‹ Back` returning there. The eligibility rule already exists at
`app.js:216`: not Liked and `total >= 100`, with `splitDisabledReason`
disabling (not hiding) unowned playlists so the fix is teachable on hover.

- Add `<a id="nav-split">Split</a>` to `<nav>` (`index.html:23–27`,
  currently Now / Playlists / Reconnect), wired to `show("split")`.
- The tab opens on a **picker** of splittable playlists — same eligibility
  rule, reusing the cached listing (zero API calls) — with any in-progress
  split pinned at the top. Selecting one enters the existing view
  unchanged; `‹ Back` returns to the picker rather than to Playlists.
- The per-row Split button in Playlists stays as a shortcut: it sets the
  selection and switches tabs, exactly what `openSplit` does today.

**Judgement recorded:** with batching, a split takes minutes rather than
days, which weakens the case for the queue panel being prominent. Build the
tab — the picker is the useful part — but keep the queue panel understated.

### 3. Naming of split outputs

The source playlist's name goes in the title, joined with the same `·`
separator the pile names already use, so the whole title reads as one
chain:

```
{teh bomb} · jazz · funk · Bossa Nova
{teh bomb} · reggae · roots reggae · dub
{teh bomb} · untagged
```

**Checked against the live rule set (the handoff flagged this as
mandatory; verified 2026-08-23):** these titles trip nothing.

- `naming.violations` (`naming.py:41`) only inspects playlists marked
  input or home. Split outputs are neither — they are created unmarked and
  nothing marks them.
- The input pattern (`^\[.+\]$`, fullmatch) does not match a `{source} · …`
  title, so the pattern union will not re-read one as an input.
- The home-exclude markers (`^\{.*\}$` etc., fullmatch in
  `home_name_excluded`, `folders.py:24`) require the name to *end* with
  `}`; `{teh bomb} · jazz` does not fullmatch. This is moot for unmarked
  playlists anyway, and the only consequence if the user ever marks one as
  a home by hand is the ordinary ALL-CAPS rename proposal — same as any
  other lower-case home.

Three details settled in advance:

- **Truncation:** Spotify caps playlist names (100 chars). The source name
  is what groups the outputs, so it survives whole; the *pile* half is
  truncated (with an ellipsis) when the pair is too long.
- **Existing playlists:** the eight from `{teh bomb}` are named without the
  prefix (see the table in the handoff for ids). The API can rename
  (`Spotify.rename_playlist`, `spotify.py:598`, one call each). **Leave
  them alone by default**; offer renaming as a separate explicit action —
  eight calls, user-initiated, priced and confirmed like any other spend.
- The name is fixed at create time from the source playlist's name then.
  A later rename of the source does not ripple into existing outputs.

### 4. Pacing — retire the measuring ambition, keep the governor

`sortify/pacing.py`'s docstring frames the module as a measuring
instrument: the ~1240 calls of real traffic were "the one legitimate chance
to measure" the band between 1.8/min (known good) and 9.7/min (known ban).
Batching removes that traffic — 24 calls will never climb a ladder that
needs 15 clean minutes per rung.

- **The governor stays.** It still paces the remaining calls correctly,
  still halves on a 429, still honours the 7.0/min ceiling. At 24 calls per
  split the ladder simply never escalates, which is fine.
- **The docstring's ambition is retired**, rewritten to describe what the
  module now is: a rate limiter with a measured floor, not an instrument.
  Left as-is it misleads the next reader into protecting a measurement that
  can no longer happen.
- `data/pacing.json` keeps its shape (boxdash reads it); `max_clean_rate`
  stays as the historical record of what was measured (4.0 — and the
  handoff explains why it stuck there: `note_interruption` reset every new
  pile's worker to `START_RATE`, so the ladder never got a long enough
  clean stretch to beat itself).

### 5. boxdash contract — unchanged shapes, no version bump

boxdash's sortify-queue card reads `data/queue.json` and `data/pacing.json`
(both `version: 1`) and treats an unknown version as "no card". This design
changes **no field shapes**, so the version stays 1. What changes is
semantics the card tolerates naturally:

- `progress.track` jumps in steps of up to 100 rather than 1.
- A whole split can finish inside the card's 20-second poll; the card
  simply shows the terminal state.

Fields the card relies on and which must keep meaning what they mean:
`state`, `stop_reason`, `progress.{pile_id, pile_index, pile_count, track,
track_total, spent_today, bulk_today, daily_cap, reserve}`, `updated_at`;
from pacing: `rate_per_min`, `ceiling`, `max_clean_rate`, `history_429`.
If a future change does alter a shape, bump the version — the silent-card
failure is correct but silent, so it must be deliberate.

### 6. Correcting the 2026-08-18 spec

The old spec is not deleted or rewritten — its machinery decisions (claim
tokens, record-before-create, no self-start, bulk budget class, boxdash
surfacing) all still stand. It gets a dated correction note at the top
stating that its "no batch add" premise, its ~1240-call cost model, and its
"one legitimate chance to measure the ban band" framing were overturned by
the 2026-08-23 probe, pointing here. Left silently wrong it misleads the
next reader into re-deriving the one-call-per-track design.

## What this obsoletes, named rather than left behind

Built to manage a four-day job that now takes minutes — all keep working,
none are worth extending:

- The auto-resumer and stall detection (still correct, rarely exercised).
- The pacing escalation ladder (never escalates at 24 calls; kept as the
  rate limiter it also is).
- The midnight sleep at the `BULK_RESERVE` line (a 24-call job under an
  850-spendable budget will essentially never hit it; the guard stays).
- boxdash's `track j/m` granularity (jumps in 100s; still truthful).

## Non-goals

- **No folders.** The Web API has no folder endpoints — no create, move, or
  rename anywhere in `spotify.py`. sortify's folder *knowledge*
  (`data/folders.json`) comes from a desktop-client extract and could tell
  the user where a source playlist lives, but sortify cannot put outputs
  anywhere. Titles are the grouping mechanism.
- No renaming of the existing eight `{teh bomb}` outputs by default.
- No change to `Spotify.request` retry behaviour, `WINDOW_CAP`,
  `DAILY_CAP`, `BACKGROUND_DAILY_CAP`, or `BULK_RESERVE`.
- No automatic resume after a quota trip, ever (unchanged from the old
  spec).
- No prominent redesign of the queue panel — understated, per the
  judgement above.
- No batch *delete* — the probe tested add only; `remove_from_playlist`
  and playlist clearing keep their current costs until separately probed.

## Sequencing and cautions

1. **Coordinate before touching the materialiser loop.** sortify is under
   active development by other sessions (five commits on 2026-08-21 alone,
   with service restarts mid-run). Check `git log` and running agents
   first.
2. Batching (§1) lands first — it is the change with the value. Tab (§2)
   and naming (§3) are independent of it and of each other.
3. Docstring retirement (§4) and the old-spec correction (§6) ride along
   with §1.
4. Any further live probing goes through `Spotify(store)` so the shared
   ledger counts it, and `spx budget` is stated before and after.

## Acceptance sketch

- Full suite green in both orderings (`pytest -q` and
  `pytest -q -p no:randomly $(ls -r tests/test_*.py)`) plus
  `node tests/ui_harness.mjs`.
- Zero live Spotify calls during development; tests pin behaviour at the
  `Spotify.request` wire level, no method-level fakes.
- Batch tests to pin: a 250-missing pile costs 3 add calls; a resumed pile
  reconciles from the wire-level playlist read before computing `missing`
  — with an empty `added` as much as a partial one; a reconcile that finds
  landed-but-unrecorded tracks does not re-add them; a pile with no
  `playlist_id` skips reconciliation; `spent` counts calls, not tracks.
- Naming tests to pin: `{source} · pile` titles produce zero
  `naming.violations` rows unmarked; truncation preserves the source name
  whole; the existing-outputs rename action is priced at one call each and
  never runs unbidden.
- Tab tests to pin (ui_harness): picker lists exactly the
  split-eligible playlists; in-progress split pinned first; `‹ Back` from
  the split view returns to the picker; the Playlists-row Split button
  still lands in the same view.
- The no-self-start guarantee (`tests/test_no_proactive_work.py`) stays
  green — reconciliation reads happen only inside a worker a click created.
- First real batched run is attended, with `spx budget` stated before and
  after.
