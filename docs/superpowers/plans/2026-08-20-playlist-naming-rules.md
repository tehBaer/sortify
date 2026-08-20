# Playlist Naming Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ongoing enforcement of playlist naming conventions (Homes ALL CAPS, Inputs `[bracketed]`), each rename individually user-approved via a collapsible "Naming" panel on the Playlists view.

**Architecture:** A new pure-rules module `sortify/naming.py` (reusing `folders.py` helpers) feeds two endpoints in `app.py`: `GET /api/naming` computes violations from the *cached* playlist listing (zero Spotify calls) and `POST /api/naming/{playlist_id}/rename` applies one proposal via a new `SpotifyClient.rename_playlist` (one PUT, then patches the cached listing so the UI updates without a paid Refresh). The frontend adds a banner + expandable list to the existing Playlists view, following the orphan-bar pattern.

**Tech Stack:** Python/FastAPI, pytest with `fastapi.testclient`, vanilla-JS frontend (no build step).

**Spec:** `docs/superpowers/specs/2026-08-20-playlist-naming-rules-design.md`

## Global Constraints

- **Spotify API budget (CLAUDE.md, non-negotiable):** `GET /api/naming` must spend **zero** Spotify calls — it reads only the cached listing via `sp.my_playlists()` (no `refresh=True`). Each rename costs exactly **one** call, only on explicit user click. Never call api.spotify.com directly during development; tests fake the client (see `tests/conftest.py`, which binds a throwaway data dir and account ledger before importing `sortify.app`).
- Renames go through `SpotifyClient.request` so the ledger/throttle/cooldown guards apply.
- Only playlists the user owns are ever flagged or renamed (`editable` in the cached listing).
- Emoji-prefixed names are exempt from all rules — the emoji IS the subset marker (`folders.starts_with_emoji`).
- Folder rules are **deferred**: keep `naming.py` shaped so a "flag-only, manual rename" rule type can slot in later, but build nothing for it now.
- Frontend: no framework, no dependencies; asset cache-busting is automatic (mtime-stamped `?v=`, `tests/test_cache_busting.py`). Buttons that spend calls state their price (e.g. "Rename (1 call)"), matching the house convention.
- Tests: `.venv/bin/pytest -q` must stay green; they cost zero API calls.

---

### Task 1: Pure naming rules (`sortify/naming.py`)

**Files:**
- Create: `sortify/naming.py`
- Test: `tests/test_naming.py`

**Interfaces:**
- Consumes: `folders.starts_with_emoji(name: str) -> bool`, `folders._is_caps(name: str) -> bool` (both exist in `sortify/folders.py`).
- Produces:
  - `propose(name: str, role: str, input_pattern: str | None = None) -> str | None` — the conforming form of `name` under `role` (`"home"` or `"input"`), or `None` when it already conforms, is emoji-exempt, or the rename would be a no-op.
  - `violations(playlists: list[dict], input_ids: set[str], home_ids: set[str], input_pattern: str | None = None) -> list[dict]` — rows `{"playlist_id", "current", "proposed", "rule"}`. Playlist dicts are the cached-listing shape: at least `{"id", "name", "editable"}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_naming.py`:

