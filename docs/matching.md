# How sortify decides where a song belongs

This is the user-facing explanation of the suggestion engine
(`sortify/suggest.py`) — what the percentage on a suggestion button means,
why a playlist did or didn't show up, and how to influence it.

## The three signals

Every home playlist gets a score for the playing track, built from three
signals. Each one produces the reason text you see on the button:

1. **Artist overlap** — the home already contains tracks by this artist.
   Reason: `3 tracks by Beach House here`. This is the strongest signal by
   design: a home that owns the artist always outranks any amount of
   similarity evidence (a deliberate, measured invariant — see the constants
   block in `suggest.py` for the numbers).
2. **Tag similarity** — cosine similarity between the track's Last.fm tags
   and the home's tag profile (built from all its artists' tags). Reason:
   `tags: …` when Last.fm has tags for this exact track (rare, ~4% of
   tracks), `artist tags: …` when it fell back to the artist's tags (the
   common case — this is why you almost never see plain "tags:").
3. **Neighbours** — Last.fm's "similar tracks" list for the playing track,
   summed over neighbours the home already contains (same-artist neighbours
   excluded, so this can't just re-derive signal 1). Reason:
   `4 similar tracks already here`. Neighbours re-rank homes that signals
   1–2 already surfaced; alone they can never push a home over the
   threshold — also deliberate.

A home shows up when its score clears the threshold (`MIN_SCORE`), the top
three are shown, and the percentage is just the score ×10 capped at 100 —
a confidence hint, not a probability.

## Your hints (`home_hints`)

On the **Playlists** view, every playlist marked as a Home gets a text
field: *matching hints*. Write comma-separated words describing what
belongs there — `ambient, piano, slow` — and press **Save roles**.

What they do: your words join that home's tag profile at the strength of
its strongest organic tag, so a track tagged `ambient` by Last.fm now pulls
toward the home you described as `ambient` even if the home's artists never
earned that tag. When a hint matches, the button says `your hint: ambient`.

What they don't do: outrank artist overlap (nothing does), or work on
tracks Last.fm knows nothing about — hints match against the *track's*
tags, so a track with no tags at all is still artist-overlap-only. Hints
are not run through the tag stoplist: whatever you write is used as-is
(lowercased).

Hints live in `data/config.json` under `home_hints` and take effect on the
next suggestion after saving.

## Subsets

A **subset** is a non-exclusive selection any song can join, including songs
that already have a home. Best-ofs, moods, project lists.

Any playlist you own can be one — you say so with the **Subset** chip on the
Playlists view, and that marking is the whole definition. (Subsets used to
have to be named `{like this}`. That requirement is gone: the chip only
appears on rows the list actually draws, 200 of your ~990 playlists, so a
name rule meant most of your library could never be marked at all.)

**Nothing on this page applies to subsets.** They are not scored, not
ranked, and never suggested — sortify has no opinion about which of them a
song belongs in. They were scored briefly, in August 2026, and it wasn't
wanted: a suggestion is a question you have to answer, and a best-of is not
a question.

So a subset is simply a destination you can reach quickly. **Add to
subset…** on the Now card opens the ones you've marked, and putting a song
in one changes nothing else — it does not count as filing, it does not take
the song out of its input, and the song still needs its home.

Marking one is **free**: because nothing scores them, nothing reads them,
and no Spotify calls are spent. Mark as many as you find useful.

## Where the data comes from

| Data | File | Fetched |
| --- | --- | --- |
| Artist tags | `data/tags.json` | during splits, and for the playing track's artists on an explicit refresh (`?force=1`), max 3 artists/min |
| Track tags + neighbours | `data/lastfm_tracks.json` | same refresh path, one track/min; bulk via `scripts/backfill_similar.py` |
| BPM | `data/deezer.json` | Deezer public API, same refresh path, one track/min |
| Your hints | `data/config.json` (`home_hints`) | you type them |

All of these are write-once caches: once a track/artist has an answer (even
"Last.fm doesn't know it"), it is never re-fetched. Spotify is never asked
for any of this — its dev-mode API carries no genres, no audio features,
no BPM.

## BPM

The number under the artist line on the now-playing card. It comes from
Deezer (keyless public API), matched by artist+title search, cached
permanently. Display-only for now — it does not participate in matching.
Not every track has one: no Deezer hit, or Deezer storing BPM 0
("unknown"), shows nothing.
