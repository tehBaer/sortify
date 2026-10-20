# Split a home into parts — design

Date: 2026-10-20. Status: draft for the user's review.
Replaces: the Split tab of 2026-08-17 (piles, sittings, the queued
materialiser) — see "What goes away".

## Why

A home playlist grows too large or too unfocused over time. The user wants
to split it into a number of parts they choose, start from a suggested
grouping, adjust it by hand on the phone, and only then write anything to
Spotify. The old Split tab does none of that: it picks its own number of
piles, shows piles but not songs, offers no way to move a song between
piles, and works through piles by listening to temporary "sitting"
playlists.

## Decisions taken with the user

- Replaces the old Split tab entirely, sittings included.
- Only **homes** can be split; every part becomes a home.
- The user picks the number of parts (2–6). Part sizes **follow the
  music**; they are not balanced.
- Sortify also tries one fewer and one more part and says so when one of
  those splits clearly cleaner.
- Parts can be added (empty) and removed by hand.
- Phone layout: one part at a time, part chips pinned on top as drop
  targets. Song rows stop short of the right edge: a swipe in that free
  strip scrolls. Touching a song and moving drags it at once; a tap
  without moving plays its preview.
- **Nothing is written to Spotify until Done.** Before Done sortify only
  reads (the track list).
- At Done the original is either **kept as a subset** (🐾, keeps all its
  songs) or **becomes one of the parts** (keeps its name; the other parts'
  songs are removed from it in bulk). No rename of the original is offered.
- Grouping uses all three local Last.fm signals combined (approach 1).

## The flow

1. **Pick a home.** The Split tab lists homes with their track counts and
   a filter box. Homes with fewer than 10 tracks are listed but disabled
   ("too small to split").
2. **Pick a number** (2–6, starts at 2) and press **Suggest**.
   - The track list is read with the existing snapshot cache
     (`_cached_tracks`): 0 Spotify calls when unchanged, else ~1 per 100.
   - Last.fm data missing for any of the songs is fetched first, in the
     background, with a progress line ("looking up 37 songs on Last.fm…").
     Last.fm only, never Spotify.
3. **The parts screen.**
   - Chip row pinned at the top: one chip per part, `name · count`, the
     shown part highlighted, and a **+** chip at the end.
   - Below: the shown part's songs, `title — artist`, about 80% of the
     width, left-aligned; the right strip is empty and scrolls the list.
   - Touch a song and move: it lifts immediately and follows the finger;
     release over a chip moves it to that part; release elsewhere puts it
     back. The chip under the finger highlights.
   - Tap a song (press and release without moving): plays its 30 s preview.
   - Songs with no Last.fm data carry a small "?".
   - A hint line when a neighbouring count splits clearly cleaner:
     "This splits more cleanly into 3 — try 3?" Tapping it re-suggests.
   - **Re-suggest** (with the same or another number) discards manual
     moves and added parts, and asks first if there are any.
   - Every change is saved on the box at once; leaving and coming back
     restores the split exactly.
4. **Add a part:** the **+** chip makes an empty part named "New part" and
   shows it; the name is edited on the Done screen.
5. **Remove a part:** asks first, naming where its songs go
   ("37 songs → A: 20, B: 17"); each song goes to the remaining part it
   fits best. At least 2 parts always remain.
6. **Done** (a button under the chips) opens the Done screen:
   - a name field per part, prefilled with the suggested name;
   - a folder picker, defaulting to the original's folder (the existing
     "create a home" folder list and its targetability rules);
   - the choice for the original: **Keep as subset** (default) or
     **Becomes part …** with a part picker;
   - a warning listing local files, which cannot be added through the API
     and stay in the original only;
   - empty parts are listed as "not created";
   - the cost, e.g. "creates 2 playlists · about 7 calls";
   - **Confirm**.
7. After Done the parts are homes, the split's draft is cleared, and the
   tab shows a short summary with each new playlist's name and count.

