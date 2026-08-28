# Subset Playlists Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `{}` playlists become a third role — non-exclusive selections that suggest themselves after a song has a home, plus a button to file into any of them by hand.

**Architecture:** A pure name predicate in `folders.py`; an opt-in `subset_ids` config list resolved by `_effective_subset_ids`; a second profile set built the same way homes are; the *existing* `sugg.suggest()` run over those profiles with two caller-side filters (drop the weak tier, take 2); a `subsets` array on `/api/now` that the client gates on "this track has a home"; a `from_id`-with-subset guard on `/api/act`; and an ordered undo log replacing the client's pop-the-last-key hack.

**Tech Stack:** Python/FastAPI, pytest (zero-network fakes), vanilla JS with no build step, `tests/ui_harness.mjs` as the hand-run JS harness.

**Spec:** `docs/superpowers/specs/2026-08-28-subset-playlists-design.md`

## Global Constraints

- **`sortify/suggest.py` is NOT modified by this plan.** Subsets are a second profile set through the same scorer. If a task seems to need a change there, stop and report it — the measured invariants (artist-overlap primacy, `MIN_SCORE = 0.8`, `NEIGHBOUR_WEIGHT` ceiling) are not being re-opened.
- **Every test is zero-Spotify-call.** Fake transports and monkeypatched clients only. `tests/conftest.py` isolates the data dir and account ledger; `tests/liveguard.py` refuses any test module bound to the live `data/` — never bypass either.
- **Never run app-importing snippets outside pytest** without exporting `SORTIFY_DATA_DIR` first. On 2026-08-21 that overwrote the user's real `config.json` and `folders.json`.
- **Budget:** opting a subset in costs `ceil(total/100)` calls once, on the next profile rebuild. Filing into a subset is one call. Nothing bulk, nothing background, nothing new on the polling path.
- **Subset name pattern:** `^\{.*\}$`, config key `subset_name_pattern`. Opt-in list: `subset_ids`.
- **At most 2** subset suggestions; **never** a `weak: True` entry.
- House copy rule: any control that spends Spotify calls states its cost.
- Verification: `.venv/bin/pytest -q` (781 passing at plan time), `node --check sortify/static/app.js`, `node tests/ui_harness.mjs` (160/160 at plan time). All three must stay green.
- `tests/ui_harness.mjs` has a known hazard: blocks are appended before the `// ---- summary` anchor, and two edits racing there silently eat a brace. Append with an exact-match edit on that anchor and re-run the harness immediately.

---

### Task 1: The subset name rule

