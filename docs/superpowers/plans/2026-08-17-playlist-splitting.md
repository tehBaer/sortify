# Playlist Splitting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split a 1372-track playlist into coherent, listenable piles derived from Last.fm tags, and serve one ~2 h "sitting" at a time as a disposable Spotify playlist.

**Architecture:** A Last.fm tag layer (`tags.py`) that cannot touch the Spotify budget, a pure clustering layer (`community.py` + `split.py`) that costs nothing to re-run, virtual piles persisted in `data/splits.json`, and exactly one sitting materialised as a real Spotify playlist at a time. The source playlist is never modified.

**Tech Stack:** Python 3.11+, FastAPI, httpx, pytest. Vanilla JS frontend (no framework, no build step).

**Spec:** `docs/superpowers/specs/2026-08-17-playlist-splitting-design.md`

## Global Constraints

- **Never call `api.spotify.com` directly.** Manual calls go through `.venv/bin/spx GET <path>`.
- **Check `.venv/bin/spx budget` before and after any Spotify-touching work** and state the numbers.
- Spotify budget layers are unchanged by this work: `DAILY_CAP` 600/day, `WINDOW_CAP` 12/60s, `BACKGROUND_DAILY_CAP` 40/day.
- **No new background or proactive Spotify traffic.** Every Spotify call added here is user-initiated.
- **No new third-party dependencies.** Louvain is hand-rolled; `networkx`/`numpy` are not added. `httpx` is already a dependency and is the only HTTP client used.
- `sortify/tags.py` must not import `sortify.spotify`. This is asserted by a test.
- Last.fm API key lives in `~/state/sortify/lastfm.json`, never in `data/config.json`.
- Tests must make no network calls. Run with `.venv/bin/pytest -q`.
- The client speaks the Feb-2026 dev-mode API (`items`/`item`, `/me/library`, no batch endpoints). Do not "fix" it back to pre-2026 shapes.

---

### Task 1: Delete the dead genre enricher

Spotify returns `genres: []` for every artist (717/717 measured). The enricher has been spending 40 background calls/day writing empty arrays. Removing it frees that allowance and deletes a proactive job of the exact kind that caused the bans.

**Files:**
- Modify: `sortify/app.py` (delete lines 546–619, the enricher block; delete the targeted fetch at lines 405–418)
- Modify: `sortify/spotify.py` (delete `artists_genres`, lines 472–492)
- Delete: `tests/test_enricher.py`
- Test: `tests/test_no_background_jobs.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_no_background_jobs.py
"""No proactive Spotify traffic may exist.

The genre enricher spent 40 background calls a day writing empty arrays,
because dev-mode Spotify stopped returning artist genres. Proactive traffic
is what earned the multi-hour bans, so its absence is worth a test.
"""

import threading

import sortify.app as appmod


def test_no_enricher_thread_class_exists():
    assert not hasattr(appmod, "_genre_enricher")
    assert not hasattr(appmod, "_next_missing_artist")


def test_no_background_threads_running():
    names = {t.name for t in threading.enumerate()}
    assert "genre-enricher" not in names


def test_client_has_no_artist_genre_fetch():
    from sortify import spotify
    assert not hasattr(spotify.Spotify, "artists_genres")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_no_background_jobs.py -v`
Expected: FAIL — `_genre_enricher` still exists.

- [ ] **Step 3: Delete the enricher block**

In `sortify/app.py`, delete everything from the comment line `# ---- background genre enricher ---...` through the end of `_start_enricher` (the `@app.on_event("startup")` function), leaving the `# ---- static ----` section intact.

- [ ] **Step 4: Delete the targeted foreground fetch**

In `sortify/app.py`, inside `now_playing`, delete this block entirely:

```python
    # Targeted fetch for just the playing track's artists (≤ a handful of
    # calls, keeps the current card's genre reasons sharp).
    missing = [a["id"] for a in track["artists"] if a.get("id") and a["id"] not in state["artist_info"]]
    if missing:
        try:
            fetched = sp.artists_genres(missing)
        except SpotifyError:
            fetched = {}
        if fetched:
            state["artist_info"].update(fetched)
            cache = store.cache()
            cache["artists"].update(fetched)
            store.save_cache(cache)
```

- [ ] **Step 5: Delete the client method**

In `sortify/spotify.py`, delete the whole `artists_genres` method.

- [ ] **Step 6: Delete the obsolete test file**

```bash
git rm tests/test_enricher.py
```

- [ ] **Step 7: Remove imports that are now unused**

Run this and remove any name it reports as unused from `sortify/app.py`:

```bash
.venv/bin/python -c "
import ast, sys
src = open('sortify/app.py').read()
tree = ast.parse(src)
imported = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Import):
        imported |= {a.asname or a.name.split('.')[0] for a in n.names}
    elif isinstance(n, ast.ImportFrom):
        imported |= {a.asname or a.name for a in n.names}
used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
for name in sorted(imported):
    if name not in used and src.count(name) <= 1:
        print('UNUSED:', name)
"
```

Likely candidates: `threading`, `BACKGROUND_DAILY_CAP`. Keep `time` and `log` — both are used elsewhere.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS, with `test_enricher.py` gone and `test_no_background_jobs.py` passing.

- [ ] **Step 9: Restart the service and confirm the log goes quiet**

```bash
systemctl --user restart sortify && sleep 5 && journalctl --user -u sortify --since "1 min ago" | grep -c enricher
```

Expected: `0`.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "Delete the genre enricher — Spotify stopped returning genres

All 717 cached artists come back with genres: [], so the job spent its
full 40 background calls a day writing empty arrays. Removing it returns
that allowance and drops the last proactive Spotify job."
```

---

### Task 2: Tag hygiene

A third of Last.fm's returned tags are not genres: geography, listener descriptors, label names, and junk (`All`, `misc`, `x`). Unfiltered they produce cross-genre piles like "Norwegian". This is a pure function, built and tested before any network code exists.

**Files:**
- Create: `sortify/tags.py`
- Test: `tests/test_tag_hygiene.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `clean_tags(raw: list[dict], artist_name: str, floor: int = 10, keep: int = 8) -> list[tuple[str, int]]` where `raw` items are Last.fm's `{"name": str, "count": int|str}`. Returns `[(tag, weight)]` sorted by weight descending.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tag_hygiene.py
from sortify.tags import clean_tags


def raw(*pairs):
    return [{"name": n, "count": c} for n, c in pairs]


def test_drops_tags_below_floor():
    out = clean_tags(raw(("shoegaze", 100), ("obscure", 3)), "Slowdive")
    assert out == [("shoegaze", 100)]


def test_drops_geography_and_nationality():
    out = clean_tags(
        raw(("psychedelic rock", 100), ("turkish", 51), ("netherlands", 27),
            ("Trondheim", 66), ("icelandic", 95), ("Norway", 100)),
        "Altin Gun",
    )
    assert out == [("psychedelic rock", 100)]


def test_drops_junk_and_descriptors():
    out = clean_tags(
        raw(("spiritual", 100), ("All", 100), ("misc", 66), ("x", 66),
            ("seen live", 90), ("female vocalists", 80)),
        "Shimshai",
    )
    assert out == [("spiritual", 100)]


def test_drops_self_tags_and_substring_matches():
    out = clean_tags(
        raw(("trip-hop", 100), ("shimshai", 33), ("Shimshai Live", 40)),
        "Shimshai",
    )
    assert out == [("trip-hop", 100)]


def test_keeps_compound_genres():
    out = clean_tags(
        raw(("anatolian rock", 85), ("desert blues", 100), ("instrumental hip-hop", 71)),
        "Various",
    )
    assert [t for t, _ in out] == ["desert blues", "anatolian rock", "instrumental hip-hop"]


