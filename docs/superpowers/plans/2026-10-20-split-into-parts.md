# Split a home into parts — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old Split tab with one where the user picks a home and a number of parts, gets a suggested grouping, drags songs between parts on the phone, taps to preview, and only at Done writes the parts to Spotify (original kept as a 🐾 subset or reused as one part).

**Architecture:** A pure grouping engine (`sortify/splitparts.py`) clusters the playlist's songs from the three local Last.fm stores. A thin API layer in `app.py` keeps one draft per home in `data/splitparts.json`, runs the Last.fm fill-in and the suggestion in a background thread, and executes Done in a resumable, original-last order. The frontend is a new `#view-split` in `app.js`: picker, parts screen with pinned chips and pointer-driven drag/tap, and a Done screen. The old split, sittings and queued materialiser are deleted.

**Tech Stack:** Python 3.12 stdlib + FastAPI (existing), vanilla JS (no build step), pytest, the hand-run stub-DOM harness `tests/ui_harness.mjs`.

**Spec:** `docs/superpowers/specs/2026-10-20-split-into-parts-design.md`

## Global Constraints

- Nothing is written to Spotify before Done. Before Done the only Spotify calls are reads: one snapshot read per Suggest/Done/Re-read (`GET /playlists/{id}?fields=snapshot_id`) and the track list when the snapshot changed (~1 call per 100 tracks).
- Only homes can be split; every part becomes a home (`home_ids` + `sticky_home_ids`).
- Parts: 2–6 (`MIN_PARTS = 2`, `MAX_PARTS = 6`). Homes with fewer than 10 tracks are not splittable (`MIN_TRACKS = 10`); more than 2000 is refused (`MAX_TRACKS = 2000`).
- Part sizes follow the music; never balanced.
- Grouping weights: `W_TAGS = 0.5`, `W_TRACK = 0.35`, `W_ARTIST = 0.15`; same-artist "far" rule: Jaccard of similar-track sets `< 0.1` → same-artist term `0.5` instead of `1.0`; hint margin `HINT_MARGIN = 0.05`; sliver floor `max(3, ceil(5% of the playlist))`.
- Done order: create and fill every new part first; only then touch the original (🐾 rename and drop from home lists, or batched removal). Done is resumable and never creates a part twice.
- Batch add: 100 uris per call (probed 2026-08-23). Batch remove: `REMOVE_BATCH` (Task 1 fixes the value from a probe; 100 until then).
- The original keeps its name when it becomes a part; no rename of the original is offered.
- Local files (`is_local`) cannot be added through the API: they stay in the original only and are listed on the Done screen.
- No new dependencies. No numpy. Pure Python clustering.
- Every test makes zero Spotify calls. Never call api.spotify.com by hand; a live probe only through the app's client, single-shot, with the user's go-ahead (CLAUDE.md).
- No emoji in UI chrome (house rule; 🐾 in a playlist NAME is data and fine). Mobile-only app: no keyboard shortcuts as the answer to anything.
- Tests: `.venv/bin/pytest -q` and `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs` (`node` is not on PATH) both green before every commit.
- Work happens in a sibling worktree `~/kode/spotify/sortify-splitparts` with its own `.venv` (memory: nested worktrees break the ledger symlink; a shared venv tests the wrong tree).

## Review Focus

1. **A song listed twice in the original** — expected: one row in the split, and each part gets it once at Done. Tests: Task 2 (`test_duplicate_uris_appear_once`), Task 7 (`test_done_adds_each_song_once`).
2. **A part holding only local files** — expected: treated as empty at Done ("not created"), never an empty playlist created. Test: Task 7 (`test_part_of_only_local_files_is_not_created`).
3. **Suggest pressed again while a suggestion is still running** — expected: the second request gets 409 "already running", one thread writes the draft. Test: Task 4 (`test_second_suggest_while_running_is_refused`).
4. **Removing the part that is currently shown** — expected: the screen shows another part, never a blank list. Test: Task 9 (`SP removing the shown part shows another one`).
5. **Done pressed twice quickly** — expected: one run; the second gets 409 "already running"; no duplicate playlists. Test: Task 7 (`test_second_done_while_running_is_refused`).

---

## File structure

- Create `sortify/splitparts.py` — the pure grouping engine (signals, pair score, NN-chain clustering, slivers, unknown placement, names, cleanliness/hint, single-song placement).
- Create `sortify/splitparts_api.py` — the draft store helpers and Done planning as pure-ish functions over the store and an injected Spotify client (keeps `app.py` from growing by another 600 lines). `app.py` gets thin route functions that call it.
- Modify `sortify/spotify.py` — `REMOVE_BATCH`, `remove_many`, `snapshot`.
- Modify `sortify/store.py` — `splitparts()` / `save_splitparts()`; remove `splits`, `queue`, `pacing` accessors.
- Modify `sortify/app.py` — new `/api/splitparts/*` and `/api/track_preview` routes; delete the old split/sitting/queue code.
- Delete `sortify/split.py`, `sortify/redistribute.py`, `sortify/pacing.py` (Task 3 checks for other users first).
- Modify `sortify/static/index.html`, `sortify/static/app.js`, `sortify/static/style.css` — the new view; delete the old split views, the sitting bar and the Now card's sitting mode.
- Tests: create `tests/test_splitparts.py`, `tests/test_splitparts_api.py`, `tests/test_track_preview.py`; add `tests/test_playback.py` cases; delete the old split/sitting/queue tests; edit `tests/ui_harness.mjs`.
- Docs: `CLAUDE.md`.

---

### Task 1: Batched removal and a snapshot read on the Spotify client

**Files:**
- Modify: `sortify/spotify.py` (near `remove_from_playlist`, ~line 864)
- Test: `tests/test_playback.py` (append)

**Interfaces:**
- Produces:
  - `REMOVE_BATCH: int` module constant in `sortify/spotify.py` (100 until the probe in Step 6 says otherwise)
  - `Spotify.remove_many(playlist_id: str, uris: list[str]) -> str | None` — removes in batches of `REMOVE_BATCH`, one `DELETE /playlists/{id}/items` per batch, body `{"items": [{"uri": u}, ...]}`; returns the last batch's `snapshot_id`; no call for an empty list; duplicates in `uris` are sent once.
  - `Spotify.snapshot(playlist_id: str) -> str | None` — one `GET /playlists/{id}` with `params={"fields": "snapshot_id"}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_playback.py`)

```python
# ---- batched removal and snapshot reads (split into parts) -----------------


def _recorder(sp, monkeypatch, payload=None):
    sent = []

    def fake_request(method, url, **kwargs):
        sent.append({"method": method, "url": url, "json": kwargs.get("json"),
                     "params": kwargs.get("params")})
        return FakeResponse(200, payload if payload is not None else {"snapshot_id": f"s{len(sent)}"})

    monkeypatch.setattr(sp.http, "request", fake_request)
    return sent


def test_remove_many_sends_one_call_per_batch(sp, monkeypatch):
    from sortify import spotify as spmod

    monkeypatch.setattr(spmod, "REMOVE_BATCH", 2)
    sent = _recorder(sp, monkeypatch)

    snap = sp.remove_many("P1", ["u1", "u2", "u3"])

    assert [s["method"] for s in sent] == ["DELETE", "DELETE"]
    assert sent[0]["url"].endswith("/playlists/P1/items")
    assert sent[0]["json"] == {"items": [{"uri": "u1"}, {"uri": "u2"}]}
    assert sent[1]["json"] == {"items": [{"uri": "u3"}]}
    assert snap == "s2"


def test_remove_many_sends_a_duplicate_once(sp, monkeypatch):
    sent = _recorder(sp, monkeypatch)

    sp.remove_many("P1", ["u1", "u1", "u2"])

    assert sent[0]["json"] == {"items": [{"uri": "u1"}, {"uri": "u2"}]}


def test_remove_many_with_nothing_makes_no_call(sp, monkeypatch):
    sent = _recorder(sp, monkeypatch)

    assert sp.remove_many("P1", []) is None
    assert sent == []


def test_snapshot_reads_only_the_snapshot_id(sp, monkeypatch):
    sent = _recorder(sp, monkeypatch, {"snapshot_id": "abc"})

    assert sp.snapshot("P1") == "abc"
    assert sent[0]["method"] == "GET"
    assert sent[0]["url"].endswith("/playlists/P1")
    assert sent[0]["params"] == {"fields": "snapshot_id"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_playback.py -k "remove_many or snapshot_reads"`
Expected: FAIL — `AttributeError: 'Spotify' object has no attribute 'remove_many'`

- [ ] **Step 3: Implement** — in `sortify/spotify.py`, add the constant next to the other module constants (after `LIKED_ID = "liked"`):

```python
# Tracks per DELETE /playlists/{id}/items. Batch removal works (two tracks in
# one call, read back, probed 2026-10-20); the ceiling is set from the probe
# recorded in CLAUDE.md, the same way batch add's 100 was.
REMOVE_BATCH = 100
```

and add these methods directly after `remove_from_playlist`:

```python
    def remove_many(self, playlist_id: str, uris: list[str]) -> str | None:
        """Remove tracks in batches of REMOVE_BATCH, one call per batch.

        Every occurrence of a uri goes (Spotify removes by uri), so a
        duplicate in `uris` is sent once. Returns the last batch's
        snapshot_id, or None when there was nothing to remove (no call).
        """
        unique = list(dict.fromkeys(u for u in uris if u))
        snap = None
        for i in range(0, len(unique), REMOVE_BATCH):
            batch = unique[i:i + REMOVE_BATCH]
            resp = self.request(
                "DELETE", f"/playlists/{playlist_id}/items",
                json={"items": [{"uri": u} for u in batch]},
            )
            snap = (resp or {}).get("snapshot_id") or snap
        return snap

    def snapshot(self, playlist_id: str) -> str | None:
        """The playlist's current snapshot_id: one call, nothing else read."""
        resp = self.request("GET", f"/playlists/{playlist_id}",
                            params={"fields": "snapshot_id"})
        return (resp or {}).get("snapshot_id")
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q tests/test_playback.py && .venv/bin/pytest -q`
Expected: PASS (full suite green)

- [ ] **Step 5: Commit**

```bash
git add sortify/spotify.py tests/test_playback.py
git commit -m "feat(split): batched removal and a snapshot read on the Spotify client"
```

