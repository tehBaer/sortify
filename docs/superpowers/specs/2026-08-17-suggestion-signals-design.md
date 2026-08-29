# Better suggestions: tags, similarity, and what each playlist is for

Design, 2026-08-17.

## Problem

The user played Alice Cooper's "I Never Cry" — a slow, sad ballad — and sortify
offered only playlists that already contain Alice Cooper songs, none of them
about slow or sad.

That is not a tuning problem. Ranking currently runs on **one** signal:

```python
# suggest.py
score += ARTIST_BASE + ARTIST_PER_TRACK * min(n, 5)   # this home has this artist
sim = _cosine(track_genres, prof["genre_counts"])     # always 0.0
```

The genre half is dead code. `/artists/{id}` in the Feb-2026 dev-mode API has
no `genres` key at all — verified 2026-08-17 by single-shot probe against Alice
Cooper (`3EhbVgyfGd7HkpsagwL9GS`). All 720 artists in `data/cache.json` show
`genres: []`, which was our own `a.get("genres", [])` default rather than data;
their `name` fields came through fine, so those calls succeeded. Spotify's
audio-features endpoint was already deprecated for new apps, so Spotify now
supplies no genre, mood, or energy signal of any kind.

So every suggestion sortify has ever made was pure artist overlap. The
behaviour the user complained about is the only behaviour the scorer can
produce.

## What signals actually exist

Measured on this library, not assumed. Sampling method: 40 distinct
(artist, track) pairs drawn systematically from `data/cache.json`, plus a
26-track probe run by the splitting workstream.

| signal | coverage | scope | source |
|---|---|---|---|
| artist overlap | 100% | artist | already local |
| `artist.getTopTags` | **98%** (39/40) | artist | Last.fm |
| `track.getSimilar` | **52%** (21/40) | track | Last.fm |
| `track.getTopTags` | **8%** (2/26) | track | Last.fm |
| descriptions | 100% once written | playlist | the user |

Zero API failures in the 40-track run, so those are true absences rather than
errors. The `artist.getTopTags` figure is a **control**: the splitting
workstream independently measured ~97% on this library, so reproducing it at
98% is what makes the 52% credible. A harness that cannot reproduce a known
result should not be believed on an unknown one.

Two findings shape everything below.

**Track tags are not usable here.** At 8%, and with "I Never Cry" itself
returning none, they cannot carry the feature. Last.fm's tagging follows chart
popularity and this library is mostly obscure electronic, world, and ambient.

**`track.getSimilar` is usable, and it finds the thing that was missing.**
"I Never Cry" returns eight neighbours:

```
1.00  Alice Cooper — How You Gonna See Me Now
0.97  Alice Cooper — Wake Me Gently
0.22  Deep Purple — When a Blind Man Cries
0.21  Nazareth — Dream On
```

All ballads. It is computed from listening data rather than volunteer tagging,
which is why the popularity bias bites less hard.

**But its top matches are same-artist**, and scoring those would merely
re-derive the artist overlap already counted. The new information is in the
cross-artist tail. This is a correctness requirement, not a nicety: without it
the feature reproduces the exact complaint that motivated it.

## Goals

1. A sad ballad by an artist you own is offered playlists of sad, slow music —
   not merely playlists containing that artist.
2. Every suggestion still explains itself in words the user can check.
3. Coverage never drops to nothing: something scores every track.
4. A measured before/after, so "better" is a number rather than a feeling.
5. No Spotify API calls. Last.fm has no bearing on the Spotify quota.

## Non-goals

- Rewriting the triage or Now views beyond the additions described here.
- Any use of Spotify audio features, genres, or recommendations. They are gone.
- Automatic tagging of tracks by inference. User tags are typed by the user.
- Splitting playlists. That is the other workstream's design.

## Storage: precious data separate from rebuildable data

The governing rule: **anything the user typed must never share a file with
anything a cache refresh can overwrite.** Descriptions and hand-typed tags are
irreplaceable. Last.fm tags and `getSimilar` results can always be re-fetched.

### Precious — written only by explicit user action

`data/descriptions.json`

```json
{"version": 1, "playlists": {"<playlist_id>": "slow sad stuff, late night"}}
```