def test_caps_at_keep_limit():
    out = clean_tags(raw(*[(f"genre{i}", 100 - i) for i in range(20)]), "X", keep=8)
    assert len(out) == 8


def test_handles_string_counts_and_missing_counts():
    out = clean_tags([{"name": "techno", "count": "47"}, {"name": "house"}], "X")
    assert out == [("techno", 47)]


def test_empty_input_gives_empty_output():
    assert clean_tags([], "X") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_tag_hygiene.py -v`
Expected: FAIL — `No module named 'sortify.tags'`.

- [ ] **Step 3: Write the implementation**

```python
# sortify/tags.py
"""Last.fm tag layer.

Spotify stopped returning artist genres in development mode — all 717 cached
artists come back with genres: []. Tags therefore come from Last.fm, which
covered 29 of 30 sampled artists from this library.

This module must never import sortify.spotify. It has its own HTTP client and
its own rate limiter, so tag traffic can never be routed through the Spotify
budget (or Spotify traffic through Last.fm's limiter). tests/test_tags.py
asserts the absence of that import.
"""

from __future__ import annotations

# Tags that survive the count floor but say nothing about how music sounds.
# Every entry here was observed in a real probe of this user's library.
_JUNK = {
    "all", "misc", "x", "seen live", "favorites", "favourites", "my music",
    "albums i own", "under 2000 listeners", "spotify", "10s", "00s", "90s",
    "80s", "70s", "60s",
}

_DESCRIPTORS = {
    "female vocalists", "male vocalists", "female vocalist", "male vocalist",
    "female fronted", "singer-songwriter women", "oldies", "beautiful",
    "chill", "cool", "awesome", "love", "sexy", "catchy", "melancholic",
}

# Nationalities, countries and cities. Last.fm tags these heavily, and left in
# they produce piles that cut across every genre ("Norwegian", "dutch").
_PLACES = {
    "african", "american", "argentina", "australia", "australian", "austrian",
    "belgian", "brazil", "brazilian", "british", "canada", "canadian", "chile",
    "china", "chinese", "colombia", "cuba", "cuban", "czech", "danish",
    "denmark", "dutch", "egypt", "england", "english", "estonian", "ethiopia",
    "ethiopian", "finland", "finnish", "france", "french", "german", "germany",
    "greece", "greek", "hungarian", "iceland", "icelandic", "india", "indian",
    "indonesia", "iran", "iranian", "ireland", "irish", "israel", "israeli",
    "italian", "italy", "jamaica", "jamaican", "japan", "japanese", "korea",
    "korean", "latvian", "lebanon", "lithuanian", "mali", "mexican", "mexico",
    "morocco", "netherlands", "new zealand", "niger", "nigeria", "nigerian",
    "norway", "norwegian", "oslo", "peru", "poland", "polish", "portugal",
    "portuguese", "romania", "russia", "russian", "scotland", "scottish",
    "senegal", "serbia", "slovenia", "south africa", "spain", "spanish",
    "sweden", "swedish", "swiss", "switzerland", "taiwan", "thailand",
    "trondheim", "tunisia", "turkey", "turkish", "uk", "ukraine", "usa",
    "venezuela", "vietnam", "wales", "welsh",
}

_STOPLIST = _JUNK | _DESCRIPTORS | _PLACES


def _weight(item: dict) -> int:
    try:
        return int(item.get("count", 0))
    except (TypeError, ValueError):
        return 0


def clean_tags(
    raw: list[dict], artist_name: str, floor: int = 10, keep: int = 8
) -> list[tuple[str, int]]:
    """Filter Last.fm's raw top tags down to usable genre signal.

    Applied in order: drop below `floor`, drop the stoplist, drop tags that
    overlap the artist's own name (catches self-tags and label names), keep
    the top `keep` by weight.
    """
    name_l = (artist_name or "").strip().lower()
    out: list[tuple[str, int]] = []
    for item in raw:
        tag = (item.get("name") or "").strip()
        if not tag:
            continue
        w = _weight(item)
        if w < floor:
            continue
        low = tag.lower()
        if low in _STOPLIST:
            continue
        if name_l and (low in name_l or name_l in low):
            continue
        out.append((tag, w))
    out.sort(key=lambda tw: (-tw[1], tw[0].lower()))
    return out[:keep]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_tag_hygiene.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add sortify/tags.py tests/test_tag_hygiene.py
git commit -m "Add Last.fm tag hygiene

A third of returned tags are geography, listener descriptors, label names
or junk. Unfiltered they cluster into piles like 'Norwegian' that cut
across every genre."
```

---

### Task 3: Last.fm client and tag cache

**Files:**
- Modify: `sortify/tags.py` (append)
- Modify: `sortify/store.py` (add `tags()` / `save_tags()` after the `cache()` accessors)
- Test: `tests/test_tags.py`

**Interfaces:**
- Consumes: `clean_tags` from Task 2.
- Produces:
  - `LastFm(key: str, sleep=time.sleep, client=None)` with `.top_tags(artist_name: str) -> list[dict] | None` (`None` = artist not found).
  - `load_key(path: Path | None = None) -> str | None`
  - `enrich(artist_names: dict[str, str], cached: dict, fm: LastFm, now: str) -> dict` — `artist_names` is `{spotify_artist_id: name}`; returns the updated `artists` mapping for `tags.json`.
  - `store.tags()` returns `{"version": 1, "artists": {}}`; `store.save_tags(payload)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tags.py
import json
from pathlib import Path

import pytest

