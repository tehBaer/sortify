# Artist-Similar Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rank labeled guesses for unknown-artist tracks using Last.fm `artist.getSimilar` — a guess-tier-only signal fed by a new rebuildable `data/lastfm_artists.json`, with a bounded backfill, a force-path piggyback, and a measured `ARTIST_SIM_WEIGHT`.

**Architecture:** The confident tier's math is untouched — the new signal is computed ONLY for homes that already failed the `MIN_SCORE` gate, inside `suggest()`'s weak-pool branch, so no primacy-pin re-analysis is needed and the containment is structural. Profiles gain a normalized `artist_names` set; the cache is keyed by Spotify artist ID (mirroring `tags.json`) while matching happens by normalized name (Last.fm returns names). Fetching mirrors the tags/track pattern: a bounded backfill script plus a third step in `_fetch_missing_now_tags`.

**Tech Stack:** Python/FastAPI/pytest, existing `tags.py` `LastFm` client. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-20-artist-similar-signal-design.md` — authoritative. Read §Design fully before any task.

## Global Constraints

- **ZERO Spotify API calls anywhere.** Last.fm calls only in Task 4's fetch paths, faked in every test. `data/` in the execution worktree is a SYMLINK to the live tree's data — tests never write it (`tests/conftest.py` isolates); the live backfill runs only with the controller's explicit go (Task 5 stop point).
- Execution worktree: create a SIBLING at `~/kode/spotify/sortify-artistsim` (branch `artist-similar`, base master ≥ c96987a) with its OWN venv (`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`). Worktrees must be siblings of `~/kode/spotify/sortify` or the `sortify/account_ledger.py` relative symlink breaks.
- Do not restart `sortify.service` or touch `~/kode/spotify/sortify` during execution.
- Both orderings after every task: `.venv/bin/pytest -q` AND `.venv/bin/pytest -q -p no:randomly $(ls -r tests/test_*.py)`. KNOWN BASELINE: the reverse ordering fails `tests/test_deezer.py::test_fetch_writes_once_and_respects_the_floor` and `test_a_deezer_failure_records_nothing_and_does_not_raise` on clean master (pre-existing isolation bug, tracked separately) — exactly those two, nothing more. Known ~5% flake: `test_queue_worker.py::test_rate_429_halves_and_keeps_going` — rerun once, never modify. Run `node tests/ui_harness.mjs` when app.py or the frontend is touched (no frontend changes are expected; reasons flow through the existing weak rendering).
- `lastfm_artists.json` is REBUILDABLE and follows the house miss discipline: only Last.fm error code 6 (via `artist_similar` returning None) → `miss: true`; every other error leaves the key ABSENT so it retries; merge-save with the shrink guard; `--refetch-misses` allowed.
- `tags.py` must never import `sortify.spotify`. Artist-overlap primacy constants (`ARTIST_BASE`, `ARTIST_PER_TRACK`, `TAG_WEIGHT`, `NEIGHBOUR_WEIGHT`, `MIN_SCORE`) are NOT touched by any task.
- House style; commit per task with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Storage — `lastfm_artists.json`

**Files:**
- Modify: `sortify/store.py` (append after the `deezer` block, ~line 210, following the `lastfm_tracks` block at 164–189 as the template)
- Test: `tests/test_store_and_auth.py` (append)

**Interfaces:**
- Produces: `Store.LASTFM_ARTISTS_DEFAULT = {"version": 1, "artists": {}}`; `Store.lastfm_artists() -> dict` (whole envelope via `_versioned("lastfm_artists.json", ...)`); `Store.save_lastfm_artists(payload: dict) -> None`; `Store.lastfm_artist_map() -> dict` returning the inner `artists` dict, `{}` on anything malformed — the exact analog of `lastfm_track_map()` at store.py:179.
- Record shape (written by Task 4, read by Task 3): `{<spotify_artist_id>: {"name": str, "similar": [{"artist": str, "match": float}], "fetched_at": float, "miss": bool}}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_store_and_auth.py`, matching its existing `tmp_path` style):

```python
def test_lastfm_artists_default_to_empty(tmp_path):
    assert Store(tmp_path).lastfm_artists() == {"version": 1, "artists": {}}


