# Handoff — faster splits, a Split tab, and split-output naming

**Date:** 2026-08-23
**Status:** design agreed in conversation, section by section. **Spec not yet
written.** No code changed in sortify. Next step is to write the design doc
proper (this file is the handoff, not the spec) and then an implementation plan.

---

## The headline finding, which overturns an existing spec

`docs/superpowers/specs/2026-08-18-queued-materialiser-design.md` line 13 says:

> Feb-2026 dev-mode API has no batch add, so that is **~1240 calls** — one
> POST per track plus playlist creation overhead.

**This is wrong.** Probed empirically on 2026-08-23 against a scratch playlist
(created and deleted, ~9 calls total across two probes):

```
POST  2 uris -> ACCEPTED, response keys: ['snapshot_id'], both landed
POST 100 uris -> ACCEPTED, all 100 landed
POST 150 uris -> REFUSED: 400 {"message": "Too many ids requested"}
```

**The batch limit is 100** — the ordinary Spotify limit, available all along.

Consequence: the `{teh bomb}` split (1,231 tracks, 8 piles) cost **1,239 calls
and four days**. Batched it would have cost **24 calls** (16 batches + 8
creates) and a couple of minutes. Roughly 50x.

The 2026-08-18 spec should be corrected rather than left to mislead the next
reader. Its cost model, its pacing rationale, and its "one legitimate chance to
measure the ban band" framing all rest on the false premise.

---

## Agreed design

### 1. Batching (approach A — reconcile against the playlist)

Three approaches were weighed; **A** was chosen.

- `_materialise_tick` (`sortify/app.py:1794`) currently takes
  `plan["missing"][0]` and spends one call. It takes `plan["missing"][:100]`
  and spends one call for up to a hundred tracks.
- `_claim_materialisation` grows an `added_uris` parameter beside its existing
  singular `added_uri`. Note `_readopt_materialisation` **already takes a
  list**, so the landed-but-unrecorded path is close to batch-ready.
- `_materialise_plan` cost becomes
  `ceil(len(missing)/100) + (1 if need_create)`.
- **Everything guarding the current loop stays unchanged**: record written
  *before* the create call (the comment at app.py:1843 explains why — stray
  playlists), the `claim` CAS, `_pending_materialise` single-writer,
  `_readopt_materialisation`, `_abandon_unrecorded_playlist`.

**Reconciliation is the point of approach A.** When the worker picks up a pile
that already has a `playlist_id` and a non-empty `added`, its first act is one
read of the destination playlist (`ceil(n/100)` calls) and `added` is set to
what is *actually there*. Only then is `missing` computed. So a crash mid-batch
can neither duplicate nor skip: the record is never trusted across an
interruption. Spotify permits duplicate tracks, so a naive retry after a
half-landed batch really would double them up — this is the mitigation.

Rejected: **B** (write-ahead in-flight record — same correctness, more state,
another half-written case) and **C** (batches of 10 — bounds the blast radius
to 9 tracks but gives up most of the win and solves nothing in principle).

### 2. Split as a top-level tab

`view-split` already exists as a full view (progress, queue panel, pile list,
re-cluster controls). It is only reachable from the per-row **Split** button in
Playlists, with `‹ Back` returning there. Eligibility rule already exists in
`app.js:216`: not Liked, and `total >= 100`.

- Add `<a id="nav-split">Split</a>` to `<nav>` (index.html:23), wire to
  `show("split")`.
- The tab opens on a **picker** of splittable playlists (same eligibility rule),
  with any in-progress split pinned at the top. Selecting one enters the
  existing view unchanged; `‹ Back` returns to the picker, not to Playlists.
- Keep the per-row Split button as a shortcut — it now sets the selection and
  switches tabs.

**Judgement recorded:** with batching, a split takes minutes rather than days,
which weakens the case for the queue panel being prominent. Build the tab (the
picker is the useful part) but keep the queue panel understated.

### 3. Naming of split outputs

**Folders are impossible.** See the correction section below for the precise
statement — the short version is that the Web API has no folder endpoints, so
sortify cannot create a folder or file a playlist into one.

So the source playlist's name goes in the title, using the same `·` separator
the pile names already use, so the whole title reads as one chain:

```
{teh bomb} · jazz · funk · Bossa Nova
{teh bomb} · reggae · roots reggae · dub
{teh bomb} · untagged
```

Three details settled in advance:

- **Truncation:** Spotify caps playlist names. The source name is what groups
  them, so it survives; the *pile* half is truncated if the pair is too long.
- **Existing playlists:** the eight from `{teh bomb}` are named without the
  prefix. Renaming is one call each and the API *can* rename playlists. Leave
  them alone by default; offer it as a separate explicit action.
- **`naming.py` interaction:** house rules enforce ALL-CAPS homes, bracketed
  inputs, emoji-prefixed derived lists. A `{teh bomb} · …` title must not trip
  them, or every split output becomes a naming violation on creation. **This
  needs checking against the live rule set — it is not an afterthought.**

---

## A correction worth carrying forward

