"""Score how well a track fits each home playlist.

Three signals, all explainable to the user:
  - artist overlap: the playlist already contains tracks by this artist
  - tag similarity: cosine between the track's tags and the playlist's tag
    profile. Track-level Last.fm tags (`track.getTopTags`) REPLACE the
    weaker artist-level tags when available (`_resolve_tags`) — resolution,
    not mixing (ledger ruling P1) — and the reason string says which kind
    it saw (`tags: …` vs `artist tags: …`).
  - neighbours: this track's Last.fm `getSimilar` list, summed over
    whichever neighbours are already in the home — see `_neighbour_score`
    for the binding same-artist exclusion this signal depends on to avoid
    just re-deriving artist overlap under a new name.

Spotify's audio-features endpoint is deprecated for new apps, and Spotify no
longer returns artist genres at all in development mode — tags come from
Last.fm instead (see `sortify/tags.py`). Artist-level tags describe the
*artist*, not the track, which is weaker evidence than a track-level tag.
TAG_WEIGHT is the single knob for how much that weaker evidence counts
against artist overlap, which stays the primary signal (see the constant's
comment for the measured numbers; home-artist tag coverage is ~99% since
the 2026-08-18 backfill).
"""

from __future__ import annotations

import math
from collections import Counter

from .tags import _norm_name, clean_tags, track_key

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

# Neighbours (Last.fm getSimilar): a neighbour's `match` is per-pair
# similarity in [0, 1]; the summed matches are capped at NEIGHBOUR_SUM_CAP
# before weighting (unbounded sums would swamp everything else), and
# `_neighbour_score` still returns the raw sum (its documented interface).
#
# Measured 2026-08-19 by scripts/eval_suggest.py after the getSimilar
# backfill (scripts/backfill_similar.py: 2101/2101 home tracks, 987 with
# similar data, 2 misses):
#   .venv/bin/python scripts/eval_suggest.py --n 500 --seed 7 --search
#   artist-only baseline:            top1 0.278 top3 0.408 | absent 0.000
#   tags-only (incl. track tags):    top1 0.314 top3 0.546 | absent 0.325
#   NEIGHBOUR_WEIGHT=0.3 (this):     top1 0.314 top3 0.550 | absent 0.331
#   NEIGHBOUR_WEIGHT=1.0..3.0:       top3 up to 0.558 | absent up to 0.344
#     — but every weight above 0.3 breaks artist-overlap primacy
#     (TAG_WEIGHT + w*NEIGHBOUR_SUM_CAP must stay < 3.4), so 0.3 is the
#     committed ceiling: ~0.008 top3 is the measured price of the "owning
#     the artist always outranks similarity" invariant, paid knowingly.
#   One further accepted consequence: 0.3 * NEIGHBOUR_SUM_CAP = 0.3 < MIN_SCORE
#   (0.8), so a home matched ONLY by neighbours can never surface a new
#   CONFIDENT suggestion — neighbours re-rank homes that artist overlap or
#   tags already lifted over the threshold. Deliberate (ledger R2a), not an
#   oversight. R2a scopes to the confident tier (2026-08-20): when NOTHING
#   clears MIN_SCORE, sub-threshold homes — neighbour-only ones included —
#   may surface flagged `weak: True` (see `suggest()`); the confident
#   list's contract is unchanged.
# Note the tags-only row already includes track-level tags from
# lastfm_tracks.json (fetched by the same backfill) — that is what moved
# tags from 0.534 to 0.546 vs the 2026-08-18 artist-tag-only measurement.
NEIGHBOUR_WEIGHT = 0.3
NEIGHBOUR_SUM_CAP = 1.0

