# Last.fm Tags Into Suggestions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the suggestion engine's dead Spotify-genre input with Last.fm artist tags (hygiene applied on read), add a bounded on-demand fetch for unknown now-playing artists, and commit measured weights via a hold-one-out evaluation harness.

**Architecture:** `suggest.py`'s existing machinery (profiles, cosine, reasons) is kept; only its input changes — `genre_counts` becomes `tag_counts` fed from `Store.tag_artists()` through `tags.clean_tags`. Artist-level tags are diluted by a constant and marked in reason strings. A new `scripts/eval_suggest.py` measures top-1/top-3 accuracy by hold-one-out over the user's own home playlists, first for the artist-only baseline, then for the tag scorer; the searched weights are committed as constants with the measured numbers in a comment.

**Tech Stack:** Python/FastAPI/pytest, existing `tags.py` Last.fm client. No new dependencies.

**Spec:** `~/kode/spotify/sortify-lastfm/docs/superpowers/specs/2026-08-17-suggestion-signals-design.md` — authoritative. THIS PLAN IMPLEMENTS A SCOPED PHASE: the `tag_match` feature (artist-tag path), on-demand fetch on explicit user action, and the harness. Descriptions, user tags, `getSimilar`/neighbour, track-tag fetching, UI tag row, and Last.fm push are explicitly out of scope (later phases).

## Global Constraints

- **ZERO Spotify API calls.** Last.fm calls only in Task 2's fetch path and only under its trigger rules; tests fake all clients. `data/` in this worktree is a SYMLINK to the live tree's data — never write to it; tests self-isolate via `tests/conftest.py`.
- **Do not restart `sortify.service` or touch `~/kode/spotify/sortify` / any other worktree.** A ~1240-call queued run may be live on the server at any time.
- Worktree: `~/kode/spotify/sortify-suggest`, branch `lastfm-suggest`, base master `16f96d3`, own venv. Baseline: **326 tests green**.
- Both orderings after every task: `.venv/bin/pytest -q` AND `.venv/bin/pytest -q -p no:randomly $(ls -r tests/test_*.py)`. Known unrelated ~5% flake: `test_queue_worker.py::test_rate_429_halves_and_keeps_going` — rerun that file once if it fires; never modify it.
- `data/tags.json` is permanent and write-once per artist: hygiene runs on READ (`tags.clean_tags`), never on write; existing artist entries are never overwritten; only Last.fm error code 6 is a miss (`miss: true`) — every other error is left ABSENT so it retries (spec §Fetching).
- `tags.py` must never import `sortify.spotify`. House style throughout; commit per task, `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.

---

### Task 1: The input swap — tag profiles, tag resolution, diluted artist tags

**Files:**
- Modify: `sortify/suggest.py` (whole module — it is 98 lines), `sortify/app.py` call sites (~:298 profiles build, ~:367 and ~:2153 suggest calls; grep `sugg.` for the authoritative set)
- Test: `tests/test_suggest.py` (extend; read it first), possibly `tests/test_split_now_integration.py` if it pins profile shapes

**Interfaces:**
- Produces (consumed by Tasks 2–3): `build_profile(tracks, tag_artists) -> {"artist_counts", "tag_counts", "uris"}`; `_track_tags(track, tag_artists) -> Counter`; `suggest(track, profiles, tag_artists) -> list[dict]` (same result shape as today); module constants `TAG_WEIGHT` (placeholder 4.0 until Task 3 measures), `ARTIST_TAG_DILUTION` (placeholder 0.5 until Task 3 measures), `ARTIST_BASE`/`ARTIST_PER_TRACK`/`MIN_SCORE`/`TOP_N` unchanged.
- Consumes: `Store.tag_artists() -> {artist_id: {name, tags, miss, ...}}` (v2 raw `[[tag, weight], ...]`); `tags.clean_tags` for read-time hygiene (read its real signature in `sortify/tags.py:153` first and use it exactly as `split.py` does — grep `clean_tags` there).

Design points the code must honour (from the spec):
- `artist_info`/genres disappears from this module entirely; the parameter becomes the `tag_artists` map. app.py stops passing `artist_info` to suggest functions (it may still use it elsewhere — do not delete it globally).
- A track's resolved tags = union of its artists' CLEANED tags. Because these are artist-level (weaker evidence about one track than a track tag would be), the whole tag score is multiplied by `ARTIST_TAG_DILUTION`, and the reason string must say so: `artist tags: ballad, classic rock` — never plain `tags:` (that wording is reserved for the future track/user-tag layers).
- Hygiene at read time via ONE shared helper (e.g. `_cleaned_tags(entry) -> list[str]`) so the profile side and the track side cannot drift. Tag weights from Last.fm's raw pairs are ignored for now (counts, like today's genre counts) — the harness, not intuition, would have to justify anything fancier.
- Artist overlap stays primary: with `TAG_WEIGHT * ARTIST_TAG_DILUTION = 2.0` at placeholder values, a perfect tag cosine (2.0) still loses to one artist match (3.4). A test pins this ordering and Task 3's committed weights must preserve it (spec: artist overlap primary; re-verify after measurement).
- `MIN_SCORE` gate and result shape unchanged so the frontend needs no changes.

- [ ] **Step 1: Write failing tests** (extend `tests/test_suggest.py`, matching its existing fixture style). Cover at minimum:

```python
def tag_entry(*tags):
    return {"name": "X", "tags": [[t, 100] for t in tags], "miss": False}


