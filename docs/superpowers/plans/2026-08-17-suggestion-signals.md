# Suggestion Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rank home playlists for a track using tags, track similarity, and user-written playlist descriptions — not artist overlap alone — and prove the improvement with a measured before/after.

**Architecture:** `suggest.py` becomes a set of named features, each returning `(score, reason)`, combined by weights that are *measured* rather than guessed. A hold-out harness ranks tracks the user already filed and reports top-1/top-3 accuracy. Last.fm data (track tags, similar tracks) is fetched through the existing `tags.py` client into a rebuildable cache; descriptions and user tags live in separate files that only explicit user action ever writes.

**Tech Stack:** Python 3.12, FastAPI, pytest, httpx. Vanilla JS frontend, no build step.

**Spec:** `docs/superpowers/specs/2026-08-17-suggestion-signals-design.md`

## Prerequisite

**This plan cannot start until the `lastfm-tags` branch is merged to master.**
It consumes `sortify/tags.py` (the `LastFm` client, `clean_tags`, `LastFmError`,
`load_key`) and `Store.tags()`, none of which exist on master yet. Verify with:

```bash
git log --oneline -1 && test -f sortify/tags.py && grep -q "def tags" sortify/store.py && echo "prerequisite met"
```

If that prints nothing, stop and merge `lastfm-tags` first.

## Global Constraints

- **Zero Spotify API calls.** Nothing in this plan may touch `api.spotify.com`. Last.fm is a different service with a separate quota and no bearing on the Spotify budget.
- **Last.fm pacing:** reuse `tags.py`'s `MIN_INTERVAL = 0.25` (4 requests/second). Do not add a second rate limiter.
- **Only Last.fm error code 6 means "not found".** Codes 10, 26, 29, 8, 11, 16 are failures and must raise `LastFmError`. Never record a failure as a cached miss — misses are never re-fetched, so a false miss is permanent and silent.
- **Precious files are never written by automatic processes.** `data/descriptions.json` and `data/user_tags.json` are written only in response to an explicit user action.
- **`neighbour` scoring excludes same-artist neighbours.** They are already counted by `artist_overlap`; including them reproduces the bug this work exists to fix.
- **The evaluation harness must remove the held-out track from its home's profile before ranking.** Otherwise the score is trivially perfect and the measurement is worthless.
- **No background jobs.** All fetching is explicit user action or a bounded command.
- Tests must pass with `.venv/bin/pytest -q` and cost zero API calls of any kind.

---

### Task 1: Storage for descriptions, user tags, and the Last.fm track cache

Separates user-authored data from rebuildable cache at the file level, so a cache wipe can never take the user's typing with it.

**Files:**
- Modify: `sortify/store.py` (after `save_tags`, around line 97)
- Test: `tests/test_store_signals.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Store.descriptions() -> dict`, `Store.save_descriptions(payload: dict) -> None`, `Store.user_tags() -> dict`, `Store.save_user_tags(payload: dict) -> None`, `Store.lastfm_tracks() -> dict`, `Store.save_lastfm_tracks(payload: dict) -> None`

- [ ] **Step 1: Write the failing test**

```python
"""Storage for the new suggestion signals.

The governing rule is that user-authored data (descriptions, hand-typed tags)
never shares a file with a rebuildable cache. Losing a cache is an
inconvenience; losing what the user typed is unrecoverable.
"""

from sortify.store import Store


def test_descriptions_default_to_empty(tmp_path):
    assert Store(tmp_path).descriptions() == {"version": 1, "playlists": {}}


def test_descriptions_round_trip(tmp_path):
    s = Store(tmp_path)
    s.save_descriptions({"version": 1, "playlists": {"abc": "slow sad stuff"}})
    assert s.descriptions()["playlists"]["abc"] == "slow sad stuff"


def test_user_tags_default_to_empty(tmp_path):
    assert Store(tmp_path).user_tags() == {"version": 1, "tracks": {}}


def test_user_tags_round_trip(tmp_path):
    s = Store(tmp_path)
    s.save_user_tags({"version": 1, "tracks": {"spotify:track:x": ["ballad"]}})
    assert s.user_tags()["tracks"]["spotify:track:x"] == ["ballad"]


def test_lastfm_tracks_default_to_empty(tmp_path):
    assert Store(tmp_path).lastfm_tracks() == {"version": 1, "tracks": {}}


def test_precious_and_rebuildable_live_in_different_files(tmp_path):
    """A cache wipe must not be able to take the user's typing with it."""
    s = Store(tmp_path)
    s.save_descriptions({"version": 1, "playlists": {"abc": "mine"}})
    s.save_user_tags({"version": 1, "tracks": {"uri": ["mine"]}})
    s.save_lastfm_tracks({"version": 1, "tracks": {"k": {"miss": True}}})

    (tmp_path / "lastfm_tracks.json").unlink()

    assert s.descriptions()["playlists"]["abc"] == "mine"
    assert s.user_tags()["tracks"]["uri"] == ["mine"]
    assert s.lastfm_tracks() == {"version": 1, "tracks": {}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_store_signals.py -q`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'descriptions'`

- [ ] **Step 3: Write minimal implementation**

Add to `sortify/store.py` after `save_tags`:

```python
    # descriptions.json: what each home playlist is FOR, in the user's words.
    # Precious: written only by explicit user action, never by a refresh.
    def descriptions(self) -> dict:
        return self._load("descriptions.json", {"version": 1, "playlists": {}})

    def save_descriptions(self, payload: dict) -> None:
        self._save("descriptions.json", payload)

    # user_tags.json: tags the user typed, keyed by track URI so they survive
    # playlist moves. Precious, for the same reason.
    def user_tags(self) -> dict:
        return self._load("user_tags.json", {"version": 1, "tracks": {}})

    def save_user_tags(self, payload: dict) -> None:
        self._save("user_tags.json", payload)

    # lastfm_tracks.json: track tags and similar-track results. Rebuildable —
    # deleting it costs a refetch and nothing else.
    def lastfm_tracks(self) -> dict:
        return self._load("lastfm_tracks.json", {"version": 1, "tracks": {}})

    def save_lastfm_tracks(self, payload: dict) -> None:
        self._save("lastfm_tracks.json", payload)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_store_signals.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add sortify/store.py tests/test_store_signals.py
git commit -m "Separate user-authored signal data from the rebuildable cache"
```

---

### Task 2: Evaluation harness against today's scorer

Built before any scoring change so the baseline is measured, not remembered. Produces the number every later task is judged against.

**Files:**
- Create: `sortify/evaluate.py`
- Create: `scripts/eval_suggest.py`
- Test: `tests/test_evaluate.py`

**Interfaces:**
- Consumes: `Store` from Task 1; existing `sortify.suggest.build_profile(tracks, artist_info)` and `sortify.suggest.suggest(track, profiles, artist_info)`
- Produces: `sortify.evaluate.sample_pairs(cache: dict, home_ids: list[str], n: int, seed: int) -> list[tuple[str, dict]]` returning `(home_id, track)`; `sortify.evaluate.evaluate(cache: dict, home_ids: list[str], rank: callable, n: int = 500, seed: int = 0) -> dict` returning `{"n": int, "top1": float, "top3": float}`. `rank` takes `(track, profiles)` and returns a ranked list of dicts each with a `playlist_id` key.

- [ ] **Step 1: Write the failing test**

```python
"""The evaluation harness.

The user's home playlists are labelled data: every track in a home is a
decision the user made by hand. Hold one out, rank the homes for it, and see
whether the user's actual choice comes back.

The leakage guard is the whole validity of this. If the held-out track is left
in its home's profile, artist overlap and already-present see it and the score
is trivially perfect — a harness that reports 100% and means nothing.
"""