from sortify.tags import LastFm, enrich, load_key


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.Client. Records every call it is asked to make."""

    def __init__(self, by_artist):
        self.by_artist = by_artist
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params["artist"])
        payload = self.by_artist.get(params["artist"])
        if payload is None:
            return FakeResponse({"error": 6, "message": "The artist you supplied could not be found"})
        return FakeResponse({"toptags": {"tag": payload}})


def tagset(*pairs):
    return [{"name": n, "count": c} for n, c in pairs]


def test_module_never_imports_spotify():
    src = Path("sortify/tags.py").read_text()
    assert "sortify.spotify" not in src
    assert "from .spotify" not in src
    assert "import spotify" not in src


def test_top_tags_returns_raw_unfiltered_tags():
    """top_tags is the transport; clean_tags does the filtering in enrich."""
    fm = LastFm("k", sleep=lambda s: None,
                client=FakeClient({"Slowdive": tagset(("shoegaze", 100), ("british", 40))}))
    assert fm.top_tags("Slowdive") == [{"name": "shoegaze", "count": 100},
                                       {"name": "british", "count": 40}]


def test_top_tags_returns_none_for_unknown_artist():
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({}))
    assert fm.top_tags("Spherelet") is None


def test_rate_limiter_sleeps_between_calls():
    slept = []
    fm = LastFm("k", sleep=slept.append,
                client=FakeClient({"A": tagset(("techno", 50)), "B": tagset(("house", 50))}))
    fm.top_tags("A")
    fm.top_tags("B")
    assert len(slept) == 2
    assert all(s == pytest.approx(0.25) for s in slept)


def test_enrich_stores_cleaned_tags():
    fm = LastFm("k", sleep=lambda s: None,
                client=FakeClient({"Altin Gun": tagset(("psychedelic rock", 100), ("turkish", 51))}))
    out = enrich({"a1": "Altin Gun"}, {}, fm, now="2026-08-17T16:00:00Z")
    assert out["a1"]["tags"] == [["psychedelic rock", 100]]
    assert out["a1"]["miss"] is False
    assert out["a1"]["fetched_at"] == "2026-08-17T16:00:00Z"


def test_enrich_records_misses_explicitly():
    fm = LastFm("k", sleep=lambda s: None, client=FakeClient({}))
    out = enrich({"a1": "Spherelet"}, {}, fm, now="2026-08-17T16:00:00Z")
    assert out["a1"]["miss"] is True
    assert out["a1"]["tags"] == []


def test_enrich_never_refetches_cached_artists():
    client = FakeClient({"A": tagset(("techno", 50))})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    cached = {"a1": {"name": "A", "tags": [["techno", 50]], "miss": False,
                     "fetched_at": "2026-08-01T00:00:00Z"}}
    enrich({"a1": "A"}, cached, fm, now="2026-08-17T16:00:00Z")
    assert client.calls == []


def test_enrich_never_refetches_known_misses():
    client = FakeClient({})
    fm = LastFm("k", sleep=lambda s: None, client=client)
    cached = {"a1": {"name": "Gone", "tags": [], "miss": True,
                     "fetched_at": "2026-08-01T00:00:00Z"}}
    enrich({"a1": "Gone"}, cached, fm, now="2026-08-17T16:00:00Z")
    assert client.calls == []


def test_load_key_reads_state_file(tmp_path):
    p = tmp_path / "lastfm.json"
    p.write_text(json.dumps({"api_key": "abc123"}))
    assert load_key(p) == "abc123"


def test_load_key_returns_none_when_absent(tmp_path):
    assert load_key(tmp_path / "nope.json") is None


def test_store_round_trips_tags():
    from sortify.store import Store
    s = Store()
    assert s.tags() == {"version": 1, "artists": {}}
    s.save_tags({"version": 1, "artists": {"a1": {"name": "A", "tags": [], "miss": True}}})
    assert s.tags()["artists"]["a1"]["name"] == "A"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_tags.py -v`
Expected: FAIL — `cannot import name 'LastFm'`.

- [ ] **Step 3: Add the store accessors**

In `sortify/store.py`, after `save_cache`:

```python
    # tags.json: {artist_id: {name, lastfm_name, tags, fetched_at, miss}}
    # Last.fm data, not Spotify's — kept separate from cache.json on purpose.
    def tags(self) -> dict:
        return self._load("tags.json", {"version": 1, "artists": {}})

    def save_tags(self, payload: dict) -> None:
        self._save("tags.json", payload)
```

- [ ] **Step 4: Write the client**

First move the imports to the top of `sortify/tags.py`, so the existing
`from __future__ import annotations` line is followed by:

```python
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
```

Then append the rest to the bottom of the file:

```python
API = "https://ws.audioscrobbler.com/2.0/"
KEY_PATH = Path.home() / "state" / "sortify" / "lastfm.json"

# Last.fm's stated ceiling is 5 requests/second. Sit below it: this is a
# courtesy limit on someone else's free service, not a budget to spend.
MIN_INTERVAL = 0.25


def load_key(path: Path | None = None) -> str | None:
    """Read the API key from ~/state/sortify/lastfm.json.

    Deliberately outside the repo: data/config.json sits next to
    version-controlled files.
    """
    p = Path(path or KEY_PATH)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("api_key") or None
    except (json.JSONDecodeError, OSError):
        return None


class LastFm:
    """Minimal Last.fm client with its own rate limiter.

    `sleep` and `client` are injectable so tests run without network or delay.
    """

    def __init__(self, key: str, sleep=time.sleep, client=None):
        self.key = key
        self._sleep = sleep
        self._client = client or httpx.Client(
            headers={"User-Agent": "sortify/0.1 (+https://github.com/local/sortify)"}
        )

    def top_tags(self, artist_name: str) -> list[dict] | None:
        """Raw top tags for an artist, or None if Last.fm has no such artist."""
        self._sleep(MIN_INTERVAL)
        resp = self._client.get(
            API,
            params={
                "method": "artist.getTopTags",
                "artist": artist_name,
                "api_key": self.key,
                "format": "json",
                "autocorrect": "1",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return None
        tags = data.get("toptags", {}).get("tag", [])
        # Last.fm collapses a single tag into an object rather than a list.
        if isinstance(tags, dict):
            tags = [tags]
        return tags


def enrich(artist_names: dict[str, str], cached: dict, fm: LastFm, now: str) -> dict:
    """Fetch tags for artists not already in `cached`. Returns the merged map.

    Misses are recorded as `miss: true` so they are never asked about again —
    at ~3% of artists, re-asking every split would be pure waste.
    """
    out = dict(cached)
    for aid, name in artist_names.items():
        if aid in out:
            continue
        raw = fm.top_tags(name)
        if raw is None:
            out[aid] = {"name": name, "lastfm_name": None, "tags": [],
                        "fetched_at": now, "miss": True}
        else:
            out[aid] = {"name": name, "lastfm_name": name,
                        "tags": [[t, w] for t, w in clean_tags(raw, name)],
                        "fetched_at": now, "miss": False}
    return out
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_tags.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sortify/tags.py sortify/store.py tests/test_tags.py
git commit -m "Add Last.fm client and tag cache

Own HTTP client and rate limiter, no import of sortify.spotify — asserted
by test — so tag traffic can never spend Spotify quota. Misses are cached
explicitly so the ~3% Last.fm can't match are asked about once."
```

---

### Task 4: Louvain community detection

**Files:**
- Create: `sortify/community.py`
- Test: `tests/test_community.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `louvain(adj: dict[str, dict[str, float]], resolution: float = 1.0) -> dict[str, int]` mapping node → community label. Deterministic: nodes are visited in sorted order, so the same input always yields the same output.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_community.py
from sortify.community import louvain


def test_empty_graph():
    assert louvain({}) == {}


def test_single_node():
    assert louvain({"a": {}}) == {"a": 0}


def test_two_disconnected_cliques_split():
    adj = {
        "a": {"b": 1.0, "c": 1.0}, "b": {"a": 1.0, "c": 1.0}, "c": {"a": 1.0, "b": 1.0},
        "x": {"y": 1.0, "z": 1.0}, "y": {"x": 1.0, "z": 1.0}, "z": {"x": 1.0, "y": 1.0},
    }
    comm = louvain(adj)
    assert comm["a"] == comm["b"] == comm["c"]
    assert comm["x"] == comm["y"] == comm["z"]
    assert comm["a"] != comm["x"]


def test_weak_bridge_does_not_merge_cliques():
    adj = {
        "a": {"b": 10.0, "c": 10.0}, "b": {"a": 10.0, "c": 10.0},
        "c": {"a": 10.0, "b": 10.0, "x": 0.1},
        "x": {"y": 10.0, "z": 10.0, "c": 0.1},
        "y": {"x": 10.0, "z": 10.0}, "z": {"x": 10.0, "y": 10.0},
    }
    comm = louvain(adj)
    assert comm["a"] == comm["b"] == comm["c"]
    assert comm["x"] == comm["y"] == comm["z"]
    assert comm["a"] != comm["x"]


def test_fully_isolated_nodes_each_get_own_community():
    comm = louvain({"a": {}, "b": {}, "c": {}})
    assert len(set(comm.values())) == 3


def test_deterministic_across_runs():
    adj = {
        "a": {"b": 1.0, "c": 2.0}, "b": {"a": 1.0, "c": 1.0},
        "c": {"a": 2.0, "b": 1.0, "d": 0.5}, "d": {"c": 0.5},
    }
    assert louvain(adj) == louvain(adj)


def test_every_node_gets_a_community():
    adj = {"a": {"b": 1.0}, "b": {"a": 1.0}, "lonely": {}}
    comm = louvain(adj)
    assert set(comm) == {"a", "b", "lonely"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_community.py -v`
Expected: FAIL — `No module named 'sortify.community'`.

- [ ] **Step 3: Write the implementation**

```python
# sortify/community.py
"""Louvain community detection.

Hand-rolled rather than pulling in networkx: this project has no scientific
computing dependencies and one function does not justify the first. The
algorithm is the standard two-phase Louvain — local moving to maximise
modularity, then collapse each community into a single node, repeat.