```python
"""Pure naming rules — no network, no store.

The rules come from the 2026-08-20 design doc: Homes are ALL CAPS, Inputs
are [bracketed], an emoji prefix marks a derived playlist and exempts it
from both. `violations` only ever proposes renames for playlists the user
owns (editable) with a marked role.
"""

from sortify.naming import propose, violations


# ---- propose: home rule ----------------------------------------------------

def test_home_lowercase_proposes_upper():
    assert propose("beach vibes", "home") == "BEACH VIBES"

def test_home_already_caps_conforms():
    assert propose("BEACH VIBES", "home") is None

def test_home_mixed_case_proposes_upper():
    assert propose("Beach Vibes", "home") == "BEACH VIBES"

def test_home_non_alphabetic_name_is_not_flagged():
    # upper() is a no-op on "1234 · ???" — proposing an identical name
    # would be a rename that changes nothing.
    assert propose("1234 · ???", "home") is None

def test_home_emoji_prefix_is_exempt():
    # The emoji IS the subset marker; these are valid as-is.
    assert propose("🐾 quiet corner", "home") is None


# ---- propose: input rule ---------------------------------------------------

def test_input_unbracketed_proposes_brackets():
    assert propose("new finds", "input") == "[new finds]"

def test_input_already_bracketed_conforms():
    assert propose("[new finds]", "input") is None

def test_input_conformance_uses_configured_pattern_when_given():
    # config's input_name_pattern is the convention's source of truth.
    assert propose("<new finds>", "input", input_pattern=r"^<.+>$") is None
    assert propose("new finds", "input", input_pattern=r"^<.+>$") == "[new finds]"

def test_input_emoji_prefix_is_exempt():
    assert propose("🧸 inbox", "input") is None

def test_whitespace_is_stripped_before_judging():
    assert propose("  BEACH VIBES  ", "home") is None
    assert propose("  new finds  ", "input") == "[new finds]"


# ---- violations ------------------------------------------------------------

PLAYLISTS = [
    {"id": "h1", "name": "beach vibes", "editable": True},
    {"id": "h2", "name": "ALREADY FINE", "editable": True},
    {"id": "i1", "name": "new finds", "editable": True},
    {"id": "i2", "name": "[inbox]", "editable": True},
    {"id": "x1", "name": "someone elses", "editable": False},
    {"id": "u1", "name": "unmarked lowercase", "editable": True},
]

def test_violations_flags_only_marked_editable_nonconforming():
    rows = violations(PLAYLISTS, input_ids={"i1", "i2"}, home_ids={"h1", "h2", "x1"})
    assert rows == [
        {"playlist_id": "h1", "current": "beach vibes",
         "proposed": "BEACH VIBES", "rule": "homes are ALL CAPS"},
        {"playlist_id": "i1", "current": "new finds",
         "proposed": "[new finds]", "rule": "inputs are [bracketed]"},
    ]

def test_violations_input_role_wins_over_home():
    # Same precedence as /api/playlists: input first.
    both = [{"id": "b1", "name": "double marked", "editable": True}]
    rows = violations(both, input_ids={"b1"}, home_ids={"b1"})
    assert rows[0]["proposed"] == "[double marked]"

def test_violations_empty_when_everything_conforms():
    ok = [{"id": "h2", "name": "ALREADY FINE", "editable": True}]
    assert violations(ok, input_ids=set(), home_ids={"h2"}) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_naming.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sortify.naming'`

- [ ] **Step 3: Write the implementation**

Create `sortify/naming.py`:

```python
"""Naming-convention rules for playlists.

House conventions (design doc 2026-08-20): Home playlists are ALL CAPS,
input playlists are [bracketed], and an emoji prefix marks a derived
super/subset playlist that is exempt from both. Pure functions over the
cached listing shape — no store, no network, so checking costs nothing.

Folder rules ({…} subset folders) are deferred: the Web API cannot rename
folders, so when they arrive they will be flag-only rows. `violations`
already returns per-row `rule` strings, so a new rule type is one more
branch here, not a new shape.
"""

from __future__ import annotations

import re

from .folders import _is_caps, starts_with_emoji


def propose(name: str, role: str, input_pattern: str | None = None) -> str | None:
    """The conforming form of `name` under `role`, or None when nothing
    needs to change: already conforming, emoji-exempt, or a no-op rename
    (a caps-rule name with no letters to uppercase)."""
    s = name.strip()
    if not s or starts_with_emoji(s):
        return None
    if role == "home":
        if _is_caps(s):
            return None
        proposed = s.upper()
        return proposed if proposed != s else None
    if role == "input":
        pattern = input_pattern or r"^\[.+\]$"
        if re.fullmatch(pattern, s):
            return None
        return f"[{s}]"
    return None


def violations(playlists: list[dict], input_ids: set[str], home_ids: set[str],
               input_pattern: str | None = None) -> list[dict]:
    """Naming violations among the user's own marked playlists.

    Input beats home when both are marked — the same precedence
    /api/playlists uses when it labels roles.
    """
    out = []
    for p in playlists:
        if not p.get("editable"):
            continue
        role = ("input" if p["id"] in input_ids
                else "home" if p["id"] in home_ids
                else None)
        if role is None:
            continue
        proposed = propose(p["name"], role, input_pattern)
        if proposed is not None:
            out.append({
                "playlist_id": p["id"],
                "current": p["name"],
                "proposed": proposed,
                "rule": ("inputs are [bracketed]" if role == "input"
                         else "homes are ALL CAPS"),
            })
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_naming.py -q`
Expected: PASS (all tests)

- [ ] **Step 5: Run the whole suite, then commit**

Run: `.venv/bin/pytest -q` — expected: green.