from sortify import evaluate


def track(uri, artist_id="a1", artist_name="A"):
    return {"uri": uri, "name": uri, "type": "track", "is_local": False,
            "id": uri.split(":")[-1],
            "artists": [{"id": artist_id, "name": artist_name}]}


def cache_with(home_tracks):
    return {"playlists": {pid: {"tracks": ts} for pid, ts in home_tracks.items()}}


def test_sampling_is_repeatable_for_a_seed():
    cache = cache_with({"h1": [track(f"u{i}") for i in range(20)]})

    first = evaluate.sample_pairs(cache, ["h1"], n=5, seed=7)
    second = evaluate.sample_pairs(cache, ["h1"], n=5, seed=7)

    assert first == second
    assert len(first) == 5


def test_a_perfect_ranker_scores_one():
    cache = cache_with({"h1": [track(f"u{i}") for i in range(10)]})

    def rank(tr, profiles):
        return [{"playlist_id": "h1"}]

    result = evaluate.evaluate(cache, ["h1"], rank, n=5, seed=1)

    assert result["top1"] == 1.0
    assert result["top3"] == 1.0
    assert result["n"] == 5


def test_top3_counts_a_hit_outside_first_place():
    cache = cache_with({"h1": [track(f"u{i}") for i in range(10)],
                        "h2": [track(f"v{i}") for i in range(10)]})

    def rank(tr, profiles):
        return [{"playlist_id": "wrong"}, {"playlist_id": "also-wrong"},
                {"playlist_id": "h1"}]

    result = evaluate.evaluate(cache, ["h1"], rank, n=4, seed=1)

    assert result["top1"] == 0.0
    assert result["top3"] == 1.0


def test_the_held_out_track_is_absent_from_its_own_profile():
    """The leakage guard. If this regresses, every accuracy number afterwards
    is meaningless, and meaninglessly high."""
    cache = cache_with({"h1": [track("u1"), track("u2"), track("u3")]})
    seen = {}

    def rank(tr, profiles):
        seen[tr["uri"]] = [t["uri"] for t in profiles["h1"]]
        return [{"playlist_id": "h1"}]

    evaluate.evaluate(cache, ["h1"], rank, n=3, seed=1)

    for uri, profile_uris in seen.items():
        assert uri not in profile_uris, f"{uri} leaked into its own profile"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_evaluate.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sortify.evaluate'`

- [ ] **Step 3: Write minimal implementation**

Create `sortify/evaluate.py`:

```python
"""Measure suggestion quality against the user's own filing decisions.

Every track in a home playlist is a label: the user put it there. Holding one
out and asking where it belongs turns "the suggestions feel better" into a
number, which is the only way to tune weights across several signals without
guessing.

Pure local computation — no API calls of any kind.
"""

from __future__ import annotations

import random


def sample_pairs(cache: dict, home_ids: list[str], n: int, seed: int) -> list[tuple[str, dict]]:
    """(home_id, track) pairs drawn repeatably for a given seed."""
    pairs = []
    for pid in home_ids:
        entry = cache["playlists"].get(pid)
        if not entry:
            continue
        for t in entry["tracks"]:
            if t.get("uri"):
                pairs.append((pid, t))
    pairs.sort(key=lambda p: (p[0], p[1]["uri"]))
    rng = random.Random(seed)
    rng.shuffle(pairs)
    return pairs[:n]


def evaluate(cache: dict, home_ids: list[str], rank, n: int = 500, seed: int = 0) -> dict:
    """Top-1 and top-3 accuracy of `rank` over held-out tracks.

    `rank(track, profiles)` returns a ranked list of dicts with "playlist_id".
    Three matters because three is what the card shows.
    """
    pairs = sample_pairs(cache, home_ids, n, seed)
    top1 = top3 = 0
    for true_home, tr in pairs:
        # The held-out track is removed from EVERY home, not just its own: it
        # may sit in several, and leaving it anywhere leaks the answer.
        profiles = {
            pid: [t for t in cache["playlists"][pid]["tracks"] if t["uri"] != tr["uri"]]
            for pid in home_ids
            if pid in cache["playlists"]
        }
        ranked = [r["playlist_id"] for r in rank(tr, profiles)]
        if ranked[:1] == [true_home]:
            top1 += 1
        if true_home in ranked[:3]:
            top3 += 1
    total = len(pairs) or 1
    return {"n": len(pairs), "top1": top1 / total, "top3": top3 / total}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_evaluate.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Write the runner script**

Create `scripts/eval_suggest.py`:

```python
"""Report suggestion accuracy against the user's own library.

Usage: .venv/bin/python scripts/eval_suggest.py [n] [seed]

Reads only cached data. Makes no API calls.
"""

import sys

from sortify import evaluate, suggest
from sortify.store import Store

store = Store()
cache = store.cache()
home_ids = [p for p in store.config().get("home_ids", []) if p in cache["playlists"]]
n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
seed = int(sys.argv[2]) if len(sys.argv) > 2 else 0

artist_info = cache["artists"]


def rank(track, profiles):
    built = {pid: suggest.build_profile(ts, artist_info) for pid, ts in profiles.items()}
    return suggest.suggest(track, built, artist_info)


result = evaluate.evaluate(cache, home_ids, rank, n=n, seed=seed)
print(f"homes: {len(home_ids)}  tracks evaluated: {result['n']}")
print(f"top-1: {result['top1']:.1%}")
print(f"top-3: {result['top3']:.1%}")
```

- [ ] **Step 6: Record the baseline**

Run: `.venv/bin/python scripts/eval_suggest.py 500 0`
Expected: two percentages. **Write them into the commit message below**, replacing the bracketed values. These are the numbers every later task is measured against.

- [ ] **Step 7: Commit**

```bash
git add sortify/evaluate.py scripts/eval_suggest.py tests/test_evaluate.py
git commit -m "Measure suggestion accuracy against the user's own filing

Baseline for the artist-overlap-only scorer over 500 held-out tracks:
top-1 [X]%, top-3 [Y]%. Every later change is judged against this.

The held-out track is removed from every home's profile before ranking;
leaving it in makes artist overlap and already-present see the answer, and
the harness would report near-perfect accuracy while measuring nothing."
```

---

### Task 3: Last.fm client methods for track tags and similar tracks

**Files:**
- Modify: `sortify/tags.py` (add methods to `LastFm`, after `top_tags`)
- Test: `tests/test_lastfm_tracks.py`

**Interfaces:**
- Consumes: `sortify.tags.LastFm`, `sortify.tags.LastFmError`
- Produces: `LastFm.similar_tracks(artist: str, track: str) -> list[dict] | None` returning `[{"artist": str, "track": str, "match": float}]`; `LastFm.track_tags(artist: str, track: str) -> list[dict] | None` returning Last.fm's raw tag dicts. Both return `None` for genuine not-found (error 6) and raise `LastFmError` for every other failure.

- [ ] **Step 1: Write the failing test**

```python
"""Track-level Last.fm lookups.

The error handling is the point. Only code 6 means "no such track"; every
other code is our failure. Recording a failure as a miss is permanent, because
misses are deliberately never re-fetched, so one outage mid-backfill would
poison the library with false negatives and no error anywhere.
"""

import pytest

from sortify.tags import LastFm, LastFmError


class FakeClient:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.calls = payload, status, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        payload, status = self.payload, self.status
        return type("R", (), {
            "status_code": status,
            "json": staticmethod(lambda: payload),
        })()


def fm(payload, status=200):
    client = FakeClient(payload, status)
    return LastFm("key", sleep=lambda _: None, client=client), client