Node iteration is sorted throughout, so results are reproducible. A split the
user cannot reproduce is a split they cannot trust.
"""

from __future__ import annotations


def _degrees(adj: dict) -> dict:
    # A self-loop contributes twice to a node's degree.
    return {n: sum(nbrs.values()) + nbrs.get(n, 0.0) for n, nbrs in adj.items()}


def _one_level(adj: dict, resolution: float) -> dict:
    """One pass of local moving. Returns {node: community index}."""
    deg = _degrees(adj)
    m2 = sum(deg.values())
    comm = {n: i for i, n in enumerate(sorted(adj))}
    if m2 == 0:
        return comm
    tot = {}
    for n, c in comm.items():
        tot[c] = tot.get(c, 0.0) + deg[n]

    improved = True
    while improved:
        improved = False
        for n in sorted(adj):
            c_old = comm[n]
            tot[c_old] -= deg[n]
            links: dict[int, float] = {}
            for nb, w in adj[n].items():
                if nb != n:
                    links[comm[nb]] = links.get(comm[nb], 0.0) + w
            best_c = c_old
            best_gain = links.get(c_old, 0.0) - resolution * tot.get(c_old, 0.0) * deg[n] / m2
            for c, w_in in sorted(links.items()):
                gain = w_in - resolution * tot.get(c, 0.0) * deg[n] / m2
                if gain > best_gain + 1e-12:
                    best_c, best_gain = c, gain
            tot[best_c] = tot.get(best_c, 0.0) + deg[n]
            comm[n] = best_c
            if best_c != c_old:
                improved = True
    return comm


def louvain(adj: dict[str, dict[str, float]], resolution: float = 1.0) -> dict[str, int]:
    """Partition a weighted undirected graph into communities.

    `adj` must be symmetric: adj[a][b] == adj[b][a].
    """
    if not adj:
        return {}
    mapping = {n: n for n in adj}
    cur = {n: dict(nbrs) for n, nbrs in adj.items()}

    while True:
        comm = _one_level(cur, resolution)
        if len(set(comm.values())) == len(cur):
            break
        mapping = {orig: comm[node] for orig, node in mapping.items()}
        collapsed: dict = {}
        for n, nbrs in cur.items():
            cn = comm[n]
            row = collapsed.setdefault(cn, {})
            for nb, w in nbrs.items():
                cnb = comm[nb]
                row[cnb] = row.get(cnb, 0.0) + w
        cur = collapsed

    # Relabel to a dense 0..k-1 range in a stable order.
    labels = {c: i for i, c in enumerate(sorted(set(mapping.values()), key=str))}
    return {n: labels[c] for n, c in mapping.items()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_community.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add sortify/community.py tests/test_community.py
git commit -m "Add hand-rolled Louvain community detection

No networkx: one function does not justify this project's first
scientific-computing dependency. Sorted iteration throughout keeps
results reproducible."
```

---

### Task 5: The splitter

**Files:**
- Create: `sortify/split.py`
- Test: `tests/test_split.py`

**Interfaces:**
- Consumes: `louvain` from Task 4.
- Produces: `split_tracks(tracks: list[dict], tags: dict, params: dict | None = None) -> list[dict]` returning piles as `[{"id": "p1", "name": str, "tags": [str], "uris": [str]}]`. `tracks` are slim tracks from `cache.json`; `tags` is the `artists` map from `tags.json`.
- Params and defaults: `resolution` 1.0, `min_pile` 15, `top_name_tags` 3.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_split.py
from sortify.split import split_tracks

TAGS = {
    "bh": {"name": "Beach House", "tags": [["dream pop", 100], ["shoegaze", 80]], "miss": False},
    "sd": {"name": "Slowdive", "tags": [["shoegaze", 100], ["dream pop", 90]], "miss": False},
    "kv": {"name": "Kvelertak", "tags": [["black metal", 100], ["hardcore punk", 70]], "miss": False},
    "mayhem": {"name": "Mayhem", "tags": [["black metal", 100], ["norwegian black metal", 90]], "miss": False},
    "gone": {"name": "Gone", "tags": [], "miss": True},
}


def track(uri, artist):
    return {"uri": uri, "duration_ms": 300000,
            "artists": [{"id": artist, "name": TAGS.get(artist, {}).get("name", artist)}]}


def many(artist, n, start=0):
    return [track(f"spotify:track:{artist}{i}", artist) for i in range(start, start + n)]


def test_separates_two_genre_families():
    tracks = many("bh", 10) + many("sd", 10) + many("kv", 10) + many("mayhem", 10)
    piles = split_tracks(tracks, TAGS, {"min_pile": 5})
    assert len(piles) == 2
    by_uri = {u: p["id"] for p in piles for u in p["uris"]}
    assert by_uri["spotify:track:bh0"] == by_uri["spotify:track:sd0"]
    assert by_uri["spotify:track:kv0"] == by_uri["spotify:track:mayhem0"]
    assert by_uri["spotify:track:bh0"] != by_uri["spotify:track:kv0"]


def test_pile_names_use_distinctive_tags():
    tracks = many("bh", 10) + many("sd", 10) + many("kv", 10) + many("mayhem", 10)
    piles = split_tracks(tracks, TAGS, {"min_pile": 5})
    names = " | ".join(p["name"] for p in piles)
    assert "black metal" in names
    assert "dream pop" in names or "shoegaze" in names


def test_untagged_tracks_get_their_own_pile():
    tracks = many("bh", 10) + many("gone", 4)
    piles = split_tracks(tracks, TAGS, {"min_pile": 5})
    untagged = [p for p in piles if p["id"] == "untagged"]
    assert len(untagged) == 1
    assert len(untagged[0]["uris"]) == 4


def test_untagged_pile_survives_below_min_pile():
    """Untagged is exempt from merging — it must never be folded into a genre."""
    tracks = many("bh", 30) + many("gone", 1)
    piles = split_tracks(tracks, TAGS, {"min_pile": 15})
    assert any(p["id"] == "untagged" and len(p["uris"]) == 1 for p in piles)


def test_every_track_lands_in_exactly_one_pile():
    tracks = many("bh", 10) + many("sd", 5) + many("kv", 7) + many("gone", 3)
    piles = split_tracks(tracks, TAGS, {"min_pile": 5})
    placed = [u for p in piles for u in p["uris"]]
    assert sorted(placed) == sorted(t["uri"] for t in tracks)
    assert len(placed) == len(set(placed))


def test_all_untagged_gives_one_pile():
    piles = split_tracks(many("gone", 20), TAGS)
    assert len(piles) == 1
    assert piles[0]["id"] == "untagged"


def test_empty_input_gives_no_piles():
    assert split_tracks([], TAGS) == []


def test_tracks_follow_first_listed_artist():
    """A featured guest must not drag a track out of its pile."""
    feat = {"uri": "spotify:track:feat", "duration_ms": 300000,
            "artists": [{"id": "kv", "name": "Kvelertak"}, {"id": "bh", "name": "Beach House"}]}
    piles = split_tracks(many("bh", 10) + many("kv", 10) + [feat], TAGS, {"min_pile": 5})
    by_uri = {u: p["id"] for p in piles for u in p["uris"]}
    assert by_uri["spotify:track:feat"] == by_uri["spotify:track:kv0"]


def test_small_piles_merge_into_nearest_neighbour():
    tracks = many("bh", 30) + many("sd", 2)
    piles = split_tracks(tracks, TAGS, {"min_pile": 15})
    assert len([p for p in piles if p["id"] != "untagged"]) == 1


def test_preserves_playlist_order_within_a_pile():
    tracks = many("bh", 5)
    piles = split_tracks(tracks, TAGS, {"min_pile": 1})
    assert piles[0]["uris"] == [t["uri"] for t in tracks]


def test_deterministic():
    tracks = many("bh", 10) + many("kv", 10)
    assert split_tracks(tracks, TAGS, {"min_pile": 5}) == split_tracks(tracks, TAGS, {"min_pile": 5})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_split.py -v`
Expected: FAIL — `No module named 'sortify.split'`.

- [ ] **Step 3: Write the implementation**

```python
# sortify/split.py
"""Cluster a playlist's tracks into coherent piles using Last.fm tags.