- [ ] **Step 6 (controller, with the user's explicit go-ahead): probe the batch-remove ceiling.** Not an implementer step. Check `.venv/bin/spx budget` first. Through the app's client in the LIVE tree (`~/kode/spotify/sortify`, which shares the ledger), on a throwaway playlist created and deleted in a `try/finally`: create a private playlist named `sortify probe — safe to delete` (1 call), add 100 uris from `data/cache.json` (1 call), `remove_many` with all 100 in one call (1 call), read `total` back (1 call), unfollow (1 call) — about 5 calls. If the 100-uri DELETE is refused with 400, repeat with 50. Record the result in CLAUDE.md's batch-delete sentence ("ceiling probed 2026-10-2x: 100 accepted" or "100 → 400, 50 accepted") and set `REMOVE_BATCH` to the accepted value; commit `docs: batch-remove ceiling probed`.

---

### Task 2: The grouping engine

**Files:**
- Create: `sortify/splitparts.py`
- Test: `tests/test_splitparts.py`

**Interfaces:**
- Consumes: `sortify.tags.clean_tags(raw, artist_name, floor, keep) -> list[tuple[str, int]]`, `sortify.tags.track_key(artist, title) -> str`.
- Produces (all in `sortify/splitparts.py`):
  - constants `W_TAGS, W_TRACK, W_ARTIST, SAME_ARTIST_FAR, FAR_JACCARD, HINT_MARGIN, MIN_PARTS, MAX_PARTS, MIN_TRACKS, MAX_TRACKS`
  - `class SplitError(ValueError)` — message is user-printable
  - `@dataclass Suggestion(parts: list[list[str]], names: list[str], unknown: list[str], scores: dict[int, float], hint: int | None)` — `parts` are uri lists, largest first, each in playlist order
  - `suggest(tracks: list[dict], n: int, tag_artists: dict, track_map: dict, artist_map: dict) -> Suggestion`
  - `place(tracks: list[dict], groups: list[list[str]], to_place: list[str], tag_artists: dict, track_map: dict, artist_map: dict) -> dict[str, int]` — for each uri in `to_place`, the index into `groups` it fits best (groups exclude those uris; an empty group never wins over a non-empty one)
  - `has_data(tracks, tag_artists, track_map, artist_map) -> set[str]` — uris with at least one signal (used to mark "?")
  - `name_parts(tracks: list[dict], groups: list[list[str]], tag_artists: dict) -> list[str]` — distinctive-tag names, unique, `"Part N"` fallback

- [ ] **Step 1: Write the failing tests** — `tests/test_splitparts.py`:

```python
"""The split-into-parts grouping engine: pure, offline, deterministic.

Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md.
Fixtures are synthetic: artists carry one dominant Last.fm tag each, so the
"right" grouping is known by construction.
"""

import time

import pytest

from sortify import splitparts as spp
from sortify.tags import track_key


def make(groups: dict[str, int], start: int = 0):
    """Tracks + tag map: `groups` maps a tag to how many songs carry it.
    Each song has its own artist tagged only with that tag (count 100)."""
    tracks, tags = [], {}
    i = start
    for tag, count in groups.items():
        for _ in range(count):
            aid = f"a{i}"
            tracks.append({"uri": f"spotify:track:t{i}", "name": f"Song {i}",
                           "artists": [{"id": aid, "name": f"Artist {i}"}]})
            tags[aid] = {"name": f"Artist {i}", "tags": [{"name": tag, "count": 100}],
                         "miss": False}
            i += 1
    return tracks, tags


def part_tags(sugg, tracks, tags):
    by_uri = {t["uri"]: t for t in tracks}
    return [sorted({tags[by_uri[u]["artists"][0]["id"]]["tags"][0]["name"] for u in p})
            for p in sugg.parts]


def test_exactly_n_parts_from_clear_groups():
    tracks, tags = make({"rock": 10, "jazz": 10, "ambient": 10})

    s = spp.suggest(tracks, 3, tags, {}, {})

    assert len(s.parts) == 3
    assert sorted(part_tags(s, tracks, tags)) == [["ambient"], ["jazz"], ["rock"]]


def test_sizes_follow_the_music():
    tracks, tags = make({"rock": 30, "jazz": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert [len(p) for p in s.parts] == [30, 10]


def test_no_sliver_parts():
    tracks, tags = make({"rock": 20, "jazz": 20, "polka": 2})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert len(s.parts) == 2
    assert min(len(p) for p in s.parts) >= 3
    assert sum(len(p) for p in s.parts) == 42


def test_same_input_same_output_regardless_of_order():
    tracks, tags = make({"rock": 12, "jazz": 9, "ambient": 7})

    a = spp.suggest(tracks, 3, tags, {}, {})
    b = spp.suggest(list(reversed(tracks)), 3, tags, {}, {})

    assert [sorted(p) for p in a.parts] == [sorted(p) for p in b.parts]
    assert a.names == b.names


def test_each_part_keeps_playlist_order():
    tracks, tags = make({"rock": 10, "jazz": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    order = {t["uri"]: i for i, t in enumerate(tracks)}
    for p in s.parts:
        assert [order[u] for u in p] == sorted(order[u] for u in p)


def test_duplicate_uris_appear_once():
    tracks, tags = make({"rock": 10, "jazz": 10})

    s = spp.suggest(tracks + [dict(tracks[0])], 2, tags, {}, {})

    every = [u for p in s.parts for u in p]
    assert len(every) == len(set(every)) == 20


def test_songs_without_data_follow_their_artist_and_are_marked():
    tracks, tags = make({"rock": 10, "jazz": 10})
    # Two songs by an artist with no tags: one has a getSimilar record that
    # points at the jazz songs, so it has data; the other has nothing.
    tracks.append({"uri": "spotify:track:x1", "name": "With record",
                   "artists": [{"id": "ax", "name": "Ghost"}]})
    tracks.append({"uri": "spotify:track:x2", "name": "No record",
                   "artists": [{"id": "ax", "name": "Ghost"}]})
    jazz = [t for t in tracks if tags.get(t["artists"][0]["id"], {}).get("tags", [{}])[0].get("name") == "jazz"]
    track_map = {track_key("Ghost", "With record"): {
        "similar": [{"artist": t["artists"][0]["name"], "track": t["name"], "match": 1.0}
                    for t in jazz], "tags": [], "miss": False}}

    s = spp.suggest(tracks, 2, tags, track_map, {})

    part_of = {u: i for i, p in enumerate(s.parts) for u in p}
    jazz_part = part_of[jazz[0]["uri"]]
    assert part_of["spotify:track:x1"] == jazz_part
    assert part_of["spotify:track:x2"] == jazz_part
    assert s.unknown == ["spotify:track:x2"]


def test_a_song_with_no_data_and_no_artist_mate_goes_to_the_largest_part():
    tracks, tags = make({"rock": 14, "jazz": 10})
    tracks.append({"uri": "spotify:track:lonely", "name": "Lonely",
                   "artists": [{"id": "az", "name": "Nobody"}]})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert "spotify:track:lonely" in s.parts[0]
    assert s.unknown == ["spotify:track:lonely"]


def test_same_artist_songs_split_when_their_similar_lists_disagree():
    tracks, tags = make({"rock": 8, "ballad": 8})
    tags["aA"] = {"name": "Both", "tags": [{"name": "rock", "count": 100},
                                          {"name": "ballad", "count": 100}], "miss": False}
    tracks += [{"uri": "spotify:track:A1", "name": "Loud", "artists": [{"id": "aA", "name": "Both"}]},
               {"uri": "spotify:track:A2", "name": "Soft", "artists": [{"id": "aA", "name": "Both"}]}]
    by_tag = {"rock": [], "ballad": []}
    for t in tracks[:16]:
        by_tag[tags[t["artists"][0]["id"]]["tags"][0]["name"]].append(t)

    def sims(group):
        return [{"artist": t["artists"][0]["name"], "track": t["name"], "match": 1.0} for t in group]

    track_map = {track_key("Both", "Loud"): {"similar": sims(by_tag["rock"]), "tags": [], "miss": False},
                 track_key("Both", "Soft"): {"similar": sims(by_tag["ballad"]), "tags": [], "miss": False}}

    s = spp.suggest(tracks, 2, tags, track_map, {})

    part_of = {u: i for i, p in enumerate(s.parts) for u in p}
    assert part_of["spotify:track:A1"] == part_of[by_tag["rock"][0]["uri"]]
    assert part_of["spotify:track:A2"] == part_of[by_tag["ballad"][0]["uri"]]
    assert part_of["spotify:track:A1"] != part_of["spotify:track:A2"]


def test_the_hint_names_a_clearly_cleaner_count():
    tracks, tags = make({"rock": 10, "jazz": 10, "ambient": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert s.hint == 3
    assert set(s.scores) == {2, 3}


def test_no_hint_when_the_chosen_count_is_right():
    tracks, tags = make({"rock": 10, "jazz": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert s.hint is None


def test_names_come_from_distinctive_tags():
    tracks, tags = make({"rock": 10, "jazz": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert sorted(s.names) == ["jazz", "rock"]


def test_out_of_range_counts_and_sizes_are_refused():
    tracks, tags = make({"rock": 10, "jazz": 10})
    with pytest.raises(spp.SplitError):
        spp.suggest(tracks, 1, tags, {}, {})
    with pytest.raises(spp.SplitError):
        spp.suggest(tracks, 7, tags, {}, {})
    small, small_tags = make({"rock": 9})
    with pytest.raises(spp.SplitError):
        spp.suggest(small, 2, small_tags, {}, {})
    big = [{"uri": f"spotify:track:b{i}", "name": f"B{i}", "artists": []} for i in range(2001)]
    with pytest.raises(spp.SplitError):
        spp.suggest(big, 2, {}, {}, {})


def test_not_enough_data_is_refused_with_a_reason():
    tracks = [{"uri": f"spotify:track:n{i}", "name": f"N{i}",
               "artists": [{"id": f"n{i}", "name": f"N{i}"}]} for i in range(12)]
    with pytest.raises(spp.SplitError, match="Last.fm"):
        spp.suggest(tracks, 2, {}, {}, {})


def test_place_puts_a_song_where_it_fits_best():
    tracks, tags = make({"rock": 10, "jazz": 10})
    rock = [t["uri"] for t in tracks[:10]]
    jazz = [t["uri"] for t in tracks[10:]]

    got = spp.place(tracks, [rock[1:], jazz[1:], []], [rock[0], jazz[0]], tags, {}, {})

    assert got == {rock[0]: 0, jazz[0]: 1}


def test_artist_similarity_counts():
    tracks, tags = make({"rock": 10, "jazz": 10})
    # One more song by an untagged artist that Last.fm calls similar to the
    # jazz artists: it has data (artist-similar) and lands with jazz.
    tracks.append({"uri": "spotify:track:s1", "name": "S1",
                   "artists": [{"id": "as", "name": "Sideman"}]})
    artist_map = {"as": {"name": "Sideman", "miss": False,
                         "similar": [{"artist": f"Artist {i}", "match": 1.0} for i in range(10, 20)]}}

    s = spp.suggest(tracks, 2, tags, {}, artist_map)

    part_of = {u: i for i, p in enumerate(s.parts) for u in p}
    assert part_of["spotify:track:s1"] == part_of[tracks[10]["uri"]]
    assert "spotify:track:s1" not in s.unknown


def test_a_thousand_songs_in_reasonable_time():
    groups = {f"g{i}": 50 for i in range(20)}
    tracks, tags = make(groups)

    t0 = time.monotonic()
    s = spp.suggest(tracks, 4, tags, {}, {})
    elapsed = time.monotonic() - t0

    assert len(s.parts) == 4
    assert elapsed < 15, f"{elapsed:.1f}s"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_splitparts.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'sortify.splitparts'`

- [ ] **Step 3: Write `sortify/splitparts.py`**

```python
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


def has_data(tracks: list[dict], tag_artists: dict, track_map: dict, artist_map: dict) -> set[str]:
    sig = build_signals(tracks, tag_artists, track_map, artist_map)
    return {u for u, h in zip(sig.uris, sig.has_data) if h}


def name_parts(tracks: list[dict], groups: list[list[str]], tag_artists: dict) -> list[str]:
    sig = build_signals(tracks, tag_artists, {}, {})
    return _names_for(sig, [[sig.index[u] for u in g if u in sig.index] for g in groups])


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
```

Note for the implementer: the "hint" test expects `scores` to hold only valid counts (`{2, 3}` for n=2 because 1 is below `MIN_PARTS`). If `test_the_hint_names_a_clearly_cleaner_count` fails because the clean 3-group fixture's score gap is under `HINT_MARGIN`, report the measured scores (print `s.scores`) rather than changing the constant: the margin is a spec value, tuned in Task 11 on real homes.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q tests/test_splitparts.py && .venv/bin/pytest -q`
Expected: PASS, full suite green; note the time the 1000-song test took in the report.

- [ ] **Step 5: Commit**

```bash
git add sortify/splitparts.py tests/test_splitparts.py
git commit -m "feat(split): the split-into-parts grouping engine"
```

---

### Task 3: Delete the old split, sittings and queued materialiser (backend)

**Files:**
- Modify: `sortify/app.py`, `sortify/store.py`, `sortify/naming.py` (only if `split_output_name` ends up unused), `tests/conftest.py` (only if it references removed store methods)
- Delete: `sortify/split.py`, `sortify/redistribute.py`, `sortify/pacing.py`, `sortify/community.py` (each only after Step 1 shows no other user)
- Delete tests: `tests/test_split.py`, `tests/test_split_api.py`, `tests/test_split_decisions.py`, `tests/test_split_now_integration.py`, `tests/test_refresh_split.py`, `tests/test_redistribute.py`, `tests/test_sittings.py`, `tests/test_sitting_reconcile.py`, `tests/fuzz_sittings.py`, `tests/test_materialise.py`, `tests/test_queue_api.py`, `tests/test_queue_worker.py`, `tests/test_pacing.py`, `tests/test_rename_outputs.py`, `tests/test_community.py` (the last only if `community.py` goes)

**Interfaces:**
- Consumes: nothing new.
- Produces: `/api/now` (and `/api/now/suggest`) responses no longer carry a `sitting` key; `/api/playlists` no longer carries `sitting_orphans`. `Store` no longer has `splits`, `save_splits`, `queue`, `save_queue`, `pacing`, `save_pacing`, `QUEUE_DEFAULT`, `PACING_DEFAULT`. No `/api/split/*` or `/api/sittings/*` route exists.

This is a deletion task: there is no new behaviour to test-drive. The safety net is the full suite plus explicit "nothing references it any more" greps.

- [ ] **Step 1: Confirm which modules have other users**

Run:
```bash
grep -rn -E "from \.(split|redistribute|pacing|community) |from \. import .*\b(split|redistribute|pacing|community)\b|import sortify\.(split|redistribute|pacing|community)" sortify scripts tests | grep -v -E "^sortify/(split|redistribute|pacing|community)\.py"
```
Expected: only `sortify/app.py` (and the test files being deleted) import `split`, `redistribute` and `pacing`; only `sortify/split.py` imports `community`. If anything else imports one of them, keep that module and note it in the report.

- [ ] **Step 2: Delete the routes and helpers in `sortify/app.py`**

Delete these routes and their functions entirely:
`POST /api/split/{playlist_id}` (`create_split`), `GET /api/split/{playlist_id}/progress` (`split_progress`), `GET /api/split/{playlist_id}` (`get_split`), `POST /api/split/{playlist_id}/recluster` (`recluster`), `POST /api/split/{playlist_id}/sitting` (`start_sitting`), `POST /api/split/{playlist_id}/sitting/finish` (`finish_sitting`), `POST /api/sittings/cleanup` (`cleanup_sittings`), `POST /api/split/{playlist_id}/queue` (`enqueue_piles`), `POST /api/split/{playlist_id}/rename_outputs`, `GET /api/split/{playlist_id}/queue` (`queue_status`), `POST /api/split/{playlist_id}/queue/pause` (`pause_queue`), `POST /api/split/{playlist_id}/queue/resume` (`resume_queue`), `DELETE /api/split/{playlist_id}/queue` (`cancel_queue`), `POST /api/split/{playlist_id}/decide` (`decide`).

Delete these helpers and models: `SplitParams`, `_split_summary`, `_pile_progress`, `_run_split` (and the split progress state it writes), `SittingIn`, `_sweep_sitting_orphans`, `_find_sitting_orphans`, `_abandon_orphaned_playlist`, `_materialise_sitting`, `_pile_fingerprint`, `_materialise_plan`, `_claim_materialisation`, `_rerecord_materialisation`, `_materialise_tick`, `_readopt_materialisation`, `_apply_queue_state`, `_set_queue_state`, `_queue_progress`, `_queue_next_action`, `_drain_queue`, `_drain_queue_body`, `QueueIn`, `_start_queue_worker`, `_effective_queue`, `_require_owned_queue`, `DecideIn`, `_sitting_for_context`, and every module-level constant, lock or global used only by them.

Then remove the references outside those blocks:
- the `"sitting": _sitting_for_context(ctx_id),` entry in the Now payload (~line 4242) and any `sitting`-only branch around it;
- the `"sitting_orphans": _find_sitting_orphans(splits),` entry in `/api/playlists` (~line 453) and the `splits = store.splits()` read feeding it;
- any startup hook or call site of `_start_queue_worker` / `_sweep_sitting_orphans`;
- the imports `from .pacing import Governor`, `from .split import (...)`, `from .redistribute ...`, and `split_output_name` from the `.naming` import if nothing else uses it.

Leave `_merge_save_tag_artists`, `_merge_save_lastfm_tracks`, `_merge_save_lastfm_artists`, `_lastfm_client`, `_cached_tracks`, `BULK_RESERVE`/`bulk=` in `spotify.py`: they have other users (the Now card, playlist reads) or are general budget classes.

- [ ] **Step 3: Delete the store accessors in `sortify/store.py`**

Delete `splits`, `save_splits`, the `QUEUE_DEFAULT` and `PACING_DEFAULT` constants, `queue`, `save_queue`, `pacing`, `save_pacing`, and the comments that describe them. Keep `_versioned` (the Last.fm envelopes use it). In the `CACHED_READS` comment, change "config/queue/pacing/splits/…" to "config/…".

- [ ] **Step 4: Delete the modules and the tests** listed under Files (only those Step 1 cleared). If `naming.split_output_name` has no remaining user, delete it and its tests in `tests/test_naming.py`.

- [ ] **Step 5: Prove nothing references the old feature**

Run:
```bash
grep -rn -E "_sitting|sitting_|Sitting|_materialis|_queue|queue\(\)|save_queue|pacing|Governor|redistribut|split_tracks|SplitParams|/api/split/|/api/sittings|store\.splits|save_splits|select_orphans|is_sitting_playlist|pick_sitting|louvain" sortify scripts tests --include=*.py
```
Expected: no matches, except unrelated words you can name in the report (e.g. a comment in `spotify.py` about sittings being disposable — reword it to drop the reference, since sittings no longer exist).

- [ ] **Step 6: Run the suite and fix fallout**

Run: `.venv/bin/pytest -q`
Expected: green. A remaining failure is a test that incidentally asserted the removed `sitting`/`sitting_orphans` keys or used a removed store method: delete that assertion (or that test, if it only existed for the removed feature) and say which in the report. Never weaken an assertion about something that still exists.

Run also: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | tail -1` — expected still green (the harness stubs the server; the frontend is removed in Task 8).

- [ ] **Step 7: Commit**

```bash
git add -A sortify tests
git commit -m "refactor(split): delete the old split, sittings and queued materialiser (backend)"
```

---

### Task 4: Draft store, the homes list, and Suggest

**Files:**
- Modify: `sortify/store.py` (add accessors)
- Create: `sortify/splitparts_api.py`
- Modify: `sortify/app.py` (routes; place them in a new `# ---- split into parts ----` section after the `/api/playlists/{id}/subset` route)
- Test: `tests/test_splitparts_api.py`

**Interfaces:**
- Consumes (Task 2): `splitparts.suggest`, `splitparts.has_data`, `SplitError`, `MIN_TRACKS`, `MAX_PARTS`, `MIN_PARTS`. (Task 1): `Spotify.snapshot`. Existing: `app._cached_tracks(pid, snapshot_id)`, `app._resolve_homes(cfg, playlists, exclude, input_ids)`, `app._effective_input_ids(cfg, playlists)`, `app._lastfm_client()`, `tags.enrich`, `tags.fetch_track`, `tags.track_key`, `app._merge_save_tag_artists`, `app._merge_save_lastfm_tracks`, `app._merge_save_lastfm_artists`, `app._now_iso()`, `sp.my_playlists()`, `store.folders()`.
- Produces:
  - `Store.splitparts() -> dict` (envelope `{"version": 1, "drafts": {}}`), `Store.save_splitparts(payload: dict) -> None`
  - in `sortify/splitparts_api.py`:
    - `slim_tracks(tracks: list[dict]) -> list[dict]` — `{uri, name, artists: [{id, name}], is_local}` per song, deduped, playlist order
    - `_songs(tracks: list[dict]) -> dict[str, dict]` — `{uri: {name, artists, local}}` for the UI
    - `draft_from(playlist: dict, snapshot_id: str | None, tracks: list[dict], sugg: Suggestion, n: int) -> dict` — the draft shape below
    - `get_draft(store, pid) -> dict | None`, `put_draft(store, draft) -> None`, `drop_draft(store, pid) -> None`
    - `missing_lastfm(tracks, tag_artists, track_map, artist_map) -> tuple[dict[str, str], list[tuple[str, str]], list[tuple[str, str]]]` — (artist id → name needing tags, [(artist name, title)] needing a track record, [(artist id, name)] needing artist-similar)
  - Draft shape (persisted in `data/splitparts.json` under `drafts[pid]`):
    ```
    {"playlist_id": str, "name": str, "snapshot_id": str|None, "n": int,
     "parts": [{"key": "p1", "name": str, "uris": [str, ...]}, ...],
     "unknown": [uri, ...], "hint": int|None, "scores": {"2": float, ...},
     "songs": {uri: {"name": str, "artists": str, "local": bool}},
     "tracks": [{"uri", "name", "artists": [{"id", "name"}], "is_local"}, ...],
     "done": null | {...}}
    ```
    `songs` is what the UI draws; `tracks` is what the engine needs for
    remove-part and re-read without reading Spotify (playlist order, deduped).
    ```
    ```
  - Routes:
    - `GET /api/splitparts/homes` → `{"homes": [{"id", "name", "total", "folder", "splittable": bool, "reason": str|None, "draft": bool}]}` (0 Spotify calls; sorted by name, case-insensitive)
    - `POST /api/splitparts/{pid}/suggest` body `{"n": int}` → `{"started": true}`; 409 `{"detail": "already running"}` while a job for any playlist runs; 404 if `pid` is not a home; 400 for `n` outside 2–6
    - `GET /api/splitparts/{pid}/progress` → `{"state": "idle"|"running"|"done"|"error", "phase": str, "done": int, "total": int, "message": str|None}`
    - `GET /api/splitparts/{pid}` → the draft, 404 when none
    - `DELETE /api/splitparts/{pid}` → `{"ok": true}` (discards the draft)

- [ ] **Step 1: Write the failing tests** — `tests/test_splitparts_api.py`:

```python
"""Split into parts: the draft, the homes list, Suggest, and (later tasks)
moves, parts and Done. Zero Spotify calls: every client method is faked."""

import threading

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from sortify import splitparts_api as spa

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

HOME = {"id": "H1", "name": "Big Home", "owner": "me", "editable": True,
        "total": 24, "snapshot_id": "snap-1", "image": None, "description": ""}
SMALL = {"id": "H2", "name": "Tiny", "owner": "me", "editable": True,
         "total": 5, "snapshot_id": "snap-2", "image": None, "description": ""}
OTHER = {"id": "X1", "name": "[Inbox]", "owner": "me", "editable": True,
         "total": 40, "snapshot_id": "snap-x", "image": None, "description": ""}


def tracks_for(groups):
    tracks, tags, i = [], {}, 0
    for tag, count in groups.items():
        for _ in range(count):
            aid = f"a{i}"
            tracks.append({"uri": f"spotify:track:t{i}", "name": f"Song {i}",
                           "artists": [{"id": aid, "name": f"Artist {i}"}],
                           "is_local": False})
            tags[aid] = {"name": f"Artist {i}", "tags": [{"name": tag, "count": 100}], "miss": False}
            i += 1
    return tracks, tags


TRACKS, TAGS = tracks_for({"rock": 14, "jazz": 10})


@pytest.fixture
def client(monkeypatch):
    appmod.store.save_config({"client_id": "c" * 32, "input_ids": ["X1"],
                              "home_ids": ["H1", "H2"], "sticky_home_ids": []})
    appmod.store.save_cache({"playlists": {"H1": {"snapshot_id": "snap-1", "tracks": list(TRACKS),
                                                  "fetched_at": 1.0}},
                             "artists": {}, "me": {"id": "me"},
                             "playlist_list": {"fetched_at": 1.0, "items": [HOME, SMALL, OTHER]}})
    appmod.store.save_tag_artists(dict(TAGS))
    appmod.store.save_splitparts({"version": 1, "drafts": {}})
    calls = []
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: [HOME, SMALL, OTHER])
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: calls.append(("snapshot", pid)) or "snap-1")
    monkeypatch.setattr(appmod.sp, "playlist_tracks",
                        lambda pid: calls.append(("tracks", pid)) or list(TRACKS))
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)
    # Suggest runs on a thread in production; tests run it inline.
    monkeypatch.setattr(appmod, "_splitparts_threaded", False)
    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.calls = calls
    yield c
    appmod._splitparts_job.clear()


def test_homes_lists_only_homes_with_splittability(client):
    res = client.get("/api/splitparts/homes").json()["homes"]

    assert [h["id"] for h in res] == ["H1", "H2"]
    big, tiny = res
    assert big["splittable"] is True and big["reason"] is None
    assert tiny["splittable"] is False and "fewer than 10" in tiny["reason"]
    assert client.calls == []


def test_suggest_builds_and_saves_a_draft(client):
    res = client.post("/api/splitparts/H1/suggest", json={"n": 2})
    assert res.status_code == 200

    prog = client.get("/api/splitparts/H1/progress").json()
    assert prog["state"] == "done"
    draft = client.get("/api/splitparts/H1").json()
    assert draft["n"] == 2
    assert [len(p["uris"]) for p in draft["parts"]] == [14, 10]
    assert [p["key"] for p in draft["parts"]] == ["p1", "p2"]
    assert draft["songs"]["spotify:track:t0"] == {"name": "Song 0", "artists": "Artist 0", "local": False}
    assert appmod.store.splitparts()["drafts"]["H1"]["snapshot_id"] == "snap-1"


def test_suggest_reads_only_the_snapshot_when_unchanged(client):
    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert client.calls == [("snapshot", "H1")]


def test_suggest_rereads_tracks_when_the_snapshot_moved(client, monkeypatch):
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: client.calls.append(("snapshot", pid)) or "snap-9")

    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert client.calls == [("snapshot", "H1"), ("tracks", "H1")]
    assert client.get("/api/splitparts/H1").json()["snapshot_id"] == "snap-9"


def test_suggest_refuses_a_non_home_and_a_bad_count(client):
    assert client.post("/api/splitparts/X1/suggest", json={"n": 2}).status_code == 404
    assert client.post("/api/splitparts/H1/suggest", json={"n": 1}).status_code == 400
    assert client.post("/api/splitparts/H1/suggest", json={"n": 7}).status_code == 400


def test_a_split_error_lands_in_progress_not_a_draft(client):
    appmod.store.save_tag_artists({})   # no Last.fm data at all

    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    prog = client.get("/api/splitparts/H1/progress").json()
    assert prog["state"] == "error"
    assert "Last.fm" in prog["message"]
    assert client.get("/api/splitparts/H1").status_code == 404


def test_second_suggest_while_running_is_refused(client, monkeypatch):
    appmod._splitparts_job.update({"pid": "H1", "state": "running"})

    res = client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert res.status_code == 409
    assert "already running" in res.json()["detail"]


def test_discarding_a_draft(client):
    client.post("/api/splitparts/H1/suggest", json={"n": 2})

    assert client.delete("/api/splitparts/H1").json() == {"ok": True}
    assert client.get("/api/splitparts/H1").status_code == 404


def test_missing_lastfm_lists_what_to_fetch():
    tracks = [{"uri": "u1", "name": "One", "artists": [{"id": "a1", "name": "A"}]},
              {"uri": "u2", "name": "Two", "artists": [{"id": "a2", "name": "B"}]}]
    tags = {"a1": {"name": "A", "tags": [], "miss": True}}
    track_map = {"a\x1fone": {"similar": [], "tags": [], "miss": True}}
    artist_map = {"a2": {"name": "B", "similar": [], "miss": False}}

    need_tags, need_tracks, need_similar = spa.missing_lastfm(tracks, tags, track_map, artist_map)

    assert need_tags == {"a2": "B"}
    assert need_tracks == [("B", "Two")]
    assert need_similar == [("a1", "A")]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_splitparts_api.py`
Expected: FAIL — `ImportError: cannot import name 'splitparts_api'`

- [ ] **Step 3: Add the store accessors** — in `sortify/store.py`, after `save_preview_rejects`:

```python
    # splitparts.json: {version, drafts: {playlist_id: draft}} — the
    # split-into-parts drafts. Every move the user makes is written here at
    # once, so leaving the Split tab and coming back restores the split.
    SPLITPARTS_DEFAULT = {"version": 1, "drafts": {}}

    def splitparts(self) -> dict:
        return self._versioned("splitparts.json", self.SPLITPARTS_DEFAULT)

    def save_splitparts(self, payload: dict) -> None:
        self._save("splitparts.json", payload)
```

- [ ] **Step 4: Write `sortify/splitparts_api.py`**

```python
"""Split into parts: the draft and the Done plan, kept out of app.py.

Pure over a Store and plain data; app.py owns the routes, the threads and
the Spotify client. Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md.
"""

from __future__ import annotations

import threading

from .splitparts import Suggestion
from .tags import track_key

# One lock for every read-modify-write of splitparts.json: a move and a
# Done step from two requests must not lose each other's write.
_lock = threading.RLock()


def _songs(tracks: list[dict]) -> dict[str, dict]:
    out = {}
    for t in tracks:
        u = t.get("uri")
        if u and u not in out:
            out[u] = {"name": t.get("name") or "",
                      "artists": ", ".join(a.get("name") or "" for a in t.get("artists") or []),
                      "local": bool(t.get("is_local"))}
    return out


def slim_tracks(tracks: list[dict]) -> list[dict]:
    """What the engine needs per song, deduped, playlist order kept."""
    out, seen = [], set()
    for t in tracks:
        u = t.get("uri")
        if u and u not in seen:
            seen.add(u)
            out.append({"uri": u, "name": t.get("name") or "",
                        "artists": [{"id": a.get("id"), "name": a.get("name") or ""}
                                    for a in t.get("artists") or []],
                        "is_local": bool(t.get("is_local"))})
    return out


def draft_from(playlist: dict, snapshot_id: str | None, tracks: list[dict],
               sugg: Suggestion, n: int) -> dict:
    return {
        "playlist_id": playlist["id"],
        "name": playlist.get("name") or "",
        "snapshot_id": snapshot_id,
        "n": n,
        "parts": [{"key": f"p{i + 1}", "name": name, "uris": list(uris)}
                  for i, (uris, name) in enumerate(zip(sugg.parts, sugg.names))],
        "unknown": list(sugg.unknown),
        "hint": sugg.hint,
        "scores": {str(k): v for k, v in sugg.scores.items()},
        "songs": _songs(tracks),
        "tracks": slim_tracks(tracks),
        "done": None,
    }


def get_draft(store, pid: str) -> dict | None:
    with _lock:
        d = store.splitparts()["drafts"].get(pid)
        return d if isinstance(d, dict) else None


def put_draft(store, draft: dict) -> None:
    with _lock:
        env = store.splitparts()
        env["drafts"][draft["playlist_id"]] = draft
        store.save_splitparts(env)


def drop_draft(store, pid: str) -> None:
    with _lock:
        env = store.splitparts()
        env["drafts"].pop(pid, None)
        store.save_splitparts(env)


def missing_lastfm(tracks: list[dict], tag_artists: dict, track_map: dict,
                   artist_map: dict) -> tuple[dict[str, str], list[tuple[str, str]], list[tuple[str, str]]]:
    """What Last.fm has never been asked about for these songs. A recorded
    miss counts as asked — never re-fetched (the house write-once rule)."""
    need_tags: dict[str, str] = {}
    need_tracks: list[tuple[str, str]] = []
    need_similar: list[tuple[str, str]] = []
    seen_similar: set[str] = set()
    for t in tracks:
        artists = [a for a in t.get("artists") or [] if a.get("name")]
        if not artists or t.get("is_local"):
            continue
        for a in artists:
            aid = a.get("id")
            if aid and aid not in tag_artists and aid not in need_tags:
                need_tags[aid] = a["name"]
        title = t.get("name") or ""
        if title and not any(isinstance(track_map.get(track_key(a["name"], title)), dict)
                             for a in artists):
            need_tracks.append((artists[0]["name"], title))
        first = artists[0]
        aid = first.get("id")
        if aid and aid not in seen_similar and not isinstance(artist_map.get(aid), dict):
            seen_similar.add(aid)
            need_similar.append((aid, first["name"]))
    return need_tags, need_tracks, need_similar
```

- [ ] **Step 5: Add the routes to `sortify/app.py`** — imports at the top with the other local imports: `from . import splitparts as spp` and `from . import splitparts_api as spa`. Then the new section:

```python
# ---- split into parts ------------------------------------------------------
#
# Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md. Before
# Done, the only Spotify calls are reads: the snapshot (1) and the track
# list when that moved (~1 per 100). Suggest runs on a thread because the
# Last.fm fill-in for never-seen songs is paced at ~4 requests a second.

_splitparts_job: dict = {}
_splitparts_job_lock = threading.Lock()
# Tests flip this to run jobs inline.
_splitparts_threaded = True


class SplitSuggestIn(BaseModel):
    n: int


def _splitparts_home(pid: str) -> dict:
    cfg = store.config()
    items = sp.my_playlists()
    homes = _resolve_homes(cfg, items, "", _effective_input_ids(cfg, items))
    for p in homes:
        if p["id"] == pid:
            return p
    raise HTTPException(404, "not one of your homes")


@app.get("/api/splitparts/homes")
def splitparts_homes():
    cfg = store.config()
    items = sp.my_playlists()
    homes = _resolve_homes(cfg, items, "", _effective_input_ids(cfg, items))
    drafts = store.splitparts()["drafts"]
    folders = store.folders()
    out = []
    for p in sorted(homes, key=lambda p: (p.get("name") or "").lower()):
        total = p.get("total") or 0
        reason = None
        if total < spp.MIN_TRACKS:
            reason = f"too small to split — fewer than {spp.MIN_TRACKS} songs"
        elif total > spp.MAX_TRACKS:
            reason = f"too big to split here — more than {spp.MAX_TRACKS} songs"
        out.append({"id": p["id"], "name": p.get("name") or "", "total": total,
                    "folder": (folders.get(p["id"]) or {}).get("path"),
                    "splittable": reason is None, "reason": reason,
                    "draft": p["id"] in drafts})
    return {"homes": out}


def _splitparts_fill(tracks: list[dict], progress) -> None:
    """Fetch Last.fm data for never-seen songs; never raises. A missing key
    or a Last.fm failure just leaves those songs without data ("?")."""
    fm = _lastfm_client()
    if fm is None:
        return
    need_tags, need_tracks, need_similar = spa.missing_lastfm(
        tracks, store.tag_artists(), store.lastfm_track_map(), store.lastfm_artist_map())
    total = len(need_tags) + len(need_tracks) + len(need_similar)
    done = 0
    progress(phase="looking up songs on Last.fm", done=0, total=total)
    if need_tags:
        cached = store.tag_artists()
        try:
            merged = enrich(need_tags, cached, fm, _now_iso(),
                            on_progress=lambda d, t: progress(done=d))
        except LastFmError as exc:
            log.warning("split: tag fetch stopped early: %s", exc)
            merged = exc.partial if exc.partial is not None else cached
        if len(merged) > len(cached):
            _merge_save_tag_artists(merged)
        done = len(need_tags)
    for artist, title in need_tracks:
        try:
            record = fetch_track(fm, artist, title, time.time())
            _merge_save_lastfm_tracks({track_key(artist, title): record})
        except Exception:
            log.warning("split: track record failed for %r — %r", artist, title, exc_info=True)
        done += 1
        progress(done=done)
    for aid, name in need_similar:
        try:
            similar = fm.artist_similar(name)
            _merge_save_lastfm_artists({aid: {"name": name, "similar": similar or [],
                                              "fetched_at": _now_iso(), "miss": similar is None}})
        except Exception:
            log.warning("split: artist-similar failed for %r", name, exc_info=True)
        done += 1
        progress(done=done)


def _splitparts_run_suggest(playlist: dict, n: int) -> None:
    pid = playlist["id"]

    def progress(**fields):
        with _splitparts_job_lock:
            _splitparts_job.update(fields)

    try:
        progress(phase="reading the playlist", done=0, total=0)
        snap = sp.snapshot(pid)
        tracks = _cached_tracks(pid, snap)
        _splitparts_fill(tracks, progress)
        progress(phase="grouping", done=0, total=0)
        sugg = spp.suggest(tracks, n, store.tag_artists(), store.lastfm_track_map(),
                           store.lastfm_artist_map())
        spa.put_draft(store, spa.draft_from(playlist, snap, tracks, sugg, n))
        progress(state="done", phase="done", message=None)
    except spp.SplitError as exc:
        progress(state="error", message=str(exc))
    except SpotifyError as exc:
        progress(state="error", message=str(exc))
    except Exception as exc:  # never leave the job "running" forever
        log.exception("split: suggest failed")
        progress(state="error", message=f"suggest failed: {exc}")


@app.post("/api/splitparts/{pid}/suggest")
def splitparts_suggest(pid: str, body: SplitSuggestIn):
    if not spp.MIN_PARTS <= body.n <= spp.MAX_PARTS:
        raise HTTPException(400, f"choose between {spp.MIN_PARTS} and {spp.MAX_PARTS} parts")
    playlist = _splitparts_home(pid)
    with _splitparts_job_lock:
        if _splitparts_job.get("state") == "running":
            raise HTTPException(409, "already running")
        _splitparts_job.clear()
        _splitparts_job.update({"pid": pid, "state": "running", "phase": "starting",
                                "done": 0, "total": 0, "message": None})
    if _splitparts_threaded:
        threading.Thread(target=_splitparts_run_suggest, args=(playlist, body.n),
                         daemon=True).start()
    else:
        _splitparts_run_suggest(playlist, body.n)
    return {"started": True}


@app.get("/api/splitparts/{pid}/progress")
def splitparts_progress(pid: str):
    with _splitparts_job_lock:
        job = dict(_splitparts_job)
    if job.get("pid") != pid:
        return {"state": "idle", "phase": "", "done": 0, "total": 0, "message": None}
    return {k: job.get(k) for k in ("state", "phase", "done", "total", "message")}


@app.get("/api/splitparts/{pid}")
def splitparts_get(pid: str):
    draft = spa.get_draft(store, pid)
    if draft is None:
        raise HTTPException(404, "no split in progress for this playlist")
    return draft


@app.delete("/api/splitparts/{pid}")
def splitparts_discard(pid: str):
    spa.drop_draft(store, pid)
    return {"ok": True}
```

Check that `enrich`, `fetch_track`, `track_key`, `LastFmError` are already imported in `app.py` (the Now fill-in uses them; `grep -n "from .tags import" sortify/app.py`); add any that are missing to that import.

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest -q tests/test_splitparts_api.py && .venv/bin/pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 7: Commit**

```bash
git add sortify/store.py sortify/splitparts_api.py sortify/app.py tests/test_splitparts_api.py
git commit -m "feat(split): drafts, the homes list and Suggest"
```

---

### Task 5: Moving songs, adding and removing parts

**Files:**
- Modify: `sortify/splitparts_api.py`, `sortify/app.py` (same section as Task 4)
- Test: `tests/test_splitparts_api.py` (append)

**Interfaces:**
- Consumes (Task 4): `spa.get_draft`, `spa.put_draft`, `spa._lock`, the draft shape incl. `tracks` (built by `spa.slim_tracks`). (Task 2): `spp.place(tracks, groups, to_place, tag_artists, track_map, artist_map) -> dict[str, int]`, `spp.MAX_PARTS`, `spp.MIN_PARTS`.
- Produces:
  - `spa.move(draft: dict, uri: str, to_key: str) -> dict` — pure; raises `KeyError` for an unknown uri or part
  - `spa.add_part(draft: dict) -> dict` — appends `{"key": "p<max+1>", "name": "New part", "uris": []}`; raises `ValueError("at most 6 parts")` at `MAX_PARTS`
  - `spa.removal_moves(draft: dict, key: str, tag_artists, track_map, artist_map) -> dict[str, str]` — uri → destination part key
  - `spa.remove_part(draft: dict, key: str, moves: dict[str, str]) -> dict` — raises `ValueError("at least 2 parts")` when 2 remain
  - Routes, all returning the updated draft unless noted:
    - `POST /api/splitparts/{pid}/move` body `{"uri": str, "to": str}` (404 no draft; 400 unknown uri/part)
    - `POST /api/splitparts/{pid}/parts` (409 at 6 parts)
    - `POST /api/splitparts/{pid}/parts/{key}/remove` body `{"dry": bool}` → with `dry: true`: `{"moves": {dest_key: count}}`, nothing saved; else the draft (409 when only 2 parts)
  - None of these touch Spotify.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_splitparts_api.py`)

```python
# ---- moves and parts -------------------------------------------------------


@pytest.fixture
def drafted(client):
    client.post("/api/splitparts/H1/suggest", json={"n": 2})
    client.calls.clear()
    return client


def part_of(draft, uri):
    return next(p["key"] for p in draft["parts"] if uri in p["uris"])


def test_move_puts_the_song_at_the_end_of_its_new_part(drafted):
    d = drafted.get("/api/splitparts/H1").json()
    uri = d["parts"][0]["uris"][0]

    d2 = drafted.post("/api/splitparts/H1/move", json={"uri": uri, "to": "p2"}).json()

    assert part_of(d2, uri) == "p2"
    assert d2["parts"][1]["uris"][-1] == uri
    assert sum(len(p["uris"]) for p in d2["parts"]) == 24
    assert appmod.store.splitparts()["drafts"]["H1"]["parts"] == d2["parts"]
    assert drafted.calls == []


def test_move_refuses_unknown_song_or_part(drafted):
    d = drafted.get("/api/splitparts/H1").json()
    uri = d["parts"][0]["uris"][0]

    assert drafted.post("/api/splitparts/H1/move", json={"uri": "nope", "to": "p2"}).status_code == 400
    assert drafted.post("/api/splitparts/H1/move", json={"uri": uri, "to": "p9"}).status_code == 400


def test_add_part_makes_an_empty_new_part(drafted):
    d = drafted.post("/api/splitparts/H1/parts").json()

    assert d["parts"][-1] == {"key": "p3", "name": "New part", "uris": []}


def test_add_part_stops_at_six(drafted):
    for _ in range(4):
        assert drafted.post("/api/splitparts/H1/parts").status_code == 200
    assert drafted.post("/api/splitparts/H1/parts").status_code == 409


def test_remove_part_dry_run_says_where_songs_go_and_saves_nothing(drafted):
    drafted.post("/api/splitparts/H1/parts")
    before = drafted.get("/api/splitparts/H1").json()

    res = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": True}).json()

    assert res == {"moves": {"p1": 10}}   # the empty p3 never wins
    assert drafted.get("/api/splitparts/H1").json() == before


def test_remove_part_moves_each_song_to_its_best_part(drafted):
    drafted.post("/api/splitparts/H1/parts")
    d = drafted.get("/api/splitparts/H1").json()
    moved = list(d["parts"][1]["uris"])

    d2 = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": False}).json()

    assert [p["key"] for p in d2["parts"]] == ["p1", "p3"]
    assert all(part_of(d2, u) == "p1" for u in moved)


def test_at_least_two_parts_remain(drafted):
    res = drafted.post("/api/splitparts/H1/parts/p2/remove", json={"dry": False})

    assert res.status_code == 409
    assert "at least 2" in res.json()["detail"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_splitparts_api.py -k "move or part"`
Expected: FAIL — 404/405 on the new routes

- [ ] **Step 3: Implement** — append to `sortify/splitparts_api.py`:

```python
NEW_PART_NAME = "New part"


def _part(draft: dict, key: str) -> dict:
    for p in draft["parts"]:
        if p["key"] == key:
            return p
    raise KeyError(f"no part {key!r}")


def move(draft: dict, uri: str, to_key: str) -> dict:
    target = _part(draft, to_key)
    source = next((p for p in draft["parts"] if uri in p["uris"]), None)
    if source is None:
        raise KeyError(f"song {uri!r} is not in this split")
    if source is not target:
        source["uris"].remove(uri)
        target["uris"].append(uri)
    return draft


def add_part(draft: dict) -> dict:
    from .splitparts import MAX_PARTS
    if len(draft["parts"]) >= MAX_PARTS:
        raise ValueError(f"at most {MAX_PARTS} parts")
    nums = [int(p["key"][1:]) for p in draft["parts"] if p["key"][1:].isdigit()]
    draft["parts"].append({"key": f"p{max(nums, default=0) + 1}",
                           "name": NEW_PART_NAME, "uris": []})
    return draft


def removal_moves(draft: dict, key: str, tag_artists: dict, track_map: dict,
                  artist_map: dict) -> dict[str, str]:
    from .splitparts import place
    leaving = _part(draft, key)["uris"]
    others = [p for p in draft["parts"] if p["key"] != key]
    picks = place(draft["tracks"], [p["uris"] for p in others], leaving,
                  tag_artists, track_map, artist_map)
    return {u: others[picks.get(u, 0)]["key"] for u in leaving}


def remove_part(draft: dict, key: str, moves: dict[str, str]) -> dict:
    from .splitparts import MIN_PARTS
    if len(draft["parts"]) <= MIN_PARTS:
        raise ValueError(f"at least {MIN_PARTS} parts must remain")
    leaving = _part(draft, key)
    draft["parts"] = [p for p in draft["parts"] if p["key"] != key]
    for u in leaving["uris"]:
        _part(draft, moves[u])["uris"].append(u)
    return draft
```

and the routes in `sortify/app.py`'s split-into-parts section:

```python
class SplitMoveIn(BaseModel):
    uri: str
    to: str


class SplitRemoveIn(BaseModel):
    dry: bool = False


def _splitparts_draft(pid: str) -> dict:
    draft = spa.get_draft(store, pid)
    if draft is None:
        raise HTTPException(404, "no split in progress for this playlist")
    return draft


@app.post("/api/splitparts/{pid}/move")
def splitparts_move(pid: str, body: SplitMoveIn):
    with spa._lock:
        draft = _splitparts_draft(pid)
        try:
            spa.move(draft, body.uri, body.to)
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'\""))
        spa.put_draft(store, draft)
    return draft


@app.post("/api/splitparts/{pid}/parts")
def splitparts_add_part(pid: str):
    with spa._lock:
        draft = _splitparts_draft(pid)
        try:
            spa.add_part(draft)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        spa.put_draft(store, draft)
    return draft


@app.post("/api/splitparts/{pid}/parts/{key}/remove")
def splitparts_remove_part(pid: str, key: str, body: SplitRemoveIn):
    with spa._lock:
        draft = _splitparts_draft(pid)
        if len(draft["parts"]) <= spp.MIN_PARTS:
            raise HTTPException(409, f"at least {spp.MIN_PARTS} parts must remain")
        try:
            moves = spa.removal_moves(draft, key, store.tag_artists(),
                                      store.lastfm_track_map(), store.lastfm_artist_map())
        except KeyError as exc:
            raise HTTPException(400, str(exc).strip("'\""))
        if body.dry:
            counts: dict[str, int] = {}
            for dest in moves.values():
                counts[dest] = counts.get(dest, 0) + 1
            return {"moves": counts}
        spa.remove_part(draft, key, moves)
        spa.put_draft(store, draft)
    return draft
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q tests/test_splitparts_api.py && .venv/bin/pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add sortify/splitparts_api.py sortify/app.py tests/test_splitparts_api.py
git commit -m "feat(split): move songs, add and remove parts"
```

---

### Task 6: Single-song preview

**Files:**
- Modify: `sortify/app.py` (next to `/api/playlist_preview`)
- Test: `tests/test_track_preview.py`

**Interfaces:**
- Consumes: the draft's `tracks` (Task 4), `app._deezer_client().fetch_preview(artist, title, exclude=...) -> {"url", "deezer_id", "miss"}`, `store.preview_rejects()`.
- Produces: `GET /api/track_preview?pid=<playlist id>&uri=<track uri>` → `{"url": str, "uri": str, "deezer_id": int|None}`; 404 `{"detail": "no preview"}` when Deezer has none or errors; 404 when the uri is not in that playlist's draft. Zero Spotify calls.

- [ ] **Step 1: Write the failing tests** — `tests/test_track_preview.py`:

```python
"""Tapping a song in the split plays its 30 s Deezer preview. Zero Spotify
calls; the Deezer client is faked (conftest blocks the real one)."""

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

TRACK = {"uri": "spotify:track:t1", "name": "Song One",
         "artists": [{"id": "a1", "name": "Artist One"}], "is_local": False}


@pytest.fixture
def client(monkeypatch):
    appmod.store.save_splitparts({"version": 1, "drafts": {"H1": {
        "playlist_id": "H1", "tracks": [TRACK], "parts": [], "songs": {}}}})
    appmod.store.save_preview_rejects({"spotify:track:t1": {"rejected": [77]}})
    asked = []

    class FakeDeezer:
        def fetch_preview(self, artist, title, exclude=()):
            asked.append((artist, title, list(exclude)))
            return {"url": "https://cdn/clip.mp3", "deezer_id": 5, "miss": False}

    monkeypatch.setattr(appmod, "_deezer_client", lambda: FakeDeezer())
    c = TestClient(appmod.app, raise_server_exceptions=False)
    c.asked = asked
    return c


def test_a_song_resolves_to_its_clip(client):
    res = client.get("/api/track_preview", params={"pid": "H1", "uri": "spotify:track:t1"})

    assert res.json() == {"url": "https://cdn/clip.mp3", "uri": "spotify:track:t1", "deezer_id": 5}
    assert client.asked == [("Artist One", "Song One", [77])]


def test_no_clip_is_a_404(client, monkeypatch):
    class Miss:
        def fetch_preview(self, artist, title, exclude=()):
            return {"miss": True}

    monkeypatch.setattr(appmod, "_deezer_client", lambda: Miss())

    res = client.get("/api/track_preview", params={"pid": "H1", "uri": "spotify:track:t1"})

    assert res.status_code == 404
    assert res.json()["detail"] == "no preview"


def test_a_deezer_error_is_a_404_not_a_500(client, monkeypatch):
    class Broken:
        def fetch_preview(self, artist, title, exclude=()):
            raise RuntimeError("deezer down")

    monkeypatch.setattr(appmod, "_deezer_client", lambda: Broken())

    assert client.get("/api/track_preview",
                      params={"pid": "H1", "uri": "spotify:track:t1"}).status_code == 404


def test_a_song_outside_the_split_is_refused(client):
    res = client.get("/api/track_preview", params={"pid": "H1", "uri": "spotify:track:zz"})

    assert res.status_code == 404
    assert client.asked == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_track_preview.py`
Expected: FAIL — 404 for every request (route missing), first test's body mismatch

- [ ] **Step 3: Implement** — in `sortify/app.py`, after `playlist_preview`:

```python
@app.get("/api/track_preview")
def track_preview(pid: str, uri: str):
    """One song's 30 s Deezer clip, for a tap in the Split tab. The song is
    looked up in that playlist's split draft (no Spotify call); recordings
    the user marked wrong are skipped, as in the medley player."""
    draft = spa.get_draft(store, pid) or {}
    track = next((t for t in draft.get("tracks") or [] if t.get("uri") == uri), None)
    if track is None or not track.get("artists"):
        raise HTTPException(404, "not in this split")
    artist = track["artists"][0].get("name") or ""
    skip = (store.preview_rejects().get(uri) or {}).get("rejected") or []
    try:
        rec = _deezer_client().fetch_preview(artist, track.get("name") or "", exclude=skip)
    except Exception:
        log.warning("deezer preview failed for %r", track.get("name"))
        raise HTTPException(404, "no preview")
    if rec.get("miss") or not rec.get("url"):
        raise HTTPException(404, "no preview")
    return {"url": rec["url"], "uri": uri, "deezer_id": rec.get("deezer_id")}
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q tests/test_track_preview.py && .venv/bin/pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add sortify/app.py tests/test_track_preview.py
git commit -m "feat(split): single-song preview for a tap in the split"
```

---

### Task 7: Done — plan, cost, execution in a safe order, resume, re-read

**Files:**
- Modify: `sortify/splitparts_api.py`, `sortify/app.py` (same section; also widen `splitparts_progress` to return `summary`)
- Test: `tests/test_splitparts_api.py` (append)

**Interfaces:**
- Consumes: Task 1 `sp.snapshot`, `sp.remove_many`, `REMOVE_BATCH`; Task 4 draft + `_splitparts_job`, `_splitparts_job_lock`, `_splitparts_threaded`, `spa._songs`, `spa.slim_tracks`; Task 5 `spa._part`, `_splitparts_draft`; existing `sp.create_playlist_full(name) -> (id, snapshot|None)`, `sp.add_to_playlist(pid, uris) -> snapshot|None`, `sp.remember_playlist(item)`, `sp.rename_playlist(pid, name)`, `roles.mark_subset_name(name)`, `filing.start(pid, name, dest, *, items, on_success, threaded)`, `store.set_folder_path(pid, path)`, `_home_folder_choices(cfg, top_home_id) -> (choices, default)`, `creatable_home_name_problem(name, input_pattern=..., exclude_patterns=..., exclude_emoji=...)`, `_profile_state`.
- Produces:
  - `spa.done_plan(draft: dict, original: str, part_key: str | None, names: dict[str, str] | None = None, remove_batch: int = 100) -> dict` with keys `new_parts` (`[{"key","name","uris"}]`, local songs and duplicates removed), `empty` (names of parts not created), `local` (song names that stay in the original), `removals` (uris), `calls` (int), `creates` (int); raises `ValueError` with a user message for a bad choice or fewer than 2 parts with songs
  - `spa.reconcile(draft, tracks, snapshot_id, tag_artists, track_map, artist_map) -> dict`
  - Routes:
    - `GET /api/splitparts/{pid}/plan?original=subset|part&part=<key>` → `{"calls", "creates", "empty", "local", "removals": int, "folders": [{"path","blocked"}], "folder": str|None}`
    - `POST /api/splitparts/{pid}/done` body `{"names": {key: name}, "folder": str|null, "original": "subset"|"part", "part": key|null}` → `{"started": true}`; 400 bad choice/name; 409 "already running"
    - `POST /api/splitparts/{pid}/reread` → the reconciled draft
    - `GET /api/splitparts/{pid}/progress` now also returns `"summary"` (null until a Done finishes): `{"created": [{"id","name","count"}], "original": "subset"|"part", "empty": [...], "local": [...]}`; a Done that found the playlist changed ends with `state: "changed"`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_splitparts_api.py`)

```python
# ---- Done ------------------------------------------------------------------

from sortify.spotify import SpotifyError


@pytest.fixture
def env(drafted, monkeypatch):
    appmod.store.update_config(input_name_pattern=r"^\[.+\]$")
    log = []
    ids = iter(["N1", "N2", "N3", "N4", "N5"])

    def create(name, description="", bulk=False, spend_reserve=False, public=False):
        new = next(ids)
        log.append(("create", name, new))
        return new, f"snap-{new}"

    def add(pid, uris, bulk=False, spend_reserve=False):
        log.append(("add", pid, list(uris)))
        return f"snap-add-{pid}"

    def remove_many(pid, uris):
        log.append(("remove", pid, list(uris)))
        return "snap-removed"

    def rename(pid, name):
        log.append(("rename", pid, name))

    monkeypatch.setattr(appmod.sp, "create_playlist_full", create)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", add)
    monkeypatch.setattr(appmod.sp, "remove_many", remove_many)
    monkeypatch.setattr(appmod.sp, "rename_playlist", rename)
    filed = []
    monkeypatch.setattr(appmod.filing, "start",
                        lambda pid, name, dest, **kw: filed.append((pid, name, dest)))
    drafted.log = log
    drafted.filed = filed
    return drafted


def progress(c):
    return c.get("/api/splitparts/H1/progress").json()


def edit_draft(fn):
    env_ = appmod.store.splitparts()
    fn(env_["drafts"]["H1"])
    appmod.store.save_splitparts(env_)


def test_plan_prices_keep_as_subset(env):
    plan = env.get("/api/splitparts/H1/plan", params={"original": "subset"}).json()

    # snapshot + (create + 1 add) x 2 + rename
    assert plan["calls"] == 6
    assert plan["creates"] == 2
    assert plan["removals"] == 0 and plan["empty"] == [] and plan["local"] == []


def test_plan_prices_original_becomes_a_part(env):
    plan = env.get("/api/splitparts/H1/plan", params={"original": "part", "part": "p1"}).json()

    # snapshot + (create + 1 add) + 1 removal batch
    assert plan["calls"] == 4
    assert plan["creates"] == 1
    assert plan["removals"] == 10


def test_done_keep_as_subset_creates_all_parts_then_renames_the_original(env):
    res = env.post("/api/splitparts/H1/done", json={
        "names": {"p1": "Rocky", "p2": "Jazzy"}, "folder": "ROOT / Mix", "original": "subset"})
    assert res.status_code == 200

    p = progress(env)
    assert p["state"] == "done"
    assert [e[0] for e in env.log] == ["create", "add", "create", "add", "rename"]
    assert env.log[0][1] == "Rocky" and env.log[2][1] == "Jazzy"
    assert env.log[-1] == ("rename", "H1", "🐾 Big Home")
    cfg = appmod.store.config()
    assert {"N1", "N2"} <= set(cfg["home_ids"]) and {"N1", "N2"} <= set(cfg["sticky_home_ids"])
    assert "H1" not in cfg["home_ids"]
    assert env.filed == [("N1", "Rocky", "ROOT / Mix"), ("N2", "Jazzy", "ROOT / Mix")]
    assert p["summary"]["created"] == [{"id": "N1", "name": "Rocky", "count": 14},
                                       {"id": "N2", "name": "Jazzy", "count": 10}]
    assert env.get("/api/splitparts/H1").status_code == 404
    assert env.calls == [("snapshot", "H1")]


def test_done_original_becomes_a_part_removes_the_rest_from_it(env):
    d = env.get("/api/splitparts/H1").json()
    others = d["parts"][1]["uris"]

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None,
                                               "original": "part", "part": "p1"})

    assert progress(env)["state"] == "done"
    assert [e[0] for e in env.log] == ["create", "add", "remove"]
    assert env.log[-1] == ("remove", "H1", others)
    assert "H1" in appmod.store.config()["home_ids"]
    assert env.filed == []


def test_part_of_only_local_files_is_not_created(env):
    env.post("/api/splitparts/H1/parts")
    d = env.get("/api/splitparts/H1").json()
    two = d["parts"][0]["uris"][:2]
    for u in two:
        env.post("/api/splitparts/H1/move", json={"uri": u, "to": "p3"})
    edit_draft(lambda dr: [dr["songs"][u].update(local=True) for u in two])

    plan = env.get("/api/splitparts/H1/plan", params={"original": "subset"}).json()
    assert plan["creates"] == 2
    assert plan["empty"] == ["New part"]
    assert sorted(plan["local"]) == sorted(d["songs"][u]["name"] for u in two)

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})
    assert [e[0] for e in env.log].count("create") == 2


def test_done_adds_each_song_once(env):
    edit_draft(lambda dr: dr["parts"][0]["uris"].append(dr["parts"][0]["uris"][0]))

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    first_add = next(e for e in env.log if e[0] == "add")
    assert len(first_add[2]) == len(set(first_add[2])) == 14


def test_done_stops_when_the_playlist_changed(env, monkeypatch):
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: "snap-2")

    env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    p = progress(env)
    assert p["state"] == "changed"
    assert env.log == []


def test_done_resumes_after_a_failure_without_duplicates(env, monkeypatch):
    real_add = appmod.sp.add_to_playlist
    failed = []

    def flaky(pid, uris, bulk=False, spend_reserve=False):
        if pid == "N2" and not failed:
            failed.append(1)
            raise SpotifyError(429, "rate limited")
        return real_add(pid, uris)

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky)
    body = {"names": {}, "folder": None, "original": "subset"}

    env.post("/api/splitparts/H1/done", json=body)
    assert progress(env)["state"] == "error"
    assert "press Done again" in progress(env)["message"]
    assert not any(e[0] == "rename" for e in env.log)

    env.post("/api/splitparts/H1/done", json=body)
    assert progress(env)["state"] == "done"
    assert [e[0] for e in env.log].count("create") == 2
    assert [e[0] for e in env.log].count("rename") == 1


def test_second_done_while_running_is_refused(env):
    appmod._splitparts_job.update({"pid": "H1", "state": "running"})

    res = env.post("/api/splitparts/H1/done", json={"names": {}, "folder": None, "original": "subset"})

    assert res.status_code == 409
    assert env.log == []


def test_an_input_shaped_part_name_is_refused_before_any_call(env):
    res = env.post("/api/splitparts/H1/done", json={
        "names": {"p1": "[Oops]"}, "folder": None, "original": "subset"})

    assert res.status_code == 400
    assert env.log == []


def test_a_bad_original_choice_is_refused(env):
    assert env.post("/api/splitparts/H1/done", json={
        "names": {}, "folder": None, "original": "part", "part": "p9"}).status_code == 400


def test_reread_keeps_moves_and_places_new_songs(env, monkeypatch):
    d = env.get("/api/splitparts/H1").json()
    moved = d["parts"][0]["uris"][3]
    env.post("/api/splitparts/H1/move", json={"uri": moved, "to": "p2"})
    gone = d["parts"][0]["uris"][0]
    new = {"uri": "spotify:track:new", "name": "Fresh", "is_local": False,
           "artists": [{"id": "a0", "name": "Artist 0"}]}
    changed = [t for t in TRACKS if t["uri"] != gone] + [new]
    monkeypatch.setattr(appmod.sp, "snapshot", lambda pid: "snap-2")
    monkeypatch.setattr(appmod.sp, "playlist_tracks", lambda pid: list(changed))

    d2 = env.post("/api/splitparts/H1/reread").json()

    assert d2["snapshot_id"] == "snap-2"
    assert part_of(d2, moved) == "p2"
    assert all(gone not in p["uris"] for p in d2["parts"])
    assert part_of(d2, "spotify:track:new") == "p1"   # same artist as rock song 0
    assert "spotify:track:new" in d2["unknown"]
    assert d2["songs"]["spotify:track:new"]["name"] == "Fresh"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_splitparts_api.py -k "plan or done or reread or local or once or input_shaped or bad_original"`
Expected: FAIL — routes missing (404/405)

- [ ] **Step 3: Implement the pure parts** — append to `sortify/splitparts_api.py` (add `import math` at the top):

```python
ORIGINAL_CHOICES = ("subset", "part")


def done_plan(draft: dict, original: str, part_key: str | None,
              names: dict[str, str] | None = None, remove_batch: int = 100) -> dict:
    """What Done will do, and what it costs, without doing any of it."""
    if original not in ORIGINAL_CHOICES:
        raise ValueError("choose what happens to the original")
    keys = [p["key"] for p in draft["parts"]]
    keep_key = part_key if original == "part" else None
    if original == "part" and keep_key not in keys:
        raise ValueError("pick the part the original becomes")
    songs = draft.get("songs") or {}
    names = names or {}

    def local(u: str) -> bool:
        return bool((songs.get(u) or {}).get("local"))

    new_parts, empty = [], []
    for p in draft["parts"]:
        name = (names.get(p["key"]) or p["name"]).strip() or p["name"]
        if p["key"] == keep_key:
            continue
        uris = list(dict.fromkeys(u for u in p["uris"] if not local(u)))
        if uris:
            new_parts.append({"key": p["key"], "name": name, "uris": uris})
        else:
            empty.append(name)
    if len(new_parts) + (1 if keep_key else 0) < 2:
        raise ValueError("at least 2 parts need songs in them")

    removals: list[str] = []
    if keep_key:
        keep = set(_part(draft, keep_key)["uris"])
        removals = list(dict.fromkeys(
            u for p in draft["parts"] if p["key"] != keep_key
            for u in p["uris"] if u not in keep and not local(u)))
    calls = 1 + sum(1 + math.ceil(len(p["uris"]) / 100) for p in new_parts)
    calls += 1 if original == "subset" else math.ceil(len(removals) / remove_batch)
    return {"new_parts": new_parts, "empty": empty,
            "local": [songs[u]["name"] for u in songs if local(u)],
            "removals": removals, "calls": calls, "creates": len(new_parts)}


def reconcile(draft: dict, tracks: list[dict], snapshot_id: str | None,
              tag_artists: dict, track_map: dict, artist_map: dict) -> dict:
    """The playlist changed in Spotify: keep the user's moves for songs
    still there, drop songs that left, place new songs where they fit best
    and mark them "?"."""
    from .splitparts import place
    slim = slim_tracks(tracks)
    present = {t["uri"] for t in slim}
    for p in draft["parts"]:
        p["uris"] = [u for u in p["uris"] if u in present]
    placed = {u for p in draft["parts"] for u in p["uris"]}
    new = [t["uri"] for t in slim if t["uri"] not in placed]
    picks = place(slim, [p["uris"] for p in draft["parts"]], new,
                  tag_artists, track_map, artist_map)
    for u in new:
        draft["parts"][picks.get(u, 0)]["uris"].append(u)
    draft["unknown"] = [u for u in draft["unknown"] if u in present] + \
        [u for u in new if u not in draft["unknown"]]
    draft["tracks"] = slim
    draft["songs"] = _songs(tracks)
    draft["snapshot_id"] = snapshot_id
    return draft
```

- [ ] **Step 4: Implement the routes and the runner** in `sortify/app.py`'s split section. Add `REMOVE_BATCH` to the `from .spotify import (...)` list. Change `splitparts_progress` to include the summary:

```python
    return {k: job.get(k) for k in ("state", "phase", "done", "total", "message", "summary")}
```

(and make the idle branch return `"summary": None` too). Then:

```python
class SplitDoneIn(BaseModel):
    names: dict[str, str] = Field(default_factory=dict)
    folder: str | None = None
    original: str = "subset"
    part: str | None = None


def _splitparts_name_problem(names: list[str]) -> str | None:
    cfg = store.config()
    for name in names:
        problem = creatable_home_name_problem(
            name,
            input_pattern=cfg.get("input_name_pattern"),
            exclude_patterns=cfg.get("home_name_exclude_patterns") or [],
            exclude_emoji=bool(cfg.get("home_exclude_emoji_names")),
        )
        if problem:
            return f"{name!r}: {problem}"
    return None


@app.get("/api/splitparts/{pid}/plan")
def splitparts_plan(pid: str, original: str = "subset", part: str | None = None):
    draft = _splitparts_draft(pid)
    try:
        plan = spa.done_plan(draft, original, part, remove_batch=REMOVE_BATCH)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    choices, default = _home_folder_choices(store.config(), pid)
    return {"calls": plan["calls"], "creates": plan["creates"], "empty": plan["empty"],
            "local": plan["local"], "removals": len(plan["removals"]),
            "folders": choices, "folder": default}


def _splitparts_seed_tracks(pid: str, snapshot_id: str | None, uris: list[str],
                            full: dict[str, dict]) -> None:
    """Mirror a Done step in the local track cache (no call), so the new
    homes take part in filing suggestions at once. `full` maps uri to the
    richest track dict we hold (the original's cached tracks)."""
    if not snapshot_id:
        return
    cache = store.cache()
    entry = cache["playlists"].get(pid) or {}
    entry.update({"snapshot_id": snapshot_id, "fetched_at": time.time(),
                  "tracks": [full[u] for u in uris if u in full]})
    cache["playlists"][pid] = entry
    store.save_cache(cache)


def _splitparts_file(created: list[dict], folder: str) -> None:
    """File the new parts one after another: the mover drives one desktop
    client, so the jobs must not overlap."""
    def run():
        for c in created:
            filing.start(c["id"], c["name"], folder, items=sp.my_playlists(),
                         on_success=lambda path, p=c["id"]: store.set_folder_path(p, path),
                         threaded=False)
    if _splitparts_threaded:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()


def _splitparts_run_done(pid: str, opts: dict) -> None:
    def progress(**fields):
        with _splitparts_job_lock:
            _splitparts_job.update(fields)

    try:
        draft = spa.get_draft(store, pid)
        if draft is None:
            progress(state="error", message="no split in progress for this playlist")
            return
        first_attempt = draft.get("done") is None
        state = draft.get("done") or {"opts": opts, "created": {}, "filled": [],
                                      "original_done": False}
        opts = state["opts"]   # a resumed Done keeps the choices it started with
        draft["done"] = state

        def save():
            spa.put_draft(store, draft)

        if first_attempt:
            # Only before anything was written: once parts exist, a resume
            # must finish the job rather than stop on our own changes.
            progress(phase="checking the playlist")
            if sp.snapshot(pid) != draft["snapshot_id"]:
                draft["done"] = None
                progress(state="changed", message=(
                    "the playlist changed in Spotify since the suggestion — "
                    "re-read it to continue"))
                return
        plan = spa.done_plan(draft, opts["original"], opts.get("part"),
                             names=opts.get("names"), remove_batch=REMOVE_BATCH)
        save()
        full = {t["uri"]: t for t in
                (store.cache()["playlists"].get(pid) or {}).get("tracks") or []}
        for t in draft["tracks"]:
            full.setdefault(t["uri"], t)
        me = (store.cache().get("me") or {}).get("id")
        created = []
        for i, part in enumerate(plan["new_parts"]):
            key, name = part["key"], part["name"]
            progress(phase=f"creating “{name}” ({i + 1} of {len(plan['new_parts'])})", done=i)
            new_id = state["created"].get(key)
            if not new_id:
                new_id, _ = sp.create_playlist_full(name)
                state["created"][key] = new_id
                cfg = store.config()
                store.update_config(
                    home_ids=sorted(set(cfg.get("home_ids") or []) | {new_id}),
                    sticky_home_ids=sorted(set(cfg.get("sticky_home_ids") or []) | {new_id}))
                save()
            if key not in state["filled"]:
                snap = None
                for b in range(0, len(part["uris"]), 100):
                    snap = sp.add_to_playlist(new_id, part["uris"][b:b + 100]) or snap
                snap = snap or f"created:{new_id}"
                sp.remember_playlist({"id": new_id, "name": name, "owner": me,
                                      "editable": True, "total": len(part["uris"]),
                                      "snapshot_id": snap, "image": None, "description": ""})
                _splitparts_seed_tracks(new_id, snap, part["uris"], full)
                state["filled"].append(key)
                save()
            created.append({"id": new_id, "name": name, "count": len(part["uris"])})

        if not state["original_done"]:
            progress(phase="updating the original", done=len(plan["new_parts"]))
            if opts["original"] == "subset":
                new_name = roles.mark_subset_name(draft["name"])
                if new_name != draft["name"]:
                    sp.rename_playlist(pid, new_name)
                cfg = store.config()
                store.update_config(
                    home_ids=[h for h in cfg.get("home_ids") or [] if h != pid],
                    sticky_home_ids=[h for h in cfg.get("sticky_home_ids") or [] if h != pid])
            else:
                snap = sp.remove_many(pid, plan["removals"])
                gone = set(plan["removals"])
                _splitparts_seed_tracks(pid, snap, [t["uri"] for t in draft["tracks"]
                                                    if t["uri"] not in gone], full)
            state["original_done"] = True
            save()

        _profile_state.clear()
        _profile_state["built_at"] = 0.0
        if opts.get("folder"):
            _splitparts_file(created, opts["folder"])
        spa.drop_draft(store, pid)
        progress(state="done", phase="done", done=len(plan["new_parts"]), message=None,
                 summary={"created": created, "original": opts["original"],
                          "empty": plan["empty"], "local": plan["local"]})
    except SpotifyError as exc:
        progress(state="error", message=f"{exc} — your split is saved; press Done again to continue")
    except Exception as exc:
        log.exception("split: done failed")
        progress(state="error", message=f"done failed: {exc} — your split is saved; press Done again to continue")


@app.post("/api/splitparts/{pid}/done")
def splitparts_done(pid: str, body: SplitDoneIn):
    draft = _splitparts_draft(pid)
    try:
        plan = spa.done_plan(draft, body.original, body.part, names=body.names,
                             remove_batch=REMOVE_BATCH)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    problem = _splitparts_name_problem([p["name"] for p in plan["new_parts"]])
    if problem:
        raise HTTPException(400, problem)
    with _splitparts_job_lock:
        if _splitparts_job.get("state") == "running":
            raise HTTPException(409, "already running")
        _splitparts_job.clear()
        _splitparts_job.update({"pid": pid, "state": "running", "phase": "starting",
                                "done": 0, "total": plan["creates"], "message": None,
                                "summary": None})
    opts = {"names": body.names, "folder": body.folder, "original": body.original,
            "part": body.part}
    if _splitparts_threaded:
        threading.Thread(target=_splitparts_run_done, args=(pid, opts), daemon=True).start()
    else:
        _splitparts_run_done(pid, opts)
    return {"started": True}


@app.post("/api/splitparts/{pid}/reread")
def splitparts_reread(pid: str):
    _splitparts_draft(pid)
    snap = sp.snapshot(pid)               # network outside the draft lock
    tracks = _cached_tracks(pid, snap)
    with spa._lock:
        draft = _splitparts_draft(pid)
        spa.reconcile(draft, tracks, snap, store.tag_artists(),
                      store.lastfm_track_map(), store.lastfm_artist_map())
        spa.put_draft(store, draft)
    return draft
```

Check `Field` is imported from pydantic in `app.py` (it is: `from pydantic import BaseModel, Field`) and that `filing` and `roles` are imported (they are, as `from . import filing` and `from . import playlist_roles as roles`).

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest -q tests/test_splitparts_api.py && .venv/bin/pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 6: Commit**

```bash
git add sortify/splitparts_api.py sortify/app.py tests/test_splitparts_api.py
git commit -m "feat(split): Done — priced, original last, resumable; re-read after a change"
```

---

### Task 8: Frontend — remove the old split and sittings; the new Split tab up to the parts list

**Files:**
- Modify: `sortify/static/index.html`, `sortify/static/app.js`, `sortify/static/style.css`
- Test: `tests/ui_harness.mjs`

**Interfaces:**
- Consumes (Tasks 4–5 routes): `GET /api/splitparts/homes`, `GET /api/splitparts/{pid}`, `POST /api/splitparts/{pid}/suggest {n}`, `GET /api/splitparts/{pid}/progress`.
- Produces (in `app.js`, used by Tasks 9–10): globals `spHomes`, `spDraft`, `spPid`, `spShown`, `spN`; functions `showSplit()`, `openSpHome(home)`, `spSection(id)`, `spPanel(id)`, `renderSpSetup()`, `spSuggest(n)`, `spPoll(kind)` (`kind` is `"suggest"` or `"done"`), `spStopPolling()`, `renderSpParts()`, `renderSpChips()`, `renderSpList()`, `spPart(key)`, `spSongLabel(uri)`; element ids below.
- Markup ids (index.html): `view-split`, `sp-pick`, `sp-filter`, `sp-homes`, `sp-work`, `sp-back`, `sp-title`, `sp-setup`, `sp-minus`, `sp-n`, `sp-plus`, `sp-suggest`, `sp-setup-msg`, `sp-progress`, `sp-parts`, `sp-chips`, `sp-hint`, `sp-list`, `sp-actions`, `sp-done`.

- [ ] **Step 1: Remove the old frontend**

In `sortify/static/app.js` delete, with every reference to them: the globals `split`, `sitting`, `splitInFlight`, `splitProgress`, `splitProgressTimer`, `queueActionInFlight`, `queueStatus`, `queuePanelTimer`; the functions `splitDisabledReason`, `renderOrphans` (and the `btn-clean-sittings` handler and the `sitting_orphans` read in the Playlists view), `paintSittingBar`, `syncSittingFromNow`, `showSplitPicker`, `openSplit`, `stopSplitProgressPolling`, `renderSplitProgress`, `paintSplitProgress`, `pollSplitProgress`, `doSplit`, `splitParams`, `applySplitData`, `syncSitting`, `renderPiles`, `formatQueueDuration`, `queueActionKey`, `queuePiles`, `renderSplitSittingBar`, `stopQueuePolling`, `renderQueuePanel`, `paintQueuePanel`, `pollQueueStatus`, `queueControlKey`, `pauseQueue`, `resumeQueue`, `cancelQueue`, `startSitting`, `finishSitting`, `sittingCardBody`, `wireSittingCard`, `decideKeep`, `decideReject`, `decideUndecide`; the per-row Split button in the Playlists view (`bSplit`); the `btn-split-back`, `btn-retry-split*`, `btn-recluster`, `btn-save-all`, `btn-rename-outputs`, `btn-now-finish-sitting`, `btn-split-finish-sitting` handlers.

In `renderNow`, delete the sitting branch: the `inSitting` variable and everything conditional on it, so the card body is always `ordinaryCardBody(d, tr, ctx)`; delete the `syncSittingFromNow(d)` and `paintSittingBar()` calls (including the one in the `if (!d)` early return — that branch then just returns).

In `sortify/static/index.html` delete `#now-sitting-bar` (inside `#view-now`), the Playlists view's `#pl-orphan-bar`, and the `#view-splitpick` and old `#view-split` sections. In `style.css` delete rules that only style removed elements (`.sitting-banner` if no remaining element uses it, `.pile*`, `#piles`, `#queue-panel`, `#split-*`); check each selector with `grep -n "<selector-name>" sortify/static/*.html sortify/static/app.js` before deleting it.

In `tests/ui_harness.mjs` delete the blocks that only test removed code: `C1`, `C2`, `I2`, `I3`, `I4`, `O1`, `M`, `P`, `P3`, `P4`, `R1`, `RN`, and any check in other blocks that asserts a sitting/pile/queue element (read each failing check before deleting it; keep every check about behaviour that still exists). `I1` ("renderNow() with nowState === null must not throw") stays — adapt it if it named `paintSittingBar`.

- [ ] **Step 2: Prove nothing old is left**

```bash
grep -n -E "sitting|Sitting|renderPiles|showSplitPicker|openSplit|splitpick|queuePanel|decideKeep|orphan" sortify/static/app.js sortify/static/index.html sortify/static/style.css
```
Expected: no matches. Run the harness: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | tail -1` → all remaining checks pass; note the new total.

- [ ] **Step 3: Write the failing harness checks** — add `confirm` and `elementFromPoint` seams first. In the `vm.createContext({...})` object add:

```js
  // The Split tab asks before discarding work; checks script the answer.
  confirm: () => true,
```

and in the `const document = {...}` stub add:

```js
  // The Split tab's drop target is found geometrically; checks script it.
  elementFromPoint: () => null,
```

Then append a block before the summary (its own banner and closing brace):

```js
// ============================================================================
// SP — split a home into parts: the picker, the counter, Suggest, the parts.
// Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md.
// ============================================================================
{
  resetLog();
  const HOMES = { homes: [
    { id: "H1", name: "Big Home", total: 24, folder: "ROOT / Mix", splittable: true, reason: null, draft: false },
    { id: "H2", name: "Tiny", total: 5, folder: null, splittable: false, reason: "too small to split — fewer than 10 songs", draft: false },
  ] };
  const DRAFT = {
    playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "rock", uris: ["u1", "u2", "u3"] },
            { key: "p2", name: "jazz", uris: ["u4", "u5"] }],
    unknown: ["u3"], hint: null, scores: { "2": 0.3 },
    songs: { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
             u3: { name: "Three", artists: "C", local: false }, u4: { name: "Four", artists: "D", local: false },
             u5: { name: "Five", artists: "E", local: false } },
    tracks: [], done: null,
  };
  routes["GET /api/splitparts/homes"] = { status: 200, body: HOMES };
  routes["GET /api/splitparts/H1"] = { status: 404, body: { detail: "no split in progress for this playlist" } };
  await run(`showSplit()`);
  await tick();
  const homesHtml = () => $$("sp-homes").innerHTML;
  check("SP the Split tab lists homes",
        /Big Home/.test(homesHtml()) && /Tiny/.test(homesHtml()), homesHtml().slice(0, 300));
  check("SP ...a too-small home is listed but disabled, with why",
        /<button[^>]*data-id="H2"[^>]*disabled/.test(homesHtml()) && /fewer than 10/.test(homesHtml()),
        homesHtml());
  $$("sp-filter").value = "tin";
  run(`renderSpHomes()`);
  check("SP the filter narrows the list", !/Big Home/.test(homesHtml()) && /Tiny/.test(homesHtml()), homesHtml());
  $$("sp-filter").value = "";
  run(`renderSpHomes()`);

  await run(`openSpHome(spHomes[0])`);
  await tick();
  check("SP a home without a draft opens on the counter, at 2",
        $$("sp-setup").hidden === false && $$("sp-parts").hidden === true && $$("sp-n").textContent === "2",
        `setup=${$$("sp-setup").hidden} n=${$$("sp-n").textContent}`);
  run(`$("sp-minus").onclick()`);
  check("SP ...the counter does not go below 2", $$("sp-n").textContent === "2", $$("sp-n").textContent);
  for (let i = 0; i < 6; i++) run(`$("sp-plus").onclick()`);
  check("SP ...nor above 6", $$("sp-n").textContent === "6", $$("sp-n").textContent);
  run(`$("sp-minus").onclick(); $("sp-minus").onclick(); $("sp-minus").onclick(); $("sp-minus").onclick()`);

  routes["POST /api/splitparts/H1/suggest"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "done", phase: "done", done: 0, total: 0, message: null, summary: null } };
  routes["GET /api/splitparts/H1"] = { status: 200, body: DRAFT };
  await run(`$("sp-suggest").onclick()`);
  await tick(); await tick();
  const sent = log.find((l) => l.method === "POST" && l.path === "/api/splitparts/H1/suggest");
  check("SP Suggest posts the chosen number", sent && JSON.parse(sent.body).n === 2, JSON.stringify(sent));
  check("SP ...and lands on the parts, one chip per part with its count",
        $$("sp-parts").hidden === false &&
        /data-key="p1"[^>]*>[\s\S]*rock[\s\S]*3/.test($$("sp-chips").innerHTML) &&
        /data-key="p2"[\s\S]*jazz[\s\S]*2/.test($$("sp-chips").innerHTML),
        $$("sp-chips").innerHTML);
  check("SP ...the first part's songs are listed, title and artist",
        /One[\s\S]*A/.test($$("sp-list").innerHTML) && !/Four/.test($$("sp-list").innerHTML),
        $$("sp-list").innerHTML);
  check("SP ...a song without Last.fm data carries the ? mark",
        /data-uri="u3"[\s\S]*sp-unknown/.test($$("sp-list").innerHTML), $$("sp-list").innerHTML);
  run(`spShowPart("p2")`);
  check("SP tapping another chip lists that part",
        /Four/.test($$("sp-list").innerHTML) && !/One/.test($$("sp-list").innerHTML), $$("sp-list").innerHTML);

  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "error", phase: "", done: 0, total: 0, message: "not enough Last.fm data on these songs to suggest a split", summary: null } };
  await run(`spSuggest(2)`);
  await tick(); await tick();
  check("SP a failed suggestion shows its reason on the counter screen",
        $$("sp-setup").hidden === false && /not enough Last\.fm data/.test($$("sp-setup-msg").textContent),
        $$("sp-setup-msg").textContent);

  routes["GET /api/splitparts/H1"] = { status: 200, body: DRAFT };
  await run(`openSpHome(spHomes[0])`);
  await tick();
  check("SP a home with a draft opens straight on its parts", $$("sp-parts").hidden === false, "");
  run(`spStopPolling(); show("lists")`);
}
```

- [ ] **Step 4: Run the harness to verify the SP checks fail**

Run: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | grep -E "^FAIL  SP|checks passed"`
Expected: FAIL lines for the SP checks (`showSplit` not defined).

