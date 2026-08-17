# Splitting giant playlists into listenable, coherent piles

Design, 2026-08-17.

## Problem

The user has playlists too large to work through: the immediate target holds
**1372 tracks**. At this library's measured average of 5.5 min per track
(median 5.0, over the 3097 tracks already in `data/cache.json`) that is about
**126 hours** of listening.

Two distinct complaints, both of which must be answered:

1. **Too long to sit through.** There is no natural place to stop and resume.
2. **Too incoherent to judge.** Deep house sits next to Tuareg desert blues
   sits next to stoner rock, so no listening session builds the context needed
   to decide whether a track belongs in one of the user's own playlists.

Splitting purely by time answers (1) and not (2), and produces 40–126 chunks
to keep track of — trading one management problem for another. Time therefore
decides where a session *stops*; a tag-derived grouping decides what goes
*together*.

## Constraints

### Spotify supplies no genre data

`/artists/{id}` still returns a `genres` array, but it is empty for this app.
Measured, not assumed: all **717 artists** in `data/cache.json` have
`genres: []`, and all 717 have a populated `name`, so those calls succeeded —
a failed fetch stores `name: null`. This is the Nov-2024 deprecation that also
removed audio-features, audio-analysis, recommendations, and related-artists
for development-mode apps.

Consequences:

- Any "split by energy/mood/tempo" feature is impossible; those fields are gone.
- The genre signal must come from outside Spotify.
- The background genre enricher (`sortify/app.py`) has been spending its full
  40 background calls/day writing empty arrays. It is deleted by this design.

### Reads are cheap; bulk writes are ruinous

`add_to_playlist` posts a single URI per request — the Feb-2026 API dropped
batch adds. So:

- Reading all 1372 tracks costs **~15 calls** (`limit=100`, 14 pages plus
  metadata).
- Materialising every pile as a real Spotify playlist would cost **~1384
  calls**: three days of `DAILY_CAP` (600), sustained at a rate matching the
  traffic that earned the ~23 h ban on 2026-08-13. **Rejected.**

### Sortify cannot control playback

There is no `/me/player/play` or queue endpoint in the client, and none is
added here. Sortify observes what the user plays via
`/me/player/currently-playing` and sorts at listening pace. A pile must
therefore exist as a real Spotify playlist at the moment the user listens to
it — a purely virtual pile would be a list they can read but not play.

This is what forces the sitting mechanism in Section 4: piles stay virtual and
free, and exactly one sitting at a time is made real.

### The tag source has usable coverage

Probed read-only against Last.fm using 30 randomly sampled artists from
`data/cache.json`: **29/30 tagged (97%)**, with a specific vocabulary —
`desert blues`, `anatolian rock`, `psychill`, `future garage`,
`instrumental hip-hop`, `boogaloo`. This is sufficient signal for community
detection rather than noise-dressing.

The same probe showed roughly a third of returned tags are not genres:
geography (`niger`, `icelandic`, `Trondheim`, `netherlands`), listener
descriptors (`female vocalists`, `oldies`), label names
(`dusted wax kingdom`), and junk (`All`, `misc`, `x`). Left unfiltered these
produce cross-genre piles such as "Norwegian". Tag hygiene is a required
stage, not a refinement.

## Non-goals

- Playback control of any kind.
- Modifying the source playlist. It is read-only throughout; see Section 5.
- Re-deriving the existing input/home playlist conventions — piles are
  discovered from tag data alone and are deliberately **not** biased toward
  the user's existing home playlists.
- Any background or proactive Spotify traffic. Every Spotify call added by
  this design is user-initiated.

## Architecture

```
 [picker: existing /api/playlists]
            |  user picks a playlist, hits "Split"
            v
 [read tracks: existing playlist_tracks]  ~15 Spotify calls, once
            |
            v
 [tags.py] --> Last.fm  ~700 requests, 0 Spotify calls
            |          cached permanently in data/tags.json
            v
 [split.py]  pure function, 0 calls, re-runnable for free
            |          persisted to data/splits.json
            v
 [piles: virtual]
            |  user starts a sitting from a pile
            v
 [sitting: one disposable Spotify playlist]  ~24 calls
            |  user listens in the Spotify app
            v
 [existing /api/act]  keep = 1 call, reject = 0 calls
```

## Section 1 — Tag layer (`sortify/tags.py`, new)

A module **structurally incapable of spending Spotify quota**: its own HTTP
client, its own rate limiter, and no import of `sortify.spotify`. The failure
mode being ruled out by construction is a later change routing tag traffic
through the Spotify budget, or Spotify traffic through the Last.fm limiter.