`data/user_tags.json`

```json
{"version": 1, "tracks": {"spotify:track:xyz": ["ballad", "melancholy"]}}
```

Keyed by track URI, which survives playlist moves and library edits.

### Rebuildable — safe to delete at any time

`data/lastfm_tracks.json`

```json
{"version": 1,
 "tracks": {"<artist><title>": {
    "similar": [{"artist": "Nazareth", "track": "Dream On", "match": 0.21}],
    "tags": ["ballad"],
    "fetched_at": 1786980000,
    "miss": false}}}
```

Artist tags stay where the splitting workstream already put them
(`data/tags.json`, `{"version": 1, "artists": {...}}`). This design reads that
file and does not write it, so the two workstreams never contend for it.

Keys are `artist\x1ftitle`, lowercased and whitespace-collapsed. A unit
separator avoids collisions with titles containing dashes or slashes.

## Scoring

`suggest.py` becomes a set of named features. Each returns `(score, reason)`,
and the total is a weighted sum. The reason strings are what the card already
renders, so explainability is preserved by construction rather than bolted on.

### Tag resolution for a track

Tags come from three places with descending authority:

1. **User tags** for this track URI — the user knows their library best.
2. **Track tags** from Last.fm, when they exist (8% of the time).
3. **Artist tags** from Last.fm as fallback (98%), *flagged as artist-level*
   both in the UI and in the reason string.

Artist tags are diluted by a constant factor when scoring, because "Alice
Cooper is shock rock" is weaker evidence about one ballad than "this track is
tagged ballad". The dilution factor is set by the harness, not by hand.

### Features

| feature | what it computes | reason shown |
|---|---|---|
| `artist_overlap` | existing: `ARTIST_BASE + ARTIST_PER_TRACK × min(n,5)` | `3 tracks by Nazareth here` |
| `tag_match` | cosine between the track's resolved tag vector and the home's tag profile (tags of the tracks it contains) | `tags: ballad, classic rock` |
| `description_match` | overlap between the track's tags and the words of the home's description, after stopword removal | `matches "slow sad stuff"` |
| `neighbour` | how many of the track's `getSimilar` neighbours are already in this home, weighted by Last.fm's `match` score | `4 similar tracks already here` |
| `already_present` | existing: the track is already in this home | unchanged |

`neighbour` **excludes same-artist neighbours entirely.** Those are already
counted by `artist_overlap`, and including them is what would reproduce the
original complaint.

The home tag profile is built exactly as `build_profile` builds the genre
profile today — the machinery exists and is currently fed a dead input. This is
a substitution, not new plumbing.

### Weights

Initial weights are placeholders. Final values come from the harness below by
coordinate search over a small grid, and are committed as constants with the
measured accuracy in a comment. Hand-tuned weights across four interacting
signals are superstition; the harness is the reason this design is worth
building rather than guessing.

## Evaluation harness

The user's own playlists are labelled training data: every track in a home is
an example of a track the user decided belongs there.

`scripts/eval_suggest.py` (not part of the served app):

1. Collect every (track, home) pair from cached home playlists.
2. Sample N pairs (default 500, seeded for repeatability).
3. For each, **rebuild that home's profile with the held-out track removed**,
   then rank all homes for it.
4. Report top-1 and top-3 accuracy — how often the user's actual choice is
   ranked first, and within the first three (three being what the card shows).

**Step 3 is the whole validity of the harness.** If the track is left in the
profile, `already_present` and `artist_overlap` see it and the score is
trivially perfect — a harness that reports 100% and means nothing. A test
asserts that removing the track changes its own score, so this cannot silently
regress.

Tracks in several homes count as correct if any of their homes is in the top-k.

The first thing this produces is a **baseline for today's artist-only scorer**,
so the change can be stated as a delta rather than an assertion. It runs on
cached data, makes no API calls of any kind, and is repeatable.

## Fetching

Pacing follows the existing convention rather than a rate limit read off a
page: `tags.py` already uses `MIN_INTERVAL = 0.25` (4 requests/second), and
this design reuses that client and therefore that pacing. Fetches happen only
on explicit user action or a bounded backfill command. There is no background
job — the previous one earned the discipline recorded in CLAUDE.md, and the
same rule applies here even though the quota belongs to a different service.

