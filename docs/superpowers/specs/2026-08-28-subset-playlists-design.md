# Subset playlists — non-exclusive selections that suggest themselves

Design, 2026-08-28. Approved in the 2026-08-28 session. Every decision below
was explicitly approved by the user; this document records them, it does not
reopen them.

Supersedes the deferred-subset appendix of
`2026-08-21-create-home-playlists-design.md`, which surveyed the idea and
recorded the measurements. Two decisions from that appendix are **reversed
here on the user's instruction**: subsets DO get suggestions (the appendix
had them as manual targets only), and the identifying convention is `{}`
alone (the appendix's rule also swept in emoji-prefixed names).

## Problem

Homes are mutually exclusive: a song has exactly one, and filing it there is
the decision sortify exists to help make. Subsets are the other thing the
user's library does — `{solfest}`, `{tøft}`, `{ny jazz}` — curated selections
that any song can join, including songs that already have a home. Nothing in
sortify knows they exist. Today they are invisible to the suggestion engine,
absent from the Now card, and reachable only by leaving for the Spotify
client.

Two things are wanted, in this order:

1. After a song has been filed to its home, if it is a strong match for a
   subset, offer it — the home decision first, the optional one after.
2. A separate button for putting the playing song into any subset by hand.

## Measured context (2026-08-28, local cache read, zero API calls)

- **70 editable `{}` playlists**, 71 including one not owned by this account.
- **Zero overlap** with `home_ids` or `input_ids` — the role space is clean,
  so nothing has to be un-marked to make room.
- 4429 tracks across them; **median 22**, three above 200, one outlier:
  `{teh bomb}` at 1231.
- **1 of 70** has its tracks cached, so making one scorable costs a read.

`playlist_tracks` paginates at 100, so warming a subset costs
`ceil(total/100)` calls: 1 for the median, 13 for `{teh bomb}`.

## Decisions

### 1. Role: opt-in, name-gated

New config key `subset_ids` — the list of subsets that may suggest
themselves. New key `subset_name_pattern`, default `^\{.*\}$`, mirroring the
existing `input_name_pattern`: only a playlist whose name matches may be
marked. The Playlists view grows a third chip beside In/Home for eligible
rows.

**Opting in gates suggestion, not reach.** A subset that is not opted in
still receives songs through the button in §5; it simply never proposes
itself. This separation is the whole point of the opt-in: it exists so that
finished one-offs (`{project 17}`, `{utforsk #4}`) stay filable without
competing for attention with standing selections.

`_effective_subset_ids(cfg, playlists)` resolves the marked ids down to those
that are editable, match the pattern, and are neither inputs nor homes — the
same shape as `_effective_input_ids`.

**Binding invariant, with a test:** every name matching
`subset_name_pattern` must also be rejected by `home_name_excluded`. `{}` is
already in `home_name_exclude_patterns`, so this holds today; the test exists
so the two rules cannot drift apart later and let one playlist be both a home
and a subset.

### 2. Profiles: the same mechanism homes use

`_ensure_profiles_locked` builds `subset_profiles` for the opted-in subsets
exactly as it builds home profiles — `_cached_tracks(id, snapshot_id)`,
snapshot-keyed, so the warm cost is zero and a subset is re-read only when
its snapshot moves.

Cold cost is one call per subset (13 for `{teh bomb}`), paid on the first
profile rebuild after opting in. This is the contract homes already have.
The Playlists view states the pending cost before the save that incurs it.

Opt-in bounds *which* subsets can ever cost anything, but not *how many* get
marked in one save — a chip tap per row and a single Save can opt in all 70
at once, and `set_config` clears `_profile_state`, so the next `/api/now`
poll would pay the whole cold cost inline. That is exactly the traffic
shape (a burst inside a poll, not spread across the day) that earned this
project its quota trips, so a guard is needed despite the opt-in: a new
`SUBSET_WARM_BUDGET` (25 calls) caps the cold cost a single config save may
introduce. `POST /api/config` refuses a save whose newly-marked, not-yet-cached
subsets would exceed it, naming the count, the cost, and the remedy (mark
fewer, save, let them warm, then mark more); `_ensure_profiles_locked` carries
the same check as a backstop, mirroring the `>40 candidate homes` guard's
shape, so no other path to a profile rebuild can reach the same cost.

### 3. Scoring: `suggest.py` is not modified

`sugg.suggest()` takes a `profiles` dict and knows nothing about homes.
Subsets are a second profile set through the same function, with the same
signals, the same corpus, and the same measured constants. **No change to
`sortify/suggest.py` is part of this work** — the tuned invariants
(artist-overlap primacy, `MIN_SCORE`, the neighbour ceiling) are untouched
because nothing about them is being asked a new question.

Two caller-side differences:

- **The weak tier is dropped.** `suggest()` returns sub-threshold guesses
  (`weak: True`) when nothing clears `MIN_SCORE` and the track is filed
  nowhere. That tier exists to force a decision that must be made; a
  curated selection is optional by nature, so a guess is noise. Subsets
  appear only when they clear `MIN_SCORE` or when the track is `already` in
  them.
- **Two, not three.** Homes show `TOP_N` = 3. Subsets show at most 2: the
  post-file card is a "done, moving on" moment, and three further decisions
  there work against it.

### 4. When the row appears

Both of:

- **immediately after filing to a home** — the state the card is already in
  (`✓ filed to X`), which is the user's stated flow; and
- **when the playing track is already in a home** from any earlier session.

**The server computes, the client decides when to show.** This split is
forced by a real gap: `_profile_state`'s profiles hold a `uris` set captured
at build time, and nothing updates it when `/api/act` files a track. So in
the seconds after a filing the server still believes the track is not in that
home, and a server-side gate would suppress the subset row at exactly the
moment the user asked for it.

Therefore `/api/now` returns the subset matches whenever there are any —
local arithmetic over cached data, no extra cost — and the client shows the
row when **either** it filed this track this session (its own `filedUris`)
**or** the payload flags a home as `already`.

A track with no home shows no subset row at all. The home decision comes
first; that ordering is the feature.

Subsets the track is **already in** are shown as a muted line ("already in
`{solfest}`"), not a button. Without it there is no feedback that a song is
already in a selection, and the user re-checks by hand.

### 5. The button

`Add to subset…` sits beside `Add to…` in the Now card's minor row and opens
the existing picker, scoped to **all** `{}` playlists — not just the opted-in
ones (§1).

### 6. Filing into a subset is not filing

- The action sends `from_id: null`. A song put into a best-of has not been
  sorted; it must not leave the input it came from.
- It never sets the client's filed state. The card keeps whatever it had:
  home suggestions if the song is unfiled, `✓ filed to X` if it was just
  filed.
- **`/api/act` grows a server-side guard**: a request pairing a `from_id`
  with a subset `to_id` is a 400. Zero calls to enforce, and it makes the
  rule structural rather than a property of one caller.

### 7. Prerequisite: the undo stack must stop guessing

`btn-undo-now` pops the last key of `filedUris`
(`sortify/static/app.js`). A subset add writes no such key, so undoing one
would clear an unrelated song's filed state. This is a live bug that subsets
would expose, so it is fixed as part of this work, not after it: the client
keeps an ordered log of `{uri, kind}` and clears `filedUris[uri]` only for
`kind === "home"`.

`undoRemoval` (added 2026-08-28 for the strip's remove button) already
targets its exact uri and is unaffected.

## Scope

**In:** the Now card. **Out:** the triage view — a different rhythm (batch,
no playback), and doubling the surface before the feature has been used once
is how both halves end up half-right. It can be added later against a
payload that already exists.

**Also out:** creating subsets from inside sortify. `POST /api/playlists/create`
takes an explicit `role` and refuses anything but `"home"` today; extending
it to subsets is a small, separate change that this design deliberately does
not bundle.

## Files

| File | Change |
| --- | --- |
| `sortify/folders.py` | `is_subset_name(name, pattern)`, beside the other name rules |
| `sortify/app.py` | `_effective_subset_ids`; `subset_profiles` in `_ensure_profiles_locked`; `subsets` + `subset_targets` in `/api/now`; `/api/act` guard; `subset_ids` in `/api/config` |
| `sortify/static/app.js` | subset row, `Add to subset…`, the ordered undo log |
| `sortify/static/style.css` | subset row and already-in line |
| `sortify/static/index.html` | nothing expected; the row is rendered by app.js |
| `tests/` | new `test_subsets.py`; `tests/ui_harness.mjs` gains a subset scenario |

## Tests

All zero-Spotify-call.

- **Name rule**, pure: `{x}` matches, `[x]`/`<x>`/`__x__`/plain/emoji do not.
- **The drift invariant**: every name matching `subset_name_pattern` is also
  rejected by `home_name_excluded`.
- **Resolution**: `_effective_subset_ids` drops ids that are not editable,
  not `{}`-shaped, or are also inputs or homes.
- **Payload**: `/api/now` carries at most 2 subset matches, never a `weak`
  entry, and flags already-in subsets. It does **not** gate on whether the
  track has a home — that gate is the client's (§4).
- **The guard**: `/api/act` with a `from_id` and a subset `to_id` is a 400,
  and spends nothing.
- **Undo log**: a subset add followed by undo restores the subset add and
  leaves an unrelated track's filed state intact — the regression §7 exists
  to prevent.
- **Harness**: the subset row renders in the filed state and for a track
  flagged `already` in a home; it does not render for a homeless track even
  when the payload carries matches; and filing into a subset leaves
  `filedUris` untouched, so the card does not fall into its done state.

## Budget

Nothing bulk, nothing background, nothing on the polling path beyond what
homes already cost. Opting in a subset costs `ceil(total/100)` calls once,
on the next profile rebuild, at the user's explicit action and with the cost
stated before the save. Filing into a subset is one call. Suggestion scoring
itself is local arithmetic over cached data.