**Files:**
- Modify: `sortify/folders.py` (add after `home_name_excluded`)
- Test: `tests/test_subsets.py` (new file; all this feature's Python tests live here)

**Interfaces:**
- Consumes: existing `home_name_excluded(name, patterns, emoji) -> bool`.
- Produces: `is_subset_name(name: str, pattern: str) -> bool` — True when the trimmed name fully matches `pattern`.

- [ ] **Step 1: Write the failing tests**

```python
"""Subset playlists: {} selections that any song can join.

Spec: docs/superpowers/specs/2026-08-28-subset-playlists-design.md.
Zero-Spotify-call throughout: fake transports and monkeypatched clients only.
"""

from sortify.folders import home_name_excluded, is_subset_name

SUBSET_PAT = r"^\{.*\}$"
HOME_EXCLUDES = [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"]


def test_braced_names_are_subsets():
    for name in ("{solfest}", "{ny jazz}", "{teh bomb}", "{}", "  {tøft}  "):
        assert is_subset_name(name, SUBSET_PAT), name


def test_other_shapes_are_not_subsets():
    for name in ("[Hazy]", "<motor>", "__start__", "THROTTLE BACK PSY", "🐾 sub", ""):
        assert not is_subset_name(name, SUBSET_PAT), name


def test_a_subset_name_can_never_be_a_home():
    """The binding invariant (spec §1): the two rules must not drift apart and
    let one playlist be both a home and a subset."""
    for name in ("{solfest}", "{}", "{ny jazz}"):
        assert is_subset_name(name, SUBSET_PAT)
        assert home_name_excluded(name, HOME_EXCLUDES, emoji=True), name
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_subsets.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_subset_name'`

- [ ] **Step 3: Implement in `sortify/folders.py`**

Place directly after `home_name_excluded`:

```python
def is_subset_name(name: str, pattern: str) -> bool:
    """True for a subset playlist name — `{like this}` by default.

    A subset is a non-exclusive selection: any song can be in one, including
    songs that already have a home. The pattern lives in config so the
    convention can move without a code change, exactly like
    `input_name_pattern`.

    Every name this accepts must also be rejected by `home_name_excluded`,
    or a playlist could be both a home and a subset — see the invariant test
    in tests/test_subsets.py.
    """
    return bool(re.fullmatch(pattern, name.strip()))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_subsets.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full suite, then commit**

Run: `.venv/bin/pytest -q` — expected all green.

```bash
git add sortify/folders.py tests/test_subsets.py
git commit -m "feat: is_subset_name, with the invariant that a subset is never a home"
```

---

### Task 2: Resolving the opt-in list

**Files:**
- Modify: `sortify/app.py` (add `_effective_subset_ids` after `_effective_input_ids`; extend the `from .folders import ...` line with `is_subset_name`)
- Test: `tests/test_subsets.py` (append)

**Interfaces:**
- Consumes: `is_subset_name(name, pattern)` (Task 1); existing `_effective_input_ids(cfg, playlists) -> set[str]`.
- Produces: `_effective_subset_ids(cfg: dict, playlists: list[dict]) -> set[str]` — the marked ids that survive every gate. Config keys read: `subset_ids` (list, absent = `[]`), `subset_name_pattern` (str, absent = `^\{.*\}$`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subsets.py`:

```python
from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

SUBSET_LISTING = [
    {"id": "s1", "name": "{solfest}", "owner": "me", "editable": True,
     "total": 22, "snapshot_id": "s-s1", "image": None, "description": ""},
    {"id": "s2", "name": "{ny jazz}", "owner": "me", "editable": True,
     "total": 40, "snapshot_id": "s-s2", "image": None, "description": ""},
    {"id": "notmine", "name": "{someone else}", "owner": "them", "editable": False,
     "total": 5, "snapshot_id": "s-nm", "image": None, "description": ""},
    {"id": "plain", "name": "Ordinary Home", "owner": "me", "editable": True,
     "total": 9, "snapshot_id": "s-pl", "image": None, "description": ""},
    {"id": "inp", "name": "[Buffer]", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s-in", "image": None, "description": ""},
]


def _cfg(**over):
    base = {
        "input_ids": [], "home_ids": [], "subset_ids": [],
        "input_name_pattern": r"^\[.+\]$",
        "subset_name_pattern": r"^\{.*\}$",
    }
    base.update(over)
    return base


def test_marked_braced_playlists_resolve():
    cfg = _cfg(subset_ids=["s1", "s2"])
    assert appmod._effective_subset_ids(cfg, SUBSET_LISTING) == {"s1", "s2"}


def test_unmarked_braced_playlists_do_not_resolve():
    """Opting in gates suggestion; an unmarked {} playlist is still filable
    by hand, but must never build a profile or propose itself (spec §1)."""
    assert appmod._effective_subset_ids(_cfg(), SUBSET_LISTING) == set()


def test_a_marked_playlist_that_is_not_braced_is_dropped():
    assert appmod._effective_subset_ids(_cfg(subset_ids=["plain"]), SUBSET_LISTING) == set()


def test_a_marked_playlist_we_cannot_edit_is_dropped():
    assert appmod._effective_subset_ids(_cfg(subset_ids=["notmine"]), SUBSET_LISTING) == set()


def test_inputs_and_homes_win_over_a_subset_mark():
    """Stale config must never make one playlist two roles at once."""
    cfg = _cfg(subset_ids=["s1", "s2"], home_ids=["s2"], input_ids=["s1"])
    assert appmod._effective_subset_ids(cfg, SUBSET_LISTING) == set()


def test_an_id_missing_from_the_listing_is_dropped():
    assert appmod._effective_subset_ids(_cfg(subset_ids=["ghost"]), SUBSET_LISTING) == set()


def test_the_pattern_defaults_when_config_omits_it():
    cfg = {"input_ids": [], "home_ids": [], "subset_ids": ["s1"],
           "input_name_pattern": r"^\[.+\]$"}
    assert appmod._effective_subset_ids(cfg, SUBSET_LISTING) == {"s1"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_subsets.py -v`
Expected: FAIL — `AttributeError: module 'sortify.app' has no attribute '_effective_subset_ids'`

- [ ] **Step 3: Implement in `sortify/app.py`**

Extend the existing folders import to include `is_subset_name`, then add directly after `_effective_input_ids`:

```python
# The convention, when config does not say otherwise. Subsets are `{like
# this}` — a shape `home_name_exclude_patterns` already refuses, so a subset
# can never also be a home (pinned by tests/test_subsets.py).
DEFAULT_SUBSET_PATTERN = r"^\{.*\}$"


def _effective_subset_ids(cfg: dict, playlists: list[dict]) -> set[str]:
    """Subsets that may suggest themselves.

    Opt-in, unlike inputs: only ids the user marked count, and marking is
    what earns a profile (and so the read that builds it). Every other {}
    playlist stays filable by hand through the picker — see the spec's
    "opting in gates suggestion, not reach".

    A mark is dropped when the playlist is gone, not ours to edit, no longer
    {}-shaped, or has since become an input or a home. Those last two make
    the role exclusive in the one direction that matters: a stale
    `subset_ids` entry can never quietly turn a home into something else.
    """
    marked = set(cfg.get("subset_ids") or [])
    if not marked:
        return set()
    pattern = cfg.get("subset_name_pattern") or DEFAULT_SUBSET_PATTERN
    taken = _effective_input_ids(cfg, playlists) | set(cfg.get("home_ids") or [])
    return {
        p["id"] for p in playlists
        if p["id"] in marked
        and p["id"] not in taken
        and p.get("editable")
        and is_subset_name(p.get("name") or "", pattern)
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_subsets.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Run the full suite, then commit**

Run: `.venv/bin/pytest -q`

```bash
git add sortify/app.py tests/test_subsets.py
git commit -m "feat: _effective_subset_ids — opt-in, name-gated, never doubling as a home"
```

---

### Task 3: Subset profiles and the `/api/now` payload

**Files:**
- Modify: `sortify/app.py` (`_ensure_profiles_locked`; a new `_subset_matches` helper; the `/api/now` return dict; `_subset_targets_payload`)
- Test: `tests/test_subsets.py` (append)

**Interfaces:**
- Consumes: `_effective_subset_ids` (Task 2); existing `sugg.build_profile(tracks, tag_artists, hints=None)`, `sugg.suggest(track, profiles, tag_artists, track_map, artist_map, playlist_artists)`, `_cached_tracks(pid, snapshot_id)`.
- Produces:
  - `_profile_state["subset_profiles"]: dict[str, dict]` and `_profile_state["subsets"]: list[dict]` (the listing entries).
  - `_subset_matches(state, track, tag_artists, track_map) -> list[dict]` — at most 2 entries `{playlist_id, name, pct, already, reasons}`, never `weak`.
  - `/api/now` gains `"subsets": [...]` (matches) and `"subset_targets": [{id, name, total}]` (ALL `{}` playlists, for the picker).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subsets.py`:

```python
import pytest


@pytest.fixture
def wired(monkeypatch):
    """A profile state built from fakes — no Store writes, no HTTP."""
    listing = SUBSET_LISTING + [
        {"id": "h1", "name": "Home One", "owner": "me", "editable": True,
         "total": 12, "snapshot_id": "s-h1", "image": None, "description": ""},
    ]
    tracks = {
        "h1": [{"uri": "spotify:track:a", "id": "a", "name": "A", "is_local": False,
                "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
                "added_at": "2026-01-01T00:00:00Z"}],
        "s1": [{"uri": "spotify:track:b", "id": "b", "name": "B", "is_local": False,
                "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}],
                "added_at": "2026-01-01T00:00:00Z"}],
        "s2": [{"uri": "spotify:track:c", "id": "c", "name": "C", "is_local": False,
                "type": "track", "artists": [{"id": "ar9", "name": "Ar Nine"}],
                "added_at": "2026-01-01T00:00:00Z"}],
    }
    appmod.store.save_config(_cfg(subset_ids=["s1", "s2"], home_ids=["h1"]))
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: listing)
    monkeypatch.setattr(appmod, "_cached_tracks", lambda pid, snap: tracks.get(pid, []))
    appmod._profile_state.clear()
    appmod._profile_state["built_at"] = 0.0
    return appmod._ensure_profiles(force=True)


PLAYING = {"uri": "spotify:track:z", "id": "z", "name": "Z", "is_local": False,
           "type": "track", "artists": [{"id": "ar1", "name": "Ar One"}]}


def test_only_opted_in_subsets_get_profiles(wired):
    assert set(wired["subset_profiles"]) == {"s1", "s2"}


def test_a_subset_sharing_the_artist_matches(wired):
    matches = appmod._subset_matches(wired, PLAYING, appmod.store.tag_artists(),
                                     appmod.store.lastfm_track_map())
    assert [m["playlist_id"] for m in matches] == ["s1"]
    assert matches[0]["name"] == "{solfest}"
    assert any("Ar One" in r for r in matches[0]["reasons"])


def test_matches_never_include_weak_guesses(wired):
    """suggest()'s sub-threshold tier exists to force a decision that must be
    made; a curated selection is optional, so a guess there is noise."""
    matches = appmod._subset_matches(wired, PLAYING, appmod.store.tag_artists(),
                                     appmod.store.lastfm_track_map())
    assert all(not m.get("weak") for m in matches)
    assert "s2" not in [m["playlist_id"] for m in matches]


def test_at_most_two_matches(wired, monkeypatch):
    fake = [{"playlist_id": p, "pct": 90, "already": False, "reasons": []}
            for p in ("s1", "s2", "s3")]
    monkeypatch.setattr(appmod.sugg, "suggest", lambda *a, **k: fake)
    matches = appmod._subset_matches(wired, PLAYING, {}, {})
    assert len(matches) == 2


def test_a_subset_the_track_is_already_in_is_flagged(wired):
    track = {**PLAYING, "uri": "spotify:track:b", "id": "b"}
    matches = appmod._subset_matches(wired, track, appmod.store.tag_artists(),
                                     appmod.store.lastfm_track_map())
    assert any(m["already"] for m in matches if m["playlist_id"] == "s1")


def test_subset_targets_include_every_brace_playlist_not_just_opted_in(wired):
    """Filing by hand must never be gated by a list curated for suggestions.

    Both s1 and s2 are {}-shaped and editable, so both are reachable even
    though only the opted-in ones can suggest themselves.
    """
    ids = {t["id"] for t in appmod._subset_targets_payload(wired)}
    assert ids == {"s1", "s2"}
    assert "plain" not in ids       # not {}-shaped
    assert "notmine" not in ids     # {}-shaped but not ours to edit
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_subsets.py -v -k "wired or subset_targets or matches or profiles"`
Expected: FAIL — `KeyError: 'subset_profiles'` / `AttributeError: '_subset_matches'`

- [ ] **Step 3: Implement in `sortify/app.py`**

In `_ensure_profiles_locked`, after the home `profiles` dict is built and before the co-occurrence corpus:

```python
    # Subsets: the same mechanism as homes, on an opt-in set. Snapshot-keyed
    # like every other cached read, so warm cost is zero; the first build
    # after opting one in pays ceil(total/100) calls for it, which is the
    # contract homes already have.
    subset_ids = _effective_subset_ids(cfg, all_playlists)
    subsets = [p for p in all_playlists if p["id"] in subset_ids]
    subset_profiles = {
        s["id"]: sugg.build_profile(
            _cached_tracks(s["id"], s["snapshot_id"]), tag_artists
        )
        for s in subsets
    }
```

Add `subset_profiles=subset_profiles, subsets=subsets,` to the `_profile_state.update(...)` call.

Then add these two helpers next to `_homes_payload`:

```python
# Homes show three; a subset row appears in a "done, moving on" moment, and
# three more decisions there work against it. (Spec §3.)
SUBSET_TOP_N = 2


def _subset_matches(state: dict, track: dict, tag_artists: dict,
                    track_map: dict) -> list[dict]:
    """Subsets worth offering for this track. Local arithmetic, zero calls.

    The same scorer homes use, over the subset profiles — suggest.py is not
    modified for this. Two caller-side rules: no weak (sub-threshold) tier,
    because a guess about an optional selection is noise rather than
    pressure to decide; and at most SUBSET_TOP_N.
    """
    if not state.get("subset_profiles"):
        return []
    names = {s["id"]: s["name"] for s in state.get("subsets", [])}
    scored = sugg.suggest(
        track, state["subset_profiles"], tag_artists, track_map,
        state.get("artist_similar") or {}, state.get("playlist_artists") or {},
    )
    out = []
    for s in scored:
        if s.get("weak"):
            continue
        out.append({
            "playlist_id": s["playlist_id"],
            "name": names.get(s["playlist_id"], "subset"),
            "pct": s["pct"],
            "already": s["already"],
            "reasons": s["reasons"],
        })
    return out[:SUBSET_TOP_N]


def _subset_targets_payload(state: dict) -> list[dict]:
    """Every editable {}-named playlist, opted in or not — the picker's list.

    Opting in gates whether a subset SUGGESTS itself; filing by hand reaches
    all of them, so this reads the listing rather than the opt-in set.
    """
    cfg = store.config()
    pattern = cfg.get("subset_name_pattern") or DEFAULT_SUBSET_PATTERN
    return [
        {"id": p["id"], "name": p["name"], "total": p.get("total")}
        for p in state.get("playlists", [])
        if p.get("editable") and is_subset_name(p.get("name") or "", pattern)
    ]
```

In `/api/now`'s return dict, beside `"homes": _homes_payload(state),`:

```python
        "subsets": _subset_matches(track, state, tag_artists, track_map) if sortable else [],
        "subset_targets": _subset_targets_payload(state),
```

**Note the argument order** — use `_subset_matches(state, track, tag_artists, track_map)` exactly as defined above; write the call to match the definition.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_subsets.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite, then commit**

Run: `.venv/bin/pytest -q` — `test_now_polling.py` and `test_now_tag_fetch.py` exercise `/api/now` and must stay green.

```bash
git add sortify/app.py tests/test_subsets.py
git commit -m "feat: subset profiles and the /api/now subsets payload"
```

---

### Task 4: The `/api/act` guard and `subset_ids` persistence

**Files:**
- Modify: `sortify/app.py` (`ConfigIn`, `set_config`, `act`, and `/api/playlists`'s `role` field)
- Test: `tests/test_subsets.py` (append)

**Interfaces:**
- Consumes: `_effective_subset_ids` (Task 2).
- Produces: `ConfigIn.subset_ids: list[str] = []` persisted by `/api/config`; `/api/playlists` rows carry `role == "subset"`; `/api/act` returns 400 for a `from_id` paired with a subset `to_id`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subsets.py`:

```python
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(appmod.app, raise_server_exceptions=False)


def test_config_persists_subset_ids(client, monkeypatch):
    appmod.store.save_config(_cfg())
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: SUBSET_LISTING)
    res = client.post("/api/config", json={
        "input_ids": [], "home_ids": [], "home_hints": {}, "subset_ids": ["s1"]})
    assert res.status_code == 200
    assert appmod.store.config()["subset_ids"] == ["s1"]


def test_playlists_listing_reports_the_subset_role(client, monkeypatch):
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: SUBSET_LISTING)
    monkeypatch.setattr(appmod, "_split_summary", lambda pid, splits: None)
    rows = {p["id"]: p for p in client.get("/api/playlists").json()["playlists"]}
    assert rows["s1"]["role"] == "subset"
    assert rows["s2"]["role"] is None      # {}-shaped but not opted in


def test_act_refuses_to_remove_from_an_input_when_filing_into_a_subset(client, monkeypatch):
    """A song put into a best-of has not been sorted — it must not leave the
    input it came from (spec §6). Structural, not a property of one caller."""
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: SUBSET_LISTING)
    spent = []
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda *a, **k: spent.append(a) or "snap")
    monkeypatch.setattr(appmod.sp, "remove_from_playlist",
                        lambda *a, **k: spent.append(a) or "snap")
    res = client.post("/api/act", json={
        "action": "move", "uri": "spotify:track:z", "from_id": "inp", "to_id": "s1"})
    assert res.status_code == 400
    assert "subset" in res.json()["detail"].lower()
    assert spent == []          # refused before anything was spent


def test_act_allows_adding_to_a_subset_without_a_from_id(client, monkeypatch):
    appmod.store.save_config(_cfg(subset_ids=["s1"]))
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: SUBSET_LISTING)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", lambda *a, **k: "snap")
    res = client.post("/api/act", json={
        "action": "move", "uri": "spotify:track:z", "from_id": None, "to_id": "s1"})
    assert res.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_subsets.py -v -k "config_persists or subset_role or refuses or allows_adding"`
Expected: FAIL — `subset_ids` not persisted; `role` is `None`; the move returns 200 instead of 400.

- [ ] **Step 3: Implement in `sortify/app.py`**

Extend `ConfigIn`:

```python
class ConfigIn(BaseModel):
    input_ids: list[str]
    home_ids: list[str] = []
    # {playlist_id: "ambient, piano"} — the user's own matching hints per
    # home, free text split on commas at profile-build time.
    home_hints: dict[str, str] = {}
    # Subsets that may suggest themselves. Opt-in: marking one is what earns
    # it a profile, and so the read that builds it.
    subset_ids: list[str] = []
```

In `set_config`, add `subset_ids=sorted(set(body.subset_ids)),` to the `store.update_config(...)` call.

In `/api/playlists`, compute the subset set once beside `inputs` and extend the role expression:

```python
    subsets = _effective_subset_ids(cfg, items)
```

```python
        p["role"] = (
            "input" if p["id"] in inputs
            else "home" if p["id"] in cfg.get("home_ids", [])
            else "subset" if p["id"] in subsets
            else None
        )
```

At the top of `act`, before any branch:

```python
    # Filing into a subset is not filing. A song put into a best-of still
    # needs its home, so it must not leave the input it came from — and that
    # rule belongs here, where every caller passes, rather than in whichever
    # button happens to be current. Costs nothing to enforce. (Spec §6.)
    if body.to_id and body.from_id:
        cfg = store.config()
        if body.to_id in _effective_subset_ids(cfg, sp.my_playlists()):
            raise HTTPException(
                400,
                "that destination is a subset — adding to a subset must not "
                "remove the track from its input; send from_id: null",
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_subsets.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite, then commit**

Run: `.venv/bin/pytest -q`

```bash
git add sortify/app.py tests/test_subsets.py
git commit -m "feat: subset role in the listing, subset_ids persistence, and the act guard"
```

---

### Task 5: The ordered undo log (prerequisite bug fix)

**Files:**
- Modify: `sortify/static/app.js` (`filedUris` neighbourhood, `nowFile`, `nowRemove`, `$("btn-undo-now").onclick`)
- Test: `tests/ui_harness.mjs` (append one scenario before the `// ---- summary` anchor)

**Interfaces:**
- Consumes: existing `filedUris`, `nowActions`, `api()`.
- Produces: `nowActionLog: Array<{uri, kind}>` where `kind` is `"home"` or `"subset"`; `btn-undo-now` clears `filedUris` only for `kind === "home"`.

This is a live bug today: `btn-undo-now` pops the last **key of `filedUris`**, so once subsets add actions that write no such key, undoing one would clear an unrelated song's filed state. Fix it before the subset UI can create that situation.

- [ ] **Step 1: Write the failing harness scenario**

Append immediately before `// ---- summary ---` in `tests/ui_harness.mjs`:

```javascript
// ============================================================================
// UL — the undo log is ordered, and only home filings own a filed state.
// btn-undo-now used to pop the last KEY of filedUris, which is not the last
// ACTION once subset adds (which write no key) exist: undoing one wiped an
// unrelated track's "filed" badge.
// ============================================================================
{
  resetLog();
  routes["GET /api/now?force=1"] = {
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:ul1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: null, sitting: null, suggestions: [],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      subsets: [], subset_targets: [{ id: "S1", name: "{sel}", total: 4 }],
      inputs: [],
    },
  };
  routes["POST /api/act"] = { status: 200, body: {} };
  routes["POST /api/undo"] = { status: 200, body: { restored_to: null } };
  run(`filedUris = {}; nowActions = 0; removedUri = null; nowActionLog = []; pollNow(true)`);
  await tick();
  run("stopNowPolling()");

  // An earlier track was filed to a home; this session still remembers it.
  run(`filedUris["spotify:track:earlier"] = "Some Home";
       nowActionLog = [{ uri: "spotify:track:earlier", kind: "home" }]`);
  // Now a subset add on the CURRENT track — writes no filedUris key.
  await run(`nowAddToSubset("S1")`);
  await tick();
  check("UL a subset add writes no filed state",
        run(`filedUris["spotify:track:ul1"] === undefined`),
        `filed=${run(`JSON.stringify(filedUris)`)}`);
  check("UL a subset add is recorded in the ordered log",
        run(`nowActionLog.length === 2 && nowActionLog[1].kind === "subset"`),
        run(`JSON.stringify(nowActionLog)`));

  await run(`$("btn-undo-now").onclick()`);
  await tick();
  check("UL undoing the subset add leaves the earlier home filing alone",
        run(`filedUris["spotify:track:earlier"] === "Some Home"`),
        `filed=${run(`JSON.stringify(filedUris)`)}`);
  check("UL and the log pops the action that was actually undone",
        run(`nowActionLog.length === 1 && nowActionLog[0].kind === "home"`),
        run(`JSON.stringify(nowActionLog)`));
}
```

- [ ] **Step 2: Run the harness to verify it fails**

Run: `node tests/ui_harness.mjs 2>&1 | grep -E "^(PASS|FAIL)  UL|checks passed"`
Expected: FAIL on the log checks (`nowActionLog` is not defined; `nowAddToSubset` does not exist yet — the guarded pattern means these are failed checks, and the "leaves the earlier filing alone" check fails because the pop hack deletes `spotify:track:earlier`).

If a missing function throws and ends the run, guard the call the way the RU scenario does:
```javascript
const wired = run(`typeof nowAddToSubset === "function"`);
check("UL nowAddToSubset exists", wired, `type=${run(`typeof nowAddToSubset`)}`);
if (wired) { await run(`nowAddToSubset("S1")`); await tick(); }
```

- [ ] **Step 3: Implement in `sortify/static/app.js`**

Beside `let filedUris = {};`:

```javascript
// Every filing action this session, oldest first. The undo stack is the
// server's; this mirrors ONLY what the client must undo locally, which is
// why each entry carries its kind: a subset add writes no filed state, so
// undoing one must not clear a filed badge that belongs to another track.
let nowActionLog = [];
```

In `nowFile`, after `filedUris[tr.uri] = ...`:

```javascript
    nowActionLog.push({ uri: tr.uri, kind: "home" });
```

In `nowRemove`, after `filedUris[tr.uri] = "nowhere (removed from input)";`:

```javascript
    nowActionLog.push({ uri: tr.uri, kind: "home" });
```

Replace the body of `$("btn-undo-now").onclick`:

```javascript
$("btn-undo-now").onclick = async () => {
  if (!nowActions) return;
  try {
    const res = await api("/api/undo", {});
    nowActions--;
    // Pop the last ACTION, not the last filedUris key: a subset add adds an
    // entry here but no key there, so keying off the object undid the wrong
    // track's badge.
    const last = nowActionLog.pop();
    if (last && last.kind === "home") delete filedUris[last.uri];
    if (last && last.uri === removedUri) removedUri = null;
    toast(res.restored_to ? "undone — restored to input" : "undone");
    renderNow();
  } catch (e) { toast(e.message); }
};
```

`nowAddToSubset` is defined in Task 6; this task's harness scenario fails until then, so implement Task 6 before re-running the UL checks — or, to keep this task independently green, add the one-line stub Task 6 replaces:

```javascript
async function nowAddToSubset(id) {
  const tr = nowState.track;
  try {
    await api("/api/act", { action: "move", uri: tr.uri, from_id: null, to_id: id });
    nowActions++;
    nowActionLog.push({ uri: tr.uri, kind: "subset" });
    toast("added");
    renderNow();
  } catch (e) { toast(e.message); }
}
```

- [ ] **Step 4: Run the harness and the suite**

Run: `node --check sortify/static/app.js && node tests/ui_harness.mjs 2>&1 | tail -2`
Expected: all checks pass.
Run: `.venv/bin/pytest -q` — expected green (static-file tests read app.js).

- [ ] **Step 5: Commit**

```bash
git add sortify/static/app.js tests/ui_harness.mjs
git commit -m "fix: undo pops the last action, not the last filed key"
```

---

### Task 6: The Now card — subset row and Add to subset…

**Files:**
- Modify: `sortify/static/app.js` (`nowState` assembly, `ordinaryCardBody`, picker wiring, `nowAddToSubset` from Task 5)
- Modify: `sortify/static/style.css` (subset row, already-in line)
- Test: `tests/ui_harness.mjs` (append one scenario before the `// ---- summary` anchor)

**Interfaces:**
- Consumes: `/api/now`'s `subsets` and `subset_targets` (Task 3); `nowActionLog` and `nowAddToSubset` (Task 5); existing `openPicker(map, onPick, onCreate?)`.
- Produces: no interface later tasks rely on — this is the last task.

**The display gate (spec §4), stated once because it is the subtle part:** the server sends matches whenever it has them and does *not* know whether the track was just filed (profiles hold a build-time `uris` set that `/api/act` never updates). So the client shows the row when **either** `filedUris[tr.uri]` is set (it filed this session) **or** some entry in `d.suggestions` has `already === true` (the track is in a home from before).

- [ ] **Step 1: Write the failing harness scenario**

Append immediately before `// ---- summary ---`:

```javascript
// ============================================================================
// SS — subsets are offered only once the home question is settled.
// The server cannot gate this itself: profiles carry a build-time uris set
// that /api/act never updates, so for a few seconds after filing the server
// still believes the track has no home. The client decides.
// ============================================================================
{
  resetLog();
  const body = (over) => ({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:ss1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: null, sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 80, reasons: [], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      subsets: [{ playlist_id: "S1", name: "{solfest}", pct: 70, already: false,
                  reasons: ["2 tracks by Artist here"] },
                { playlist_id: "S2", name: "{tøft}", pct: 60, already: true,
                  reasons: [] }],
      subset_targets: [{ id: "S1", name: "{solfest}", total: 4 }],
      inputs: [],
      ...over,
    },
  });
  const html = () => $$("now-card").innerHTML;

  routes["GET /api/now?force=1"] = body({});
  routes["POST /api/act"] = { status: 200, body: {} };
  run(`filedUris = {}; nowActions = 0; nowActionLog = []; removedUri = null; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("SS an unfiled track is offered no subsets",
        !html().includes("{solfest}"), `html has solfest=${html().includes("{solfest}")}`);
  check("SS but the Add to subset button is always there",
        html().includes('id="btn-now-subset"'), "missing btn-now-subset");

  // File it to a home: the row appears for the track just filed.
  await run(`nowFile("H1")`);
  await tick();
  check("SS filing to a home reveals the subset offers",
        html().includes("{solfest}"), "no subset row after filing");
  check("SS an already-in subset is a muted line, not a button",
        html().includes("already in") && html().includes("{tøft}") &&
        !html().includes('data-subset="S2"'),
        `html=${html().slice(0, 0)}already=${html().includes("already in")}`);

  // A track already in a home (server-flagged) gets the row with no filing.
  routes["GET /api/now?force=1"] = body({
    track: { uri: "spotify:track:ss2", name: "Other", duration_ms: 200000,
             artists: [{ name: "Artist" }], sortable: true, image: null },
    suggestions: [{ playlist_id: "H1", pct: 80, reasons: [], already: true }],
  });
  run(`filedUris = {}; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("SS a track already in a home gets the row without filing again",
        html().includes("{solfest}"), "no subset row for an already-filed track");

  // Adding to a subset must not put the card into its filed state.
  await run(`nowAddToSubset("S1")`);
  await tick();
  check("SS adding to a subset does not consume the filed state",
        run(`filedUris["spotify:track:ss2"] === undefined`),
        run(`JSON.stringify(filedUris)`));
  check("SS and it sends no from_id",
        bodies("/api/act").slice(-1)[0].from_id === null,
        JSON.stringify(bodies("/api/act").slice(-1)[0]));
}
```

- [ ] **Step 2: Run the harness to verify it fails**

Run: `node tests/ui_harness.mjs 2>&1 | grep -E "^(PASS|FAIL)  SS|checks passed"`
Expected: FAIL — no `btn-now-subset`, no subset row.

- [ ] **Step 3: Implement**

In the `nowState` assembly (where `homes` is turned into a Map), keep `subsets` and `subset_targets` as-is on `nowState` — they arrive on `d` and need no reshaping except a Map for the picker:

```javascript
    nowState = { ...data, homes: new Map((data.homes || []).map((h) => [h.id, h])),
                 subsetTargets: new Map((data.subset_targets || [])
                   .map((s) => [s.id, { id: s.id, name: s.name, total: s.total, folder: null }])) };
```

In `ordinaryCardBody`, build the subset block and append it to BOTH the filed branch and the ordinary one:

```javascript
// Subsets are the second question, never the first: they appear once the
// home question is settled — either because we just filed this track, or
// because the server says it is already in a home. A homeless track shows
// none of this, by design.
function subsetBlock(d, tr) {
  const settled = !!filedUris[tr.uri] || (d.suggestions || []).some((s) => s.already);
  if (!settled) return "";
  const offers = (d.subsets || []).filter((s) => !s.already);
  const already = (d.subsets || []).filter((s) => s.already);
  let out = "";
  for (const s of offers) {
    out += `<button class="sub-offer" data-subset="${esc(s.playlist_id)}">
      <span class="s-pct">${s.pct}%</span>
      <span class="s-name">+ ${esc(s.name)}</span>
      <span class="s-why">${esc((s.reasons || []).join(" · "))}</span>
    </button>`;
  }
  for (const s of already) {
    out += `<p class="sub-already hint">already in <b>${esc(s.name)}</b></p>`;
  }
  return out ? `<div class="subsets">${out}</div>` : "";
}
```

In `ordinaryCardBody`, the filed branch becomes:

```javascript
  const filedTo = filedUris[tr.uri];
  if (filedTo) {
    return `<p class="done-msg">✓ filed to <b>${esc(filedTo)}</b></p>` +
           subsetBlock(d, tr) + subsetButtonRow();
  }
```

and the ordinary path appends `subsetBlock(d, tr)` after the capture chips. The button row:

```javascript
function subsetButtonRow() {
  return `<div class="minor-actions">
    <button id="btn-now-subset">Add to subset…</button>
  </div>`;
}
```

Add `btn-now-subset` to the existing `minor-actions` row in the unfiled path instead of a second row — i.e. that row becomes:

```javascript
  body += `<div class="minor-actions">
    <button id="btn-now-more"><kbd>m</kbd> Add to…</button>
    <button id="btn-now-subset">Add to subset…</button>
  </div>`;
```

Wire both, next to the existing `btn-now-more` wiring:

```javascript
    const sub = $("btn-now-subset");
    if (sub) sub.onclick = () => openPicker(nowState.subsetTargets, nowAddToSubset);
    $("now-card").querySelectorAll(".sub-offer").forEach((b) => {
      b.onclick = () => nowAddToSubset(b.dataset.subset);
    });
```

Replace the Task 5 stub `nowAddToSubset` with the real one (same body plus the name in the toast):

```javascript
async function nowAddToSubset(id) {
  const tr = nowState.track;
  const name = nowState.subsetTargets?.get(id)?.name
    || (nowState.subsets || []).find((s) => s.playlist_id === id)?.name || "subset";
  try {
    // from_id stays null: a song in a best-of has not been sorted, so it
    // must not leave its input. The server refuses the other shape too.
    await api("/api/act", { action: "move", uri: tr.uri, from_id: null, to_id: id });
    nowActions++;
    nowActionLog.push({ uri: tr.uri, kind: "subset" });
    if (nowState.subsets) {
      const hit = nowState.subsets.find((s) => s.playlist_id === id);
      if (hit) hit.already = true;
    }
    toast(`+ ${name}`);
    renderNow();
  } catch (e) { toast(e.message); }
}
```

CSS:

```css
.subsets { margin-top: .7rem; }
.sub-offer {
  display: block; width: 100%; text-align: left; margin-bottom: .4rem;
  padding: .5rem .8rem; border-radius: 10px;
  background: transparent; border: 1px dashed var(--border);
}
.sub-offer .s-name { font-weight: 600; }
.sub-already { margin: .3rem 0 0; }
```

- [ ] **Step 4: Run everything**

Run: `node --check sortify/static/app.js && node tests/ui_harness.mjs 2>&1 | tail -2`
Expected: all checks pass.
Run: `.venv/bin/pytest -q`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add sortify/static/app.js sortify/static/style.css tests/ui_harness.mjs
git commit -m "feat: subset offers on the Now card, and Add to subset…"
```

---

### Task 7: Marking subsets in the Playlists view

**Files:**
- Modify: `sortify/static/app.js` (`makeListRow`, `saveConfig`, `loadLists`)
- Test: `tests/ui_harness.mjs` (append one scenario before the `// ---- summary` anchor)

**Interfaces:**
- Consumes: `/api/playlists` rows carrying `role: "subset"` (Task 4); `ConfigIn.subset_ids` (Task 4).
- Produces: nothing later tasks rely on.

- [ ] **Step 1: Write the failing harness scenario**

Append immediately before `// ---- summary ---`:

```javascript
// ============================================================================
// SM — only {}-named playlists can be marked as subsets, and the mark saves.
// ============================================================================
{
  resetLog();
  routes["GET /api/playlists"] = {
    status: 200,
    body: {
      playlists: [
        { id: "s1", name: "{solfest}", editable: true, total: 22, role: null,
          folder: null, hints: "", split: null },
        { id: "h1", name: "Ordinary", editable: true, total: 12, role: "home",
          folder: null, hints: "", split: null },
      ],
      fetched_at: 1, sitting_orphans: [],
    },
  };
  routes["POST /api/config"] = { status: 200, body: { ok: true } };
  await run(`loadLists()`);
  await tick();
  check("SM a {} playlist offers a Subset chip",
        $$("playlists").children.some((r) =>
          String(r.innerHTML).includes("r-subset")),
        "no r-subset chip rendered");

  run(`roles["s1"] = "subset"`);
  await run(`saveConfig()`);
  await tick();
  const sent = bodies("/api/config").slice(-1)[0];
  check("SM saving sends subset_ids",
        Array.isArray(sent.subset_ids) && sent.subset_ids.includes("s1"),
        JSON.stringify(sent));
  check("SM and does not put it in home_ids",
        !(sent.home_ids || []).includes("s1"), JSON.stringify(sent.home_ids));
}
```

- [ ] **Step 2: Run the harness to verify it fails**

Run: `node tests/ui_harness.mjs 2>&1 | grep -E "^(PASS|FAIL)  SM|checks passed"`
Expected: FAIL — no `r-subset` chip, `subset_ids` absent from the POST body.

- [ ] **Step 3: Implement in `sortify/static/app.js`**

In `makeListRow`, add the chip to the `pl-roles` markup after the Home chip:

```html
      <button class="chip r-subset">Subset</button>
```

Update the destructure and add the paint/click rules:

```javascript
  const [bIn, bHome, bSubset, bSort, bSplit] = row.querySelectorAll("button");
```

```javascript
    bSubset.classList.toggle("on-subset", roles[p.id] === "subset");
    // Only a {}-named playlist can be a subset — the server resolves the
    // same rule, so offering the chip anywhere else would be a lie.
    bSubset.hidden = !/^\{.*\}$/.test((p.name || "").trim()) || !p.editable;
```

```javascript
  bSubset.onclick = () => {
    roles[p.id] = roles[p.id] === "subset" ? null : "subset";
    paint();
  };
```

Extend `saveConfig`:

```javascript
async function saveConfig() {
  const input_ids = Object.keys(roles).filter((k) => roles[k] === "input");
  const home_ids = Object.keys(roles).filter((k) => roles[k] === "home");
  const subset_ids = Object.keys(roles).filter((k) => roles[k] === "subset");
  await api("/api/config", { input_ids, home_ids, home_hints: hintTexts, subset_ids });
}
```

CSS, beside the other role chips:

```css
.chip.on-subset { background: var(--accent); color: var(--bg); border-color: transparent; }
```

- [ ] **Step 4: Run everything**

Run: `node --check sortify/static/app.js && node tests/ui_harness.mjs 2>&1 | tail -2`
Run: `.venv/bin/pytest -q`
Expected: both green.

- [ ] **Step 5: Commit**

```bash
git add sortify/static/app.js sortify/static/style.css tests/ui_harness.mjs
git commit -m "feat: mark {} playlists as subsets in the Playlists view"
```

---

### Task 8: Documentation

**Files:**
- Modify: `docs/matching.md`
- Modify: `CLAUDE.md` (the Conventions section)

**Interfaces:**
- Consumes: everything above. Produces: nothing.

- [ ] **Step 1: Add a subsets section to `docs/matching.md`**

Append after the "Your hints" section:

```markdown
## Subsets

A **subset** is a `{braced}` playlist — a non-exclusive selection any song
can join, including songs that already have a home. Best-ofs, moods,
project lists.

Subsets are scored by the same three signals as homes, against the tracks
already in them, with two differences: they never show sub-threshold
guesses (a guess about an optional selection is noise, not pressure to
decide), and at most two are offered at a time.

They appear only once the home question is settled — right after you file a
track, or when it is already in a home. A track with no home is never
offered a subset; the home decision comes first.

**Opting in** (the Subset chip on the Playlists view) is what lets a subset
propose itself, and costs one read of that playlist on the next profile
rebuild. Every `{}` playlist stays reachable by hand through **Add to
subset…**, opted in or not.
```

- [ ] **Step 2: Add the convention to `CLAUDE.md`**

In the Conventions section, after the Homes bullet:

```markdown
- **Subsets** are `{braced}` playlists (`subset_name_pattern`): non-exclusive
  selections, never a filing home, never an input. `subset_ids` is the
  opt-in list of those allowed to suggest themselves — marking one costs a
  read of it; the picker reaches all of them regardless. `suggest.py` is
  shared with homes and was not modified for them.
```

- [ ] **Step 3: Verify and commit**

Run: `.venv/bin/pytest -q` (docs are read by no test, but the suite must be green before any commit).

```bash
git add docs/matching.md CLAUDE.md
git commit -m "docs: subsets in matching.md and the CLAUDE.md conventions"
```

---

## Self-review notes

- **Spec coverage:** §1 role → Tasks 1, 2, 4, 7; §2 profiles → Task 3; §3 scoring (weak dropped, 2 not 3) → Task 3; §4 when the row appears + the client-side gate → Task 6; §5 the button → Task 6; §6 filing-is-not-filing + the act guard → Tasks 4, 6; §7 undo log → Task 5; Tests section → distributed across every task; Budget → Global Constraints. Scope exclusions (triage, subset creation) are respected: no task touches either.
- **Type consistency:** `is_subset_name(name, pattern)` (T1) is called with exactly two args in T2 and T3. `_effective_subset_ids(cfg, playlists)` (T2) is called in T3, T4. `_subset_matches(state, track, tag_artists, track_map)` (T3) — the plan flags the argument-order trap explicitly at its call site. `nowActionLog` entries are `{uri, kind}` in T5 and T6. `nowAddToSubset(id)` is stubbed in T5 and replaced in T6, deliberately and with the replacement spelled out.
- **Two traps deliberately spelled out for implementers**, because both are the kind that pass review and fail in use: `_subset_matches`'s argument order (the call site in `/api/now` must match the definition — the plan says so at the call site), and Task 6's display gate, which cannot be moved server-side however natural that looks (profiles hold a build-time `uris` set, so the server does not yet know a just-filed track has a home).