# Artist-similar (Last.fm artist.getSimilar): guess-tier-only by construction
# (see suggest()'s gate) — this signal is only ever consulted after a home
# has already failed to clear MIN_SCORE, so it can never itself promote a
# home into the confident tier no matter how high it scores, and sweeping
# ARTIST_SIM_WEIGHT therefore cannot touch a primacy invariant the way
# NEIGHBOUR_WEIGHT can - there is no confident-tier row to break.
#
# Measured 2026-08-21 by scripts/eval_suggest.py after the artist-similar
# backfill (scripts/backfill_artist_similar.py: 1449/1449 home artists
# attempted, 1438 tagged (10 misses, 1 error left absent)):
#   .venv/bin/python scripts/eval_suggest.py --n 500 --seed 7
#   .venv/bin/python scripts/eval_suggest.py --n 500 --seed 7 --search-artist-sim
#   OFF (ARTIST_SIM_WEIGHT=0, signal disabled): top1 0.312 top3 0.524 | artist-absent n=161 top1 0.155 top3 0.317
#   ARTIST_SIM_WEIGHT=0.25 .. 3.0 (every swept value): top1 0.314 top3 0.530 | artist-absent top1 0.161 top3 0.335
# Every non-zero weight from 0.25 to 3.0 produced IDENTICAL numbers - the
# signal either fires (any weight > 0, sim_sum capped by ARTIST_SIM_CAP
# below scales the guess-tier score but never reorders which home wins for
# this sample) or it doesn't (weight 0). Verified the self-check this
# implies (spec Evaluation 4): the non-absent-subset counts are bit-
# identical across the OFF row and every swept weight (n=339, top1=131,
# top3=211 in all seven runs) - the signal cannot reach the confident tier
# by construction, and it didn't. All weights tie, so per the task-5
# assignment ("if every weight ties, keep 1.0 and record the tie") this
# stays at the placeholder value, now a measured one: on the artist-absent
# subset it lifts top1 0.155 to 0.161 and top3 0.317 to 0.335 versus OFF,
# at any positive weight.
ARTIST_SIM_WEIGHT = 1.0
ARTIST_SIM_CAP = 1.0


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


def build_profile(
    tracks: list[dict], tag_artists: dict[str, dict], hints: list[str] | None = None
) -> dict:
    """Precompute what the suggester needs to know about one home playlist.

    tag_counts is built from ARTIST tags only; the seed track's vector may be
    track-level tags (_resolve_tags), so the cosine compares track vocabulary
    against an artist-tag profile. Asymmetric on purpose for now — only ~4% of
    cached tracks carry track tags, and the measured effect of the asymmetry
    is positive; revisit if track-tag coverage is ever backfilled harder.

    `hints` are the user's own words about this home (config's `home_hints`),
    already split and lowercased by the caller. They enter tag_counts at the
    strength of the profile's strongest ORGANIC tag — strong enough that a
    200-track home can't dilute them to noise, but inside the same TAG_WEIGHT
    channel as everything else, so the artist-overlap-primacy invariant needs
    no new analysis. Deliberately NOT run through `clean_tags`: the stoplist
    exists to drop Last.fm junk, and a word the user typed on purpose is the
    opposite of junk.
    """
    artist_counts: Counter = Counter()
    tag_counts: Counter = Counter()
    uris = set()
    track_keys: set[str] = set()
    artist_names: set[str] = set()
    for t in tracks:
        uris.add(t["uri"])
        for a in t["artists"]:
            track_keys.add(track_key(a.get("name") or "", t.get("name") or ""))
            if a.get("name"):
                artist_names.add(_norm_name(a["name"]))
            if not a.get("id"):
                continue
            artist_counts[a["id"]] += 1
            for tag in _cleaned_tags(tag_artists.get(a["id"], {})):
                tag_counts[tag] += 1
    hint_set = {h.strip().lower() for h in (hints or []) if h.strip()}
    if hint_set:
        weight = max(tag_counts.values(), default=1)
        for h in hint_set:
            tag_counts[h] += weight
    return {
        "artist_counts": artist_counts,
        "tag_counts": tag_counts,
        "uris": uris,
        "track_keys": track_keys,
        "hints": hint_set,
        "artist_names": artist_names,
    }


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