- [ ] **Step 5: Implement the markup** — in `sortify/static/index.html`, where the old split sections were:

```html
  <section id="view-split" hidden>
    <div id="sp-pick">
      <h2>Split a home</h2>
      <p class="hint">Pick a home that has grown too big or too mixed. Nothing changes
      in Spotify until you press Done.</p>
      <input id="sp-filter" placeholder="Filter homes…" autocomplete="off">
      <div id="sp-homes"></div>
    </div>
    <div id="sp-work" hidden>
      <div class="triage-top">
        <button id="sp-back">‹ Back</button>
        <span id="sp-title"></span>
      </div>
      <div id="sp-setup" hidden>
        <p>Split into</p>
        <div class="sp-counter">
          <button id="sp-minus" class="icon-btn" aria-label="Fewer parts">−</button>
          <span id="sp-n">2</span>
          <button id="sp-plus" class="icon-btn" aria-label="More parts">+</button>
          <span>parts</span>
        </div>
        <button id="sp-suggest" class="primary">Suggest</button>
        <p id="sp-setup-msg" class="hint"></p>
      </div>
      <div id="sp-progress" hidden></div>
      <div id="sp-parts" hidden>
        <div id="sp-chips"></div>
        <div id="sp-hint" hidden></div>
        <div id="sp-list"></div>
        <div id="sp-actions" class="row"></div>
      </div>
      <div id="sp-done" hidden></div>
    </div>
  </section>
```