def test_similar_tracks_are_returned_with_match_scores():
    client_payload = {"similartracks": {"track": [
        {"name": "Dream On", "artist": {"name": "Nazareth"}, "match": "0.21"},
    ]}}
    fmc, _ = fm(client_payload)

    got = fmc.similar_tracks("Alice Cooper", "I Never Cry")

    assert got == [{"artist": "Nazareth", "track": "Dream On", "match": 0.21}]


def test_a_track_with_no_similars_returns_an_empty_list_not_none():
    """Empty and not-found are different facts and must stay distinguishable."""
    fmc, _ = fm({"similartracks": {"track": []}})

    assert fmc.similar_tracks("Obscure", "Thing") == []


def test_not_found_returns_none():
    fmc, _ = fm({"error": 6, "message": "Track not found"})

    assert fmc.similar_tracks("Nobody", "Nothing") is None


def test_code_6_with_a_malformed_request_message_raises():
    """Code 6 is documented as "invalid parameters"; Last.fm merely reuses it
    for unknown tracks, and the message text is the only discriminator.

    This is not hypothetical. If the key file is missing, load_key returns
    None, httpx renders the parameter as empty, Last.fm rejects the malformed
    request with code 6 — and treating that as not-found would cache the
    entire library as permanent misses in one run, with no error anywhere.
    """
    fmc, _ = fm({"error": 6, "message": "Invalid parameters - your request is malformed"})

    with pytest.raises(LastFmError):
        fmc.similar_tracks("Alice Cooper", "I Never Cry")


@pytest.mark.parametrize("code", [8, 10, 11, 16, 26, 29])
def test_every_other_error_code_raises(code):
    fmc, _ = fm({"error": code, "message": "nope"})

    with pytest.raises(LastFmError):
        fmc.similar_tracks("Alice Cooper", "I Never Cry")


