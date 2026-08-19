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


def pick_sitting(
    uris: list[str], durations: dict[str, int], decided: dict, target_ms: int
) -> list[str]:
    """The next undecided tracks from a pile, in playlist order, up to target.

    Order is preserved rather than shuffled so an interrupted sitting resumes
    identically. Always returns at least one track if any remain — a single
    track longer than the target must still be servable.
    """
    picked: list[str] = []
    total = 0
    for u in uris:
        if u in decided:
            continue
        d = durations.get(u, 0)
        if picked and total + d > target_ms:
            break
        picked.append(u)
        total += d
    return picked


# ---- sitting markers and reconciliation ------------------------------------
#
# A sitting playlist names itself so the ACCOUNT, not `splits.json`, can say
# which ones exist. That inversion is the point: creating a playlist and
# recording it are two operations that cannot be made atomic, so a record-
# authoritative design leaks on a lost create response, on a crash between
# create and save, and on a failed unfollow with no free slot to re-record
# into — none of which is a concurrency bug, and all of which survived four
# rounds of per-slot fixes (see
# `.superpowers/sdd/2026-08-17-playlist-splitting/progress.md`, Ruling R17).
# Reading them back costs nothing: `my_playlists()` serves from cache.json.

SITTING_PREFIX = "▶ "
SITTING_DESCRIPTION = "sortify sitting — safe to delete"


def is_sitting_playlist(entry: dict, me: str | None) -> bool:
    """True if this cached listing entry is one of sortify's own sittings.

    Three conditions, each load-bearing:

    - **the name prefix**, which is what a human sees in the Spotify app;
    - **owned by the user**, because unfollowing someone else's playlist
      removes it from their library — the sweep must never reach a playlist
      sortify did not create;
    - **the description**, which is the only marker a user is unlikely to
      reproduce by accident, and the one that separates a sitting from a
      *materialised pile* — a permanent playlist whose own description
      promises sortify will never delete it.

    A missing `description` key means the entry was cached before the listing
    kept descriptions, and falls back to the prefix. Absent is permissive;
    present-and-different is not. Refusing absent would find nothing until the
    user spent ~21 calls on a Refresh, while trusting a description that is
    there and says something else would be the dangerous direction. The rule
    therefore tightens by itself on the next refresh, with no migration.
    """
    if not me or entry.get("owner") != me:
        return False
    if not (entry.get("name") or "").startswith(SITTING_PREFIX):
        return False
    description = entry.get("description")
    return description is None or description == SITTING_DESCRIPTION


def select_orphans(
    playlists: list[dict], me: str | None, protected: set[str], cap: int
) -> tuple[list[dict], int]:
    """Leftover sittings in a cached listing, capped, plus how many remain.

    `protected` holds every id that is legitimately in use right now — an
    in-flight materialisation, or another split's live sitting. The cap bounds
    one user action's burst: if this rule is ever wrong, being wrong costs
    `cap` calls rather than one per playlist in the library.
    """
    found = [p for p in playlists
             if p.get("id") not in protected and is_sitting_playlist(p, me)]
    return found[:cap], max(len(found) - cap, 0)
