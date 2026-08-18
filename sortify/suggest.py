"""Score how well a track fits each home playlist.

Two signals, both explainable to the user:
  - artist overlap: the playlist already contains tracks by this artist
  - artist-tag similarity: cosine between the track's artists' Last.fm tags
    and the playlist's tag profile

Spotify's audio-features endpoint is deprecated for new apps, and Spotify no
longer returns artist genres at all in development mode — tags come from
Last.fm instead (see `sortify/tags.py`). They describe the *artist*, not the
track, which is weaker evidence than a track-level tag would be. TAG_WEIGHT
is the single knob for how much that weaker evidence counts against artist
overlap, which stays the primary signal (see the constant's comment: only
~7% of this library's home artists have any Last.fm tags at all, so the
weight is provisional until more artists are tagged).
"""

from __future__ import annotations

import math
from collections import Counter

from .tags import clean_tags

# Measured 2026-08-18 by scripts/eval_suggest.py AFTER the home-artist
# backfill (scripts/backfill_tags.py: 1427/1427 home artists attempted,
# 1417 tagged, 10 real misses):
#   .venv/bin/python scripts/eval_suggest.py --n 500 --seed 7 --search
#   artist-only baseline (TAG_WEIGHT=0): top1 0.268 top3 0.424
#   TAG_WEIGHT=3.0:                      top1 0.290 top3 0.534
# On the spec's target case — tracks whose artist has no overlap in any
# home, where only tags can rescue the ranking — top3 went 0.000 -> 0.253
# (n=158/500). Higher weights (4.0/6.0) buy top3 0.540 but lose top1 and
# would let a perfect tag cosine outrank a single artist match (the
# artist-overlap-primacy pin caps the weight below ARTIST_BASE +
# ARTIST_PER_TRACK = 3.4), so 3.0 is the committed ceiling. Earlier
# pre-backfill numbers (7.1% coverage, ~0.006 lift) are in
# task-3-report.md for the record.
ARTIST_BASE = 3.0
ARTIST_PER_TRACK = 0.4
TAG_WEIGHT = 3.0
# ARTIST_TAG_DILUTION was removed (fix round 1, ruling R3a): it and
# TAG_WEIGHT only ever appeared as a product (TAG_WEIGHT * ARTIST_TAG_DILUTION
# * sim below), so the original 16-cell grid was a 1-D search over that
# product wearing a 2-D costume — see the duplicate-valued cells in
# task-3-report.md's fix-round section. A separate dilution factor is worth
# reintroducing if/when track-level tags exist (spec phase 2: Last.fm
# `track.getTopTags`), since a track-level tag is stronger evidence than an
# artist-level one and the two should not compete at face value.
MIN_SCORE = 0.8
TOP_N = 3


def _cleaned_tags(entry: dict) -> list[str]:
    """Hygiene-filtered tag names for one `tag_artists()` record.

    Shared by the profile side and the track side so they can't drift.
    Missing artists and recorded Last.fm misses both yield no tags. Last.fm's
    weights are ignored here, same as today's genre counts — just presence
    per artist. Uses `clean_tags`' own defaults (floor 10, keep 8) rather than
    a split's tuned params, so a split run with retuned hygiene intentionally
    diverges from what suggestions see.

    Guard-on-read: this is the suggestion path, not the user-triggered split,
    so a malformed or wrong-version entry (e.g. a stale v1-shaped record)
    degrades to "no tags" rather than raising — suggestions fall back to
    artist-overlap-only instead of breaking whatever endpoint called this
    (`/api/now` polls it every `PROFILE_TTL`).
    """
    if not isinstance(entry, dict) or entry.get("miss"):
        return []
    try:
        cleaned = clean_tags(entry.get("tags", []), entry.get("name") or "")
    except (AttributeError, TypeError):
        return []
    return [t for t, _w in cleaned]


def build_profile(tracks: list[dict], tag_artists: dict[str, dict]) -> dict:
    """Precompute what the suggester needs to know about one home playlist."""
    artist_counts: Counter = Counter()
    tag_counts: Counter = Counter()
    uris = set()
    for t in tracks:
        uris.add(t["uri"])
        for a in t["artists"]:
            if not a.get("id"):
                continue
            artist_counts[a["id"]] += 1
            for tag in _cleaned_tags(tag_artists.get(a["id"], {})):
                tag_counts[tag] += 1
    return {"artist_counts": artist_counts, "tag_counts": tag_counts, "uris": uris}


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    if not dot:
        return 0.0
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


def _track_tags(track: dict, tag_artists: dict[str, dict]) -> Counter:
    c: Counter = Counter()
    for a in track["artists"]:
        for tag in _cleaned_tags(tag_artists.get(a.get("id"), {})):
            c[tag] += 1
    return c


def suggest(track: dict, profiles: dict[str, dict], tag_artists: dict[str, dict]) -> list[dict]:
    """Rank home playlists for one track. Returns [{playlist_id, score, already, reasons}]."""
    track_tags = _track_tags(track, tag_artists)
    results = []
    for pid, prof in profiles.items():
        reasons = []
        score = 0.0

        for a in track["artists"]:
            n = prof["artist_counts"].get(a.get("id"), 0)
            if n:
                score += ARTIST_BASE + ARTIST_PER_TRACK * min(n, 5)
                reasons.append(f"{n} track{'s' if n > 1 else ''} by {a['name']} here")

        sim = _cosine(track_tags, prof["tag_counts"])
        if sim > 0.05:
            score += TAG_WEIGHT * sim
            overlap = sorted(
                (t for t in track_tags if t in prof["tag_counts"]),
                key=lambda t: track_tags[t] * prof["tag_counts"][t],
                reverse=True,
            )
            if overlap:
                reasons.append("artist tags: " + ", ".join(overlap[:3]))

        already = track["uri"] in prof["uris"]
        if already or score >= MIN_SCORE:
            results.append(
                {
                    "playlist_id": pid,
                    "score": round(score, 2),
                    "pct": min(round(score * 10), 100),
                    "already": already,
                    "reasons": reasons,
                }
            )

    results.sort(key=lambda r: (not r["already"], -r["score"]))
    return results[:TOP_N]