def test_track_tags_are_returned():
    fmc, _ = fm({"toptags": {"tag": [{"name": "ballad", "count": 100}]}})

    assert fmc.track_tags("Alice Cooper", "I Never Cry") == [{"name": "ballad", "count": 100}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_lastfm_tracks.py -q`
Expected: FAIL with `AttributeError: 'LastFm' object has no attribute 'similar_tracks'`

- [ ] **Step 3: Write minimal implementation**

Add to `LastFm` in `sortify/tags.py`, following the existing `top_tags` pattern for request/error handling:

```python
    def similar_tracks(self, artist: str, track: str) -> list[dict] | None:
        """Tracks Last.fm considers close to this one, by listening data.

        None means Last.fm has no such track (error 6). An empty list means it
        knows the track and has no neighbours for it — a different fact, and
        one worth caching so we stop asking.
        """
        d = self._call("track.getSimilar", {"artist": artist, "track": track})
        if d is None:
            return None
        items = (d.get("similartracks") or {}).get("track") or []
        return [
            {"artist": (i.get("artist") or {}).get("name", ""),
             "track": i.get("name", ""),
             "match": float(i.get("match") or 0.0)}
            for i in items
            if i.get("name")
        ]

    def track_tags(self, artist: str, track: str) -> list[dict] | None:
        d = self._call("track.getTopTags", {"artist": artist, "track": track})
        if d is None:
            return None
        return (d.get("toptags") or {}).get("tag") or []
```

**Reuse `tags.py`'s existing request path — do not write a new one.** As of
`206cf6d` it already gets the subtle part right: it returns `None` only when
the code is `NOT_FOUND_CODE` **and** `_looks_like_not_found(message)` is true,
and raises `LastFmError` otherwise. Extract the shared body of `top_tags` into
a `_call(method, params) -> dict | None` helper that preserves that behaviour
exactly, including the `MIN_INTERVAL` sleep, and have all three methods use it.

Re-implementing the error handling independently is the one thing that must
not happen here — code 6 doubles as "invalid parameters", and getting it wrong
silently poisons the whole cache rather than failing loudly.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_lastfm_tracks.py -q && .venv/bin/pytest -q`
Expected: PASS, and the whole suite still green

- [ ] **Step 5: Commit**

```bash
git add sortify/tags.py tests/test_lastfm_tracks.py
git commit -m "Add track-level Last.fm lookups: similar tracks and track tags

Only error 6 means not-found. Every other code raises, because a failure
cached as a miss is never re-fetched and would silently poison the library."
```

---

### Task 4: Tag resolution

Turns a track into a weighted tag vector from three sources of descending authority.

**Files:**
- Create: `sortify/signals.py`
- Test: `tests/test_signals_tags.py`

**Interfaces:**
- Consumes: nothing from earlier tasks at runtime; reads the dict shapes produced by Task 1
- Produces: `sortify.signals.track_key(artist: str, title: str) -> str`; `sortify.signals.resolve_tags(track: dict, user_tags: dict, lastfm_tracks: dict, artist_tags: dict) -> list[tuple[str, float, str]]` returning `(tag, weight, source)` where source is `"user"`, `"track"`, or `"artist"`; constants `W_USER = 1.0`, `W_TRACK = 1.0`, `W_ARTIST = 0.4`

- [ ] **Step 1: Write the failing test**

```python
"""Resolving a track's tags from three sources.

Authority descends: what the user typed, then what Last.fm says about this
track, then what it says about the artist. Artist tags are diluted because
"Alice Cooper is shock rock" is weak evidence about one ballad — that dilution
is the difference between a useful signal and the complaint that started this.
"""

from sortify import signals


def track(uri="spotify:track:x", artist_id="a1", artist="Alice Cooper", name="I Never Cry"):
    return {"uri": uri, "name": name, "artists": [{"id": artist_id, "name": artist}]}


def test_track_keys_ignore_case_and_spacing():
    assert signals.track_key("Alice Cooper", "I Never Cry") == \
           signals.track_key("alice   cooper", " i never cry ")


def test_user_tags_come_back_at_full_weight():
    got = signals.resolve_tags(
        track(), {"tracks": {"spotify:track:x": ["ballad"]}},
        {"tracks": {}}, {"artists": {}})

    assert ("ballad", signals.W_USER, "user") in got


def test_track_tags_are_used_when_last_fm_has_them():
    key = signals.track_key("Alice Cooper", "I Never Cry")
    got = signals.resolve_tags(
        track(), {"tracks": {}},
        {"tracks": {key: {"tags": ["ballad"]}}}, {"artists": {}})

    assert ("ballad", signals.W_TRACK, "track") in got


def test_artist_tags_are_the_fallback_and_are_diluted():
    got = signals.resolve_tags(
        track(), {"tracks": {}}, {"tracks": {}},
        {"artists": {"a1": {"tags": ["shock rock"]}}})

    assert got == [("shock rock", signals.W_ARTIST, "artist")]
    assert signals.W_ARTIST < signals.W_TRACK


def test_a_user_tag_outranks_the_same_tag_from_the_artist():
    got = signals.resolve_tags(
        track(), {"tracks": {"spotify:track:x": ["ballad"]}}, {"tracks": {}},
        {"artists": {"a1": {"tags": ["ballad"]}}})

    assert [w for t, w, _ in got if t == "ballad"] == [signals.W_USER]


def test_a_track_nobody_knows_resolves_to_nothing():
    assert signals.resolve_tags(track(), {"tracks": {}}, {"tracks": {}}, {"artists": {}}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_signals_tags.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sortify.signals'`

- [ ] **Step 3: Write minimal implementation**

Create `sortify/signals.py`:

```python
"""Turning a track into the evidence available about it.

Spotify supplies no genre or mood data at all since Feb 2026, so everything
here comes from Last.fm or from the user.
"""

from __future__ import annotations

import re

# Descending authority. The user knows their own library; Last.fm knows this
# track; Last.fm knows the artist, which says much less about any one song.
W_USER = 1.0
W_TRACK = 1.0
W_ARTIST = 0.4

_SPACE = re.compile(r"\s+")
SEP = "\x1f"  # unit separator: titles contain dashes and slashes, not this


def track_key(artist: str, title: str) -> str:
    norm = lambda s: _SPACE.sub(" ", (s or "").strip().lower())
    return f"{norm(artist)}{SEP}{norm(title)}"


def resolve_tags(track: dict, user_tags: dict, lastfm_tracks: dict,
                 artist_tags: dict) -> list[tuple[str, float, str]]:
    """(tag, weight, source) for one track, highest authority first.

    A tag seen at higher authority is not repeated lower down — a user tag of
    "ballad" should not also count as an artist tag of "ballad".

    Both caches store Last.fm's raw responses, so tags arrive as dicts with
    counts, not bare strings. Accepting both shapes is deliberate: hygiene
    (stoplists, count floors, keep limits) runs here on read, so it can be
    retuned without refetching several hundred requests' worth of data.
    """
    out: list[tuple[str, float, str]] = []
    seen: set[str] = set()

    def add(tags, weight, source):
        for t in tags or []:
            name = (t if isinstance(t, str) else t.get("name", "")).strip().lower()
            if name and name not in seen:
                seen.add(name)
                out.append((name, weight, source))

    add(user_tags.get("tracks", {}).get(track["uri"]), W_USER, "user")

    artists = track.get("artists") or [{}]
    key = track_key(artists[0].get("name", ""), track.get("name", ""))
    add((lastfm_tracks.get("tracks", {}).get(key) or {}).get("tags"), W_TRACK, "track")

    for a in artists:
        if a.get("id"):
            add((artist_tags.get("artists", {}).get(a["id"]) or {}).get("tags"),
                W_ARTIST, "artist")
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_signals_tags.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add sortify/signals.py tests/test_signals_tags.py
git commit -m "Resolve a track's tags from user, track and artist sources

Artist tags are diluted: 'Alice Cooper is shock rock' is weak evidence about
one ballad, and treating it as strong evidence is the bug this fixes."
```

---

### Task 5: Neighbour lookup, excluding same-artist

**Files:**
- Modify: `sortify/signals.py`
- Test: `tests/test_signals_neighbours.py`

**Interfaces:**
- Consumes: `signals.track_key` from Task 4
- Produces: `sortify.signals.neighbours(track: dict, lastfm_tracks: dict) -> list[tuple[str, float]]` returning `(track_key, match)` for cross-artist neighbours only

- [ ] **Step 1: Write the failing test**

```python
"""Similar-track neighbours.

Last.fm's top matches for "I Never Cry" are two other Alice Cooper songs.
Scoring those would re-derive the artist overlap that is already counted, and
reproduce the exact complaint this work exists to fix: a sad ballad offered
only playlists that contain that artist. The signal lives in the cross-artist
tail — Deep Purple's "When a Blind Man Cries", Nazareth's "Dream On".
"""

from sortify import signals


def track(artist="Alice Cooper", name="I Never Cry"):
    return {"uri": "spotify:track:x", "name": name, "artists": [{"id": "a1", "name": artist}]}


def cache_with(similar):
    return {"tracks": {signals.track_key("Alice Cooper", "I Never Cry"): {"similar": similar}}}


def test_same_artist_neighbours_are_excluded():
    cache = cache_with([
        {"artist": "Alice Cooper", "track": "Wake Me Gently", "match": 0.97},
        {"artist": "Nazareth", "track": "Dream On", "match": 0.21},
    ])

    got = signals.neighbours(track(), cache)

    assert got == [(signals.track_key("Nazareth", "Dream On"), 0.21)]


def test_exclusion_ignores_case_and_spacing():
    cache = cache_with([{"artist": "alice  cooper", "track": "Poison", "match": 0.9}])

    assert signals.neighbours(track(), cache) == []


def test_an_unknown_track_has_no_neighbours():
    assert signals.neighbours(track(), {"tracks": {}}) == []


def test_a_known_track_with_no_neighbours_is_empty_not_an_error():
    assert signals.neighbours(track(), cache_with([])) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_signals_neighbours.py -q`
Expected: FAIL with `AttributeError: module 'sortify.signals' has no attribute 'neighbours'`

- [ ] **Step 3: Write minimal implementation**

Add to `sortify/signals.py`:

```python
def neighbours(track: dict, lastfm_tracks: dict) -> list[tuple[str, float]]:
    """(track_key, match) for tracks Last.fm places near this one.

    Same-artist neighbours are dropped. They dominate Last.fm's results and are
    already counted by artist overlap, so scoring them would just restate that
    signal more loudly — which is precisely the behaviour being fixed.
    """
    artists = track.get("artists") or [{}]
    own = {_SPACE.sub(" ", (a.get("name") or "").strip().lower()) for a in artists}
    key = track_key(artists[0].get("name", ""), track.get("name", ""))
    entry = lastfm_tracks.get("tracks", {}).get(key) or {}
    out = []
    for n in entry.get("similar") or []:
        name = _SPACE.sub(" ", (n.get("artist") or "").strip().lower())
        if name and name not in own:
            out.append((track_key(n["artist"], n["track"]), float(n.get("match") or 0.0)))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_signals_neighbours.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add sortify/signals.py tests/test_signals_neighbours.py
git commit -m "Find cross-artist similar tracks, dropping same-artist matches

Last.fm's top similars for a track are usually by the same artist. Counting
those would restate artist overlap rather than add anything."
```

---

### Task 6: The feature-based scorer

**Files:**
- Modify: `sortify/suggest.py` (replace the genre half; keep `_cosine` and the artist term)
- Test: `tests/test_suggest_features.py`

**Interfaces:**
- Consumes: `signals.resolve_tags`, `signals.neighbours`, `signals.track_key`
- Produces: `suggest.Ctx` dataclass with fields `user_tags: dict`, `lastfm_tracks: dict`, `artist_tags: dict`, `descriptions: dict`; `suggest.build_profile(tracks: list[dict], ctx: Ctx, description: str = "") -> dict` with keys `artist_counts`, `tag_counts`, `uris`, `keys`, `description`; `suggest.suggest(track: dict, profiles: dict, ctx: Ctx) -> list[dict]` unchanged in output shape (`playlist_id`, `score`, `pct`, `already`, `reasons`); weight constants `W_ARTIST_BASE`, `W_ARTIST_PER_TRACK`, `W_TAG`, `W_DESC`, `W_NEIGHBOUR`

- [ ] **Step 1: Write the failing test**

```python
"""The scorer, as named features that each explain themselves.

Every feature returns a score and a reason, because a suggestion the user
cannot check is a suggestion they cannot trust.
"""

from sortify import suggest
from sortify.signals import track_key


def ctx(user=None, lastfm=None, artist=None, desc=None):
    return suggest.Ctx(
        user_tags={"tracks": user or {}},
        lastfm_tracks={"tracks": lastfm or {}},
        artist_tags={"artists": artist or {}},
        descriptions={"playlists": desc or {}},
    )


def track(uri="spotify:track:x", artist_id="a1", artist="Alice Cooper", name="I Never Cry"):
    return {"uri": uri, "name": name, "type": "track", "is_local": False, "id": "x",
            "artists": [{"id": artist_id, "name": artist}]}


def other(uri, artist_id, artist, name):
    return {"uri": uri, "name": name, "type": "track", "is_local": False, "id": uri,
            "artists": [{"id": artist_id, "name": artist}]}


def test_a_home_of_ballads_beats_a_home_that_merely_shares_the_artist():
    """The original complaint, as a test. A sad ballad should prefer the sad
    playlist over the one that happens to contain that artist."""
    c = ctx(
        user={"spotify:track:x": ["ballad"]},
        artist={"a2": {"tags": ["ballad"]}, "a3": {"tags": ["shock rock"]}},
    )
    ballads = suggest.build_profile([other("u1", "a2", "Nazareth", "Dream On")], c)
    cooperish = suggest.build_profile([other("u2", "a1", "Alice Cooper", "Poison")], c)

    ranked = suggest.suggest(track(), {"ballads": ballads, "cooperish": cooperish}, c)

    assert ranked[0]["playlist_id"] == "ballads"


def test_a_description_match_is_scored_and_explained():
    c = ctx(user={"spotify:track:x": ["ballad"]}, desc={"h1": "slow sad ballads, late night"})
    prof = suggest.build_profile([other("u1", "a9", "Someone", "Thing")], c)

    ranked = suggest.suggest(track(), {"h1": prof}, c)

    assert ranked[0]["score"] > 0
    assert any("ballad" in r for r in ranked[0]["reasons"])


def test_a_neighbour_already_in_the_home_is_scored_and_explained():
    key = track_key("Alice Cooper", "I Never Cry")
    c = ctx(lastfm={key: {"similar": [{"artist": "Nazareth", "track": "Dream On", "match": 0.9}]}})
    prof = suggest.build_profile([other("u1", "a2", "Nazareth", "Dream On")], c)

    ranked = suggest.suggest(track(), {"h1": prof}, c)

    assert ranked[0]["score"] > 0
    assert any("similar" in r for r in ranked[0]["reasons"])


def test_same_artist_neighbours_contribute_nothing():
    """Guards the exclusion end to end, not just in signals.neighbours."""
    key = track_key("Alice Cooper", "I Never Cry")
    c = ctx(lastfm={key: {"similar": [
        {"artist": "Alice Cooper", "track": "Poison", "match": 1.0}]}})
    prof = suggest.build_profile([other("u1", "a1", "Alice Cooper", "Poison")], c)
    base = suggest.build_profile([other("u2", "a1", "Alice Cooper", "Elected")], c)

    ranked = {r["playlist_id"]: r["score"] for r in
              suggest.suggest(track(), {"withnbr": prof, "without": base}, c)}

    assert ranked["withnbr"] == ranked["without"]


def test_a_track_already_in_a_home_is_flagged():
    c = ctx()
    prof = suggest.build_profile([track()], c)

    ranked = suggest.suggest(track(), {"h1": prof}, c)

    assert ranked[0]["already"] is True


def test_an_empty_description_contributes_nothing():
    c = ctx(user={"spotify:track:x": ["ballad"]}, desc={"h1": ""})
    prof = suggest.build_profile([], c)

    assert suggest.suggest(track(), {"h1": prof}, c) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_suggest_features.py -q`
Expected: FAIL with `AttributeError: module 'sortify.suggest' has no attribute 'Ctx'`

- [ ] **Step 3: Write the implementation**

Rewrite `sortify/suggest.py`. Keep `_cosine` as it is. Replace the module docstring, the constants, `build_profile`, and `suggest`:

```python
"""Score how well a track fits each home playlist.

Four signals, each of which can explain itself to the user:

  - artist overlap: this home already contains this artist
  - tag match: the track's tags against the tags of what the home contains
  - description match: the track's tags against what the user says the home is for
  - neighbours: tracks Last.fm places near this one that are already here

Spotify supplies none of this. Its audio-features endpoint is deprecated for
new apps and the Feb-2026 dev-mode API dropped artist genres entirely, so tags
and similarity come from Last.fm and descriptions come from the user.

Weights are measured, not guessed: scripts/eval_suggest.py ranks tracks the
user already filed and reports how often their own choice comes back.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from . import signals

W_ARTIST_BASE = 3.0
W_ARTIST_PER_TRACK = 0.4
W_TAG = 4.0
W_DESC = 3.0
W_NEIGHBOUR = 2.0

MIN_SCORE = 0.8
TOP_N = 3

_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "some",
              "stuff", "music", "songs", "playlist", "a", "of", "to", "in"}


@dataclass
class Ctx:
    """Everything the scorer needs that is not the track or the profiles."""
    user_tags: dict = field(default_factory=lambda: {"tracks": {}})
    lastfm_tracks: dict = field(default_factory=lambda: {"tracks": {}})
    artist_tags: dict = field(default_factory=lambda: {"artists": {}})
    descriptions: dict = field(default_factory=lambda: {"playlists": {}})


def _describe_words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOPWORDS}


def build_profile(tracks: list[dict], ctx: Ctx, description: str = "") -> dict:
    """Precompute what the suggester needs to know about one home playlist."""
    artist_counts: Counter = Counter()
    tag_counts: Counter = Counter()
    uris, keys = set(), set()
    for t in tracks:
        uris.add(t["uri"])
        artists = t.get("artists") or [{}]
        keys.add(signals.track_key(artists[0].get("name", ""), t.get("name", "")))
        for a in artists:
            if a.get("id"):
                artist_counts[a["id"]] += 1
        for tag, weight, _src in signals.resolve_tags(
            t, ctx.user_tags, ctx.lastfm_tracks, ctx.artist_tags
        ):
            tag_counts[tag] += weight
    return {"artist_counts": artist_counts, "tag_counts": tag_counts,
            "uris": uris, "keys": keys, "description": description,
            "description_words": _describe_words(description)}


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    if not dot:
        return 0.0
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


def suggest(track: dict, profiles: dict[str, dict], ctx: Ctx) -> list[dict]:
    """Rank home playlists for one track. [{playlist_id, score, pct, already, reasons}]"""
    resolved = signals.resolve_tags(track, ctx.user_tags, ctx.lastfm_tracks, ctx.artist_tags)
    track_tags = Counter()
    for tag, weight, _src in resolved:
        track_tags[tag] += weight
    tag_names = set(track_tags)
    nbrs = signals.neighbours(track, ctx.lastfm_tracks)

    results = []
    for pid, prof in profiles.items():
        reasons, score = [], 0.0

        for a in track.get("artists") or []:
            n = prof["artist_counts"].get(a.get("id"), 0)
            if n:
                score += W_ARTIST_BASE + W_ARTIST_PER_TRACK * min(n, 5)
                reasons.append(f"{n} track{'s' if n > 1 else ''} by {a['name']} here")

        sim = _cosine(track_tags, prof["tag_counts"])
        if sim > 0.05:
            score += W_TAG * sim
            shared = sorted((t for t in tag_names if t in prof["tag_counts"]),
                            key=lambda t: prof["tag_counts"][t], reverse=True)
            if shared:
                reasons.append("tags: " + ", ".join(shared[:3]))

        hits = tag_names & prof["description_words"]
        if hits:
            score += W_DESC * len(hits) / max(len(prof["description_words"]), 1)
            reasons.append(f"matches \"{prof['description']}\": " + ", ".join(sorted(hits)[:3]))

        near = sum(m for key, m in nbrs if key in prof["keys"])
        if near:
            score += W_NEIGHBOUR * near
            count = sum(1 for key, _ in nbrs if key in prof["keys"])
            reasons.append(f"{count} similar track{'s' if count > 1 else ''} already here")

        already = track["uri"] in prof["uris"]
        if already or score >= MIN_SCORE:
            results.append({"playlist_id": pid, "score": round(score, 2),
                            "pct": min(round(score * 10), 100),
                            "already": already, "reasons": reasons})

    results.sort(key=lambda r: (not r["already"], -r["score"]))
    return results[:TOP_N]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_suggest_features.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Update the existing callers and the harness**

`sortify/app.py` builds profiles in `_ensure_profiles_locked` and calls `sugg.suggest` in `triage` and `now_playing`. Replace `artist_info` with a `Ctx` built from the store, and pass each home's description:

```python
    ctx = sugg.Ctx(
        user_tags=store.user_tags(),
        lastfm_tracks=store.lastfm_tracks(),
        artist_tags=store.tags(),
        descriptions=store.descriptions(),
    )
    descriptions = ctx.descriptions.get("playlists", {})
    profiles = {
        h["id"]: sugg.build_profile(home_tracks[h["id"]], ctx, descriptions.get(h["id"], ""))
        for h in homes
    }
```

Store `ctx` in `_profile_state` alongside `profiles`, and change both `sugg.suggest(t, state["profiles"], artist_info)` call sites to `sugg.suggest(t, state["profiles"], state["ctx"])`.

Update `scripts/eval_suggest.py`'s `rank` to match:

```python
ctx = suggest.Ctx(
    user_tags=store.user_tags(), lastfm_tracks=store.lastfm_tracks(),
    artist_tags=store.tags(), descriptions=store.descriptions(),
)
descriptions = ctx.descriptions.get("playlists", {})


def rank(track, profiles):
    built = {pid: suggest.build_profile(ts, ctx, descriptions.get(pid, ""))
             for pid, ts in profiles.items()}
    return suggest.suggest(track, built, ctx)
```

Delete `tests/test_suggest.py`'s genre-based cases and any `_known_artists` usage that no longer applies; keep any case that asserts artist-overlap behaviour, updating it to the `Ctx` signature.

- [ ] **Step 6: Run the whole suite and the harness**

Run: `.venv/bin/pytest -q && .venv/bin/python scripts/eval_suggest.py 500 0`
Expected: suite green. The harness runs; with no Last.fm data fetched yet, accuracy should be close to the Task 2 baseline — tags and neighbours have nothing to work with until Task 8 backfills. **A large drop here means a bug, not a weighting problem.**

- [ ] **Step 7: Commit**

```bash
git add sortify/suggest.py sortify/app.py scripts/eval_suggest.py tests/
git commit -m "Score home playlists on four explained signals, not artist alone

Replaces the dead genre cosine — the field has not existed since Feb 2026 —
with tag, description and similar-track signals. Every feature still produces
a reason string, so a suggestion remains something the user can check."
```

---

### Task 7: Backfill command

Fetches Last.fm data for cached tracks, paced, bounded, and explicitly invoked.

**Files:**
- Modify: `sortify/cli.py`
- Test: `tests/test_backfill.py`

**Interfaces:**
- Consumes: `LastFm.similar_tracks`, `LastFm.track_tags` (Task 3); `Store.lastfm_tracks`, `Store.save_lastfm_tracks` (Task 1); `signals.track_key` (Task 4)
- Produces: `sortify.cli.backfill(store, fm, limit: int) -> dict` returning `{"fetched": int, "missing": int, "failed": int, "skipped": int}`

- [ ] **Step 1: Write the failing test**

```python
"""Backfilling Last.fm data for tracks already in the cache.

Explicitly invoked and bounded — there is no background job here. The previous
background job in this project earned a 23-hour ban, and the discipline holds
even though Last.fm is a different service with a different quota.
"""

import pytest

from sortify import cli
from sortify.store import Store
from sortify.tags import LastFmError


class FakeFm:
    def __init__(self, similar=None, tags=None, raises=None):
        self._similar, self._tags, self._raises = similar, tags, raises
        self.calls = 0

    def similar_tracks(self, artist, track):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._similar

    def track_tags(self, artist, track):
        return self._tags


def store_with_tracks(tmp_path, n):
    s = Store(tmp_path)
    s.save_cache({"playlists": {"p1": {"tracks": [
        {"uri": f"spotify:track:{i}", "name": f"song {i}",
         "artists": [{"id": "a1", "name": "Artist"}]} for i in range(n)
    ]}}, "artists": {}, "me": None, "playlist_list": None})
    return s


def test_backfill_caches_what_it_fetches(tmp_path):
    s = store_with_tracks(tmp_path, 1)
    fm = FakeFm(similar=[{"artist": "Other", "track": "Song", "match": 0.5}], tags=[])

    result = cli.backfill(s, fm, limit=10)

    assert result["fetched"] == 1
    assert list(s.lastfm_tracks()["tracks"].values())[0]["similar"][0]["artist"] == "Other"


def test_backfill_respects_its_limit(tmp_path):
    s = store_with_tracks(tmp_path, 50)
    fm = FakeFm(similar=[], tags=[])

    cli.backfill(s, fm, limit=5)

    assert fm.calls == 5


def test_an_already_cached_track_is_not_refetched(tmp_path):
    s = store_with_tracks(tmp_path, 1)
    cli.backfill(s, FakeFm(similar=[], tags=[]), limit=10)
    fm = FakeFm(similar=[], tags=[])

    result = cli.backfill(s, fm, limit=10)

    assert fm.calls == 0
    assert result["skipped"] == 1


def test_a_genuine_not_found_is_cached_as_a_miss(tmp_path):
    s = store_with_tracks(tmp_path, 1)

    result = cli.backfill(s, FakeFm(similar=None, tags=None), limit=10)

    assert result["missing"] == 1
    assert list(s.lastfm_tracks()["tracks"].values())[0]["miss"] is True


def test_a_service_failure_is_not_cached_at_all(tmp_path):
    """A failure cached as a miss is permanent, because misses are never
    re-fetched. One outage mid-backfill would poison the library silently."""
    s = store_with_tracks(tmp_path, 1)

    result = cli.backfill(s, FakeFm(raises=LastFmError("rate limited")), limit=10)

    assert result["failed"] == 1
    assert s.lastfm_tracks()["tracks"] == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_backfill.py -q`
Expected: FAIL with `AttributeError: module 'sortify.cli' has no attribute 'backfill'`

- [ ] **Step 3: Write minimal implementation**

Add to `sortify/cli.py`:

```python
def backfill(store, fm, limit: int) -> dict:
    """Fetch Last.fm data for cached tracks that have none yet.

    Bounded and explicit: no background job, no unbounded walk. A failure is
    left absent from the cache so the next run retries it — recording it as a
    miss would be permanent, since misses are never re-fetched.
    """
    from . import signals

    cache = store.cache()
    known = store.lastfm_tracks()
    seen, stats = set(), {"fetched": 0, "missing": 0, "failed": 0, "skipped": 0}

    for pl in cache.get("playlists", {}).values():
        for t in pl.get("tracks", []):
            artists = t.get("artists") or [{}]
            artist, title = artists[0].get("name", ""), t.get("name", "")
            if not artist or not title:
                continue
            key = signals.track_key(artist, title)
            if key in seen:
                continue
            seen.add(key)
            if key in known["tracks"]:
                stats["skipped"] += 1
                continue
            if stats["fetched"] + stats["missing"] + stats["failed"] >= limit:
                return _save_backfill(store, known, stats)
            try:
                similar = fm.similar_tracks(artist, title)
                tags = fm.track_tags(artist, title) if similar is not None else None
            except LastFmError:
                stats["failed"] += 1
                continue
            if similar is None:
                known["tracks"][key] = {"miss": True, "fetched_at": time.time()}
                stats["missing"] += 1
            else:
                # Store what Last.fm actually said. Filtering at fetch time
                # would freeze today's stoplist and count floor into a
                # permanent cache, so retuning either would cost a full
                # refetch. Hygiene belongs on read.
                known["tracks"][key] = {
                    "similar": similar,
                    "tags": tags or [],
                    "miss": False,
                    "fetched_at": time.time(),
                }
                stats["fetched"] += 1
    return _save_backfill(store, known, stats)


def _save_backfill(store, known: dict, stats: dict) -> dict:
    store.save_lastfm_tracks(known)
    return stats
```

Add `from .tags import LastFmError` to the imports at the top of `cli.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_backfill.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Wire it to the command line**

In `cli.py`'s `main()`, add a `backfill` subcommand alongside the existing ones:

```python
    if argv[0] == "backfill":
        limit = int(argv[1]) if len(argv) > 1 else 200
        key = load_key()
        if not key:
            print("no Last.fm key at ~/state/sortify/lastfm.json")
            return 1
        stats = backfill(Store(), LastFm(key), limit=limit)
        print(f"fetched {stats['fetched']}, no data {stats['missing']}, "
              f"already had {stats['skipped']}, failed {stats['failed']}")
        if stats["failed"]:
            print("failures were NOT cached — rerun to retry them")
        return 0
```

Add `from .tags import LastFm, load_key` to the imports.

- [ ] **Step 6: Run a real backfill**

Run: `.venv/bin/spx backfill 200`
Expected: roughly `fetched N, no data M, ...` after about a minute at 4 requests/second. Zero Spotify calls — confirm with `.venv/bin/spx budget` before and after; the number must not move.

- [ ] **Step 7: Commit**

```bash
git add sortify/cli.py tests/test_backfill.py
git commit -m "Add a bounded, explicit Last.fm backfill command

No background job: the last one in this project earned a 23-hour ban. A
service failure is left uncached so the next run retries it, since a miss is
never re-fetched and a false one would be permanent."
```

---

### Task 8: Measure and set the weights

**Files:**
- Modify: `sortify/suggest.py` (the five weight constants)
- Create: `scripts/tune_weights.py`

**Interfaces:**
- Consumes: `sortify.evaluate.evaluate` (Task 2), `suggest` weights (Task 6)
- Produces: committed weight constants with the measured accuracy recorded beside them

- [ ] **Step 1: Backfill enough data to measure against**

Run: `.venv/bin/spx backfill 1500`
Expected: this takes roughly 6 minutes at 4 requests/second. Without it, tag and neighbour weights are being tuned against empty inputs and every setting scores the same.

- [ ] **Step 2: Write the tuning script**

Create `scripts/tune_weights.py`:

```python
"""Coordinate search over the scorer's weights.

Deliberately coarse. Four weights on 500 sampled tracks from one library can
overfit if given a fine grid, and a model this small cannot memorise much as
long as we do not invite it to.

Reads only cached data. Makes no API calls.
"""

from sortify import evaluate, suggest
from sortify.store import Store

store = Store()
cache = store.cache()
home_ids = [p for p in store.config().get("home_ids", []) if p in cache["playlists"]]
ctx = suggest.Ctx(
    user_tags=store.user_tags(), lastfm_tracks=store.lastfm_tracks(),
    artist_tags=store.tags(), descriptions=store.descriptions(),
)
descriptions = ctx.descriptions.get("playlists", {})


def rank(track, profiles):
    built = {pid: suggest.build_profile(ts, ctx, descriptions.get(pid, ""))
             for pid, ts in profiles.items()}
    return suggest.suggest(track, built, ctx)


GRID = {"W_TAG": [0, 2, 4, 6], "W_DESC": [0, 1.5, 3, 5],
        "W_NEIGHBOUR": [0, 1, 2, 4], "W_ARTIST_BASE": [1, 2, 3]}

best = evaluate.evaluate(cache, home_ids, rank, n=300, seed=0)
print(f"start: top-1 {best['top1']:.1%}  top-3 {best['top3']:.1%}")

for name, values in GRID.items():
    original = getattr(suggest, name)
    winner, winning = original, best
    for v in values:
        setattr(suggest, name, v)
        got = evaluate.evaluate(cache, home_ids, rank, n=300, seed=0)
        print(f"  {name}={v}: top-3 {got['top3']:.1%}")
        if got["top3"] > winning["top3"]:
            winner, winning = v, got
    setattr(suggest, name, winner)
    best = winning
    print(f"{name} -> {winner}  (top-3 {best['top3']:.1%})")

print("\nfinal weights:")
for name in GRID:
    print(f"  {name} = {getattr(suggest, name)}")
print(f"top-1 {best['top1']:.1%}  top-3 {best['top3']:.1%}")
```

- [ ] **Step 3: Run the search**

Run: `.venv/bin/python scripts/tune_weights.py`
Expected: a final weight set and its accuracy.

- [ ] **Step 4: Verify on a held-back seed**

Run: `.venv/bin/python scripts/eval_suggest.py 500 99`
Expected: accuracy on seed 99 (not the seed tuned on) should be close to the tuned figure. **A large gap means overfitting** — widen the sample to 1000, re-run Step 3, and use those weights instead.

- [ ] **Step 5: Write the weights into the source**

Edit the constants in `sortify/suggest.py` to the tuned values, with the evidence beside them:

```python
# Tuned by scripts/tune_weights.py over 300 held-out tracks, verified on an
# untuned seed. Baseline (artist overlap alone) was top-1 [A]%, top-3 [B]%;
# these give top-1 [C]%, top-3 [D]%. Re-run the script before changing them.
W_ARTIST_BASE = ...
```

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: green. If a feature test now fails because a weight went to zero, that is a real finding — report it rather than adjusting the test.

- [ ] **Step 7: Commit**

```bash
git add sortify/suggest.py scripts/tune_weights.py
git commit -m "Set scorer weights from measurement

Baseline top-3 [B]%, tuned top-3 [D]% over held-out tracks the user filed by
hand, verified on a seed that was not tuned against."
```

---

### Task 9: Endpoints for descriptions, user tags, and tag display

**Files:**
- Modify: `sortify/app.py`
- Test: `tests/test_signal_endpoints.py`

**Interfaces:**
- Consumes: `Store` accessors (Task 1), `signals.resolve_tags` (Task 4)
- Produces: `POST /api/description {playlist_id, text}`; `POST /api/track-tag {uri, tag}`; a `tags` key on the `/api/now` and `/api/triage/{id}` track payloads, shaped `[{"name": str, "source": "user"|"track"|"artist"}]`

- [ ] **Step 1: Write the failing test**

```python
"""Endpoints for the user's own signal data."""

import pytest

from sortify import app as appmod


def test_saving_a_description_persists_it():
    appmod.save_description(appmod.DescriptionIn(playlist_id="p1", text="slow sad stuff"))

    assert appmod.store.descriptions()["playlists"]["p1"] == "slow sad stuff"


def test_a_blank_description_clears_rather_than_stores_empty():
    appmod.save_description(appmod.DescriptionIn(playlist_id="p2", text="x"))
    appmod.save_description(appmod.DescriptionIn(playlist_id="p2", text="  "))

    assert "p2" not in appmod.store.descriptions()["playlists"]


def test_adding_a_tag_persists_it_lowercased():
    appmod.add_track_tag(appmod.TrackTagIn(uri="spotify:track:t1", tag="  Ballad "))

    assert appmod.store.user_tags()["tracks"]["spotify:track:t1"] == ["ballad"]


def test_the_same_tag_twice_is_stored_once():
    appmod.add_track_tag(appmod.TrackTagIn(uri="spotify:track:t2", tag="ballad"))
    appmod.add_track_tag(appmod.TrackTagIn(uri="spotify:track:t2", tag="BALLAD"))

    assert appmod.store.user_tags()["tracks"]["spotify:track:t2"] == ["ballad"]


def test_an_empty_tag_is_rejected():
    with pytest.raises(appmod.HTTPException):
        appmod.add_track_tag(appmod.TrackTagIn(uri="spotify:track:t3", tag="   "))


def test_tags_are_labelled_by_source_for_the_card():
    """The card must be able to show that a tag describes the artist rather
    than this track, so the user knows how much to trust it."""
    appmod.store.save_user_tags({"version": 1, "tracks": {"spotify:track:t4": ["mine"]}})
    track = {"uri": "spotify:track:t4", "name": "Song",
             "artists": [{"id": "a1", "name": "Artist"}]}

    got = appmod._track_tags_payload(track)

    assert {"name": "mine", "source": "user"} in got
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_signal_endpoints.py -q`
Expected: FAIL with `AttributeError: module 'sortify.app' has no attribute 'save_description'`

- [ ] **Step 3: Write minimal implementation**

Add the models beside the existing ones in `sortify/app.py`:

```python
class DescriptionIn(BaseModel):
    playlist_id: str
    text: str


class TrackTagIn(BaseModel):
    uri: str
    tag: str
```

Add the endpoints and the payload helper:

```python
def _track_tags_payload(track: dict) -> list[dict]:
    """Tags for one track, labelled by where they came from.

    The label matters: an artist-level tag is a statement about the artist, not
    about this song, and the card says so rather than presenting both alike.
    """
    resolved = sugg.signals.resolve_tags(
        track, store.user_tags(), store.lastfm_tracks(), store.tags()
    )
    return [{"name": name, "source": source} for name, _w, source in resolved]


@app.post("/api/description")
def save_description(body: DescriptionIn):
    """What a home playlist is for, in the user's words.

    Precious data: this file is written here and nowhere else, so no refresh
    or cache rebuild can ever overwrite it.
    """
    payload = store.descriptions()
    text = body.text.strip()
    if text:
        payload["playlists"][body.playlist_id] = text
    else:
        payload["playlists"].pop(body.playlist_id, None)
    store.save_descriptions(payload)
    _profile_state["built_at"] = 0.0   # descriptions feed profiles; rebuild next request
    return {"ok": True}


@app.post("/api/track-tag")
def add_track_tag(body: TrackTagIn):
    tag = body.tag.strip().lower()
    if not tag:
        raise HTTPException(400, "empty tag")
    payload = store.user_tags()
    existing = payload["tracks"].setdefault(body.uri, [])
    if tag not in existing:
        existing.append(tag)
    store.save_user_tags(payload)
    _profile_state["built_at"] = 0.0
    return {"ok": True, "tags": existing}
```

In `now_playing`, add `"tags": _track_tags_payload(track)` to the returned `track` dict. In `triage`, add `"tags": _track_tags_payload(t)` to each entry in `tracks_out`.

Add `import sortify.signals` access via `sugg.signals` (already imported transitively by `suggest`), or import `signals` directly at the top of `app.py` and use it.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_signal_endpoints.py -q && .venv/bin/pytest -q`
Expected: PASS, whole suite green

- [ ] **Step 5: Commit**

```bash
git add sortify/app.py tests/test_signal_endpoints.py
git commit -m "Add endpoints for descriptions and user tags, and expose tag sources

Tags are labelled by origin so the card can distinguish a statement about
this track from a statement about its artist."
```

---

### Task 10: The interface

**Files:**
- Modify: `sortify/static/index.html`
- Modify: `sortify/static/app.js`
- Modify: `sortify/static/style.css`

**Interfaces:**
- Consumes: `POST /api/description`, `POST /api/track-tag`, and the `tags` key on track payloads (Task 9)
- Produces: no programmatic interface

No `index.html` change is needed: the tag row renders into the existing
`#now-card` and `#card`, and the description field into the playlist rows that
`app.js` builds. All three live in `app.js` and `style.css`.

- [ ] **Step 1: Render tags on the Now and triage cards**

In `app.js`, add near `esc`:

```javascript
// Artist-level tags describe the artist, not this song. Showing them
// identically to track tags would overstate what we know, so they are marked.
function tagRow(tags) {
  if (!tags || !tags.length) return "";
  const chips = tags.slice(0, 8).map((t) =>
    `<span class="tag t-${esc(t.source)}" title="${esc(t.source)} tag">${esc(t.name)}</span>`
  ).join("");
  return `<div class="tags">${chips}
    <button class="tag-add" title="Add a tag to this track">+ tag</button></div>`;
}
```

Insert `${tagRow(tr.tags)}` into both card templates, immediately after the `.t-artist` line in `renderNow` and in `renderCard`.

- [ ] **Step 2: Wire the add-tag button**

In `app.js`, after each card renders, bind the button. Add to the end of `renderNow` and `renderCard`:

```javascript
  const addBtn = $("now-card").querySelector(".tag-add") || $("card").querySelector(".tag-add");
  if (addBtn) addBtn.onclick = async () => {
    const tag = prompt("Tag for this track:");
    if (!tag) return;
    const uri = (triage && !$("view-triage").hidden)
      ? triage.tracks[triage.idx].uri : nowState.track.uri;
    try {
      await api("/api/track-tag", { uri, tag });
      toast(`tagged "${tag}"`);
      if (!$("view-now").hidden) pollNow(true);
    } catch (e) { toast(e.message); }
  };
```

- [ ] **Step 3: Add the description field to the Playlists view**

In `renderLists`, inside the row template, after the `.pl-meta` div, add for homes only:

```javascript
    const desc = roles[p.id] === "home"
      ? `<input class="pl-desc" placeholder="what belongs in here?"
                value="${esc(p.description || "")}">`
      : "";
```

Include `${desc}` in `row.innerHTML`, and bind after the buttons are wired:

```javascript
    const descInput = row.querySelector(".pl-desc");
    if (descInput) descInput.onblur = async () => {
      try {
        await api("/api/description", { playlist_id: p.id, text: descInput.value });
      } catch (e) { toast(e.message); }
    };
```

Add `description` to the `/api/playlists` payload in `app.py`:

```python
    descriptions = store.descriptions().get("playlists", {})
    for p in out:
        p["description"] = descriptions.get(p["id"], "")
```

- [ ] **Step 4: Style the tags**

Append to `style.css`:

```css
/* Tag chips. Artist-level tags are visibly weaker because they say less: they
   describe the artist rather than this particular track. */
.tags { display: flex; flex-wrap: wrap; gap: .3rem; margin: .5rem 0; }
.tag { font-size: .75rem; padding: .1rem .45rem; border-radius: 999px;
       background: var(--chip, #2a2a2a); }
.tag.t-user { outline: 1px solid var(--accent, #1db954); }
.tag.t-artist { opacity: .6; font-style: italic; }
.tag-add { font-size: .75rem; padding: .1rem .45rem; border-radius: 999px; }
.pl-desc { flex: 1 1 100%; margin-top: .3rem; font-size: .8rem; }
```

- [ ] **Step 5: Verify in the running app**

```bash
.venv/bin/pytest -q && systemctl --user restart sortify
```

Then open sortify and confirm: the Now card shows tags with artist-level ones visibly fainter and italic; `+ tag` adds a tag and the card re-renders with it; the Playlists view shows a description box on homes only, and typing in one then clicking away persists it across a reload.

- [ ] **Step 6: Commit**

```bash
git add sortify/static/ sortify/app.py
git commit -m "Show track tags and let the user add their own

Artist-level tags render faint and italic: they describe the artist, not this
song, and presenting them identically would overstate what we know."
```

---

## Done when

- `.venv/bin/pytest -q` is green.
- `.venv/bin/python scripts/eval_suggest.py 500 99` reports a top-3 accuracy meaningfully above the Task 2 baseline, on a seed that was not tuned against.
- `.venv/bin/spx budget` shows the same number before and after a backfill — no Spotify call was made by any of this.
- Playing a sad ballad by an artist you own offers a playlist of sad music, not merely a playlist containing that artist.