Note the counter's "−" and "+" are text glyphs, not emoji.

- [ ] **Step 6: Implement the JS** — in `sortify/static/app.js`: change `const views = [...]` to drop `"splitpick"` (keep `"split"`); in `show()`, make `nav-split` active for `view === "split"` only and stop the Split tab's polling and preview when leaving it:

```js
  if (view !== "split") { spStopPolling(); spStopPreview(); }
```

(`spStopPreview` is defined in Task 9; in this task add it as the first function of the section below with the body `function spStopPreview() {}` and Task 9 replaces it — it is a real no-op until previews exist, not a placeholder.) Rewire the nav: `$("nav-split").onclick = () => { closeNavPop(); showSplit(); };`. Then add the section where the old split code was:

```js
// ---- split a home into parts ------------------------------------------------
//
// Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md. The
// server holds the draft; this view draws what it was last sent and posts
// one action at a time. Nothing reaches Spotify before Done.

let spHomes = [];       // last /api/splitparts/homes
let spDraft = null;     // the open draft, as the server last sent it
let spPid = null;       // the home being split
let spShown = null;     // key of the part whose songs are listed
let spN = 2;            // the counter
let spPollTimer = null;
const SP_MIN = 2, SP_MAX = 6;

function spStopPreview() {}

function spSection(id) {
  for (const s of ["sp-pick", "sp-work"]) $(s).hidden = s !== id;
}
function spPanel(id) {
  for (const s of ["sp-setup", "sp-progress", "sp-parts", "sp-done"]) $(s).hidden = s !== id;
}
function spStopPolling() { clearTimeout(spPollTimer); spPollTimer = null; }
const spUrl = (tail = "") => `/api/splitparts/${encodeURIComponent(spPid)}${tail}`;

async function showSplit() {
  show("split");
  spSection("sp-pick");
  $("sp-homes").innerHTML = '<p class="hint">loading…</p>';
  try {
    spHomes = (await api("/api/splitparts/homes")).homes;
  } catch (e) {
    $("sp-homes").innerHTML = `<p class="hint">${esc(e.message)}</p>`;
    return;
  }
  renderSpHomes();
}

function renderSpHomes() {
  const q = ($("sp-filter").value || "").trim().toLowerCase();
  const rows = spHomes.filter((h) => !q || h.name.toLowerCase().includes(q));
  $("sp-homes").innerHTML = rows.length
    ? rows.map((h) => `<button class="sp-home picker-row" data-id="${esc(h.id)}"${h.splittable ? "" : " disabled"}>
        <span class="p-name">${esc(h.name)}</span>
        <span class="p-sub">${h.total} songs${h.draft ? " · split in progress" : ""}${h.reason ? " · " + esc(h.reason) : ""}</span>
      </button>`).join("")
    : '<p class="hint">no homes match</p>';
  // Buttons come back in markup order, so the i-th one is rows[i].
  $("sp-homes").querySelectorAll("button").forEach((b, i) => {
    if (rows[i].splittable) b.onclick = () => openSpHome(rows[i]);
  });
}
$("sp-filter").oninput = renderSpHomes;

async function openSpHome(home) {
  spPid = home.id;
  spDraft = null;
  $("sp-title").textContent = home.name;
  spSection("sp-work");
  try {
    spDraft = await api(spUrl());
  } catch (e) {
    if (e.status !== 404) { toast(e.message); }
  }
  if (spDraft) { spShown = spDraft.parts[0]?.key || null; renderSpParts(); }
  else { spN = 2; renderSpSetup(""); }
}
$("sp-back").onclick = () => { spStopPolling(); spStopPreview(); showSplit(); };

function renderSpSetup(msg) {
  spPanel("sp-setup");
  $("sp-n").textContent = String(spN);
  $("sp-setup-msg").textContent = msg || "";
}
$("sp-minus").onclick = () => { spN = Math.max(SP_MIN, spN - 1); $("sp-n").textContent = String(spN); };
$("sp-plus").onclick = () => { spN = Math.min(SP_MAX, spN + 1); $("sp-n").textContent = String(spN); };
$("sp-suggest").onclick = () => spSuggest(spN);

async function spSuggest(n) {
  spN = n;
  try {
    await api(spUrl("/suggest"), { n });
  } catch (e) {
    renderSpSetup(e.message);
    return;
  }
  spPanel("sp-progress");
  $("sp-progress").innerHTML = '<div class="spinner"></div><p class="hint">starting…</p>';
  await spPoll("suggest");
}

// One poll; schedules the next while the server job runs. `kind` says what
// a finished job means: a fresh draft ("suggest") or a finished Done ("done",
// handled in Task 10's spDoneFinished).
async function spPoll(kind) {
  spStopPolling();
  let p;
  try {
    p = await api(spUrl("/progress"));
  } catch (e) {
    spPollTimer = setTimeout(() => spPoll(kind), 2000);
    return;
  }
  if (p.state === "running") {
    const count = p.total ? ` — ${p.done} of ${p.total}` : "";
    $("sp-progress").innerHTML = `<div class="spinner"></div><p class="hint">${esc(p.phase || "working")}${count}</p>`;
    spPollTimer = setTimeout(() => spPoll(kind), 1000);
    return;
  }
  if (kind === "done") { spDoneFinished(p); return; }
  if (p.state === "error") { renderSpSetup(p.message || "the suggestion failed"); return; }
  try {
    spDraft = await api(spUrl());
  } catch (e) {
    renderSpSetup(e.message);
    return;
  }
  spShown = spDraft.parts[0]?.key || null;
  renderSpParts();
}

function spPart(key) { return spDraft.parts.find((p) => p.key === key) || null; }
function spSongLabel(uri) {
  const s = spDraft.songs[uri] || { name: uri, artists: "" };
  return s.artists ? `${s.name} — ${s.artists}` : s.name;
}

function renderSpParts() {
  spPanel("sp-parts");
  if (!spPart(spShown)) spShown = spDraft.parts[0]?.key || null;
  renderSpChips();
  renderSpList();
}

function renderSpChips() {
  $("sp-chips").innerHTML = spDraft.parts.map((p) =>
    `<button class="sp-chip${p.key === spShown ? " on" : ""}" data-key="${esc(p.key)}">
       <span class="sp-chip-name">${esc(p.name)}</span> <span class="sp-chip-n">${p.uris.length}</span>
     </button>`).join("");
  $("sp-chips").querySelectorAll("button").forEach((b, i) => {
    const key = spDraft.parts[i].key;
    b.onclick = () => spShowPart(key);
  });
}

function spShowPart(key) {
  spShown = key;
  renderSpChips();
  renderSpList();
}

function renderSpList() {
  const part = spPart(spShown);
  const unknown = new Set(spDraft.unknown || []);
  $("sp-list").innerHTML = part && part.uris.length
    ? part.uris.map((u) => {
        const s = spDraft.songs[u] || { name: u, artists: "" };
        return `<div class="sp-song" data-uri="${esc(u)}">
          <span class="sp-song-name">${esc(s.name)}${unknown.has(u) ? ' <span class="sp-unknown" title="no Last.fm data — check where it landed">?</span>' : ""}</span>
          <span class="sp-song-artist">${esc(s.artists)}</span>
        </div>`;
      }).join("")
    : '<p class="hint">no songs in this part yet — drag some onto its chip</p>';
}
```

