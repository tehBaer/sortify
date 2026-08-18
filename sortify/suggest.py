"""Score how well a track fits each home playlist.

Two signals, both explainable to the user:
  - artist overlap: the playlist already contains tracks by this artist
  - artist-tag similarity: cosine between the track's artists' Last.fm tags
    and the playlist's tag profile

Spotify's audio-features endpoint is deprecated for new apps, and Spotify no
longer returns artist genres at all in development mode — tags come from
Last.fm instead (see `sortify/tags.py`). They describe the *artist*, not the
track, which is weaker evidence than a track-level tag would be, so the tag
score is diluted before it competes with artist overlap, which stays the
primary signal.
"""

from __future__ import annotations

import math
from collections import Counter

from .tags import clean_tags

# A pure tag match can reach TAG_WEIGHT * ARTIST_TAG_DILUTION = 2.0; a single
# artist match starts at 3.4, so "right artist" beats "similar tags" unless
# the tag fit is perfect. Placeholders until Task 3 measures real weights.
ARTIST_BASE = 3.0
ARTIST_PER_TRACK = 0.4
TAG_WEIGHT = 4.0
ARTIST_TAG_DILUTION = 0.5
MIN_SCORE = 0.8
TOP_N = 3


def _cleaned_tags(entry: dict) -> list[str]:
    """Hygiene-filtered tag names for one `tag_artists()` record.

    Shared by the profile side and the track side so they can't drift.
    Missing artists and recorded Last.fm misses both yield no tags. Last.fm's
    weights are ignored here, same as today's genre counts — just presence
    per artist.
    """
    if not entry or entry.get("miss"):
        return []
    cleaned = clean_tags(entry.get("tags", []), entry.get("name") or "")
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
            score += TAG_WEIGHT * ARTIST_TAG_DILUTION * sim
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