I first told the user "the Web API has no notion of folders, so sortify can't
see yours either". The first half is right; the second is wrong, and they
challenged it correctly.

- The Web API genuinely has no folder endpoints — no create, move or rename
  anywhere in `spotify.py`. This is what blocks the original request.
- **But sortify's folder knowledge never came from the API.**
  `extract_folder_map` (`folders.py:82`) walks JSON produced by
  [`spotify-folders`](https://github.com/mikez/spotify-folders), which reads the
  *desktop client's local cache*. `app.py:252` parses it, `save_folders` stores
  `{playlist_id: {path, caps}}`.
- **Update 2026-08-23:** `data/folders.json` is populated again (1010 entries,
  all 68 homes covered). It had been wiped to `{}` in the 2026-08-21 live-data
  clobber and was restored from the spotify-backup snapshot at
  `~/kode/spotify-library/folders.json`. See "Playlist folders" in CLAUDE.md
  for the full mechanism.
- Homes are actually identified by `cfg["home_ids"]` — an explicit configured
  list — with the ALL-CAPS convention as a heuristic for *suggesting*
  candidates. Folders are only a hint layered on top when the extract exists.

With the extract in place sortify can *tell* the user where a source playlist
lives. It still cannot put the outputs there — only the desktop client can.

---

## Pacing: retire the ambition honestly

`sortify/pacing.py` is a deliberate measuring instrument. Its docstring:

> 678 calls @ 9.7/min earned a ~23h quota ban; 1208 @ 1.8/min was penalty-free.
> The band between is unmeasured, and this job's ~1240 calls of real traffic are
> the one legitimate chance to measure it.

Batching removes the traffic that measurement needed — 24 calls will never climb
a ladder that needs 15 clean minutes per rung.

**Leave the governor as it is.** It still paces the remaining calls correctly and
still halves on a 429. But the docstring's measuring ambition no longer
describes reality and should be retired rather than left to mislead.

Also worth knowing: `note_interruption()` clamps the rate back to `START_RATE`
(1.8) on "pause, midnight sleep, quiet period, or process restart". Every new
pile starts a fresh worker, so **every pile re-climbed the ladder from 1.8**.
That is why four days of runs left `max_clean_rate` stuck at 4.0 — the
measurement never got a clean stretch long enough to beat itself.

---

## Current state (all verified 2026-08-23)

**The `{teh bomb}` split is complete** — 8/8 piles, 1,231/1,231 tracks, across
four days, with **zero 429s** despite two prior bans on this account.

| pile | tracks | playlist |
|---|---|---|
| p7 · prog · classic · psychedelic | 302 | `0tIoKnsTsx07mN9jykca09` |
| p1 · jazz · funk · Bossa Nova | 251 | `3hdbSbEkfeXkCpLDsvIlyo` |
| p5 · funk · soul · afrobeat | 250 | `6Ifr9qcLY57FAzClHYe6ZB` |
| p4 · house · electronic · deep | 145 | `4xJchaAx4p02JpoGPhopbM` |
| p6 · hip-hop · rap · underground | 98 | `2qZkiywnMl6fhtKomxbmbG` |
| p2 · reggae · roots · dub | 71 | `4l18iKlGF44uZvdAsXwoD9` |
| p3 · cumbia · latin · salsa | 59 | `7GpRNsZIxV9Xf94cvv2r6r` |
| untagged | 55 | `2czJQx6646w8x9F1fllMWe` |

Source: `3km9EmUcfrlQKKqRincV6T` (`{teh bomb}`, 1,231 tracks).

`DAILY_CAP` was raised 600 → 1000 by someone during 2026-08-21;
`BULK_RESERVE` is still 150, so the spendable figure is **850**, not 450.

---

## Cautions for whoever picks this up

**sortify is under active development by other sessions.** Five commits on
2026-08-21 alone, with service restarts mid-run. A change to the materialiser's
core loop will collide unless the timing is coordinated. Check `git log` and
running agents before starting.

**This change makes recent work obsolete, and that is fine.** boxdash's
sortify-queue card reports `track j/m` — it keeps working but will jump in steps
of 100 and a whole split will finish inside its 20-second poll. The
auto-resumer, the stall detection, the pacing ladder: all built to manage a
four-day job that will now take two minutes. Right outcome; worth naming rather
than quietly leaving behind.

**Boxdash's card reads two files and must keep working.**
`~/kode/sortify/data/queue.json` and `pacing.json`, both `version: 1`. If
batching changes their shape, bump the version — boxdash treats an unknown
version as "no card" rather than guessing, which is the correct failure but a
silent one. Fields it relies on: `state`, `stop_reason`, `progress.{pile_id,
pile_index, pile_count, track, track_total, spent_today, bulk_today, daily_cap,
reserve}`, `updated_at`, and from pacing `rate_per_min`, `ceiling`,
`max_clean_rate`, `history_429`.

**Probes cost real quota.** Both batch probes went through `Spotify(store)` so
the calls were counted in the shared ledger rather than sneaking around it. Do
the same for any further probing.