- **Credentials.** API key read from `~/state/sortify/lastfm.json`, matching
  the `~/state/<app>/` convention used by the user's other projects. Never
  from `data/config.json`, which sits next to version-controlled files. A
  missing key disables the feature with a clear error; it never falls back to
  Spotify.
- **Rate limit.** 4 requests/second, below Last.fm's stated ceiling of 5.
  Independent of `WINDOW_CAP`.
- **Request.** `artist.getTopTags`, `autocorrect=1`, matched by artist name.
- **Hygiene**, applied in order:
  1. drop tags with `count < 10`;
  2. drop tags on the stoplist — countries, nationalities, cities, and the
     known junk set (`seen live`, `favorites`, `All`, `misc`, `x`);
  3. drop tags that case-insensitively match the artist's own name or a
     substring of it (catches self-tags and label names);
  4. keep the top 8 surviving tags with their weights.
- **Cache.** `data/tags.json`, keyed by **Spotify artist id** so it joins
  directly against cached tracks. Permanent — never re-fetched.
- **Misses.** The ~3% Last.fm cannot match are recorded explicitly as
  `"miss": true` so they are never re-asked, and their tracks are routed to a
  dedicated "untagged" pile rather than silently dropped.

`data/tags.json`:

```json
{
  "version": 1,
  "artists": {
    "5PbpKlxQE0Ktl5lcNABoFI": {
      "name": "Altin Gün",
      "lastfm_name": "Altın Gün",
      "tags": [["psychedelic rock", 100], ["anatolian rock", 85]],
      "fetched_at": "2026-08-17T16:00:00Z",
      "miss": false
    }
  }
}
```

Cost for the target playlist: roughly 600–800 Last.fm requests, about three
minutes, **zero Spotify calls**.

## Section 2 — Splitter (`sortify/split.py`, new)

A pure function, `(tracks, tags, params) -> piles`. No network, no file I/O.
This matters for two reasons: it is fully testable offline, and re-clustering
with different parameters is free once tags are cached — which the user will
want, because the first clustering is unlikely to be the last.

1. Build a weighted graph over artists; edge weight is shared-tag weight
   (cosine over tag-weight vectors).
2. Run Louvain community detection. Community count falls out of the data
   rather than being guessed. Implementation is hand-rolled (Louvain is ~120
   lines) rather than adding a `networkx` dependency for one function; the
   project currently has no scientific-computing dependencies and this design
   does not introduce the first.
3. Each track joins the community of its **first listed artist**. Spotify
   orders `artists` with the primary credit first, and featured artists should
   not pull a track out of its pile.
4. Name each pile by its most **distinctive** tags: TF-IDF against *this
   playlist's own* tag distribution, not global frequency. This yields
   "desert blues · tuareg" rather than "world".
5. Piles below `min_pile` merge into their nearest neighbour, where a pile's
   centroid is the weight-summed tag vector of its member artists and
   "nearest" is highest cosine similarity between centroids. Merging repeats
   until every pile meets `min_pile` or only one pile remains. Untagged tracks
   form their own pile and are exempt from merging in both directions.

Parameters, all persisted with the result so a split is reproducible:
`resolution` (Louvain, default 1.0), `min_pile` (default 15 tracks),
`tag_floor` (default 10), `max_tags_per_artist` (default 8).

`data/splits.json`:

```json
{
  "version": 1,
  "splits": {
    "<playlist_id>": {
      "created_at": "2026-08-17T16:00:00Z",
      "snapshot_id": "<spotify snapshot at read time>",
      "params": {"resolution": 1.0, "min_pile": 15,
                 "tag_floor": 10, "max_tags_per_artist": 8},
      "piles": [
        {"id": "p1",
         "name": "desert blues · tuareg",
         "tags": ["desert blues", "tuareg", "african"],
         "uris": ["spotify:track:..."]}
      ],
      "decided": {"spotify:track:...": {"action": "keep",
                                        "to_id": "<home playlist id>",
                                        "at": "2026-08-17T17:00:00Z"}},
      "active_sitting": null
    }
  }
}
```

Re-clustering replaces `piles` and preserves `decided`, so re-splitting never
loses progress.

## Section 3 — Picker (UI + existing endpoint)

`/api/playlists` already returns every playlist with `name`, `total`,
`folder`, and `role`. The picker reuses it as-is and adds a "Split" action per
row, so **selecting a playlist costs no new Spotify calls**. Playlists already
split show their pile count and progress instead.