def test_lastfm_artists_round_trip_and_map(tmp_path):
    s = Store(tmp_path)
    s.save_lastfm_artists({"version": 1, "artists": {
        "id1": {"name": "Slowdive", "similar": [{"artist": "Ride", "match": 0.9}],
                "fetched_at": 1.0, "miss": False},
    }})
    assert s.lastfm_artist_map()["id1"]["similar"][0]["artist"] == "Ride"


def test_lastfm_artist_map_guards_malformed_payloads(tmp_path):
    s = Store(tmp_path)
    (tmp_path / "lastfm_artists.json").write_text('{"version": 99, "artists": []}')
    assert s.lastfm_artist_map() == {}
```

- [ ] **Step 2:** `.venv/bin/pytest -q tests/test_store_and_auth.py -k lastfm_artists` — expect FAIL (`AttributeError: lastfm_artists`).
- [ ] **Step 3:** Implement the four members by copying the `lastfm_tracks` block's structure verbatim, renaming file/keys (`tracks` → `artists`). `lastfm_artist_map` must return `{}` unless the envelope's `artists` is a dict (mirror `lastfm_track_map`'s guard exactly).
- [ ] **Step 4:** Re-run to PASS, then both full orderings.
- [ ] **Step 5:** Commit: `feat: lastfm_artists.json storage — rebuildable artist-similar cache`.

---

### Task 2: Client — `LastFm.artist_similar`

**Files:**
- Modify: `sortify/tags.py` (append after `track_similar`, which ends at ~line 449 — it is the template)
- Test: `tests/test_tags.py` (append; reuse its existing fake-transport fixtures)

**Interfaces:**
- Produces: `LastFm.artist_similar(artist_name: str) -> list[dict] | None` — None means Last.fm code 6 "artist not found"; any other error raises `LastFmError`; success returns `[{"artist": str, "match": float}, ...]`.
- Request: `method=artist.getSimilar`, params `artist`, `api_key`, `format=json`, `limit=20`, `timeout=self._timeout`, preceded by `self._sleep(MIN_INTERVAL)`. Response envelope: `{"similarartists": {"artist": [{"name": ..., "match": "0.87", ...}]}}` — note `match` arrives as a STRING and a single result may arrive as a bare dict, both exactly like the track path.

- [ ] **Step 1: Write the failing tests:**

```python
def test_artist_similar_slims_entries_and_floats_match():
    fm = fm_with_response({"similarartists": {"artist": [
        {"name": "Ride", "match": "0.87", "url": "ignored"},
        {"name": "Lush", "match": 0.5},
    ]}})
    assert fm.artist_similar("Slowdive") == [
        {"artist": "Ride", "match": 0.87}, {"artist": "Lush", "match": 0.5},
    ]


def test_artist_similar_wraps_single_dict_result():
    fm = fm_with_response({"similarartists": {"artist": {"name": "Ride", "match": "1"}}})
    assert fm.artist_similar("Slowdive") == [{"artist": "Ride", "match": 1.0}]


def test_artist_similar_code_6_is_none_other_errors_raise():
    assert fm_with_response({"error": 6, "message": "Artist not found"}).artist_similar("X") is None
    with pytest.raises(LastFmError):
        fm_with_response({"error": 29, "message": "Rate limit exceeded"}).artist_similar("X")


def test_artist_similar_rejects_blank_name():
    with pytest.raises(LastFmError):
        fm_with_response({}).artist_similar("   ")