## Grouping engine (`sortify/splitparts.py`, pure, no I/O)

Input: the playlist's tracks (uri, name, artists with ids) and the three
local Last.fm stores. Output: exactly N parts (lists of uris), a name per
part, the set of "?" uris, and a cleanliness score per tried N.

1. **Pair score** for songs a, b in [0, 1], combining:
   - artist tags: cosine of the primary artists' cleaned tag vectors
     (`tags.json`, `clean_tags`), 1.0 for the same artist;
   - track similarity: the `match` of b in a's getSimilar list or of a in
     b's (`lastfm_tracks.json`, keyed by `track_key(artist, title)`),
     whichever is higher, else 0;
   - artist similarity: the `match` between the two primary artists in
     `lastfm_artists.json`, else 0.

   Combined as a weighted sum with weights fixed in code (starting point
   0.5 tags, 0.35 track, 0.15 artist), tuned once on the user's real homes
   during the build and then frozen. When two songs share an artist but
   their getSimilar data puts them far apart — both have data, neither
   lists the other, and the Jaccard overlap of their similar-track sets is
   below 0.1 — the artist-tag term is 0.5 instead of 1.0, so a ballad can
   leave its artist's rockers.
2. **Clustering:** average-linkage agglomerative clustering on 1 − score,
   via the nearest-neighbour-chain algorithm (O(n²) time and memory, pure
   Python), stopped at N clusters. Target: a 1000-song home in under
   5 seconds on the box.
3. **No slivers:** a cluster smaller than max(3, 5% of the playlist) is
   folded into its most similar cluster, and clustering continues from
   the next merge level so the result still has exactly N parts.
4. **No data:** songs with none of the three signals are placed with the
   part holding most of their artist's other songs; with no such song,
   in the largest part. All of them are marked "?".
5. **Names:** the tags most distinctive of each part against the others
   (the TF-IDF naming in the old `split._name_piles`, moved here).
6. **Cleanliness and the hint:** for N−1, N and N+1 (within 2–6), score
   the split as mean within-part similarity minus mean between-part
   similarity. The hint names a neighbour only when its score beats N's
   by at least 0.05 (a constant tuned with the weights), so it does not
   flicker.
7. **Deterministic:** same tracks, same stores, same N → same result
   (stable ordering, ties broken by uri).
8. **Best fit for a single song** (used by remove-part and by re-read):
   the part with the highest mean pair score to the song.

## Single-song preview

The existing preview player walks a whole playlist and cannot play one
given track. New: `GET /api/track_preview?uri=…` resolves one track to a
Deezer 30 s clip with the existing Deezer resolver and its cache
(`deezer.json`) and the rejected-recordings list; it returns
`{url}` or 404 "no preview". The client plays it with the same audio and
Spotify-resume handling as the existing player (`POST /api/preview_resume`,
once, when previewing ends). Zero Spotify calls.

## State on the box

`data/splitparts.json`, one draft per home id:
`{playlist_id, snapshot_id, n, parts: [{key, name, uris}], unknown: [uris],
hint, done: null | {...progress...}}`. Every UI action writes it through
small endpoints; the client never holds the only copy.

## Done: order, cost and resume

Before writing anything, Done re-reads the playlist's snapshot (1 call).
If it changed, Done stops and offers **Re-read**:
- moves are kept for songs still there;
- new songs go to their best-fit part, marked "?";
- songs no longer there drop out.

Then, recording each finished step in the draft's `done` field so a
repeat of Done skips it:

1. For each part that becomes a new playlist: create it (1 call), add its
   songs in batches of 100 (1 call per 100), mark it a home exactly as
   `/api/playlists/create` does (`home_ids` and `sticky_home_ids`,
   `remember_playlist`, profile cache reset), and start folder filing
   (`filing.start`, 0 API calls, one playlist at a time).
