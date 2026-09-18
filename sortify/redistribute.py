"""Tag-driven redistribution of one buffer playlist's tracks into others.

Built for the one-off job of emptying [Ursus] and [Neue] into the remaining
buffer inboxes, but nothing here knows those names.

The scoring is cosine similarity between a track's Last.fm tag vector and
each candidate playlist's, over **idf-weighted** tags. The weighting is the
part that earns its keep: `electronic` and `pop` appear in nearly every
buffer's profile, so on raw counts they swamp the comparison and everything
lands in whichever buffer is biggest. Discounting a tag by how many profiles
carry it is what lets `mantra` decide a track for [Mantric] and `psydub` for
[FILTER Y].

Pure logic — injected data, no I/O, no Spotify imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .tags import clean_tags

# A track whose artists yield no usable tags at all. It still gets a target
# (the caller asked for a best guess on everything), but it is marked so the
# report can separate "chosen" from "guessed".
BLIND_SCORE = 0.0


def artist_ids(track: dict) -> list[str]:
    out = []
    for a in track.get("artists") or []:
        aid = a.get("id") if isinstance(a, dict) else a
        if aid:
            out.append(aid)
    return out


def track_vector(track: dict, tag_artists: dict) -> dict[str, float]:
    """Tag weights for one track, summed over its artists.

    Runs each artist's raw tags through `clean_tags`, the same filter the
    splitter uses, so stoplisted junk and self-tags never reach the scoring.
    """
    vec: dict[str, float] = {}
    for aid in artist_ids(track):
        entry = tag_artists.get(aid)
        if not entry:
            continue
        for tag, weight in clean_tags(entry.get("tags") or [],
                                      entry.get("name") or ""):
            vec[tag.lower()] = vec.get(tag.lower(), 0.0) + float(weight)
    return vec


def playlist_vector(tracks: Iterable[dict], tag_artists: dict) -> dict[str, float]:
    """The summed tag vector of every track in a playlist."""
    vec: dict[str, float] = {}
    for track in tracks:
        for tag, weight in track_vector(track, tag_artists).items():
            vec[tag] = vec.get(tag, 0.0) + weight
    return vec


def with_anchors(vec: dict[str, float], anchors: dict[str, float]) -> dict[str, float]:
    """Merge hand-written tag weights into a profile.

    For a playlist too small to profile itself ([Gammalakustisk] holds one
    untagged track), the name is the only signal there is. Anchors add to the
    real vector rather than replacing it, so a profile that later grows real
    tracks keeps both.
    """
    out = dict(vec)
    for tag, weight in anchors.items():
        out[tag.lower()] = out.get(tag.lower(), 0.0) + float(weight)
    return out


def build_idf(profiles: dict[str, dict[str, float]]) -> dict[str, float]:
    """Inverse document frequency over the candidate profiles.

    A tag in every profile carries no information about which to pick; a tag
    in one profile carries all of it.
    """
    n = len(profiles) or 1
    seen: dict[str, int] = {}
    for vec in profiles.values():
        for tag in vec:
            seen[tag] = seen.get(tag, 0) + 1
    return {tag: math.log(1.0 + n / count) for tag, count in seen.items()}


def idf_of(idf: dict[str, float], tag: str) -> float:
    """A tag no profile carries is maximally distinctive, not unknown."""
    if tag in idf:
        return idf[tag]
    return math.log(1.0 + len(idf) + 1) if idf else 1.0


def _weighted(vec: dict[str, float], idf: dict[str, float]) -> dict[str, float]:
    return {tag: w * idf_of(idf, tag) for tag, w in vec.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    dot = sum(a[t] * b[t] for t in shared)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass
class Assignment:
    target: str
    score: float
    matched: list[str] = field(default_factory=list)
    blind: bool = False
    uri: str = ""
    name: str = ""
    source: str = ""
    duplicate: bool = False
    runner_up: str = ""
    runner_up_score: float = 0.0


def assign(track: dict, profiles: dict[str, dict[str, float]],
           idf: dict[str, float], tag_artists: dict,
           blind_seq: int = 0) -> Assignment:
    """Pick the best-matching profile for one track.

    `blind_seq` spreads the tracks that match nothing. Argmax over a row of
    zeros is whichever key sorts first, which would dump every untagged track
    into a single buffer; rotating by sequence at least scatters them.
    """
    vec = track_vector(track, tag_artists)
    keys = sorted(profiles)
    if not keys:
        raise ValueError("no candidate profiles")

    wv = _weighted(vec, idf)
    ranked = sorted(
        ((cosine(wv, _weighted(profiles[k], idf)), k) for k in keys),
        key=lambda sk: (-sk[0], sk[1]),
    )
    best_score, best = ranked[0]

    if best_score <= 0.0:
        # No signal: a forced guess, spread across the buffers by sequence.
        return Assignment(
            target=keys[blind_seq % len(keys)], score=BLIND_SCORE,
            matched=[], blind=True,
            uri=track.get("uri", ""), name=track.get("name", ""),
        )

    prof = profiles[best]
    matched = sorted(
        (t for t in set(vec) & set(prof)),
        key=lambda t: -(vec[t] * prof[t] * idf_of(idf, t)),
    )[:5]
    runner, runner_score = ("", 0.0)
    if len(ranked) > 1:
        runner_score, runner = ranked[1][0], ranked[1][1]
    return Assignment(
        target=best, score=round(best_score, 4), matched=matched, blind=False,
        uri=track.get("uri", ""), name=track.get("name", ""),
        runner_up=runner, runner_up_score=round(runner_score, 4),
    )


def plan(sources: dict[str, list[dict]],
         profiles: dict[str, dict[str, float]],
         tag_artists: dict,
         target_tracks: dict[str, list[dict]] | None = None) -> list[Assignment]:
    """Assign every track in every source playlist to a target profile.

    A source is never a target: the sources are being emptied, so routing a
    track back into one would be a no-op that still costs two calls.
    """
    candidates = {k: v for k, v in profiles.items() if k not in sources}
    if not candidates:
        raise ValueError("every candidate profile is also a source")

    held: dict[str, set[str]] = {}
    for key, tracks in (target_tracks or {}).items():
        held[key] = {t.get("uri", "") for t in tracks}

    idf = build_idf(candidates)
    out: list[Assignment] = []
    blind_seq = 0
    for source, tracks in sources.items():
        for track in tracks:
            got = assign(track, candidates, idf=idf,
                         tag_artists=tag_artists, blind_seq=blind_seq)
            if got.blind:
                blind_seq += 1
            got.source = source
            got.duplicate = got.uri in held.get(got.target, set())
            out.append(got)
    return out


def add_batches(assignments: Iterable[Assignment],
                limit: int = 100) -> Iterator[tuple[str, list[str]]]:
    """Group into (target, uris) batches within the playlist-add limit.

    100 uris per POST is the probed ceiling (2026-08-23; 150 returned 400).
    """
    by_target: dict[str, list[str]] = {}
    for a in assignments:
        if getattr(a, "duplicate", False):
            continue
        by_target.setdefault(a.target, []).append(a.uri)
    for target in sorted(by_target):
        uris = by_target[target]
        for i in range(0, len(uris), limit):
            yield target, uris[i:i + limit]