```

(`fm_with_response` stands for `tests/test_tags.py`'s existing fake-transport helper — read the file first and use its actual fixture name and construction; do not invent a second fake.)

- [ ] **Step 2:** Run `-k artist_similar` — expect FAIL (`AttributeError`).
- [ ] **Step 3:** Implement by copying `track_similar`'s body, dropping the track param and `_validate_track_args` (validate the artist name with the same non-empty check inline), reading `similarartists`/`artist` instead of `similartracks`/`track`, slimming to `{"artist": name, "match": float}` with the same try/except float coercion and dict-wrap.
- [ ] **Step 4:** PASS, both orderings.
- [ ] **Step 5:** Commit: `feat: LastFm.artist_similar — artist.getSimilar, slimmed, code-6-aware`.

---

### Task 3: Scoring — guess-tier-only artist-sim

**Files:**
- Modify: `sortify/suggest.py` (constants block ~line 86; `build_profile`; new `_artist_sim_score` after `_neighbour_score`; `suggest()` weak-pool branch), `sortify/app.py` (both `sugg.suggest` call sites — grep `sugg.suggest` for the authoritative set, currently :496 and :2936 — plus the fresh `store.lastfm_track_map()` read at :487 gains a sibling `artist_map = store.lastfm_artist_map()` in each request path that feeds them)
- Test: `tests/test_suggest.py` (append)

**Interfaces:**
- Consumes: `Store.lastfm_artist_map()` (Task 1), `_norm_name` (already imported in suggest.py).
- Produces: `build_profile(...)` additionally returns `"artist_names": set[str]` (normalized credited-artist names); `_artist_sim_score(track, prof, artist_map) -> tuple[float, int, list[str]]` (capped-nowhere raw sum, count of distinct home-present similar artists, their display names best-match-first); `suggest(track, profiles, tag_artists, track_map=None, artist_map=None)`; constants `ARTIST_SIM_WEIGHT = 1.0` (placeholder until Task 5 measures), `ARTIST_SIM_CAP = 1.0`.

- [ ] **Step 1: Write the failing tests** (append; reuse the file's `track`/`tag_entry`/`profiles` helpers):

```python
def artist_record(*similar, miss=False):
    return {"name": "X", "similar": [{"artist": a, "match": m} for a, m in similar],
            "fetched_at": 1.0, "miss": miss}


def test_build_profile_collects_normalized_artist_names():
    prof = build_profile([track("d1", ["beach-house"])], ARTISTS)
    assert prof["artist_names"] == {"beach house"}


def test_artist_sim_scores_home_present_similar_artists_only():
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    amap = {"seedless": artist_record(("Slowdive", 0.8), ("Nowhere Band", 0.9))}
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    total, count, names = suggest_mod._artist_sim_score(seed, prof, amap)
    assert (total, count, names) == (0.8, 1, ["Slowdive"])


def test_artist_sim_excludes_seed_artists_and_counts_collab_neighbour_once():
    # Binding, same pin as _neighbour_score: a similar artist matching ANY
    # seed artist scores nothing; a neighbour listed by BOTH credited
    # artists is counted once at its best match.
    prof = build_profile([track("h1", ["slowdive"]), track("h2", ["beach-house"])], ARTISTS)
    amap = {
        "a1": artist_record(("Beach  House", 0.9), ("Slowdive", 0.4)),  # internal double space
        "a2": artist_record(("Slowdive", 0.7)),
    }
    seed = {"uri": "n", "name": "S", "artists": [
        {"id": "a1", "name": "Beach House"}, {"id": "a2", "name": "Other"},
    ]}
    total, count, names = suggest_mod._artist_sim_score(seed, prof, amap)
    assert (total, count, names) == (0.7, 1, ["Slowdive"])


def test_artist_sim_miss_or_absent_records_contribute_nothing():
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    assert suggest_mod._artist_sim_score(seed, prof, {}) == (0.0, 0, [])
    amap = {"seedless": artist_record(("Slowdive", 0.8), miss=True)}
    assert suggest_mod._artist_sim_score(seed, prof, amap) == (0.0, 0, [])


def test_artist_sim_creates_a_guess_from_a_zero_score_home():
    # The coverage win: no tags, no track record, artist unknown to every
    # home — but Last.fm knows the artist's neighbours live in H.
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    amap = {"seedless": artist_record(("Slowdive", 0.8))}
    res = suggest(seed, {"H": prof}, ARTISTS, {}, amap)
    assert len(res) == 1 and res[0]["weak"] is True
    assert res[0]["reasons"] == ["similar artists: Slowdive"]
    assert res[0]["score"] == round(suggest_mod.ARTIST_SIM_WEIGHT * 0.8, 2)


