"""Cluster a playlist's tracks into coherent piles using Last.fm tags.

A pure function of (tracks, tags, params). No network, no file I/O — which
means re-clustering with different parameters costs nothing, and the whole
thing is testable offline. That matters: the first clustering of a 1372-track
playlist is unlikely to be the last.
"""

from __future__ import annotations

import math

from .community import louvain
from .tags import clean_tags

DEFAULTS = {
    "resolution": 1.0,       # Louvain resolution; higher = more, smaller piles
    "min_pile": 15,          # tracks; smaller piles merge into their neighbour
    "tag_floor": 10,         # drop tags Last.fm counts below this
    "max_tags_per_artist": 8,  # keep this many tags per artist, by weight
    "top_name_tags": 3,      # tags used to name a pile
}
UNTAGGED = "untagged"


def _vec(entry: dict, floor: int, keep: int) -> dict[str, float]:
    """Tag-weight vector for one cached artist, after hygiene.

    `data/tags.json` holds Last.fm's raw tags, so the stoplist, the count
    floor and the keep limit are applied here, per split — re-tuning any of
    them is free, where filtering at fetch time would have frozen them behind
    a ~700-request re-fetch.
    """
    cleaned = clean_tags(entry.get("tags", []), entry.get("name") or "",
                         floor=floor, keep=keep)
    return {t: float(w) for t, w in cleaned}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    dot = sum(a[t] * b[t] for t in shared)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _primary_artist(track: dict) -> str | None:
    """Spotify lists the primary credit first; featured guests come after."""
    for a in track.get("artists", []):
        if a.get("id"):
            return a["id"]
    return None


def _build_graph(vecs: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    ids = sorted(vecs)
    adj: dict[str, dict[str, float]] = {a: {} for a in ids}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            sim = _cosine(vecs[a], vecs[b])
            if sim > 0.0:
                adj[a][b] = sim
                adj[b][a] = sim
    return adj


def _centroid(artist_ids: list[str], vecs: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for aid in artist_ids:
        for t, w in vecs.get(aid, {}).items():
            out[t] = out.get(t, 0.0) + w
    return out


def _name_piles(groups: list[list[str]], vecs: dict, top: int) -> list[list[str]]:
    """Name each group by its most distinctive tags.

    TF-IDF against this playlist's own tag distribution, not global frequency:
    otherwise every pile in a rock playlist is called "rock".
    """
    centroids = [_centroid(g, vecs) for g in groups]
    n = len(groups)
    df: dict[str, int] = {}
    for c in centroids:
        for t in c:
            df[t] = df.get(t, 0) + 1
    names = []
    for c in centroids:
        total = sum(c.values()) or 1.0
        scored = [
            (t, (w / total) * math.log(1 + n / (1 + df.get(t, 0))))
            for t, w in c.items()
        ]
        scored.sort(key=lambda tw: (-tw[1], tw[0].lower()))
        names.append([t for t, _ in scored[:top]])
    return names


def _merge_small(groups: list[list[str]], vecs: dict, tracks: list[dict], min_pile: int) -> list[list[str]]:
    """Fold undersized piles into their nearest neighbour by centroid cosine.

    Repeats until every pile meets min_pile or only one remains. Size is
    counted in tracks, not artists — one prolific artist can carry a pile.
    """
    counts: dict[str, int] = {}
    for t in tracks:
        aid = _primary_artist(t)
        if aid:
            counts[aid] = counts.get(aid, 0) + 1

    groups = [list(g) for g in groups]
    while len(groups) > 1:
        sizes = [sum(counts.get(a, 0) for a in g) for g in groups]
        smallest = min(range(len(groups)), key=lambda i: (sizes[i], i))
        if sizes[smallest] >= min_pile:
            break
        cen = [_centroid(g, vecs) for g in groups]
        target = max(
            (i for i in range(len(groups)) if i != smallest),
            key=lambda i: (_cosine(cen[smallest], cen[i]), -i),
        )
        groups[target].extend(groups[smallest])
        groups[target].sort()
        groups.pop(smallest)
    return groups


def split_tracks(tracks: list[dict], tags: dict, params: dict | None = None) -> list[dict]:
    """Group `tracks` into piles. `tags` is the **inner** artists map from
    `data/tags.json` (`Store.tag_artists()`), keyed by Spotify artist id — not
    the versioned envelope, which would leave every track untagged.
    """
    p = {**DEFAULTS, **(params or {})}
    if not tracks:
        return []

    # Artists that carry usable tags, and the tracks that follow them.
    vecs: dict[str, dict[str, float]] = {}
    for t in tracks:
        aid = _primary_artist(t)
        if aid and aid not in vecs:
            v = _vec(tags.get(aid, {}), p["tag_floor"], p["max_tags_per_artist"])
            if v:
                vecs[aid] = v

    untagged_uris = [t["uri"] for t in tracks if _primary_artist(t) not in vecs]

    groups: list[list[str]] = []
    if vecs:
        comm = louvain(_build_graph(vecs), p["resolution"])
        by_comm: dict[int, list[str]] = {}
        for aid in sorted(vecs):
            by_comm.setdefault(comm[aid], []).append(aid)
        groups = [by_comm[c] for c in sorted(by_comm)]
        groups = _merge_small(groups, vecs, tracks, p["min_pile"])

    names = _name_piles(groups, vecs, p["top_name_tags"]) if groups else []

    # Emit piles, preserving original playlist order inside each.
    member_of: dict[str, int] = {}
    for i, g in enumerate(groups):
        for aid in g:
            member_of[aid] = i
    buckets: list[list[str]] = [[] for _ in groups]
    for t in tracks:
        idx = member_of.get(_primary_artist(t))
        if idx is not None:
            buckets[idx].append(t["uri"])

    # Every group holds at least one artist that came from a track, so every
    # bucket is non-empty; skipping empties here would only produce
    # non-contiguous pile ids.
    assert all(buckets), "a pile came out with no tracks"
    piles = [
        {"id": f"p{i + 1}", "name": " · ".join(names[i]) or f"pile {i + 1}",
         "tags": names[i], "uris": buckets[i]}
        for i in range(len(groups))
    ]
    if untagged_uris:
        piles.append({"id": UNTAGGED, "name": "untagged", "tags": [], "uris": untagged_uris})
    return piles