This replaces any notion of configuring a target playlist id by hand.

## Section 4 — Sittings

A sitting is one disposable Spotify playlist holding a single listening
session drawn from one pile.

- Take the next N undecided tracks from the pile, **in original playlist
  order**, such that their summed `duration_ms` reaches a target duration
  (default 2 h, configurable) without exceeding it. Stable ordering makes a
  sitting resumable and reproducible; shuffling would re-serve the pile
  differently after any interruption. Track durations are already cached, so
  sizing costs nothing.
- Create a playlist (1 call), add N tracks (N calls, paced), record it as
  `active_sitting`.
- The user listens in the Spotify app on any device. The existing
  currently-playing poll and sorting UI work unchanged.
- When the sitting is finished, unfollow the playlist (1 call). Unfollowing
  discards the whole container in one call rather than removing tracks
  individually.

At the default 2 h target that is ~22 tracks and **~24 calls per sitting**.

## Section 5 — Decisions

Decisions go through the existing `/api/act`, which already supports this
exactly: `ActIn.from_id` is optional and documented as
`None = just add to the home, no removal`.

- **Keep** → `action: "move"`, `to_id: <home playlist>`, `from_id: None`
  → **1 call**.
- **Reject** → recorded in `splits.json` only; no Spotify call at all
  → **0 calls**.

The source playlist is never modified. This is a deliberate departure from the
existing input-playlist flow, which drains its source at 2 calls per decision;
here it saves ~1372 calls and leaves the original intact as an archive.

Undo for keeps uses the existing undo stack. Undo for rejects is a local
`splits.json` edit.

## Cost model

| Step | Spotify calls |
|---|---|
| Pick a playlist | 0 |
| Read 1372 tracks (once, user-initiated) | ~15 |
| Tag ~700 artists | **0** (Last.fm) |
| Cluster, and every re-cluster | **0** |
| Per ~2 h sitting | ~24 |
| Per keep | 1 |
| Per reject | 0 |

Against `DAILY_CAP` 600 and `WINDOW_CAP` 12/60s. A heavy day — one sitting
plus 22 decisions — costs well under 50 calls. Deleting the genre enricher
returns 40 background calls/day, so this design is **net negative** on
routine daily traffic.

Every Spotify call here is user-initiated. `BACKGROUND_DAILY_CAP` is untouched
and no new proactive job is introduced.

## Testing

- `split.py` is pure and tests offline against fixtures, including the
  degenerate cases: every track untagged; one giant community; all singleton
  communities; a pile exactly at `min_pile`; an artist with zero surviving
  tags after hygiene.
- Tag hygiene tests assert the specific observed junk is removed —
  `All`, `misc`, `x`, `Trondheim`, `icelandic`, `female vocalists` — and that
  a legitimate compound genre like `anatolian rock` survives.
- `tags.py` tests run against a fake HTTP client, with an explicit assertion
  that no Spotify budget function is called.
- Sitting sizing tests confirm the duration target is met without exceeding it
  and that undecided tracks are never served twice.
- The existing suite stays green. No test makes a network call.

## Risks and open questions

1. **Unverified endpoints.** `POST /me/playlists` and
   `DELETE /playlists/{id}/followers` are not in the client today and are not
   confirmed present in the Feb-2026 API shape. **Mitigation:** one
   single-shot probe before implementing Section 4. **Fallback if absent:**
   reuse one long-lived sitting playlist and clear it per track — roughly
   double the calls per sitting (~44), still viable.
2. **Clustering quality is unproven on this data.** 97% tag coverage makes
   Louvain viable but does not guarantee coherent piles. **Mitigation:**
   clustering is free to re-run, and the plan sequences tag enrichment before
   the clusterer so the real tag distribution is visible first. If communities
   come out mushy, fall back to dominant-tag bucketing — assign each artist
   its single most distinctive tag and merge undersized buckets — which is
   coarser but fully explainable.
3. **Eclectic artists.** Every track by one artist lands in one pile. Accepted:
   usually correct, and the alternative (per-track tags) is not available.
4. **Last.fm name matching.** Matching is by name, not id, so distinct artists
   sharing a name can collide. Accepted at ~3% miss rate; misses are explicit
   and land in the untagged pile.
5. **Key in transcript.** The Last.fm API key was pasted into a chat
   transcript and should be rotated at the user's convenience. It is stored
   outside the repo regardless.
