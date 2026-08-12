"""Score how well a track fits each home playlist.

Two signals, both explainable to the user:
  - artist overlap: the playlist already contains tracks by this artist
  - genre similarity: cosine between the track's artist genres and the
    playlist's genre profile

Spotify's audio-features endpoint is deprecated for new apps, and artist
genres are sparse for small artists — artist overlap is the primary signal,
genres the tiebreaker.
"""

from __future__ import annotations

import math
from collections import Counter

# A pure genre match can reach 4.0; a single artist match starts at 3.4,
# so "right artist" beats "similar genre" unless the genre fit is perfect.
ARTIST_BASE = 3.0
ARTIST_PER_TRACK = 0.4
GENRE_WEIGHT = 4.0
MIN_SCORE = 0.8
TOP_N = 3


def build_profile(tracks: list[dict], artist_info: dict[str, dict]) -> dict:
    """Precompute what the suggester needs to know about one home playlist."""
    artist_counts: Counter = Counter()
    genre_counts: Counter = Counter()
    uris = set()
    for t in tracks:
        uris.add(t["uri"])
        for a in t["artists"]:
            if not a.get("id"):
                continue
            artist_counts[a["id"]] += 1
            for g in artist_info.get(a["id"], {}).get("genres", []):
                genre_counts[g] += 1
    return {"artist_counts": artist_counts, "genre_counts": genre_counts, "uris": uris}


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    if not dot:
        return 0.0
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


def _track_genres(track: dict, artist_info: dict[str, dict]) -> Counter:
    c: Counter = Counter()
    for a in track["artists"]:
        for g in artist_info.get(a.get("id"), {}).get("genres", []):
            c[g] += 1
    return c


def suggest(track: dict, profiles: dict[str, dict], artist_info: dict[str, dict]) -> list[dict]:
    """Rank home playlists for one track. Returns [{playlist_id, score, already, reasons}]."""
    track_genres = _track_genres(track, artist_info)
    results = []
    for pid, prof in profiles.items():
        reasons = []
        score = 0.0

        for a in track["artists"]:
            n = prof["artist_counts"].get(a.get("id"), 0)
            if n:
                score += ARTIST_BASE + ARTIST_PER_TRACK * min(n, 5)
                reasons.append(f"{n} track{'s' if n > 1 else ''} by {a['name']} here")

        sim = _cosine(track_genres, prof["genre_counts"])
        if sim > 0.05:
            score += GENRE_WEIGHT * sim
            overlap = sorted(
                (g for g in track_genres if g in prof["genre_counts"]),
                key=lambda g: track_genres[g] * prof["genre_counts"][g],
                reverse=True,
            )
            if overlap:
                reasons.append("genre fit: " + ", ".join(overlap[:3]))

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