```bash
git add sortify/naming.py tests/test_naming.py
git commit -m "feat: pure naming-convention rules (homes CAPS, inputs bracketed)"
```

---

### Task 2: `GET /api/naming` endpoint

**Files:**
- Modify: `sortify/app.py` (add endpoint near the playlists/config section, after `ingest_folders` around line 270; add `from .naming import violations` to the existing import block)
- Test: `tests/test_naming_api.py`

**Interfaces:**
- Consumes: `naming.violations(playlists, input_ids, home_ids, input_pattern)` from Task 1; existing `_effective_input_ids(cfg, playlists)` (app.py:296), `store.config()`, `sp.my_playlists()`.
- Produces: `GET /api/naming` → `{"violations": [{"playlist_id", "current", "proposed", "rule"}, …]}`. Zero Spotify calls (cached listing only).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_naming_api.py`:

```python
"""The naming endpoints, network faked out.

As with the split API tests, the call budget is part of the contract:
GET /api/naming must be free (cached listing only), and a rename must be
exactly one PUT through the client.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod

LISTING = [
    {"id": "h1", "name": "beach vibes", "owner": "me", "editable": True,
     "total": 40, "snapshot_id": "s-h1", "image": None},
    {"id": "i1", "name": "new finds", "owner": "me", "editable": True,
     "total": 10, "snapshot_id": "s-i1", "image": None},
    {"id": "ok", "name": "ALREADY FINE", "owner": "me", "editable": True,
     "total": 5, "snapshot_id": "s-ok", "image": None},
]


@pytest.fixture()
def client(monkeypatch):
    calls = {"refreshes": 0}

    def fake_my_playlists(refresh=False):
        if refresh:
            calls["refreshes"] += 1
        return [dict(p) for p in LISTING]

    monkeypatch.setattr(appmod.sp, "my_playlists", fake_my_playlists)
    appmod.store.update_config(input_ids=["i1"], home_ids=["h1", "ok"])
    c = TestClient(appmod.app)
    c.calls = calls
    return c


def test_naming_lists_violations(client):
    resp = client.get("/api/naming")
    assert resp.status_code == 200
    rows = resp.json()["violations"]
    assert {r["playlist_id"]: r["proposed"] for r in rows} == {
        "h1": "BEACH VIBES", "i1": "[new finds]",
    }


def test_naming_never_refreshes_the_listing(client):
    client.get("/api/naming")
    assert client.calls["refreshes"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_naming_api.py -q`
Expected: FAIL — 404 on `/api/naming` (both tests)

- [ ] **Step 3: Implement the endpoint**

In `sortify/app.py`, extend the existing `from .naming import …`-style import block (there is a run of `from .xxx import` lines near the top — add alongside them):

```python
from .naming import violations as naming_violations
```

Add after `ingest_folders` (before `set_config`):

```python
@app.get("/api/naming")
def naming():
    """Naming-convention violations among marked playlists. Free: reads the
    cached listing, so it can run on every Playlists-view open."""
    cfg = store.config()
    items = sp.my_playlists()
    inputs = _effective_input_ids(cfg, items)
    return {"violations": naming_violations(
        items, inputs, set(cfg.get("home_ids") or []),
        cfg.get("input_name_pattern"),
    )}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_naming_api.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite, then commit**

Run: `.venv/bin/pytest -q` — expected: green.

```bash
git add sortify/app.py tests/test_naming_api.py
git commit -m "feat: GET /api/naming lists naming violations from the cached listing"
```

---

### Task 3: Rename — `SpotifyClient.rename_playlist` + `POST /api/naming/{playlist_id}/rename`

**Files:**
- Modify: `sortify/spotify.py` (add method in the `# ---- playlists` section, near `forget_playlists` around line 521)
- Modify: `sortify/app.py` (add endpoint next to `GET /api/naming` from Task 2)
- Test: `tests/test_naming_api.py` (extend)

**Interfaces:**
- Consumes: `SpotifyClient.request(method, path, **kwargs)` (spotify.py:410), module-level `_LIST_LOCK` in spotify.py, `naming_violations` import from Task 2.
- Produces:
  - `SpotifyClient.rename_playlist(playlist_id: str, name: str) -> None` — one `PUT /playlists/{id}` `{"name": …}` through `request`, then patches the cached listing's name under `_LIST_LOCK`.
  - `POST /api/naming/{playlist_id}/rename` (no body) → `{"renamed": {"playlist_id", "from", "to"}}`; 409 when that playlist has no current violation (already conforming, already renamed, unmarked, or not owned).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_naming_api.py`:

```python
def test_rename_applies_the_proposal(client, monkeypatch):
    renamed = []
    monkeypatch.setattr(appmod.sp, "rename_playlist",
                        lambda pid, name: renamed.append((pid, name)))
    resp = client.post("/api/naming/h1/rename")
    assert resp.status_code == 200
    assert renamed == [("h1", "BEACH VIBES")]
    assert resp.json()["renamed"] == {
        "playlist_id": "h1", "from": "beach vibes", "to": "BEACH VIBES"}


def test_rename_conforming_playlist_is_409(client, monkeypatch):
    # A stale tab must not rename something already fixed: the server
    # recomputes from the cached listing and refuses when nothing is wrong.
    monkeypatch.setattr(appmod.sp, "rename_playlist",
                        lambda pid, name: pytest.fail("must not spend a call"))
    assert client.post("/api/naming/ok/rename").status_code == 409
    assert client.post("/api/naming/nonexistent/rename").status_code == 409


def test_rename_playlist_patches_the_cached_listing(monkeypatch):
    # The client method itself: one PUT, then the cached name changes so
    # the UI shows the result without a paid Refresh.
    put = []
    monkeypatch.setattr(
        appmod.sp, "request",
        lambda method, path, **kw: put.append((method, path, kw.get("json"))))
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 1.0, "items": [
        {"id": "h1", "name": "beach vibes", "owner": "me", "editable": True,
         "total": 40, "snapshot_id": "s-h1", "image": None}]}
    appmod.store.save_cache(cache)

    appmod.sp.rename_playlist("h1", "BEACH VIBES")

    assert put == [("PUT", "/playlists/h1", {"name": "BEACH VIBES"})]
    items = appmod.store.cache()["playlist_list"]["items"]
    assert items[0]["name"] == "BEACH VIBES"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_naming_api.py -q`
Expected: FAIL — the two endpoint tests 404/405, `rename_playlist` raises `AttributeError`

- [ ] **Step 3: Implement the client method**

In `sortify/spotify.py`, after `forget_playlists`:

```python
    def rename_playlist(self, playlist_id: str, name: str) -> None:
        """One PUT, then patch the cached listing so the new name is visible
        without a paid Refresh. Same lock as the refresh path; a refresh
        landing in between still wins, and correctly so."""
        self.request("PUT", f"/playlists/{playlist_id}", json={"name": name})
        with _LIST_LOCK:
            cache = self.store.cache()
            entry = cache.get("playlist_list")
            if not entry or entry.get("items") is None:
                return
            for p in entry["items"]:
                if p["id"] == playlist_id:
                    p["name"] = name
            self.store.save_cache(cache)
```

- [ ] **Step 4: Implement the endpoint**

In `sortify/app.py`, directly after the `naming()` endpoint:

```python
@app.post("/api/naming/{playlist_id}/rename")
def apply_naming_rename(playlist_id: str):
    """Apply one approved rename — exactly one Spotify call.

    The proposal is recomputed here from the cached listing rather than
    trusted from the client: a stale tab posting an old violation gets a
    409, not a rename based on a name that no longer exists.
    """
    cfg = store.config()
    items = sp.my_playlists()
    inputs = _effective_input_ids(cfg, items)
    rows = naming_violations(items, inputs, set(cfg.get("home_ids") or []),
                             cfg.get("input_name_pattern"))
    row = next((r for r in rows if r["playlist_id"] == playlist_id), None)
    if row is None:
        raise HTTPException(
            409, "that playlist has no naming issue any more — the list was "
                 "stale. Reopen the Playlists view to see the current state.")
    sp.rename_playlist(playlist_id, row["proposed"])
    return {"renamed": {"playlist_id": playlist_id,
                        "from": row["current"], "to": row["proposed"]}}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_naming_api.py -q`
Expected: PASS (all tests in the file)

- [ ] **Step 6: Run the whole suite, then commit**

Run: `.venv/bin/pytest -q` — expected: green.

```bash
git add sortify/spotify.py sortify/app.py tests/test_naming_api.py
git commit -m "feat: apply approved naming renames, one PUT each, cache patched in place"
```

---

### Task 4: Naming panel on the Playlists view

**Files:**
- Modify: `sortify/static/index.html` (inside `#view-lists`, after the `#pl-orphan-bar` div, ~line 94)
- Modify: `sortify/static/app.js` (new section after the orphan-bar code, ~line 283; one call added in `loadLists`)

**Interfaces:**
- Consumes: `GET /api/naming` (Task 2), `POST /api/naming/{id}/rename` (Task 3), existing helpers `api()`, `esc()`, `toast()`, `$()`, and `loadLists()` (app.js:133).
- Produces: a collapsible banner `#pl-naming-bar` ("N naming issue(s)" + Show/Hide toggle) and list `#pl-naming-list` with per-row `current → proposed` and a "Rename (1 call)" button. Hidden entirely when there are no violations.

- [ ] **Step 1: Add the markup**

In `sortify/static/index.html`, directly after the `#pl-orphan-bar` div (after line 94):

```html
    <div id="pl-naming-bar" class="row sitting-banner" hidden>
      <span id="pl-naming-status" class="hint"></span>
      <button id="btn-naming-toggle">Show</button>
    </div>
    <div id="pl-naming-list" hidden></div>
```

- [ ] **Step 2: Add the behavior**

In `sortify/static/app.js`, first add one line inside `loadLists()`'s `try` block, right after `renderOrphans(data.sitting_orphans || []);` (line 145):

```js
    loadNaming();
```

Then add a new section after the `btn-clean-sittings` handler (after line 283), before the `btn-refresh-lists` handler:

```js
// Naming-convention violations: computed server-side from the cached
// listing (zero Spotify calls), so checking on every view open is free.
// Renames are approved one at a time — never bulk — and each states its
// price on the button, like every other spending control here.
let namingOpen = false;
async function loadNaming() {
  let rows = [];
  try {
    rows = (await api("/api/naming")).violations || [];
  } catch (_) { /* a broken check must not break the Playlists view */ }
  const bar = $("pl-naming-bar");
  bar.hidden = rows.length === 0;
  if (!rows.length) { $("pl-naming-list").hidden = true; namingOpen = false; return; }
  $("pl-naming-status").textContent =
    `${rows.length} naming issue${rows.length === 1 ? "" : "s"}`;
  $("btn-naming-toggle").textContent = namingOpen ? "Hide" : "Show";
  $("btn-naming-toggle").onclick = () => { namingOpen = !namingOpen; renderNaming(rows); };
  renderNaming(rows);
}

function renderNaming(rows) {
  const list = $("pl-naming-list");
  list.hidden = !namingOpen;
  $("btn-naming-toggle").textContent = namingOpen ? "Hide" : "Show";
  if (!namingOpen) return;
  list.innerHTML = "";
  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "pl-row";
    row.innerHTML = `
      <div class="pl-meta">
        <div class="name">${esc(r.current)} → ${esc(r.proposed)}</div>
        <div class="sub">${esc(r.rule)}</div>
      </div>
      <button class="rename-btn">Rename (1 call)</button>`;
    const btn = row.querySelector("button");
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const res = await api(`/api/naming/${r.playlist_id}/rename`, {});
        toast(`renamed to ${res.renamed.to}`);
        await loadLists();   // re-reads cache + naming; the fixed row disappears
      } catch (e) {
        toast(e.message);
        btn.disabled = false;
      }
    };
    list.appendChild(row);
  }
}
```

- [ ] **Step 3: Verify — suite and UI harness**

Run: `.venv/bin/pytest -q`
Expected: green (cache-busting stamps come from file mtimes automatically — no version to bump).

Run: `node tests/ui_harness.mjs`
Expected: exit 0 — the stub DOM auto-creates elements by id, but this confirms app.js still parses and the pinned regressions hold. (`loadNaming` is only invoked from `loadLists`, so no scripted route is needed; if the harness's fetch router rejects an unscripted `/api/naming`, the `catch (_)` swallow keeps checks passing.)

- [ ] **Step 4: Commit**

```bash
git add sortify/static/index.html sortify/static/app.js
git commit -m "feat: Naming panel on the Playlists view — per-row approved renames"
```

---

### Task 5: Live verification (user-gated, spends real calls)

**Files:** none — deployment and a hand check.

- [ ] **Step 1: Check the budget and restart the service**

Run: `.venv/bin/spx budget` — state the numbers to the user.

```bash
systemctl --user restart sortify
```

- [ ] **Step 2: Hand over to the user**

Opening the Playlists view costs nothing new (`/api/naming` reads the cache). Report to the user: how many violations the panel shows against their real listing, and that each Rename button costs one call. **Do not click Rename yourself** — every rename is the user's approval, per the design. Stop here and let them drive.