def test_artist_sim_never_reaches_the_confident_tier():
    # Containment pin (spec §Scoring): even a maxed signal must neither
    # produce an unflagged entry nor appear beside a confident one.
    ten = [(f"N{i}", 1.0) for i in range(10)]
    sim_home = build_profile(
        [track(f"h{i}", ["unknown"], title=f"T{i}") for i in range(10)], ARTISTS)
    sim_home["artist_names"] = {f"n{i}" for i in range(10)}  # force 10 hits
    owns = build_profile([track("d1", ["beach-house"])], ARTISTS)
    amap = {"seedless": artist_record(*ten)}
    lone = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    res = suggest(lone, {"SIM": sim_home}, ARTISTS, {}, amap)
    assert res and res[0]["weak"] is True  # capped, flagged, never confident
    both = {"uri": "n", "name": "S", "artists": [
        {"id": "beach-house", "name": "Beach House"}, {"id": "seedless", "name": "Seedless"}]}
    res2 = suggest(both, {"SIM": sim_home, "owns": owns}, ARTISTS, {}, amap)
    assert [r["playlist_id"] for r in res2] == ["owns"]
    assert all("weak" not in r for r in res2)


def test_artist_sim_weight_read_at_call_time(monkeypatch):
    # Same contract TAG_WEIGHT pins — the eval harness varies it per run.
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    amap = {"seedless": artist_record(("Slowdive", 0.5))}
    monkeypatch.setattr(suggest_mod, "ARTIST_SIM_WEIGHT", 1.0)
    single = suggest(seed, {"H": prof}, ARTISTS, {}, amap)[0]["score"]
    monkeypatch.setattr(suggest_mod, "ARTIST_SIM_WEIGHT", 2.0)
    assert suggest(seed, {"H": prof}, ARTISTS, {}, amap)[0]["score"] == round(single * 2, 2)
```

- [ ] **Step 2:** Run `-k artist_sim or artist_names` — expect FAIL.
- [ ] **Step 3:** Implement:

```python
# constants, under the neighbour block — placeholders until the Task 5 sweep:
ARTIST_SIM_WEIGHT = 1.0
ARTIST_SIM_CAP = 1.0
```

`build_profile`: inside the existing `for a in t["artists"]` loop add `if a.get("name"): artist_names.add(_norm_name(a["name"]))` (initialize `artist_names: set[str] = set()` beside the other accumulators; add `"artist_names": artist_names` to the returned dict).

```python
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
```

`suggest()`: add `artist_map: dict[str, dict] | None = None` to the signature (`artist_map = artist_map or {}` beside the track_map line). Restructure the tail of the per-home loop — the confident branch is UNTOUCHED; the signal enters only after the gate has failed:

```python
        already = track["uri"] in prof["uris"]
        if already or score >= MIN_SCORE:
            results.append({...unchanged...})
        else:
            sim_sum, sim_count, sim_names = _artist_sim_score(track, prof, artist_map)
            if sim_count:
                score += ARTIST_SIM_WEIGHT * min(sim_sum, ARTIST_SIM_CAP)
                reasons.append("similar artists: " + ", ".join(sim_names[:2]))
            if score > 0:
                weak_pool.append({...unchanged weak entry...})
