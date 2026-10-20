"""Split one home into a chosen number of parts.

Pure: (tracks, the three Last.fm maps, n) in, parts out. No network, no file
I/O, deterministic. Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md.

The three signals, all already on disk:
- artist tags (`tags.json`): cosine of the primary artists' cleaned tags;
- track similarity (`lastfm_tracks.json` getSimilar): does either song list
  the other;
- artist similarity (`lastfm_artists.json`): does Last.fm call the two
  primary artists similar.

Clustering is average-linkage agglomerative on 1 - score via the
nearest-neighbour chain (O(n^2), pure Python), cut at exactly n parts with
sliver parts folded into their closest neighbour.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass, field

from .tags import clean_tags, track_key

W_TAGS = 0.5
W_TRACK = 0.35
W_ARTIST = 0.15
# Two songs by one artist whose getSimilar lists disagree: the same-artist
# terms drop from 1.0 to this, so a ballad can leave its artist's rockers.
SAME_ARTIST_FAR = 0.5
FAR_JACCARD = 0.1
# A neighbouring count is hinted only when it is at least this much cleaner.
HINT_MARGIN = 0.05
MIN_PARTS, MAX_PARTS = 2, 6
MIN_TRACKS, MAX_TRACKS = 10, 2000
TAG_FLOOR, TAG_KEEP, NAME_TAGS = 10, 8, 2
# How many extra cut levels to look through for a cut without slivers.
SLIVER_LEVELS = 60


class SplitError(ValueError):
    """The input cannot be split as asked; the message is for the user."""


@dataclass
class Suggestion:
    parts: list[list[str]]
    names: list[str]
    unknown: list[str]
    scores: dict[int, float]
    hint: int | None


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _primary(track: dict) -> tuple[str | None, str]:
    """The primary credited artist (id, name): the first one with an id."""
    for a in track.get("artists") or []:
        if a.get("id"):
            return a["id"], a.get("name") or ""
    return None, ""


def _unit_vec(entry: dict) -> dict[str, float]:
    cleaned = clean_tags(entry.get("tags") or [], entry.get("name") or "",
                         floor=TAG_FLOOR, keep=TAG_KEEP)
    v = {t: float(w) for t, w in cleaned}
    norm = math.sqrt(sum(x * x for x in v.values()))
    return {t: x / norm for t, x in v.items()} if norm else {}


def _track_record(track: dict, track_map: dict) -> dict | None:
    """The track's getSimilar record under ANY credited artist's key (a
    collab's record may live under any of them); first non-miss wins."""
    title = track.get("name") or ""
    for a in track.get("artists") or []:
        rec = track_map.get(track_key(a.get("name") or "", title))
        if isinstance(rec, dict) and not rec.get("miss"):
            return rec
    return None


@dataclass
class Signals:
    uris: list[str]
    artist: list[str | None]
    vec: dict[str, dict[str, float]]
    tsim: list[dict[int, float]]
    tkeys: list[frozenset]
    has_track: list[bool]
    asim: dict[tuple[str, str], float]
    has_data: list[bool]
    index: dict[str, int]
    _cos: dict = field(default_factory=dict)


def _dedupe(tracks: list[dict]) -> list[dict]:
    seen, out = set(), []
    for t in tracks:
        u = t.get("uri")
        if u and u not in seen:
            seen.add(u)
            out.append(t)
    return out


def build_signals(tracks: list[dict], tag_artists: dict, track_map: dict,
                  artist_map: dict) -> Signals:
    songs = _dedupe(tracks)
    uris = [t["uri"] for t in songs]
    prim = [_primary(t) for t in songs]
    artist = [p[0] for p in prim]

    vec: dict[str, dict[str, float]] = {}
    for aid in sorted({a for a in artist if a}):
        entry = tag_artists.get(aid)
        if isinstance(entry, dict) and not entry.get("miss"):
            v = _unit_vec(entry)
            if v:
                vec[aid] = v

    key_to_idx: dict[str, int] = {}
    for i, t in enumerate(songs):
        for a in t.get("artists") or []:
            key_to_idx.setdefault(track_key(a.get("name") or "", t.get("name") or ""), i)
    tsim: list[dict[int, float]] = [{} for _ in songs]
    tkeys: list[frozenset] = []
    has_track: list[bool] = []
    for i, t in enumerate(songs):
        sims = (_track_record(t, track_map) or {}).get("similar") or []
        keys = set()
        for s in sims:
            k = track_key(s.get("artist") or "", s.get("track") or "")
            keys.add(k)
            j = key_to_idx.get(k)
            if j is not None and j != i:
                m = float(s.get("match") or 0.0)
                if m > tsim[i].get(j, 0.0):
                    tsim[i][j] = m
        tkeys.append(frozenset(keys))
        has_track.append(bool(sims))

    name_to_aid: dict[str, str] = {}
    for aid, name in prim:
        if aid:
            name_to_aid.setdefault(_norm(name), aid)
    asim: dict[tuple[str, str], float] = {}
    with_artist_sim: set[str] = set()
    for aid in sorted({a for a in artist if a}):
        rec = artist_map.get(aid)
        if not isinstance(rec, dict):
            continue
        sims = rec.get("similar") or []
        if sims:
            with_artist_sim.add(aid)
        for s in sims:
            other = name_to_aid.get(_norm(s.get("artist") or ""))
            if other and other != aid:
                m = float(s.get("match") or 0.0)
                for k in ((aid, other), (other, aid)):
                    if m > asim.get(k, 0.0):
                        asim[k] = m

    has = [bool(vec.get(a)) or has_track[i] or (a in with_artist_sim)
           for i, a in enumerate(artist)]
    return Signals(uris=uris, artist=artist, vec=vec, tsim=tsim, tkeys=tkeys,
                   has_track=has_track, asim=asim, has_data=has,
                   index={u: i for i, u in enumerate(uris)})


def _tag_cos(sig: Signals, a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    key = (a, b) if a < b else (b, a)
    got = sig._cos.get(key)
    if got is None:
        va, vb = sig.vec.get(a), sig.vec.get(b)
        if not va or not vb:
            got = 0.0
        else:
            if len(vb) < len(va):
                va, vb = vb, va
            got = sum(w * vb.get(t, 0.0) for t, w in va.items())
        sig._cos[key] = got
    return got


def _far(sig: Signals, i: int, j: int, track: float) -> bool:
    if track > 0.0 or not (sig.has_track[i] and sig.has_track[j]):
        return False
    a, b = sig.tkeys[i], sig.tkeys[j]
    union = len(a | b)
    return union > 0 and len(a & b) / union < FAR_JACCARD


def pair_score(sig: Signals, i: int, j: int) -> float:
    """How alike songs i and j are, in [0, 1]."""
    track = max(sig.tsim[i].get(j, 0.0), sig.tsim[j].get(i, 0.0))
    ai, aj = sig.artist[i], sig.artist[j]
    if ai is not None and ai == aj:
        same = SAME_ARTIST_FAR if _far(sig, i, j, track) else 1.0
        return W_TAGS * same + W_TRACK * track + W_ARTIST * same
    tag = _tag_cos(sig, ai, aj)
    art = sig.asim.get((ai, aj), 0.0) if ai and aj else 0.0
    return W_TAGS * tag + W_TRACK * track + W_ARTIST * art


def _similarity(sig: Signals, idx: list[int]) -> list[array]:
    n = len(idx)
    S = [array("d", bytes(8 * n)) for _ in range(n)]
    for a in range(n):
        ia, row = idx[a], S[a]
        row[a] = 1.0
        for b in range(a + 1, n):
            s = pair_score(sig, ia, idx[b])
            row[b] = s
            S[b][a] = s
    return S


def _dendrogram(S: list[array]) -> list[tuple[float, int, int]]:
    """Average-linkage merges via the nearest-neighbour chain.

    Each merge is (distance, a, b) where a is the index that goes on
    representing the merged cluster. Merges come out of chain order, not
    distance order; `_cut` sorts them, which is valid for average linkage
    (it is reducible), the same way scipy's implementation does it.
    """
    n = len(S)
    D = [array("d", (1.0 - x for x in row)) for row in S]
    size = [1] * n
    alive = [True] * n
    merges: list[tuple[float, int, int]] = []
    chain: list[int] = []
    remaining = n
    while remaining > 1:
        if not chain:
            chain.append(next(i for i in range(n) if alive[i]))
        c = chain[-1]
        prev = chain[-2] if len(chain) > 1 else -1
        best, bd = prev, (D[c][prev] if prev >= 0 else math.inf)
        row = D[c]
        for k in range(n):
            if alive[k] and k != c and row[k] < bd:
                best, bd = k, row[k]
        if best == prev:
            chain.pop()
            chain.pop()
            a, b = min(c, prev), max(c, prev)
            merges.append((bd, a, b))
            na, nb = size[a], size[b]
            Da, Db = D[a], D[b]
            for k in range(n):
                if alive[k] and k != a and k != b:
                    d = (na * Da[k] + nb * Db[k]) / (na + nb)
                    Da[k] = d
                    D[k][a] = d
            size[a] = na + nb
            alive[b] = False
            remaining -= 1
        else:
            chain.append(best)
    return merges


def _cut(n: int, merges: list[tuple[float, int, int]], k: int) -> list[list[int]]:
    """The k clusters of the dendrogram, each a sorted list of item indices,
    ordered by their smallest index."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    clusters = n
    for m in sorted(range(len(merges)), key=lambda m: (merges[m][0], m)):
        if clusters <= k:
            break
        _, a, b = merges[m]
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
            clusters -= 1
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])


def _mean_link(S: list[array], g: list[int], h: list[int]) -> float:
    if not g or not h:
        return -1.0
    return sum(S[x][y] for x in g for y in h) / (len(g) * len(h))


def _parts_at(S: list[array], merges, k: int, floor: int) -> list[list[int]]:
    """Exactly k clusters with no sliver: the first cut level with exactly k
    clusters of at least `floor` items, slivers folded into the closest of
    those; the plain k-cut when no level within SLIVER_LEVELS has one."""
    n = len(S)
    for level in range(k, min(n, k + SLIVER_LEVELS) + 1):
        groups = _cut(n, merges, level)
        big = [g for g in groups if len(g) >= floor]
        if len(big) == k:
            for g in (g for g in groups if len(g) < floor):
                t = max(range(k), key=lambda t: (_mean_link(S, g, big[t]), -t))
                big[t] = sorted(big[t] + g)
            return big
        if len(big) > k:
            break
    return _cut(n, merges, k)


def _cleanliness(S: list[array], groups: list[list[int]]) -> float:
    label = {}
    for gi, g in enumerate(groups):
        for x in g:
            label[x] = gi
    n = len(S)
    win = wn = bet = bn = 0.0
    for a in range(n):
        row, la = S[a], label[a]
        for b in range(a + 1, n):
            if label[b] == la:
                win += row[b]
                wn += 1
            else:
                bet += row[b]
                bn += 1
    return (win / wn if wn else 0.0) - (bet / bn if bn else 0.0)


def _place_unknown(sig: Signals, i: int, parts: list[list[int]]) -> int:
    """A song with no data: with most of its artist's songs, else the largest."""
    aid = sig.artist[i]
    if aid is not None:
        counts = [sum(1 for x in p if sig.artist[x] == aid and x != i) for p in parts]
        if max(counts, default=0) > 0:
            return max(range(len(parts)), key=lambda t: (counts[t], -t))
    return max(range(len(parts)), key=lambda t: (len(parts[t]), -t))


def _best_index(sig: Signals, i: int, parts: list[list[int]]) -> int:
    if not sig.has_data[i]:
        return _place_unknown(sig, i, parts)

    def mean(p: list[int]) -> float:
        peers = [x for x in p if x != i and sig.has_data[x]]
        if not peers:
            return -1.0
        return sum(pair_score(sig, i, x) for x in peers) / len(peers)

    return max(range(len(parts)), key=lambda t: (mean(parts[t]), -t))


def _names_for(sig: Signals, parts: list[list[int]]) -> list[str]:
    """Each part named by its most distinctive tags: TF-IDF against this
    playlist's own parts, so a rock playlist's parts are not all "rock"."""
    centroids = []
    for p in parts:
        c: dict[str, float] = {}
        for x in p:
            for t, w in sig.vec.get(sig.artist[x] or "", {}).items():
                c[t] = c.get(t, 0.0) + w
        centroids.append(c)
    n = len(parts)
    df: dict[str, int] = {}
    for c in centroids:
        for t in c:
            df[t] = df.get(t, 0) + 1
    names, used = [], set()
    for i, c in enumerate(centroids):
        total = sum(c.values()) or 1.0
        scored = sorted(((t, (w / total) * math.log(1 + n / (1 + df[t]))) for t, w in c.items()),
                        key=lambda tw: (-tw[1], tw[0].lower()))
        name = " · ".join(t for t, _ in scored[:NAME_TAGS]) or f"Part {i + 1}"
        base, k = name, 2
        while name in used:
            name, k = f"{base} {k}", k + 1
        used.add(name)
        names.append(name)
    return names


def suggest(tracks: list[dict], n: int, tag_artists: dict, track_map: dict,
            artist_map: dict) -> Suggestion:
    if not MIN_PARTS <= n <= MAX_PARTS:
        raise SplitError(f"choose between {MIN_PARTS} and {MAX_PARTS} parts")
    sig = build_signals(tracks, tag_artists, track_map, artist_map)
    total = len(sig.uris)
    if total < MIN_TRACKS:
        raise SplitError(f"too small to split — fewer than {MIN_TRACKS} songs")
    if total > MAX_TRACKS:
        raise SplitError(f"too big to split here — more than {MAX_TRACKS} songs")
    # Clustered in uri order, not playlist order: the dendrogram breaks ties
    # by index, and the spec wants ties broken by uri, so the same songs give
    # the same split whatever order the playlist holds them in.
    data = sorted((i for i in range(total) if sig.has_data[i]), key=lambda i: sig.uris[i])
    if len(data) < 2 * n:
        raise SplitError("not enough Last.fm data on these songs to suggest a split")

    S = _similarity(sig, data)
    merges = _dendrogram(S)
    floor = max(3, math.ceil(0.05 * total))
    scores: dict[int, float] = {}
    chosen: list[list[int]] = []
    for k in (n - 1, n, n + 1):
        if MIN_PARTS <= k <= MAX_PARTS and k <= len(data):
            groups = _parts_at(S, merges, k, floor)
            scores[k] = round(_cleanliness(S, groups), 4)
            if k == n:
                chosen = groups

    parts = [[data[x] for x in g] for g in chosen]
    unknown = [i for i in range(total) if not sig.has_data[i]]
    for i in unknown:
        parts[_place_unknown(sig, i, parts)].append(i)
    parts = [sorted(p) for p in parts]          # playlist order within a part
    parts.sort(key=lambda p: (-len(p), min(sig.uris[i] for i in p)))

    hint = None
    others = [k for k in scores if k != n]
    if others:
        best = max(others, key=lambda k: (scores[k], -k))
        if scores[best] - scores[n] >= HINT_MARGIN:
            hint = best
    return Suggestion(parts=[[sig.uris[i] for i in p] for p in parts],
                      names=_names_for(sig, parts),
                      unknown=[sig.uris[i] for i in unknown],
                      scores=scores, hint=hint)


def place(tracks: list[dict], groups: list[list[str]], to_place: list[str],
          tag_artists: dict, track_map: dict, artist_map: dict) -> dict[str, int]:
    sig = build_signals(tracks, tag_artists, track_map, artist_map)
    parts = [[sig.index[u] for u in g if u in sig.index] for g in groups]
    out = {}
    for u in to_place:
        i = sig.index.get(u)
        if i is None:
            continue
        nonempty = [t for t in range(len(parts)) if parts[t]]
        if not nonempty:
            out[u] = 0
            continue
        pick = _best_index(sig, i, [parts[t] for t in nonempty])
        out[u] = nonempty[pick]
    return out