def _track_record(track: dict, track_map: dict[str, dict]) -> dict | None:
    """The track's `lastfm_tracks.json` record, or None if it has none.

    Tried under EVERY one of the track's (artist, title) keys, not just the
    first — a collab's fetch may have run under any one of its credited
    artists, and there is exactly one record to find, so the first non-miss
    hit wins. `_resolve_tags` still cleans whatever record this returns using
    the first artist's name specifically (its own contract), independent of
    which artist's key actually located it.
    """
    title = track.get("name") or ""
    for a in track.get("artists") or []:
        key = track_key(a.get("name") or "", title)
        record = track_map.get(key)
        if isinstance(record, dict) and not record.get("miss"):
            return record
    return None


def _resolve_tags(
    track: dict, tag_artists: dict[str, dict], track_map: dict[str, dict]
) -> tuple[Counter, str]:
    """The tags to score this track with, and which level they came from.

    Track-level tags (Last.fm `track.getTopTags`, via the track's own
    `lastfm_tracks.json` record) REPLACE artist-level tags when non-empty —
    resolution, not mixing (ledger ruling P1) — because a track-level tag is
    real evidence about this recording, not an average over everything the
    artist has made. Falls back to the artist-level tags (`_track_tags`)
    when there is no usable track record, exactly like before this feature.

    `track.getTopTags` doesn't hand back Last.fm's per-tag counts (see
    `LastFm.track_top_tags`), so `clean_tags`' floor can't apply the same way
    it does to artist tags — every stored track tag survives that gate
    (`floor=0`); only the stoplist and self-name filters still run.
    """
    record = _track_record(track, track_map)
    if record is not None:
        raw = record.get("tags") or []
        if raw:
            artists = track.get("artists") or []
            artist_name = artists[0].get("name") or "" if artists else ""
            cleaned = clean_tags(
                [{"name": t, "count": 0} for t in raw], artist_name, floor=0
            )
            if cleaned:
                return Counter(t for t, _w in cleaned), "track"
    return _track_tags(track, tag_artists), "artist"


def _neighbour_score(
    track: dict, prof: dict, track_map: dict[str, dict]
) -> tuple[float, int]:
    """Sum of `match` over this track's Last.fm neighbours already in `prof`.

    BINDING regression pin: a neighbour whose artist case-insensitively
    matches ANY of the seed track's own artists is excluded before it is
    scored and before it is counted — a home whose only matching neighbours
    are by the seed artist must get zero neighbour score and no neighbour
    reason. Without this exclusion the feature just re-derives artist
    overlap under a new name, which is the exact failure (an artist only
    ever getting suggested into its own playlists) this project exists to
    fix.

    Fix round 1: the exclusion check reuses `tags._norm_name` — the same
    normaliser `track_key` uses internally — rather than its own inline
    `.strip().lower()`. A separately-written normalizer is exactly how this
    drifted the first time: an artist name with an internal double space
    ("Beach  House") collapses to "beach house" via `track_key`/`_norm_name`
    but NOT via a bare `.strip().lower()`, so the same-artist exclusion
    silently failed to fire for exactly the case it exists to catch.

    The returned sum is intentionally uncapped — the cap that protects the
    artist-overlap-primacy pin (see NEIGHBOUR_SUM_CAP) is applied where the
    score is computed, not here, so this function's return value stays a
    plain, honest total for callers (Task 4's eval harness among them) that
    want the raw signal.
    """
    record = _track_record(track, track_map)
    if record is None:
        return 0.0, 0
    seed_artists = {_norm_name(a.get("name")) for a in (track.get("artists") or [])}
    total = 0.0
    count = 0
    for n in record.get("similar") or []:
        n_artist = n.get("artist") or ""
        if _norm_name(n_artist) in seed_artists:
            continue
        key = track_key(n_artist, n.get("track") or "")
        if key not in prof["track_keys"]:
            continue
        try:
            total += float(n.get("match", 0) or 0)
        except (TypeError, ValueError):
            continue
        count += 1
    return total, count