```

`app.py`: at each of the two `sugg.suggest(...)` call sites, pass `store.lastfm_artist_map()` as the new argument, read fresh in the same place the site reads `store.lastfm_track_map()` (grep `lastfm_track_map` — every consumer gains a sibling line; same freshness trade, same comment style).

- [ ] **Step 4:** PASS; both orderings; `node tests/ui_harness.mjs` (app.py touched) — expect the established 88/88 and the known reverse-ordering deezer baseline only.
- [ ] **Step 5:** Commit: `feat: artist-similar signal ranks the guess tier (guess-tier-only by construction)`.

---

### Task 4: Fetching — backfill script + force-path piggyback

**Files:**
- Create: `scripts/backfill_artist_similar.py`
- Modify: `sortify/app.py` (a `_merge_save_lastfm_artists` helper beside `_merge_save_lastfm_tracks` at ~line 639, and a third step inside `_fetch_missing_now_tags`, ~line 2628)
- Test: `tests/test_backfill_artist_similar.py` (create), `tests/test_now_tag_fetch.py` (append)

**Interfaces:**
- Consumes: `LastFm.artist_similar` (Task 2), `Store.lastfm_artist_map`/`save_lastfm_artists` (Task 1).
- Produces: `scripts/backfill_artist_similar.py` CLI (`--limit N`, `--all-cached`, `--refetch-misses`); `app._merge_save_lastfm_artists(new_entries: dict) -> None`; the piggyback bound constant `NOW_FETCH_MAX_SIMILAR_ARTISTS = 3`.

- [ ] **Step 1:** Read `scripts/backfill_tags.py` end to end. Copy its skeleton (docstring, target selection from cached HOME playlists with `--all-cached` widening, `--limit`, progress every 25, incremental merge-save every 50 WITH the shrink guard, JSONDecodeError handling, summary line) into `scripts/backfill_artist_similar.py`, with these substitutions: targets are `(artist_id, artist_name)` pairs deduped by id; skip-known checks `artist_id in store.lastfm_artist_map()` (a `miss: true` entry counts as known unless `--refetch-misses`); the per-item fetch is:

```python
similar = fm.artist_similar(name)          # LastFmError propagates -> key stays absent
record = {
    "name": name,
    "similar": similar or [],
    "fetched_at": now,
    "miss": similar is None,               # code 6 only — the ONLY path to a miss
}
```

Exactly one Last.fm request per artist.

- [ ] **Step 2: Write the failing tests** — `tests/test_backfill_artist_similar.py` mirrors `tests/test_backfill_tags.py`'s coverage list (read it first; reuse its fake-`LastFm`/tmp-store fixtures): targets deduped and home-scoped by default; `--all-cached` widens; known ids (hits AND misses) skipped; `--refetch-misses` re-attempts misses only; code 6 records `miss: true`; a raised `LastFmError` leaves the key absent and the run alive; incremental save preserves earlier entries (shrink-guard pin); `--limit` bounds attempts; summary counts.
- [ ] **Step 3:** Run new test file — FAIL; implement until PASS.
- [ ] **Step 4: Piggyback tests** (append to `tests/test_now_tag_fetch.py`, mirroring its existing track-record cases): on a `?force=1` fetch, credited artists (up to `NOW_FETCH_MAX_SIMILAR_ARTISTS`, id present) missing from `lastfm_artist_map` get fetched with the SAME bounded client and recorded via `_merge_save_lastfm_artists`; present and `miss: true` entries are skipped (write-once); an `artist_similar` failure leaves the key absent and the response 200; the shared `NOW_FETCH_MIN_INTERVAL` floor covers this step too (no third clock); passive polls never fetch.
- [ ] **Step 5:** Run — FAIL; implement: `_merge_save_lastfm_artists` is a rename-copy of `_merge_save_lastfm_tracks` (own lock `_lastfm_artists_save_lock`, same shrink-guard log line s/tracks/artists/); the third step in `_fetch_missing_now_tags` reuses the already-swapped bounded `fm` instance, iterates `track["artists"][:NOW_FETCH_MAX_SIMILAR_ARTISTS]`, and follows the track-record step's broad-catch discipline (never raises out of the function).
- [ ] **Step 6:** Both orderings + `node tests/ui_harness.mjs`.
- [ ] **Step 7:** Commit: `feat: artist-similar fetching — bounded backfill + force-path piggyback`.

---

### Task 5: Eval harness + measurement

**Files:**
- Modify: `scripts/eval_suggest.py` (loader, threading, `weights()`, a `--search-artist-sim` sweep), then `sortify/suggest.py` (the measured constant + comment)
- Test: `tests/test_eval_suggest.py` (append)

**Interfaces:**
- Consumes: everything above.
- Produces: `load_artist_map(data_dir) -> dict`; `evaluate_pair(..., artist_map=None)` and `run_eval(..., artist_map=None)` threading it to `suggest()`; `weights(tag_weight=None, neighbour_weight=None, artist_sim_weight=None)`; CLI flag `--search-artist-sim` sweeping `ARTIST_SIM_WEIGHT` over `(0.25, 0.5, 1.0, 1.5, 2.0, 3.0)` with everything else fixed.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_eval_suggest.py`, matching its fixtures):