and in `spPoll`, `spDoneFinished` does not exist until Task 10: add `function spDoneFinished(p) { renderSpParts(); }` in this task right after `spPoll` (Task 10 replaces it with the real Done ending).

- [ ] **Step 7: Style it** — append to `sortify/static/style.css`:

```css
/* ---- split a home into parts ---- */
.sp-counter { display: flex; align-items: center; gap: .6rem; margin: .4rem 0 1rem; font-size: 1.1rem; }
#sp-n { min-width: 1.5rem; text-align: center; font-weight: 700; }
#sp-homes .sp-home { display: block; width: 100%; text-align: left; }
/* The chips stay pinned while the songs scroll under them: they are the drop
   targets, so they must be on screen whenever a song is lifted. */
#sp-chips {
  position: sticky; top: 3.2rem; z-index: 3; display: flex; flex-wrap: wrap; gap: .4rem;
  padding: .4rem 0; background: color-mix(in srgb, var(--bg) 92%, transparent);
}
.sp-chip { border-radius: 999px; padding: .4rem .8rem; }
.sp-chip.on { border-color: var(--accent); color: var(--accent); }
.sp-chip.drop { background: var(--accent); color: var(--bg); }
.sp-chip-n { color: var(--muted); font-size: .85rem; }
.sp-chip.drop .sp-chip-n { color: inherit; }
/* Song rows stop short of the right edge: a swipe in that free strip
   scrolls the list, a touch on a song drags it. */
#sp-list { padding-right: 20%; }
.sp-song {
  touch-action: none; user-select: none; -webkit-user-select: none;
  padding: .55rem .7rem; margin: .3rem 0; border-radius: 10px; background: var(--card);
  cursor: grab;
}
.sp-song-name, .sp-song-artist { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sp-song-artist { color: var(--muted); font-size: .85rem; }
.sp-unknown { color: var(--muted); font-weight: 700; }
```