def test_profile_counts_cleaned_artist_tags():
    tracks = [{"uri": "u1", "artists": [{"id": "a1", "name": "A"}]}]
    ta = {"a1": tag_entry("cumbia", "seen live")}   # "seen live" is _JUNK
    prof = build_profile(tracks, ta)
    assert prof["tag_counts"] == Counter({"cumbia": 1})
    assert "genre_counts" not in prof


def test_tag_match_scores_and_says_artist_level():
    # home full of cumbia; playing track's artist tagged cumbia but NOT in the home
    ...
    r = results[0]
    assert any(x.startswith("artist tags:") for x in r["reasons"])
    assert r["score"] > 0


def test_artist_overlap_still_outranks_a_perfect_tag_match():
    # home H1 contains the artist (no tag overlap), H2 has perfect tag cosine only
    assert scores["H1"] > scores["H2"]


def test_missing_or_miss_artists_contribute_nothing():
    # artist absent from tag_artists AND artist with miss:true both yield empty Counter
    ...


def test_dilution_applied():
    # same fixture scored with ARTIST_TAG_DILUTION monkeypatched 1.0 vs 0.5 → halved tag component
    ...
```

- [ ] **Step 2:** run to fail. **Step 3:** implement (`suggest.py` rewrite of the tag path + the two/three `app.py` call sites: build profiles from `store.tag_artists()` — check where app.py already loads it for the splitter and reuse that read). **Step 4:** run to pass; BOTH orderings (existing suggest/now tests will surface any missed call site). **Step 5:** commit.

---

### Task 2: On-demand fetch for unknown now-playing artists (explicit action only)

**Files:**
- Modify: `sortify/app.py` (the `/api/now` handler's `?force=1` path — find `NOW_FORCE_MIN_INTERVAL`), small helper near the suggest wiring
- Test: `tests/test_now_polling.py` (extend) or a new `tests/test_now_tag_fetch.py`

**Interfaces:**
- Produces: `_fetch_missing_now_tags(track) -> int` (artists fetched), called ONLY from the force branch; respects: at most **3** artists per call; skips artists already present in `tags.json` (hit or miss alike — write-once); appends new entries via the same envelope the splitter writes; only Last.fm code 6 becomes `miss: true`, any other failure fetches nothing and leaves the artist ABSENT; no Last.fm key configured → silently does nothing.
- Consumes: `tags.LastFm` + `tags.load_key()` (reuse — no second client), `Store.tag_artists()`/`save_tag_artists()`, Task 1's suggest wiring (profiles must see the new artist on the next request without a restart — verify how app.py caches `tag_artists` and re-read if needed).

Rules (spec §Fetching, binding): fetches happen on explicit user action only — `?force=1` IS one (opening/refocusing the view); the passive poll path must never fetch. No background thread, no loop. The Last.fm client's `MIN_INTERVAL` pacing is inherited by reuse. A fetch failure must never break the now response — wrap and log, return the track without tags.

- [ ] **Step 1: failing tests**: passive poll never calls the client (fake raises if called); force path fetches ≤3 unknown artists and persists them; known artist (including `miss: true`) not re-fetched; non-code-6 error leaves artist absent AND the endpoint still returns 200; no key → no client constructed.
- [ ] **Step 2:** fail. **Step 3:** implement. **Step 4:** both orderings. **Step 5:** commit.

---

### Task 3: Evaluation harness, measured weights, committed numbers

**Files:**
- Create: `scripts/eval_suggest.py`
- Modify: `sortify/suggest.py` (final constants + comment), `tests/test_suggest.py` (validity pin)

**Interfaces:**
- Produces: `python scripts/eval_suggest.py [--n 500] [--seed 7] [--baseline]` printing top-1/top-3 accuracy; `--search` runs the coarse coordinate search over `TAG_WEIGHT ∈ {2,3,4,6}` × `ARTIST_TAG_DILUTION ∈ {0.3,0.5,0.7,1.0}` and prints the grid. Reads `data/cache.json` + `data/tags.json` READ-ONLY (this is the one sanctioned use of the live data symlink — reads only), zero network of any kind.

Harness contract (spec §Evaluation, binding):
- Every (track, home) pair from cached home playlists is a labelled example; sample N seeded pairs.
- **Hold-one-out is the validity of the whole thing**: rebuild the home's profile with the held-out track removed before ranking; `already` must not fire for the held-out pair. A unit test in `tests/test_suggest.py` asserts that removing a track from its profile changes that track's score for that home (the spec's regression pin against a trivially-perfect harness).
- Multi-home tracks count correct if ANY of their homes is in top-k.
- First run and report the **artist-only baseline** (tag weight forced 0) so the improvement is a delta.

- [ ] **Step 1:** failing validity test + a harness smoke test on hand-built fixtures (tiny fake cache/tags dicts injected — the script's core must be importable functions, `main()` only parses args). **Step 2:** fail. **Step 3:** implement. **Step 4:** run the harness FOR REAL against the live cached data (read-only, zero API): record baseline, run `--search`, pick the winner, commit the constants with the measured accuracies in the comment, e.g. `TAG_WEIGHT = 3.0  # top-3 0.61 vs 0.54 artist-only baseline; eval_suggest --n 500 --seed 7, 2026-08-18`. **Re-verify the artist-overlap-primary pin still passes with the committed weights** — if the search prefers weights that break it, cap them at the pin and record both numbers in the comment. **Step 5:** both orderings, commit.

---

### Task 4: Final verification

- [ ] Both orderings green (326 baseline + new), `node tests/ui_harness.mjs` still green (frontend untouched — prove it), harness reproduces its committed numbers on a second seeded run. Commit anything outstanding. The branch is NOT merged — the user decides (a live queued run may be in progress; merging restarts nothing by itself but the service restart to pick it up is the user's call).