Enriching all ~3100 cached tracks at that pace takes about 13 minutes and costs
zero Spotify calls.

### Failure must never be recorded as absence

Only Last.fm error code **6** means "no such track". Codes 10 (invalid key),
26 (suspended), 29 (rate limit), and 8/11/16 (service errors) are our failure,
not their absence.

Recording a failure as `miss: true` would be permanent, because misses are
deliberately never re-fetched — one outage part-way through a backfill would
silently poison the library with false negatives, and the symptom is an
untagged library with no error anywhere. The splitting workstream hit exactly
this bug in review; this design inherits the fix rather than rediscovering it.

Failures are left absent from the cache so the next run retries them, and the
backfill command reports how many it skipped.

## Interface

**Now card and triage card.** A tag row under the artist line. Track and user
tags render plain; artist-level tags render visibly marked, so the user always
knows whether they are looking at a statement about the track or about the
artist. A `+ tag` input adds a tag to the playing track; it takes effect on the
next render, with no network call, so it works during a Spotify cooldown.

**Playlists view.** Each home gets a description field, saved on blur. The
placeholder asks the question the field is for: *what belongs in here?*

**Reasons.** Unchanged in mechanism; richer in content.

## Pushing tags to Last.fm — phase two

The user asked for added tags to reach Last.fm, not only sortify. This is
deliberately sequenced second: local tags are complete and useful without it,
and it needs a credential we do not have.

`track.addTags` is a write method, requiring:

- the **shared secret** from the user's Last.fm API account (we hold only the
  32-character key);
- a one-time authorisation: `auth.getToken` → the user approves at
  `last.fm/api/auth/?api_key=…&token=…` → `auth.getSession` → a session key,
  which does not expire;
- `api_sig` on every call: md5 of all parameters ordered alphabetically by
  name and concatenated as `<name><value>`, with the shared secret appended.

Verified against Last.fm's desktop-auth documentation, 2026-08-17.

The session key and secret live in `~/state/sortify/lastfm.json` beside the
existing key, mode 600, outside the repository.

Local tags are the source of truth. Pushing is best-effort: a push failure
records the tag locally regardless and is reported, never silently dropped.

## Testing

Unit tests per feature, with hand-built profiles, asserting both the score and
the reason string. Specifically:

- `neighbour` scores zero for a home whose only matching neighbours are
  same-artist — the regression test for the original complaint.
- Artist-level tags score lower than the same tags at track level.
- A user tag outranks a conflicting Last.fm tag.
- A Last.fm error code other than 6 is not recorded as a miss.
- Removing a held-out track from a profile changes that track's score.

The harness is the integration test: a weighting that scores worse than the
committed baseline fails.

## Coordination

The splitting workstream (`lastfm-tags`, `~/kode/spotify/sortify-lastfm`) owns
`sortify/tags.py`, `sortify/community.py`, `sortify/split.py`, and the
`artists` half of `data/tags.json`. This design **reads** `tags.py`'s client
and tag hygiene rather than writing a second Last.fm client, and reads
`data/tags.json` without writing it.

`suggest.py` is contended in principle but not in practice: that workstream has
scoped itself away from it and carries the dead-cosine cleanup as a follow-up.
This design does that cleanup by replacing the input rather than deleting the
branch.

Sequencing: this work should land after `lastfm-tags` merges, or rebase onto
it, so that `tags.py` exists to build on.

## Risks

**48% of the library gets no `getSimilar` data**, concentrated in the obscure
end — Acid Arab, Anzo, Massano, Seckou Keita, Solar Fields. Descriptions and
artist tags cover those, so ranking degrades rather than failing, but the
sharpest signal is unavailable exactly where the user's library is densest.
This is the strongest argument for descriptions being the floor of the design
rather than a supplement.

**68 homes is a lot of descriptions to write.** They are optional and additive:
an empty description simply contributes nothing. Ambiguous homes can be
described first, and the harness can report which homes are most often confused
so the effort goes where it pays.

**Weight tuning can overfit** to 500 sampled tracks from one library. Mitigated
by keeping the weight grid coarse and the feature count small; a model with
four weights cannot memorise much.