```python
def test_evaluate_pair_rebuilds_artist_names_without_the_held_out_track(monkeypatch):
    # Mutation pin, same family as the track_keys hold-out pin: if the
    # held-out track's artist were still in its home's artist_names, the
    # track's own artist-similar list could match it back to itself.
    held = {"uri": "u1", "name": "T1", "artists": [{"id": "a1", "name": "Lone Artist"}]}
    other = {"uri": "u2", "name": "T2", "artists": [{"id": "a2", "name": "Other"}]}
    home_tracks = {"H": [held, other]}
    profiles = ev.build_all_profiles(home_tracks, {})
    assert "lone artist" in profiles["H"]["artist_names"]  # present before hold-out
    seen = {}
    monkeypatch.setattr(ev, "suggest", lambda t, profs, *a, **k: seen.update(profs) or [])
    ev.evaluate_pair("H", held, home_tracks, profiles, {}, ev.uri_home_index(home_tracks))
    assert "lone artist" not in seen["H"]["artist_names"]
    assert "other" in seen["H"]["artist_names"]


def test_weights_can_vary_artist_sim_weight():
    with weights(artist_sim_weight=2.0):
        assert suggest_mod.ARTIST_SIM_WEIGHT == 2.0
    assert suggest_mod.ARTIST_SIM_WEIGHT == 1.0  # restored
```

(`ev` stands for however `tests/test_eval_suggest.py` already imports `scripts/eval_suggest` — read the file first and use its actual alias and any existing path-setup fixture rather than inventing a second import mechanism. `evaluate_pair` looks `suggest` up as a module-level name in `eval_suggest`'s namespace, which is what makes the monkeypatch spy work.)

- [ ] **Step 2:** FAIL → implement threading (mirror how `track_map` was threaded in the getSimilar plan's Task 4: optional, keyword-only, `or {}`), the `weights()` extension, `load_artist_map` (copy `load_track_map` s/tracks/artists/), and the sweep block:

```python
if args.search_artist_sim:
    for w in (0.25, 0.5, 1.0, 1.5, 2.0, 3.0):
        with weights(artist_sim_weight=w):
            _print_result(f"ARTIST_SIM_WEIGHT={w}", run_eval(
                home_tracks, tag_artists, pairs, track_map=track_map, artist_map=artist_map))
```

- [ ] **Step 3:** PASS; both orderings.
- [ ] **Step 4: STOP — controller runs the live backfill.** Report ready; the controller runs `scripts/backfill_artist_similar.py` in the LIVE tree (~1 Last.fm call per home artist, ~1400 artists) and says go. Do not run it yourself.
- [ ] **Step 5: Measure** (zero API calls): re-baseline row (current weights, `artist_sim_weight=0`), then the sweep. Headline: artist-absent top1/top3. **Self-check (spec §Evaluation 4): non-absent metrics must be IDENTICAL across the sweep — the signal cannot reach the confident tier by construction; any drift is a leak, stop and fix.** Commit the winning weight into `ARTIST_SIM_WEIGHT` with numbers, command, seed and date in the comment, matching the `NEIGHBOUR_WEIGHT` comment's format. If every weight ties, keep 1.0 and record the tie.
- [ ] **Step 6:** Full verification: both orderings, harness, eval reproducibility (same command twice, same numbers). Commit: `feat: measured ARTIST_SIM_WEIGHT — artist-similar signal live in the guess tier`.