def _artist_sim_score(
    track: dict, prof: dict, artist_map: dict[str, dict]
) -> tuple[float, int, list[str]]:
    """Sum of best `match` per distinct home-present similar artist.

    Same binding exclusion as `_neighbour_score`, via the same normalizer:
    a similar artist matching ANY seed artist scores nothing. A neighbour
    listed by several credited artists counts once, at its best match —
    a collab must not double-spend one neighbour.
    """
    seed_names = {_norm_name(a.get("name")) for a in (track.get("artists") or [])}
    best: dict[str, tuple[float, str]] = {}
    for a in track.get("artists") or []:
        record = artist_map.get(a.get("id"))
        if not isinstance(record, dict) or record.get("miss"):
            continue
        for s in record.get("similar") or []:
            name = s.get("artist") or ""
            norm = _norm_name(name)
            if not norm or norm in seed_names or norm not in prof["artist_names"]:
                continue
            try:
                match = float(s.get("match", 0) or 0)
            except (TypeError, ValueError):
                continue
            if match > best.get(norm, (0.0, ""))[0]:
                best[norm] = (match, name)
    ranked = sorted(best.values(), key=lambda p: -p[0])
    return sum(m for m, _ in ranked), len(ranked), [n for _, n in ranked]


def suggest(
    track: dict,
    profiles: dict[str, dict],
    tag_artists: dict[str, dict],
    track_map: dict[str, dict] | None = None,
    artist_map: dict[str, dict] | None = None,
) -> list[dict]:
    """Rank home playlists for one track. Returns [{playlist_id, score, already, reasons}].

    `track_map` and `artist_map` are optional ({} when omitted) so callers
    that haven't been updated to pass `store.lastfm_track_map()` /
    `store.lastfm_artist_map()` yet keep working — see app.py's call sites,
    which all pass fresh ones.

    When no home clears MIN_SCORE and the track is filed nowhere, the
    sub-threshold ranking is returned instead — up to TOP_N homes with
    score > 0 (i.e. at least one real reason), each flagged `weak: True`
    so the frontend can present them as guesses, not confidence. A track
    with an `already` home never gets guesses (the list wasn't empty), and
    confident entries never carry the key at all, so the confident payload
    stays byte-identical to before this tier existed. `_artist_sim_score`
    is consulted only inside this else-branch, after the MIN_SCORE gate has
    already failed on the other three signals — it can rank the weak pool
    but can never itself lift a home into the confident tier.
    """
    track_map = track_map or {}
    artist_map = artist_map or {}
    track_tags, tag_level = _resolve_tags(track, tag_artists, track_map)
    tag_reason_prefix = "tags: " if tag_level == "track" else "artist tags: "
    results = []
    weak_pool = []
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
            hints = prof.get("hints") or set()
            hint_hits = sorted(t for t in track_tags if t in hints)
            if hint_hits:
                reasons.append("your hint: " + ", ".join(hint_hits[:3]))
            # Hint matches get their own line above; listing them again here
            # would double-credit one signal in the user's eyes.
            overlap = sorted(
                (t for t in track_tags if t in prof["tag_counts"] and t not in hints),
                key=lambda t: track_tags[t] * prof["tag_counts"][t],
                reverse=True,
            )
            if overlap:
                reasons.append(tag_reason_prefix + ", ".join(overlap[:3]))

        neighbour_sum, neighbour_count = _neighbour_score(track, prof, track_map)
        if neighbour_count:
            score += NEIGHBOUR_WEIGHT * min(neighbour_sum, NEIGHBOUR_SUM_CAP)
            reasons.append(
                f"{neighbour_count} similar track{'s' if neighbour_count > 1 else ''} already here"
            )

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
        else:
            sim_sum, sim_count, sim_names = _artist_sim_score(track, prof, artist_map)
            if sim_count:
                score += ARTIST_SIM_WEIGHT * min(sim_sum, ARTIST_SIM_CAP)
                reasons.append("similar artists: " + ", ".join(sim_names[:2]))
            if score > 0:
                weak_pool.append(
                    {
                        "playlist_id": pid,
                        "score": round(score, 2),
                        "pct": min(round(score * 10), 100),
                        "already": False,
                        "reasons": reasons,
                        "weak": True,
                    }
                )

    if not results and weak_pool:
        weak_pool.sort(key=lambda r: -r["score"])
        return weak_pool[:TOP_N]
    results.sort(key=lambda r: (not r["already"], -r["score"]))
    return results[:TOP_N]