A pure function of (tracks, tags, params). No network, no file I/O — which
means re-clustering with different parameters costs nothing, and the whole
thing is testable offline. That matters: the first clustering of a 1372-track
playlist is unlikely to be the last.
"""

from __future__ import annotations

import math

from .community import louvain

DEFAULTS = {"resolution": 1.0, "min_pile": 15, "top_name_tags": 3}
UNTAGGED = "untagged"


def _vec(entry: dict) -> dict[str, float]:
    return {t: float(w) for t, w in entry.get("tags", [])}


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


def split_tracks(tracks: list[dict], tags: dict, params: dict | None = None) -> list[dict]:
    p = {**DEFAULTS, **(params or {})}
    if not tracks:
        return []

    # Artists that carry usable tags, and the tracks that follow them.
    vecs: dict[str, dict[str, float]] = {}
    for t in tracks:
        aid = _primary_artist(t)
        if aid and aid not in vecs:
            v = _vec(tags.get(aid, {}))
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

    piles = [
        {"id": f"p{i + 1}", "name": " · ".join(names[i]) or f"pile {i + 1}",
         "tags": names[i], "uris": buckets[i]}
        for i in range(len(groups))
        if buckets[i]
    ]
    if untagged_uris:
        piles.append({"id": UNTAGGED, "name": "untagged", "tags": [], "uris": untagged_uris})
    return piles


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_split.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sortify/split.py tests/test_split.py
git commit -m "Add the tag-based splitter

Pure function of (tracks, tags, params), so re-clustering is free and the
whole thing tests offline. Names piles by TF-IDF against the playlist's
own tag distribution — otherwise every pile in a rock playlist is 'rock'."
```

---

### Task 6: Split persistence and endpoints

**Files:**
- Modify: `sortify/store.py` (add `splits()` / `save_splits()`)
- Modify: `sortify/app.py` (add endpoints after `/api/triage/{playlist_id}`)
- Test: `tests/test_split_api.py`

**Interfaces:**
- Consumes: `split_tracks` (Task 5), `enrich`/`LastFm`/`load_key` (Task 3).
- Produces:
  - `POST /api/split/{playlist_id}` → `{"piles": [...], "tagged": int, "untagged": int}`. Reads tracks (Spotify, ~15 calls for 1372), enriches tags (Last.fm, 0 Spotify calls), clusters, persists.
  - `GET /api/split/{playlist_id}` → the stored split plus per-pile progress, or 404.
  - `POST /api/split/{playlist_id}/recluster` with `{"resolution": float, "min_pile": int}` → re-clusters from cached tracks and tags. **Zero Spotify calls, zero Last.fm calls.**
  - `store.splits()` returns `{"version": 1, "splits": {}}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_split_api.py
"""The split endpoints, with both networks faked out.

The point of these tests is the call budget as much as the payload: a
re-cluster that quietly re-reads 1372 tracks would cost 15 Spotify calls
every time the user nudged a slider.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify.store import Store

TAGS = {
    "bh": {"name": "Beach House", "tags": [["dream pop", 100]], "miss": False},
    "kv": {"name": "Kvelertak", "tags": [["black metal", 100]], "miss": False},
}


def tracks(n_bh=20, n_kv=20):
    out = []
    for i in range(n_bh):
        out.append({"uri": f"spotify:track:bh{i}", "id": f"bh{i}", "name": f"BH {i}",
                    "duration_ms": 300000, "artists": [{"id": "bh", "name": "Beach House"}],
                    "album": "A", "image": None, "added_at": "2023-01-01T00:00:00Z",
                    "type": "track", "is_local": False})
    for i in range(n_kv):
        out.append({"uri": f"spotify:track:kv{i}", "id": f"kv{i}", "name": f"KV {i}",
                    "duration_ms": 300000, "artists": [{"id": "kv", "name": "Kvelertak"}],
                    "album": "B", "image": None, "added_at": "2023-01-01T00:00:00Z",
                    "type": "track", "is_local": False})
    return out


@pytest.fixture
def client(monkeypatch):
    calls = {"spotify": 0, "lastfm": 0}

    def fake_playlist_tracks(pid):
        calls["spotify"] += 1
        return tracks()

    def fake_enrich(artist_names, cached, fm, now):
        calls["lastfm"] += 1
        return {**cached, **{a: TAGS[a] for a in artist_names if a in TAGS}}

    monkeypatch.setattr(appmod.sp, "playlist_tracks", fake_playlist_tracks)
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: object())
    monkeypatch.setattr(appmod, "enrich", fake_enrich)
    c = TestClient(appmod.app)
    c.calls = calls
    return c


def test_split_creates_piles(client):
    r = client.post("/api/split/PL1")
    assert r.status_code == 200
    piles = r.json()["piles"]
    assert len(piles) == 2
    assert sum(len(p["uris"]) for p in piles) == 40


def test_split_persists(client):
    client.post("/api/split/PL1")
    stored = Store().splits()["splits"]["PL1"]
    assert stored["piles"]
    assert stored["params"]["min_pile"] == 15
    assert stored["decided"] == {}


def test_get_split_returns_stored_piles(client):
    client.post("/api/split/PL1")
    r = client.get("/api/split/PL1")
    assert r.status_code == 200
    assert len(r.json()["piles"]) == 2


def test_get_split_404s_when_absent(client):
    assert client.get("/api/split/NOPE").status_code == 404


def test_recluster_spends_no_api_calls(client):
    client.post("/api/split/PL1")
    before = dict(client.calls)
    r = client.post("/api/split/PL1/recluster", json={"min_pile": 5, "resolution": 1.0})
    assert r.status_code == 200
    assert client.calls == before


def test_recluster_preserves_decisions(client):
    client.post("/api/split/PL1")
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["decided"] = {
        "spotify:track:bh0": {"action": "keep", "to_id": "H1", "at": "2026-08-17T10:00:00Z"}
    }
    s.save_splits(payload)
    client.post("/api/split/PL1/recluster", json={"min_pile": 5})
    assert "spotify:track:bh0" in Store().splits()["splits"]["PL1"]["decided"]


def test_split_reports_progress(client):
    client.post("/api/split/PL1")
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["decided"] = {
        "spotify:track:bh0": {"action": "reject", "to_id": None, "at": "2026-08-17T10:00:00Z"}
    }
    s.save_splits(payload)
    body = client.get("/api/split/PL1").json()
    assert sum(p["decided"] for p in body["piles"]) == 1