- [ ] **Step 8: Run both suites**

Run: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | grep -E "^FAIL|checks passed" && .venv/bin/pytest -q 2>&1 | tail -1`
Expected: all harness checks pass (the post-removal total + the SP checks); pytest green.

- [ ] **Step 9: Commit**

```bash
git add sortify/static tests/ui_harness.mjs
git commit -m "feat(split): the new Split tab up to the parts list; old split and sittings gone (frontend)"
```

---

### Task 9: Frontend — drag between parts, tap to preview, add and remove parts, the hint

**Files:**
- Modify: `sortify/static/app.js` (the split section), `sortify/static/style.css`
- Test: `tests/ui_harness.mjs`

**Interfaces:**
- Consumes (Task 8): `spDraft`, `spPid`, `spShown`, `spUrl`, `spPart`, `spSongLabel`, `renderSpParts`, `renderSpChips`, `renderSpList`, `spShowPart`, `spSuggest`, the no-op `spStopPreview` (replaced here). Routes: `POST /move`, `POST /parts`, `POST /parts/{key}/remove`, `GET /api/track_preview?pid&uri`, `POST /api/preview_resume`.
- Produces: `spPointerDown(uri, x, y)`, `spPointerMove(x, y)`, `spPointerUp(x, y)` (async), `spPointerCancel()`, `spChipAt(x, y) -> key|null`, `spDropKey` (the chip under a lifted song, or null), `spMove(uri, key)`, `spPreview(uri)`, `spStopPreview(resume = true)`, `spAudioUri`, `spAddPart()`, `spRemovePart(key)`, `spStartOver(n)`; `#sp-actions` holds `#sp-remove-part` and `#sp-start-over` (Task 10 adds `#sp-open-done`); `#sp-chips` gets a trailing `#sp-add` chip while fewer than 6 parts exist.
- Rules: a press that moves less than `SP_LIFT_PX = 8` pixels before release is a tap (preview); moving 8 px or more lifts the song (no long-press delay). Release over a chip of another part moves the song there; release anywhere else leaves it. Tapping the song that is playing stops it. Spotify is resumed (`POST /api/preview_resume`) once, when previewing stops, only if it was playing when the first preview started. Remove-part always asks, naming where the songs go; Start over always asks.

- [ ] **Step 1: Write the failing harness checks** — append a block before the summary:

```js
// ============================================================================
// SP2 — the parts screen: a touch that moves drags, a touch that does not is
// a preview; songs drop on chips; parts come and go.
// ============================================================================
{
  resetLog();
  AudioStub.made.length = 0; AudioStub.refuse = false;
  const songs = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                  u3: { name: "Three", artists: "C", local: false }, u4: { name: "Four", artists: "D", local: false } };
  const D = (parts, extra = {}) => ({ playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2, parts,
    unknown: [], hint: null, scores: {}, songs, tracks: [], done: null, ...extra });
  const two = () => [{ key: "p1", name: "rock", uris: ["u1", "u2"] }, { key: "p2", name: "jazz", uris: ["u3", "u4"] }];
  run(`show("split")`);
  // spDraft & co are `let` bindings in app.js, not properties of the vm's
  // global object: assign them inside run(), never through ctx.
  run(`spDraft = ${JSON.stringify(D(two()))}`);
  run(`spPid = "H1"; spShown = "p1"; spSection("sp-work"); renderSpParts(); nowState = { is_playing: true }`);

  // tap = preview
  routes["GET /api/track_preview?pid=H1&uri=u1"] = { status: 200, body: { url: "https://cdn/u1.mp3", uri: "u1", deezer_id: 1 } };
  run(`spPointerDown("u1", 10, 10)`);
  await run(`spPointerUp(10, 10)`);
  await tick();
  check("SP2 a tap plays the song's preview",
        AudioStub.made.length === 1 && AudioStub.made[0].src === "https://cdn/u1.mp3" && !AudioStub.made[0].paused &&
        run("spAudioUri") === "u1", `made=${AudioStub.made.length}`);
  check("SP2 ...and moves nothing", !log.some((l) => l.path.endsWith("/move")), "");
  run(`spPointerDown("u1", 10, 10)`);
  await run(`spPointerUp(11, 12)`);
  await tick();
  check("SP2 a tap on the playing song stops it, and Spotify is resumed once",
        AudioStub.made[0].paused && run("spAudioUri") === null &&
        log.filter((l) => l.path === "/api/preview_resume").length === 1, "");

  // no preview available
  routes["GET /api/track_preview?pid=H1&uri=u2"] = { status: 404, body: { detail: "no preview" } };
  run(`spPointerDown("u2", 10, 10)`);
  await run(`spPointerUp(10, 10)`);
  await tick();
  check("SP2 a song with no preview says so", /no preview/.test($$("toast").textContent), $$("toast").textContent);

  // drag onto another part's chip
  routes["POST /api/splitparts/H1/move"] = { status: 200,
    body: D([{ key: "p1", name: "rock", uris: ["u2"] }, { key: "p2", name: "jazz", uris: ["u3", "u4", "u1"] }]) };
  document.elementFromPoint = () => ({ closest: () => ({ dataset: { key: "p2" } }) });
  run(`spPointerDown("u1", 10, 10)`);
  run(`spPointerMove(10, 40)`);
  check("SP2 moving the finger lifts the song at once and lights the chip under it",
        run("spDropKey") === "p2" && document.body.children.some((c) => c.className === "sp-ghost"),
        `drop=${run("spDropKey")}`);
  await run(`spPointerUp(10, 40)`);
  await tick();
  const mv = log.find((l) => l.path === "/api/splitparts/H1/move");
  check("SP2 releasing over a chip moves the song to that part",
        mv && JSON.parse(mv.body).uri === "u1" && JSON.parse(mv.body).to === "p2", JSON.stringify(mv));
  check("SP2 ...and the list redraws without it", !/data-uri="u1"/.test($$("sp-list").innerHTML) &&
        run("spDropKey") === null, $$("sp-list").innerHTML);

  resetLog();
  document.elementFromPoint = () => null;
  run(`spPointerDown("u2", 10, 10); spPointerMove(10, 40)`);
  await run(`spPointerUp(10, 40)`);
  check("SP2 releasing away from every chip moves nothing", !log.some((l) => l.path.endsWith("/move")), "");
  document.elementFromPoint = () => ({ closest: () => ({ dataset: { key: "p1" } }) });
  run(`spPointerDown("u2", 10, 10); spPointerMove(10, 40)`);
  await run(`spPointerUp(10, 40)`);
  check("SP2 ...and so does releasing over its own part", !log.some((l) => l.path.endsWith("/move")), "");
  document.elementFromPoint = () => null;

  // add a part
  routes["POST /api/splitparts/H1/parts"] = { status: 200,
    body: D([...two(), { key: "p3", name: "New part", uris: [] }]) };
  check("SP2 the chip row ends with an add chip", /id="sp-add"/.test($$("sp-chips").innerHTML), $$("sp-chips").innerHTML);
  await run(`spAddPart()`);
  check("SP2 adding a part shows the new, empty part",
        run("spShown") === "p3" && /no songs in this part yet/.test($$("sp-list").innerHTML), "");

  // remove the shown part: asks with destinations, then shows another part
  run(`spDraft = ${JSON.stringify(D(two()))}`);
  run(`spShown = "p2"; renderSpParts()`);
  routes["POST /api/splitparts/H1/parts/p2/remove"] = (() => {
    let n = 0;
    return () => (n++ === 0
      ? { status: 200, body: { moves: { p1: 2 } } }
      : { status: 200, body: D([{ key: "p1", name: "rock", uris: ["u1", "u2", "u3", "u4"] }, { key: "p9", name: "x", uris: [] }]) });
  })();
  let asked = "";
  ctx.confirm = (m) => { asked = m; return false; };
  await run(`spRemovePart("p2")`);
  check("SP2 removing a part asks first, naming where its songs go",
        /jazz/.test(asked) && /2 songs/.test(asked) && /rock: 2/.test(asked), asked);
  check("SP2 ...and a no changes nothing",
        log.filter((l) => l.path.endsWith("/remove")).length === 1 && run("spShown") === "p2", "");
  ctx.confirm = () => true;
  await run(`spRemovePart("p2")`);
  check("SP removing the shown part shows another one",
        run("spShown") === "p1" && /data-uri="u3"/.test($$("sp-list").innerHTML), `shown=${run("spShown")}`);

  // the hint and Start over
  run(`spDraft = ${JSON.stringify(D(two(), { hint: 3 }))}`);
  run(`renderSpParts()`);
  check("SP2 a cleaner count is offered", $$("sp-hint").hidden === false && /3/.test($$("sp-hint").innerHTML),
        $$("sp-hint").innerHTML);
  routes["POST /api/splitparts/H1/suggest"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "running", phase: "grouping", done: 0, total: 0, message: null, summary: null } };
  ctx.confirm = (m) => { asked = m; return true; };
  await run(`spStartOver(3)`);
  const sg = log.find((l) => l.path === "/api/splitparts/H1/suggest");
  check("SP2 trying it asks first, then re-suggests with that count",
        /moves/.test(asked) && sg && JSON.parse(sg.body).n === 3, asked);
  run(`spStopPolling()`);

  // leaving the view stops a preview
  run(`spDraft = ${JSON.stringify(D(two()))}`);
  run(`renderSpParts()`);
  routes["GET /api/track_preview?pid=H1&uri=u3"] = { status: 200, body: { url: "https://cdn/u3.mp3", uri: "u3", deezer_id: 3 } };
  run(`spShowPart("p2"); spPointerDown("u3", 10, 10)`);
  await run(`spPointerUp(10, 10)`);
  await tick();
  const clip = AudioStub.made[AudioStub.made.length - 1];
  run(`show("now")`);
  check("SP2 leaving the Split tab stops the preview", clip.paused && run("spAudioUri") === null, "");
  ctx.confirm = () => true;
  run(`show("lists")`);
}
```

- [ ] **Step 2: Run the harness to verify the SP2 checks fail**

Run: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | grep -E "^FAIL  SP|checks passed"`
Expected: FAIL lines for SP2 (`spPointerDown` not defined).

- [ ] **Step 3: Implement** — in the split section of `sortify/static/app.js`, replace `function spStopPreview() {}` with the preview code, and add the rest after `renderSpList`:

```js
// ---- tap = preview ----------------------------------------------------------
// One clip at a time. Spotify is paused by the phone when the clip takes the
// audio, and given back once when previewing ends — only if it was playing.
let spAudio = null;
let spAudioUri = null;
let spResumeOwed = false;

function spStopPreview(resume = true) {
  if (spAudio) { spAudio.onended = null; spAudio.pause(); spAudio.src = ""; }
  spAudio = null;
  const was = spAudioUri;
  spAudioUri = null;
  if (resume && spResumeOwed) {
    spResumeOwed = false;
    api("/api/preview_resume", {}).catch(() => {});
  }
  if (was && spDraft && !$("sp-parts").hidden) renderSpList();
}

async function spPreview(uri) {
  if (spAudioUri === uri) { spStopPreview(); return; }
  if (spAudio) spStopPreview(false);
  if (!spResumeOwed && nowState?.is_playing) spResumeOwed = true;
  spAudioUri = uri;
  let r;
  try {
    r = await api(`/api/track_preview?pid=${encodeURIComponent(spPid)}&uri=${encodeURIComponent(uri)}`);
  } catch (e) {
    if (spAudioUri === uri) spAudioUri = null;
    toast(e.status === 404 ? "no preview for this one" : e.message);
    return;
  }
  if (spAudioUri !== uri) return;        // another tap won the race
  spAudio = new Audio(r.url);
  spAudio.onended = () => spStopPreview();
  try { await spAudio.play(); } catch (_) { toast("preview blocked — tap again"); spStopPreview(); return; }
  renderSpList();
}

// ---- touch that moves = drag -------------------------------------------------
const SP_LIFT_PX = 8;
let spDrag = null;      // {uri, x0, y0, lifted, ghost}
let spDropKey = null;   // the chip under a lifted song

function spChipAt(x, y) {
  const el = document.elementFromPoint ? document.elementFromPoint(x, y) : null;
  const chip = el && el.closest ? el.closest(".sp-chip[data-key]") : null;
  return chip ? chip.dataset.key : null;
}

function spHighlight(key) {
  spDropKey = key;
  $("sp-chips").querySelectorAll("button").forEach((b, i) => {
    const part = spDraft.parts[i];
    if (part) b.classList.toggle("drop", part.key === key);
  });
}

function spPointerDown(uri, x, y) {
  spDrag = { uri, x0: x, y0: y, lifted: false, ghost: null };
}

function spPointerMove(x, y) {
  if (!spDrag) return;
  if (!spDrag.lifted && Math.hypot(x - spDrag.x0, y - spDrag.y0) >= SP_LIFT_PX) {
    spDrag.lifted = true;
    const g = document.createElement("div");
    g.className = "sp-ghost";
    g.textContent = spSongLabel(spDrag.uri);
    document.body.appendChild(g);
    spDrag.ghost = g;
  }
  if (!spDrag.lifted) return;
  spDrag.ghost.style.left = `${x}px`;
  spDrag.ghost.style.top = `${y}px`;
  const key = spChipAt(x, y);
  spHighlight(key && key !== spShown ? key : null);
}

function spEndDrag() {
  const d = spDrag;
  spDrag = null;
  if (d && d.ghost && d.ghost.remove) d.ghost.remove();
  if (d && d.ghost && document.body.children) {   // the harness's stub body
    const i = document.body.children.indexOf(d.ghost);
    if (i >= 0) document.body.children.splice(i, 1);
  }
  spHighlight(null);
  return d;
}

async function spPointerUp(x, y) {
  if (!spDrag) return;
  const lifted = spDrag.lifted;
  const key = lifted ? spChipAt(x, y) : null;
  const d = spEndDrag();
  if (!lifted) { await spPreview(d.uri); return; }
  if (key && key !== spShown) await spMove(d.uri, key);
}

function spPointerCancel() { spEndDrag(); }

function spListPointerDown(e) {
  const row = e.target && e.target.closest ? e.target.closest(".sp-song") : null;
  if (!row || (e.button !== undefined && e.button > 0)) return;
  e.preventDefault();
  spPointerDown(row.dataset.uri, e.clientX, e.clientY);
}
document.addEventListener("pointermove", (e) => {
  if (!spDrag) return;
  if (e.preventDefault) e.preventDefault();
  spPointerMove(e.clientX, e.clientY);
}, { passive: false });
document.addEventListener("pointerup", (e) => { if (spDrag) spPointerUp(e.clientX, e.clientY); });
document.addEventListener("pointercancel", () => { if (spDrag) spPointerCancel(); });

async function spMove(uri, key) {
  try {
    spDraft = await api(spUrl("/move"), { uri, to: key });
  } catch (e) {
    toast(e.message);
    return;
  }
  renderSpParts();
  toast(`moved to ${spPart(key)?.name || "the other part"}`);
}

// ---- parts come and go ---------------------------------------------------------
async function spAddPart() {
  try {
    spDraft = await api(spUrl("/parts"), {});
  } catch (e) {
    toast(e.message);
    return;
  }
  spShown = spDraft.parts[spDraft.parts.length - 1].key;
  renderSpParts();
}

async function spRemovePart(key) {
  const part = spPart(key);
  if (!part) return;
  let dry;
  try {
    dry = await api(spUrl(`/parts/${encodeURIComponent(key)}/remove`), { dry: true });
  } catch (e) {
    toast(e.message);
    return;
  }
  const where = Object.entries(dry.moves || {})
    .map(([k, n]) => `${spPart(k)?.name || k}: ${n}`).join(", ");
  const msg = part.uris.length
    ? `Remove “${part.name}”? Its ${part.uris.length} songs go to the parts they fit best — ${where}.`
    : `Remove the empty part “${part.name}”?`;
  if (!confirm(msg)) return;
  try {
    spDraft = await api(spUrl(`/parts/${encodeURIComponent(key)}/remove`), { dry: false });
  } catch (e) {
    toast(e.message);
    return;
  }
  if (spShown === key) spShown = spDraft.parts[0]?.key || null;
  renderSpParts();
}

async function spStartOver(n) {
  if (!confirm(`Start over with ${n} parts? Your moves and added parts are lost.`)) return;
  await spSuggest(n);
}
```

Then extend the Task 8 renderers. In `renderSpChips`, append the add chip and keep the per-part wiring index-based:

```js
function renderSpChips() {
  const add = spDraft.parts.length < SP_MAX
    ? '<button id="sp-add" class="sp-chip sp-add" aria-label="Add a part">+</button>' : "";
  $("sp-chips").innerHTML = spDraft.parts.map((p) =>
    `<button class="sp-chip${p.key === spShown ? " on" : ""}${p.key === spDropKey ? " drop" : ""}" data-key="${esc(p.key)}">
       <span class="sp-chip-name">${esc(p.name)}</span> <span class="sp-chip-n">${p.uris.length}</span>
     </button>`).join("") + add;
  $("sp-chips").querySelectorAll("button").forEach((b, i) => {
    const part = spDraft.parts[i];
    if (part) b.onclick = () => spShowPart(part.key);
  });
  if (add) $("sp-add").onclick = () => spAddPart();
}
```

In `renderSpList`, after setting `innerHTML`, mark the playing row and wire the list:

```js
  $("sp-list").onpointerdown = spListPointerDown;
```

and give the playing row a class: in the row template use `class="sp-song${u === spAudioUri ? " playing" : ""}"`.

In `renderSpParts`, after `renderSpList()`, render the hint and the actions:

```js
  const hint = spDraft.hint;
  $("sp-hint").hidden = !hint;
  if (hint) {
    $("sp-hint").innerHTML = `This splits more cleanly into ${hint} — <button id="sp-hint-go">try ${hint}</button>`;
    $("sp-hint-go").onclick = () => spStartOver(hint);
  }
  const canRemove = spDraft.parts.length > SP_MIN;
  $("sp-actions").innerHTML =
    (canRemove ? '<button id="sp-remove-part">Remove this part</button>' : "") +
    '<button id="sp-start-over">Start over</button>';
  if (canRemove) $("sp-remove-part").onclick = () => spRemovePart(spShown);
  $("sp-start-over").onclick = () => spStartOver(spDraft.n);
```

- [ ] **Step 4: Style it** — append to `sortify/static/style.css`:

```css
.sp-song.playing { outline: 2px solid var(--accent); }
.sp-ghost {
  position: fixed; z-index: 30; pointer-events: none; transform: translate(-50%, -120%);
  max-width: 70vw; padding: .45rem .8rem; border-radius: 10px;
  background: var(--fg); color: var(--bg); font-size: .9rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  box-shadow: var(--shadow-card);
}
.sp-chip.sp-add { font-weight: 700; min-width: 2.6rem; }
#sp-hint { margin: .4rem 0; color: var(--muted); }
#sp-actions { margin-top: 1rem; gap: .5rem; }
```

- [ ] **Step 5: Run both suites**

Run: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | grep -E "^FAIL|checks passed" && .venv/bin/pytest -q 2>&1 | tail -1`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add sortify/static tests/ui_harness.mjs
git commit -m "feat(split): drag songs between parts, tap to preview, add and remove parts"
```

---

### Task 10: Frontend — the Done screen, progress, summary, re-read

**Files:**
- Modify: `sortify/static/app.js` (split section), `sortify/static/style.css`
- Test: `tests/ui_harness.mjs`

**Interfaces:**
- Consumes (Task 7 routes): `GET /plan?original=subset|part&part=`, `POST /done`, `GET /progress` (with `summary`, states `done`/`changed`/`error`), `POST /reread`. (Tasks 8–9): `spPoll("done")`, `spPanel`, `renderSpParts`, `spUrl`, `showSplit`, Task 8's interim `spDoneFinished` (which only redraws the parts; replaced here).
- Produces: `spOpenDone()`, `spRefreshPlan()`, `renderSpDone()`, `spReadNames()`, `spConfirmDone()`, `spDoneFinished(progress)` (real), `renderSpSummary(summary)`, `spReread()`; state `spDoneChoice = {original, part, folder, names}` and `spPlan`; `#sp-open-done` in `#sp-actions`; ids on the Done screen: `sp-name-<i>`, `sp-folder`, `sp-orig-subset`, `sp-orig-part`, `sp-orig-part-key`, `sp-cost`, `sp-done-back`, `sp-confirm`, `sp-reread`, `sp-retry`, `sp-finish`.
- Copy: no emoji in UI text (the subset mark is described in words).

- [ ] **Step 1: Write the failing harness checks** — append a block before the summary:

```js
// ============================================================================
// SP3 — Done: name, folder, what happens to the original, the price; then
// progress, and a summary or a way back.
// ============================================================================
{
  resetLog();
  const songs = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                  u3: { name: "Three", artists: "C", local: false } };
  const DRAFT = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "rock", uris: ["u1", "u2"] }, { key: "p2", name: "jazz", uris: ["u3"] }],
    unknown: [], hint: null, scores: {}, songs, tracks: [], done: null };
  run(`show("split"); spPid = "H1"; spDraft = ${JSON.stringify(DRAFT)}; spShown = "p1"; spSection("sp-work"); renderSpParts()`);
  check("SP3 the parts screen offers Done", /id="sp-open-done"/.test($$("sp-actions").innerHTML), $$("sp-actions").innerHTML);

  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 6, creates: 2, empty: [], local: ["Home demo"], removals: 0,
    folders: [{ path: "ROOT / Mix", blocked: null }, { path: "ROOT / Deep", blocked: "file this one by hand" }],
    folder: "ROOT / Mix" } };
  await run(`spOpenDone()`);
  const doneHtml = () => $$("sp-done").innerHTML;
  check("SP3 the Done screen names each part, prefilled",
        /id="sp-name-0"[^>]*value="rock"/.test(doneHtml()) && /id="sp-name-1"[^>]*value="jazz"/.test(doneHtml()), doneHtml().slice(0, 400));
  check("SP3 ...defaults to the original's folder, and a folder the mover can't reach is disabled",
        $$("sp-folder").value === "ROOT / Mix" && /value="ROOT \/ Deep" disabled/.test(doneHtml()), doneHtml());
  check("SP3 ...keeps the original as a subset by default",
        /id="sp-orig-subset" checked/.test(doneHtml()) || /id="sp-orig-subset"[^>]*checked/.test(doneHtml()), "");
  check("SP3 ...warns about local files that stay behind", /Home demo/.test(doneHtml()) && /local file/.test(doneHtml()), "");
  check("SP3 ...and shows the price before anything happens",
        /2 new playlists/.test(doneHtml()) && /about 6 calls/.test(doneHtml()) &&
        !log.some((l) => l.method === "POST"), doneHtml());

  routes["GET /api/splitparts/H1/plan?original=part&part=p1"] = { status: 200, body: {
    calls: 4, creates: 1, empty: [], local: [], removals: 1,
    folders: [{ path: "ROOT / Mix", blocked: null }], folder: "ROOT / Mix" } };
  run(`spDoneChoice.original = "part"; spDoneChoice.part = "p1"`);
  await run(`spRefreshPlan()`);
  check("SP3 choosing 'becomes a part' re-prices it, with the removals",
        /1 new playlist\b/.test(doneHtml()) && /removes 1 song/.test(doneHtml()) && /about 4 calls/.test(doneHtml()), doneHtml());
  check("SP3 ...and that part's name field says it keeps the original's name",
        /keeps the name/.test(doneHtml()) && /Big Home/.test(doneHtml()), doneHtml());

  run(`spDoneChoice.original = "subset"; spDoneChoice.part = null`);
  await run(`spRefreshPlan()`);
  $$("sp-name-0").value = "Rocky";
  $$("sp-name-1").value = "";
  routes["POST /api/splitparts/H1/done"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "done", phase: "done", done: 2, total: 2, message: null,
    summary: { created: [{ id: "N1", name: "Rocky", count: 2 }, { id: "N2", name: "jazz", count: 1 }],
               original: "subset", empty: [], local: ["Home demo"] } } };
  await run(`spConfirmDone()`);
  await tick();
  const done = log.find((l) => l.method === "POST" && l.path === "/api/splitparts/H1/done");
  const body = done && JSON.parse(done.body);
  check("SP3 Confirm sends the typed names, the folder and the choice",
        body && body.names.p1 === "Rocky" && body.names.p2 === "jazz" && body.folder === "ROOT / Mix" &&
        body.original === "subset" && body.part === null, JSON.stringify(body));
  check("SP3 ...and ends on a summary of what was made",
        /Rocky/.test(doneHtml()) && /2 songs/.test(doneHtml()) && /id="sp-finish"/.test(doneHtml()) &&
        run("spDraft") === null, doneHtml());

  // the playlist changed: a way to re-read
  run(`spDraft = ${JSON.stringify(DRAFT)}`);
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "changed", phase: "", done: 0, total: 2,
    message: "the playlist changed in Spotify since the suggestion — re-read it to continue", summary: null } };
  await run(`spConfirmDone()`);
  check("SP3 a changed playlist offers Re-read", /id="sp-reread"/.test(doneHtml()) && /changed in Spotify/.test(doneHtml()), doneHtml());
  routes["POST /api/splitparts/H1/reread"] = { status: 200, body: { ...DRAFT, unknown: ["u3"] } };
  await run(`spReread()`);
  check("SP3 ...which lands back on the parts", $$("sp-parts").hidden === false && run("spDraft.unknown.length") === 1, "");

  // an error: Done again
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "error", phase: "", done: 1, total: 2,
    message: "Spotify API 429: rate limited — your split is saved; press Done again to continue", summary: null } };
  resetLog();
  await run(`spConfirmDone()`);
  check("SP3 a failed Done says why and offers Done again",
        /press Done again/.test(doneHtml()) && /id="sp-retry"/.test(doneHtml()), doneHtml());
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "running", phase: "creating", done: 1, total: 2, message: null, summary: null } };
  await run(`$("sp-retry").onclick()`);
  check("SP3 ...and Done again posts again",
        log.filter((l) => l.method === "POST" && l.path === "/api/splitparts/H1/done").length === 2, "");
  run(`spStopPolling(); show("lists")`);
}
```

- [ ] **Step 2: Run the harness to verify the SP3 checks fail**

Run: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | grep -E "^FAIL  SP3|checks passed"`
Expected: FAIL lines for SP3.

- [ ] **Step 3: Implement** — replace Task 8's `function spDoneFinished(p) { renderSpParts(); }` with the real Done code below, and add the Done button to `renderSpParts`'s actions: `$("sp-actions").innerHTML = ... + '<button id="sp-open-done" class="primary">Done…</button>';` wired with `$("sp-open-done").onclick = spOpenDone;`.

```js
// ---- Done ---------------------------------------------------------------------
let spDoneChoice = { original: "subset", part: null, folder: null, names: {} };
let spPlan = null;

async function spOpenDone() {
  spDoneChoice = { original: "subset", part: spDraft.parts[0]?.key || null,
                   folder: undefined, names: {} };
  spPanel("sp-done");
  $("sp-done").innerHTML = '<div class="spinner"></div>';
  await spRefreshPlan();
}

async function spRefreshPlan() {
  spReadNames();
  const q = spDoneChoice.original === "part"
    ? `?original=part&part=${encodeURIComponent(spDoneChoice.part)}`
    : "?original=subset";
  try {
    spPlan = await api(spUrl("/plan" + q));
  } catch (e) {
    $("sp-done").innerHTML = `<p class="hint">${esc(e.message)}</p>
      <div class="row"><button id="sp-done-back">Back</button></div>`;
    $("sp-done-back").onclick = renderSpParts;
    return;
  }
  if (spDoneChoice.folder === undefined) spDoneChoice.folder = spPlan.folder || "";
  renderSpDone();
}

// What the name fields hold now; an emptied field falls back to the
// suggested name (the server does the same).
function spReadNames() {
  if (!spDraft) return;
  spDraft.parts.forEach((p, i) => {
    const el = $(`sp-name-${i}`);
    const v = el && typeof el.value === "string" ? el.value.trim() : "";
    if (el && !el.disabled) spDoneChoice.names[p.key] = v || spDoneChoice.names[p.key] || p.name;
  });
}

function spCostLine(plan, isPart) {
  const n = plan.creates;
  const made = `${n} new playlist${n === 1 ? "" : "s"}`;
  const orig = isPart
    ? `removes ${plan.removals} song${plan.removals === 1 ? "" : "s"} from the original`
    : "marks the original as a subset";
  return `${made} · ${orig} · about ${plan.calls} calls`;
}

function renderSpDone() {
  const isPart = spDoneChoice.original === "part";
  const names = spDraft.parts.map((p, i) => {
    const keeps = isPart && p.key === spDoneChoice.part;
    const val = keeps ? spDraft.name : (spDoneChoice.names[p.key] || p.name);
    return `<label class="sp-name-row">
        <span class="hint">${p.uris.length} song${p.uris.length === 1 ? "" : "s"}${keeps ? ` · keeps the name “${esc(spDraft.name)}”` : ""}</span>
        <input id="sp-name-${i}" value="${esc(val)}" autocomplete="off"${keeps ? " disabled" : ""}>
      </label>`;
  }).join("");
  const folders = [`<option value="">${esc(FOLDER_TOP_LEVEL)}</option>`].concat(
    (spPlan.folders || []).map((f) => `<option value="${esc(f.path)}"${f.blocked ? " disabled" : ""}>${esc(f.path)}${f.blocked ? " — file by hand" : ""}</option>`)).join("");
  const parts = spDraft.parts.map((p) =>
    `<option value="${esc(p.key)}"${p.key === spDoneChoice.part ? " selected" : ""}>${esc(p.name)}</option>`).join("");
  const warn = [];
  if (spPlan.local.length) {
    warn.push(`${spPlan.local.length} local file${spPlan.local.length === 1 ? "" : "s"} cannot be added through Spotify's API and stay${spPlan.local.length === 1 ? "s" : ""} in the original only: ${spPlan.local.map(esc).join(", ")}`);
  }
  if (spPlan.empty.length) warn.push(`Not created, no songs: ${spPlan.empty.map(esc).join(", ")}`);
  $("sp-done").innerHTML = `
    <h3>Name the parts</h3>${names}
    <h3>Folder</h3><select id="sp-folder">${folders}</select>
    <h3>The original, “${esc(spDraft.name)}”</h3>
    <label class="sp-orig"><input type="radio" name="sp-orig" id="sp-orig-subset"${isPart ? "" : " checked"}>
      Keep it as a subset — it keeps every song and stops being a home</label>
    <label class="sp-orig"><input type="radio" name="sp-orig" id="sp-orig-part"${isPart ? " checked" : ""}>
      It becomes the part <select id="sp-orig-part-key"${isPart ? "" : " disabled"}>${parts}</select></label>
    ${warn.map((w) => `<p class="hint sp-warn">${w}</p>`).join("")}
    <p id="sp-cost" class="sp-cost">${spCostLine(spPlan, isPart)}</p>
    <div class="row"><button id="sp-done-back">Back</button><button id="sp-confirm" class="primary">Confirm</button></div>`;
  $("sp-folder").value = spDoneChoice.folder || "";
  $("sp-folder").onchange = () => { spDoneChoice.folder = $("sp-folder").value; };
  $("sp-orig-subset").onchange = () => { spDoneChoice.original = "subset"; spRefreshPlan(); };
  $("sp-orig-part").onchange = () => { spDoneChoice.original = "part"; spRefreshPlan(); };
  $("sp-orig-part-key").onchange = () => { spDoneChoice.part = $("sp-orig-part-key").value; spRefreshPlan(); };
  $("sp-done-back").onclick = () => { spReadNames(); renderSpParts(); };
  $("sp-confirm").onclick = spConfirmDone;
}

async function spConfirmDone() {
  spReadNames();
  const isPart = spDoneChoice.original === "part";
  const body = { names: spDoneChoice.names, folder: spDoneChoice.folder || null,
                 original: spDoneChoice.original, part: isPart ? spDoneChoice.part : null };
  try {
    await api(spUrl("/done"), body);
  } catch (e) {
    toast(e.message, 6000);
    return;
  }
  spPanel("sp-progress");
  $("sp-progress").innerHTML = '<div class="spinner"></div><p class="hint">starting…</p>';
  await spPoll("done");
}

function spDoneFinished(p) {
  if (p.state === "done" && p.summary) { renderSpSummary(p.summary); return; }
  spPanel("sp-done");
  if (p.state === "changed") {
    $("sp-done").innerHTML = `<p>${esc(p.message)}</p>
      <div class="row"><button id="sp-reread" class="primary">Re-read the playlist</button></div>`;
    $("sp-reread").onclick = spReread;
    return;
  }
  $("sp-done").innerHTML = `<p>${esc(p.message || "Done did not finish")}</p>
    <div class="row"><button id="sp-done-back">Back</button><button id="sp-retry" class="primary">Done again</button></div>`;
  $("sp-done-back").onclick = renderSpParts;
  $("sp-retry").onclick = spConfirmDone;
}

function renderSpSummary(s) {
  spDraft = null;
  const rows = s.created.map((c) => `<li><b>${esc(c.name)}</b> — ${c.count} song${c.count === 1 ? "" : "s"}</li>`).join("");
  const orig = s.original === "subset"
    ? "The original is now a subset and keeps every song."
    : "The original kept its name and now holds its part's songs.";
  const local = s.local.length ? `<p class="hint">Local files left in the original: ${s.local.map(esc).join(", ")}</p>` : "";
  spPanel("sp-done");
  $("sp-done").innerHTML = `<h3>Done</h3><ul class="sp-summary">${rows}</ul><p class="hint">${orig}</p>${local}
    <div class="row"><button id="sp-finish" class="primary">Back to homes</button></div>`;
  $("sp-finish").onclick = showSplit;
}

async function spReread() {
  try {
    spDraft = await api(spUrl("/reread"), {});
  } catch (e) {
    toast(e.message);
    return;
  }
  renderSpParts();
  toast("re-read — new songs are marked ?");
}
```

`FOLDER_TOP_LEVEL` is the existing constant from the Playlists view's create row.

- [ ] **Step 4: Style it** — append to `sortify/static/style.css`:

```css
#sp-done h3 { font-size: 1rem; margin: 1.1rem 0 .4rem; }
.sp-name-row { display: block; margin: .4rem 0; }
.sp-name-row input { width: 100%; }
.sp-orig { display: block; margin: .35rem 0; }
.sp-warn { color: var(--muted); }
.sp-cost { margin-top: 1rem; font-weight: 600; }
.sp-summary { padding-left: 1.2rem; }
```

- [ ] **Step 5: Run both suites**

Run: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | grep -E "^FAIL|checks passed" && .venv/bin/pytest -q 2>&1 | tail -1`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add sortify/static tests/ui_harness.mjs
git commit -m "feat(split): the Done screen — names, folder, the original, the price, progress and summary"
```

---

### Task 11: Docs, a zero-call tuning look, and rollout

**Files:**
- Modify: `CLAUDE.md`
- Create: `scripts/eval_splitparts.py`

**Interfaces:**
- Consumes: `sortify.splitparts.suggest`, `Store` (read-only), `data/cache.json` cached track lists.

- [ ] **Step 1: Update `CLAUDE.md`**
  - In "Spotify API budget", the sentence about the queued materialiser (`BULK_RESERVE 150 … pacing.json carries the measured max_clean_rate …`): keep `BULK_RESERVE` (still a budget class) but delete the materialiser, the governor, `spend_reserve` per-run checkbox and `data/pacing.json` from it.
  - Add a section after "Playlist folders":

```markdown
## Split a home into parts

- The Split tab (since 2026-10): pick a home, choose 2–6 parts, get a
  suggestion, drag songs between parts on the phone, tap a song for its
  30 s Deezer preview, then Done. Spec:
  `docs/superpowers/specs/2026-10-20-split-into-parts-design.md`.
- **Nothing is written to Spotify before Done.** Suggest reads the snapshot
  (1 call) and the track list only when it moved (~1 per 100).
- The grouping (`sortify/splitparts.py`) is pure and offline: artist tags,
  Last.fm getSimilar and artist-similar, average-linkage clustering cut at
  exactly N with slivers folded in. Weights are constants in that file.
- Drafts live in `data/splitparts.json`, one per home; every move is saved
  at once.
- Done creates and fills the new parts first and touches the original only
  after that (🐾 rename and dropped from the home lists, or batched
  removal). It is resumable and never creates a part twice.
- Replaced the 2026-08 split (piles, sittings, the queued materialiser):
  all of that code is gone; its data files were kept as
  `*.bak-2026-10-20`.
```

  - Fix the stale harness count sentence ("a hand-run harness of 189 checks") to say "a hand-run harness" without a number.

- [ ] **Step 2: Write `scripts/eval_splitparts.py`** — reads only local files; never calls Spotify or Last.fm:

```python
"""Look at what the split-into-parts engine suggests for real homes.

Zero API calls: reads data/cache.json's cached track lists and the three
Last.fm files, and prints part sizes, names and cleanliness scores. Used to
tune the constants in sortify/splitparts.py once, with the user, before
they are frozen.

    .venv/bin/python scripts/eval_splitparts.py [--n 2 3] [--limit 5] [--id PID ...]
"""

from __future__ import annotations

import argparse
import time

from sortify import splitparts as spp
from sortify.store import Store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, nargs="+", default=[2, 3])
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--id", nargs="*", default=None)
    args = ap.parse_args()

    store = Store()
    cache = store.cache()
    cfg = store.config()
    listing = {p["id"]: p for p in (cache.get("playlist_list") or {}).get("items") or []}
    homes = args.id or [h for h in cfg.get("home_ids") or []
                        if h in cache["playlists"] and h in listing]
    homes = sorted(homes, key=lambda h: -len(cache["playlists"][h]["tracks"]))[:args.limit]
    tags, tmap, amap = store.tag_artists(), store.lastfm_track_map(), store.lastfm_artist_map()
    for pid in homes:
        tracks = cache["playlists"][pid]["tracks"]
        print(f"\n{listing.get(pid, {}).get('name', pid)} — {len(tracks)} songs")
        for n in args.n:
            t0 = time.monotonic()
            try:
                s = spp.suggest(tracks, n, tags, tmap, amap)
            except spp.SplitError as exc:
                print(f"  n={n}: {exc}")
                continue
            took = time.monotonic() - t0
            sizes = " / ".join(f"{len(p)} {name!r}" for p, name in zip(s.parts, s.names))
            print(f"  n={n} ({took:.1f}s): {sizes}; unknown {len(s.unknown)}; "
                  f"scores {s.scores}; hint {s.hint}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run it and commit**

Run: `.venv/bin/python scripts/eval_splitparts.py --limit 3` (from the worktree, with `data` symlinked to the live tree's `data/` per the worktree memory, read-only use) and paste the output into the report.
Then: `.venv/bin/pytest -q && ~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | tail -1`, and

```bash
git add CLAUDE.md scripts/eval_splitparts.py
git commit -m "docs(split): the new Split tab in CLAUDE.md; a zero-call engine eval script"
```

- [ ] **Step 4 (controller, with the user): tune once.** Show the user the eval output for 2–3 of their homes. Change a weight in `splitparts.py` only if they judge a grouping wrong and a change fixes it in the eval; re-run the engine tests after any change and commit `tune(split): …` naming the evidence. Then the constants are frozen.

- [ ] **Step 5 (controller, after merge, with the user): roll out.**
  1. Move the old data aside in the live tree: `cd ~/kode/spotify/sortify && for f in splits.json queue.json pacing.json; do [ -e data/$f ] && mv data/$f data/$f.bak-2026-10-20; done` (no deletion).
  2. Restart `sortify.service` following the restart-coordination memory (queue state no longer exists; still announce to live sortify sessions and wait).
  3. Check boxdash's sortify queue card is simply absent.
  4. With the user on the phone: split one real home. Report `.venv/bin/spx budget` before and after; Done should cost what its price line said.
  5. If the batch-remove probe (Task 1 Step 6) was not run yet, run it before any "becomes a part" Done.