2. Only when every new part exists and is filled, the original:
   - **Keep as subset:** rename with 🐾 (`roles.mark_subset_name` +
     `sp.rename_playlist`, 1 call) and drop it from `home_ids` and
     `sticky_home_ids`.
   - **Becomes part X:** remove every song that belongs to another part
     with batched `DELETE /playlists/{id}/items` (1 call per batch). It
     stays a home and keeps its name.
3. Clear the draft; show the summary.

Batch removal was probed 2026-10-20 (two tracks in one call, read back).
Its upper limit is unprobed: the build starts with one probe on a
throwaway playlist (100, then 50 if refused; about 5 calls, with the
user's go-ahead) and fixes the batch size from it.

The cost shown before Confirm is computed from the same plan: creates +
add batches + rename or remove batches + the snapshot read. A 300-song
home split in three with the original kept as a subset: 1 + 3 × (1 + 1)
+ 1 = 8 calls.

**Failure during Done** (rate limit, quota stop, network): it stops, the
draft keeps what finished, the original is untouched unless step 2 had
begun, and Done resumes from the first unfinished step. Filing failures
leave the playlist at the top level and say why, as creating a home does
today.

## What goes away

- The old split view, piles and re-clustering: `/api/split/*` endpoints,
  `sortify/split.py` (after moving `_name_piles`, `_vec`, `_cosine` into
  `splitparts.py`), `sortify/redistribute.py`, and their tests.
- Sittings: `/api/split/{id}/sitting*`, `/api/sittings/cleanup`, the
  orphan sweep, the Playlists view's "remove leftover sittings" bar, and
  the Now card's sitting mode (`sittingCardBody`, the `#now-sitting-bar`,
  the `sitting` field of `/api/now`).
- The queued materialiser: `/api/split/{id}/queue*`, its worker and its
  rate governor (`sortify/pacing.py`, `data/pacing.json`) — unless the
  build finds another user of either, in which case they stay.
- `BULK_RESERVE`/`bulk=` budget classes in `spotify.py` stay: other
  callers use them.
- Data: `data/splits.json`, `data/queue.json` and `data/pacing.json` are
  renamed to `*.bak-2026-10-20`, not deleted. Boxdash's "sortify queue"
  card hides itself when the queue file is absent.
- CLAUDE.md: the materialiser, sitting and old-split paragraphs are
  replaced by a short description of this feature.

## Errors and edge cases

- Last.fm unreachable: suggest with what is stored; songs without data
  get "?".
- No preview for a song: the tap says "no preview".
- A song twice in the original: it is one song in the split; at Done each
  part gets it once.
- The user removes the last-but-two part: refused, at least 2 remain.
- A home with fewer than 10 tracks: not splittable.
- More than 2000 tracks: refused with a message (memory and time bound of
  the O(n²) clustering).
- Spotify cooldown at Suggest (needs a fresh track list) or at Done:
  refused with the cooldown message; nothing half-done.

## Testing (no Spotify calls)

- `tests/test_splitparts.py`: exactly N parts; sizes follow the data (a
  3:1 fixture stays 3:1); no slivers; same input same output; the "?"
  rule; same-artist split when getSimilar disagrees; the N±1 hint with
  its margin; best-fit for one song.
- `tests/test_splitparts_api.py`: draft persistence through every action;
  Done's order (original last), batching, resume after a failure at each
  step, re-read after a snapshot change, the cost estimate; fake Spotify.
- `tests/ui_harness.mjs`: chips and counts, tap previews vs move drags
  (pointer events), drop on a chip moves the song, add and remove part,
  the hint, the Done screen's choices and cost, local-file warning.
- Then a real run on the phone on one real home, with the user.

## Out of scope

- Seed songs per part ("start part A from these").
- Balanced part sizes.
- Splitting inputs or subsets.
- Undo after Done (Spotify state is the record; the original's songs are
  either kept as a subset or removed only from the original).