def test_split_without_lastfm_key_errors_clearly(client, monkeypatch):
    monkeypatch.setattr(appmod, "_lastfm_client", lambda: None)
    r = client.post("/api/split/PL2")
    assert r.status_code == 400
    assert "lastfm" in r.json()["detail"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_split_api.py -v`
Expected: FAIL — no `splits` attribute on `Store`.

- [ ] **Step 3: Add the store accessors**

In `sortify/store.py`, after `save_tags`:

```python
    # splits.json: {playlist_id: {piles, decided, params, ...}}
    # Piles are virtual — materialising them all would cost ~1384 calls.
    def splits(self) -> dict:
        return self._load("splits.json", {"version": 1, "splits": {}})

    def save_splits(self, payload: dict) -> None:
        self._save("splits.json", payload)
```

- [ ] **Step 4: Add the endpoints**

In `sortify/app.py`, add imports at the top:

```python
from datetime import datetime, timezone

from .split import split_tracks
from .tags import LastFm, enrich, load_key
```

Then add after the `/api/triage/{playlist_id}` endpoint:

```python
# ---- splitting --------------------------------------------------------------


class SplitParams(BaseModel):
    resolution: float = 1.0
    min_pile: int = 15


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lastfm_client() -> LastFm | None:
    key = load_key()
    return LastFm(key) if key else None


def _pile_progress(split: dict) -> list[dict]:
    decided = split.get("decided", {})
    out = []
    for p in split["piles"]:
        out.append({**p, "decided": sum(1 for u in p["uris"] if u in decided),
                    "total": len(p["uris"])})
    return out


@app.post("/api/split/{playlist_id}")
def create_split(playlist_id: str, params: SplitParams = SplitParams()):
    """Read a playlist, tag its artists via Last.fm, cluster into piles.

    The only Spotify spend is the track read (~15 calls for 1372 tracks).
    Tagging is Last.fm; clustering is local.
    """
    fm = _lastfm_client()
    if fm is None:
        raise HTTPException(400, "No Last.fm API key — expected ~/state/sortify/lastfm.json")

    tracks = sp.playlist_tracks(playlist_id)
    if not tracks:
        raise HTTPException(400, "playlist has no tracks")

    names = {}
    for t in tracks:
        for a in t.get("artists", []):
            if a.get("id"):
                names.setdefault(a["id"], a.get("name") or "")

    tag_payload = store.tags()
    tag_payload["artists"] = enrich(names, tag_payload.get("artists", {}), fm, _now_iso())
    store.save_tags(tag_payload)

    cache = store.cache()
    cache["playlists"].setdefault(playlist_id, {})["tracks"] = tracks
    store.save_cache(cache)

    piles = split_tracks(tracks, tag_payload["artists"], params.model_dump())
    payload = store.splits()
    prev = payload["splits"].get(playlist_id, {})
    payload["splits"][playlist_id] = {
        "created_at": _now_iso(),
        "snapshot_id": cache["playlists"][playlist_id].get("snapshot_id"),
        "params": params.model_dump(),
        "piles": piles,
        "decided": prev.get("decided", {}),
        "active_sitting": None,
    }
    store.save_splits(payload)
    untagged = sum(len(p["uris"]) for p in piles if p["id"] == "untagged")
    return {"piles": piles, "tagged": len(tracks) - untagged, "untagged": untagged}


@app.get("/api/split/{playlist_id}")
def get_split(playlist_id: str):
    split = store.splits()["splits"].get(playlist_id)
    if not split:
        raise HTTPException(404, "no split for that playlist")
    return {**split, "piles": _pile_progress(split)}


@app.post("/api/split/{playlist_id}/recluster")
def recluster(playlist_id: str, params: SplitParams = SplitParams()):
    """Re-cluster from cached tracks and tags. Costs nothing at all."""
    payload = store.splits()
    split = payload["splits"].get(playlist_id)
    if not split:
        raise HTTPException(404, "no split for that playlist")
    tracks = store.cache()["playlists"].get(playlist_id, {}).get("tracks", [])
    if not tracks:
        raise HTTPException(400, "no cached tracks — run the split again")
    split["piles"] = split_tracks(tracks, store.tags()["artists"], params.model_dump())
    split["params"] = params.model_dump()
    store.save_splits(payload)
    return {"piles": _pile_progress(split)}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_split_api.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sortify/app.py sortify/store.py tests/test_split_api.py
git commit -m "Add split endpoints

Splitting costs ~15 Spotify calls once; re-clustering costs nothing, so
the user can retune resolution and min_pile freely. Decisions survive a
re-cluster."
```

---

### Task 7: Sittings

**Files:**
- Modify: `sortify/spotify.py` (add `create_playlist`, `unfollow_playlist`)
- Modify: `sortify/app.py` (add sitting endpoints)
- Test: `tests/test_sittings.py`

**Interfaces:**
- Consumes: `store.splits()` (Task 6).
- Produces:
  - `Spotify.create_playlist(name: str, description: str = "") -> str` returning the new playlist id.
  - `Spotify.unfollow_playlist(playlist_id: str) -> None`.
  - `POST /api/split/{playlist_id}/sitting` with `{"pile_id": str, "target_minutes": int}` → `{"sitting_id": str, "uris": [...], "minutes": int}`.
  - `POST /api/split/{playlist_id}/sitting/finish` → `{"ok": True}`.
  - `pick_sitting(uris: list[str], durations: dict[str, int], decided: dict, target_ms: int) -> list[str]` in `sortify/split.py`.

- [ ] **Step 1: Probe the two unverified endpoints**

The spec flags this as Risk 1. `POST /me/playlists` and `DELETE /playlists/{id}/followers` are not in the client and are unconfirmed in the Feb-2026 API. **Ask the user before spending these calls**, then:

```bash
.venv/bin/spx budget
```

Create a throwaway playlist, then remove it (2 calls):

```bash
.venv/bin/spx POST /me/playlists --json '{"name":"sortify probe","public":false}'
```

Take the returned `id` and:

```bash
.venv/bin/spx DELETE /playlists/<id>/followers
```

```bash
.venv/bin/spx budget
```

**If either 404s or 405s:** stop and report. The fallback is one long-lived sitting playlist cleared per track (~44 calls per sitting instead of ~24), which changes Steps 3–5 of this task.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_sittings.py
import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify.split import pick_sitting
from sortify.store import Store

FIVE_MIN = 300000


def test_pick_sitting_fills_to_target_without_exceeding():
    uris = [f"u{i}" for i in range(20)]
    durations = {u: FIVE_MIN for u in uris}
    picked = pick_sitting(uris, durations, {}, target_ms=30 * 60 * 1000)
    assert len(picked) == 6


def test_pick_sitting_skips_decided_tracks():
    uris = [f"u{i}" for i in range(10)]
    durations = {u: FIVE_MIN for u in uris}
    picked = pick_sitting(uris, durations, {"u0": {}, "u1": {}}, target_ms=15 * 60 * 1000)
    assert picked == ["u2", "u3", "u4"]


def test_pick_sitting_preserves_order():
    uris = ["b", "a", "c"]
    durations = {u: FIVE_MIN for u in uris}
    assert pick_sitting(uris, durations, {}, target_ms=15 * 60 * 1000) == ["b", "a", "c"]


def test_pick_sitting_returns_at_least_one_track():
    """A single track longer than the target must still be servable."""
    picked = pick_sitting(["long"], {"long": 3 * 60 * 60 * 1000}, {}, target_ms=60 * 1000)
    assert picked == ["long"]


def test_pick_sitting_empty_when_all_decided():
    assert pick_sitting(["a"], {"a": FIVE_MIN}, {"a": {}}, target_ms=FIVE_MIN) == []


@pytest.fixture
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="": calls.append(("create", name)) or "NEW1")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri: calls.append(("add", uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "unfollow_playlist",
                        lambda pid: calls.append(("unfollow", pid)))
    s = Store()
    s.save_splits({"version": 1, "splits": {"PL1": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None,
        "params": {"resolution": 1.0, "min_pile": 15},
        "piles": [{"id": "p1", "name": "dream pop", "tags": ["dream pop"],
                   "uris": [f"spotify:track:x{i}" for i in range(30)]}],
        "decided": {}, "active_sitting": None}}})
    cache = s.cache()
    cache["playlists"]["PL1"] = {"tracks": [
        {"uri": f"spotify:track:x{i}", "duration_ms": FIVE_MIN,
         "artists": [{"id": "a", "name": "A"}]} for i in range(30)]}
    s.save_cache(cache)
    c = TestClient(appmod.app)
    c.calls = calls
    return c


def test_sitting_creates_playlist_and_adds_tracks(client):
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["sitting_id"] == "NEW1"
    assert len(body["uris"]) == 6
    assert client.calls[0][0] == "create"
    assert sum(1 for c in client.calls if c[0] == "add") == 6


def test_sitting_is_recorded_as_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active["playlist_id"] == "NEW1"
    assert active["pile_id"] == "p1"


def test_finish_unfollows_in_one_call(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    client.calls.clear()
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 200
    assert client.calls == [("unfollow", "NEW1")]
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


def test_second_sitting_refused_while_one_is_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 409


def test_sitting_on_exhausted_pile_400s(client):
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["decided"] = {
        f"spotify:track:x{i}": {"action": "reject", "to_id": None, "at": "x"} for i in range(30)}
    s.save_splits(payload)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 400
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sittings.py -v`
Expected: FAIL — `cannot import name 'pick_sitting'`.

- [ ] **Step 4: Add `pick_sitting` to the splitter**

Append to `sortify/split.py`:

```python
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
```

- [ ] **Step 5: Add the Spotify methods**

In `sortify/spotify.py`, in the mutations section:

```python
    def create_playlist(self, name: str, description: str = "") -> str:
        """Create a playlist and return its id. One call."""
        resp = self.request(
            "POST", "/me/playlists",
            json={"name": name, "description": description, "public": False},
        )
        return (resp or {}).get("id")

    def unfollow_playlist(self, playlist_id: str) -> None:
        """Discard a whole playlist in one call.

        This is why a sitting is disposable: clearing a 22-track playlist
        track-by-track would cost 22 calls, since the Feb-2026 API has no
        batch delete.
        """
        self.request("DELETE", f"/playlists/{playlist_id}/followers")
```

- [ ] **Step 6: Add the sitting endpoints**

In `sortify/app.py`, after `recluster`:

```python
class SittingIn(BaseModel):
    pile_id: str
    target_minutes: int = 120


@app.post("/api/split/{playlist_id}/sitting")
def start_sitting(playlist_id: str, body: SittingIn):
    """Materialise one sitting as a disposable playlist. ~24 calls at 2 h."""
    payload = store.splits()
    split = payload["splits"].get(playlist_id)
    if not split:
        raise HTTPException(404, "no split for that playlist")
    if split.get("active_sitting"):
        raise HTTPException(409, "a sitting is already active — finish it first")

    pile = next((p for p in split["piles"] if p["id"] == body.pile_id), None)
    if not pile:
        raise HTTPException(404, "no such pile")

    tracks = store.cache()["playlists"].get(playlist_id, {}).get("tracks", [])
    durations = {t["uri"]: t.get("duration_ms") or 0 for t in tracks}
    uris = pick_sitting(pile["uris"], durations, split.get("decided", {}),
                        body.target_minutes * 60 * 1000)
    if not uris:
        raise HTTPException(400, "that pile is finished")

    new_id = sp.create_playlist(f"▶ {pile['name']}", "sortify sitting — safe to delete")
    for uri in uris:
        sp.add_to_playlist(new_id, uri)

    split["active_sitting"] = {"playlist_id": new_id, "pile_id": pile["id"],
                               "uris": uris, "started_at": _now_iso()}
    store.save_splits(payload)
    minutes = sum(durations.get(u, 0) for u in uris) // 60000
    return {"sitting_id": new_id, "uris": uris, "minutes": minutes}


@app.post("/api/split/{playlist_id}/sitting/finish")
def finish_sitting(playlist_id: str):
    payload = store.splits()
    split = payload["splits"].get(playlist_id)
    if not split or not split.get("active_sitting"):
        raise HTTPException(404, "no active sitting")
    sp.unfollow_playlist(split["active_sitting"]["playlist_id"])
    split["active_sitting"] = None
    store.save_splits(payload)
    return {"ok": True}
```

Add `pick_sitting` to the existing `from .split import ...` line.

- [ ] **Step 7: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sittings.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 8: Run the full suite and commit**

Run: `.venv/bin/pytest -q`

```bash
git add sortify/app.py sortify/spotify.py sortify/split.py tests/test_sittings.py
git commit -m "Add sittings: one disposable playlist at a time

Unfollowing discards a 22-track sitting in one call where clearing it
track-by-track would cost 22 — the Feb-2026 API has no batch delete."
```

---

### Task 8: Decisions

A keep adds to a home playlist and leaves the source untouched (1 call). A reject is recorded locally (0 calls). This is the departure from the input-playlist flow, which drains its source at 2 calls per decision.

**Files:**
- Modify: `sortify/app.py`
- Test: `tests/test_split_decisions.py`

**Interfaces:**
- Consumes: `store.splits()`, the existing `sp.add_to_playlist`.
- Produces: `POST /api/split/{playlist_id}/decide` with `{"uri": str, "action": "keep"|"reject", "to_id": str|None}` → `{"ok": True, "remaining": int}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_split_decisions.py
import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify.store import Store


@pytest.fixture
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri: calls.append(("add", pid, uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist",
                        lambda pid, uri: calls.append(("remove", pid, uri)) or "snap")
    s = Store()
    s.save_splits({"version": 1, "splits": {"PL1": {
        "created_at": "x", "snapshot_id": None, "params": {},
        "piles": [{"id": "p1", "name": "dream pop", "tags": [],
                   "uris": ["spotify:track:a", "spotify:track:b"]}],
        "decided": {}, "active_sitting": None}}})
    c = TestClient(appmod.app)
    c.calls = calls
    return c


def test_reject_spends_no_api_calls(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "reject"})
    assert r.status_code == 200
    assert client.calls == []


def test_keep_adds_once_and_never_removes(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    assert r.status_code == 200
    assert client.calls == [("add", "HOME1", "spotify:track:a")]


def test_decision_is_recorded(client):
    client.post("/api/split/PL1/decide",
                json={"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"})
    d = Store().splits()["splits"]["PL1"]["decided"]["spotify:track:a"]
    assert d["action"] == "keep"
    assert d["to_id"] == "HOME1"


def test_remaining_count_shrinks(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "reject"})
    assert r.json()["remaining"] == 1


def test_keep_requires_destination(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "keep"})
    assert r.status_code == 400


def test_unknown_action_rejected(client):
    r = client.post("/api/split/PL1/decide",
                    json={"uri": "spotify:track:a", "action": "burn"})
    assert r.status_code == 400


def test_deciding_twice_does_not_double_add(client):
    body = {"uri": "spotify:track:a", "action": "keep", "to_id": "HOME1"}
    client.post("/api/split/PL1/decide", json=body)
    client.post("/api/split/PL1/decide", json=body)
    assert len([c for c in client.calls if c[0] == "add"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_split_decisions.py -v`
Expected: FAIL — 404, endpoint does not exist.

- [ ] **Step 3: Write the endpoint**

In `sortify/app.py`, after `finish_sitting`:

```python
class DecideIn(BaseModel):
    uri: str
    action: str  # "keep" | "reject"
    to_id: str | None = None


@app.post("/api/split/{playlist_id}/decide")
def decide(playlist_id: str, body: DecideIn):
    """Record a decision. Keep costs one call; reject costs none.

    The source playlist is never modified — that is what makes a reject free
    and saves ~1372 calls over a full pass.
    """
    if body.action not in ("keep", "reject"):
        raise HTTPException(400, f"unknown action {body.action!r}")
    if body.action == "keep" and not body.to_id:
        raise HTTPException(400, "keep needs to_id")

    payload = store.splits()
    split = payload["splits"].get(playlist_id)
    if not split:
        raise HTTPException(404, "no split for that playlist")

    if body.uri not in split["decided"]:
        if body.action == "keep":
            if body.to_id == LIKED_ID:
                sp.save_to_liked(body.uri)
            else:
                _apply_snapshot(body.to_id, sp.add_to_playlist(body.to_id, body.uri))
        split["decided"][body.uri] = {"action": body.action, "to_id": body.to_id,
                                      "at": _now_iso()}
        store.save_splits(payload)

    total = sum(len(p["uris"]) for p in split["piles"])
    return {"ok": True, "remaining": total - len(split["decided"])}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_split_decisions.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Run the full suite and commit**

Run: `.venv/bin/pytest -q`

```bash
git add sortify/app.py tests/test_split_decisions.py
git commit -m "Add split decisions: keep costs 1 call, reject costs 0

The source playlist is never modified, unlike the input-playlist flow
which drains at 2 calls per decision. Over 1372 tracks that is ~1372
calls saved and the original survives as an archive."
```

---

### Task 9: Frontend

**Files:**
- Modify: `sortify/static/index.html` (add `view-split` section)
- Modify: `sortify/static/app.js` (add `views` entry, split rendering, sitting controls)
- Modify: `sortify/static/style.css` (pile rows)

**Interfaces:**
- Consumes: all endpoints from Tasks 6–8.
- Produces: no exported interface — this is the top of the stack.

- [ ] **Step 1: Add the view markup**

In `sortify/static/index.html`, after the `view-triage` section:

```html
  <section id="view-split" hidden>
    <div class="triage-top">
      <button id="btn-split-back">‹ Back</button>
      <span id="split-title"></span>
    </div>
    <div id="split-loading" hidden><div class="spinner"></div><p id="split-msg"></p></div>
    <div id="piles"></div>
    <div class="actionbar">
      <label>Pile size <input id="split-minpile" type="number" min="2" max="200" value="15"></label>
      <button id="btn-recluster">Re-cluster (free)</button>
    </div>
  </section>
```

- [ ] **Step 2: Add the split button to each playlist row**

In `sortify/static/app.js`, inside `renderLists`, change the `pl-roles` block to add a split button:

```javascript
      <div class="pl-roles">
        <button class="chip r-input">In</button>
        <button class="chip r-home">Home</button>
        <button class="pl-sort" title="Sort this input">▶</button>
        <button class="pl-split" title="Split into piles">⑃</button>
      </div>
```

and update the destructuring and handlers:

```javascript
    const [bIn, bHome, bSort, bSplit] = row.querySelectorAll("button");
    const paint = () => {
      bIn.classList.toggle("on-input", roles[p.id] === "input");
      bHome.classList.toggle("on-home", roles[p.id] === "home");
      bHome.hidden = p.id === "liked" || !p.editable;
      bSort.hidden = roles[p.id] !== "input";
      bSplit.hidden = p.id === "liked" || (p.total ?? 0) < 100;
    };
    bIn.onclick = () => { roles[p.id] = roles[p.id] === "input" ? null : "input"; paint(); };
    bHome.onclick = () => { roles[p.id] = roles[p.id] === "home" ? null : "home"; paint(); };
    bSort.onclick = () => { saveConfig().then(() => startTriage(p.id, p.name)); };
    bSplit.onclick = () => openSplit(p.id, p.name);
```

- [ ] **Step 3: Add the split module**

Append to `sortify/static/app.js`, before the keyboard handlers:

```javascript
// ---- splitting -------------------------------------------------------------

let split = null;   // {id, name, piles}

async function openSplit(id, name) {
  split = { id, name, piles: [] };
  show("split");
  $("split-title").textContent = name;
  $("piles").innerHTML = "";
  try {
    const data = await api(`/api/split/${id}`);
    split.piles = data.piles;
    renderPiles();
  } catch (e) {
    if (e.message === "auth needed") return;
    $("piles").innerHTML =
      `<p class="hint">Not split yet. Reading the tracks costs about
       ${Math.ceil(( playlistData.find((p) => p.id === id)?.total || 0) / 100) + 1}
       Spotify calls; tagging costs none.</p>
       <button id="btn-do-split" class="primary">Split it</button>`;
    $("btn-do-split").onclick = doSplit;
  }
}

async function doSplit() {
  $("split-loading").hidden = false;
  $("split-msg").textContent = "Reading tracks, then tagging artists via Last.fm…";
  try {
    const data = await api(`/api/split/${split.id}`, { min_pile: Number($("split-minpile").value) });
    split.piles = data.piles.map((p) => ({ ...p, decided: 0, total: p.uris.length }));
    toast(`${data.tagged} tagged, ${data.untagged} untagged`);
    renderPiles();
  } catch (e) {
    toast(e.message);
  } finally {
    $("split-loading").hidden = true;
  }
}

function renderPiles() {
  const wrap = $("piles");
  wrap.innerHTML = "";
  for (const p of split.piles) {
    const left = (p.total ?? p.uris.length) - (p.decided ?? 0);
    const row = document.createElement("div");
    row.className = "pile-row";
    row.innerHTML = `
      <div class="pl-meta">
        <div class="name">${esc(p.name)}</div>
        <div class="sub">${left} of ${p.total ?? p.uris.length} left</div>
      </div>
      <button class="primary" ${left === 0 ? "disabled" : ""}>Start 2 h sitting</button>`;
    row.querySelector("button").onclick = () => startSitting(p.id);
    wrap.appendChild(row);
  }
}

async function startSitting(pileId) {
  try {
    const data = await api(`/api/split/${split.id}/sitting`,
                           { pile_id: pileId, target_minutes: 120 });
    toast(`${data.uris.length} tracks (~${data.minutes} min) — open it in Spotify`);
    const done = await api(`/api/split/${split.id}`);
    split.piles = done.piles;
    renderPiles();
  } catch (e) {
    toast(e.message);
  }
}

$("btn-split-back").onclick = loadLists;
$("btn-recluster").onclick = async () => {
  try {
    const data = await api(`/api/split/${split.id}/recluster`,
                           { min_pile: Number($("split-minpile").value) });
    split.piles = data.piles;
    renderPiles();
    toast("re-clustered — no API calls");
  } catch (e) { toast(e.message); }
};
```

- [ ] **Step 4: Register the view**

Change line 4 of `sortify/static/app.js`:

```javascript
const views = ["setup", "lists", "triage", "now", "split"];
```

- [ ] **Step 5: Add the styles**

Append to `sortify/static/style.css`:

```css
.pile-row {
  display: flex;
  align-items: center;
  gap: .75rem;
  padding: .6rem .4rem;
  border-bottom: 1px solid var(--line, #2a2a2a);
}
.pile-row .name { font-weight: 600; }
.pile-row button { margin-left: auto; }
.pl-split { font-size: 1.1em; }
```

- [ ] **Step 6: Verify by hand**

```bash
systemctl --user restart sortify && sleep 3 && journalctl --user -u sortify --since "1 min ago" | tail -20
```

Open `http://<host>:8800`, go to Playlists, confirm the ⑃ button appears only on playlists of 100+ tracks and that clicking it opens the split view with the call estimate. **Do not click "Split it" yet** — that spends the read budget and needs the user's go-ahead.

- [ ] **Step 7: Run the full suite and commit**

Run: `.venv/bin/pytest -q`

```bash
git add sortify/static/
git commit -m "Add the split view and playlist picker action

Splitting is offered on playlists of 100+ tracks, with the Spotify call
cost shown before the user commits to it."
```

---

## Final verification

- [ ] `.venv/bin/pytest -q` — all green
- [ ] `.venv/bin/spx budget` — state the numbers before and after the one probe in Task 7
- [ ] `journalctl --user -u sortify --since "10 min ago" | grep -ci enricher` returns 0
- [ ] Confirm no background thread: `grep -rn "threading.Thread" sortify/` returns nothing
