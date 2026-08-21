# Create Home Playlists Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create home playlists from inside sortify — one Spotify call, role sticky across folder ingests, visible and usable without a Refresh.

**Architecture:** A pure name-validation function in `folders.py`; a zero-call `remember_playlist` inverse of `forget_playlists` in `spotify.py`; a `POST /api/playlists/create` endpoint in `app.py` that creates, remembers, seeds the track cache (the snapshot trap), marks the id in both `home_ids` and a new `sticky_home_ids`, and clears the profile cache; `/api/folders` unions sticky ids in, `/api/config` intersects them out. Two UI entry points: a row in the Playlists view and a no-match creation row in the Add-to… picker.

**Tech Stack:** Python/FastAPI, pytest (zero-network fakes), vanilla JS with no build step.

**Spec:** `docs/superpowers/specs/2026-08-21-create-home-playlists-design.md`

## Global Constraints

- **Every test is zero-Spotify-call.** Fake transports and monkeypatched clients only; `tests/conftest.py` already isolates the data dir and account ledger — never bypass it.
- **Budget:** 1 call per created home; 2 from the picker with a track. Nothing bulk, nothing background, nothing on the polling path.
- **Feb-2026 dev-mode API shapes:** `items`/`item`, `/me/library`, no batch endpoints. `create_playlist` posts to `/me/playlists`.
- **Never probe the live API to answer a design question.** The create response's `snapshot_id` presence is settled by logging on the first *real* (user-initiated) creation, not by a test call.
- **No JS toolchain.** Frontend has no unit-test framework; `tests/ui_harness.mjs` is a hand-run diagnostic. Frontend changes verify by hand against the running server.
- Run the whole suite with `.venv/bin/pytest -q`; keep it green at every commit.
- House copy rule: every control that spends Spotify calls states its price on the control itself (`Create (1 call)`).

---

### Task 1: Name validation (pure function)

A home whose name matches `input_name_pattern` would be re-read as an **input** by `_effective_input_ids` on the next request (pattern union beats the config list); a name matching `home_name_exclude_patterns` or carrying an emoji prefix is dropped from homes by every folder ingest. Both must be refused *before* any call is spent.

**Files:**
- Modify: `sortify/folders.py` (add one function after `home_name_excluded`, ~line 33)
- Test: `tests/test_create_home.py` (new file; holds all tests for this feature)

**Interfaces:**
- Consumes: existing `home_name_excluded(name, patterns, emoji) -> bool` in `sortify/folders.py`.
- Produces: `creatable_home_name_problem(name: str, input_pattern: str | None, exclude_patterns: list[str], exclude_emoji: bool) -> str | None` — `None` means the name is fine; a string is the user-facing refusal.

- [ ] **Step 1: Write the failing tests**

```python
"""Creating home playlists from inside sortify.

Spec: docs/superpowers/specs/2026-08-21-create-home-playlists-design.md.
Everything here is zero-Spotify-call: fake transports and monkeypatched
clients only.
"""

from sortify.folders import creatable_home_name_problem

INPUT_PAT = r"^\[.+\]$"
EXCLUDES = [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"]


def problem(name):
    return creatable_home_name_problem(
        name, input_pattern=INPUT_PAT, exclude_patterns=EXCLUDES, exclude_emoji=True
    )


def test_ordinary_names_are_creatable():
    for name in ("Late Night", "HAZE 2", "Ærlig talt", "  padded  "):
        assert problem(name) is None, name


def test_input_shaped_names_are_refused_as_would_be_inputs():
    # The pattern union in _effective_input_ids beats the home_ids config
    # list, so "[Foo]" would become an input on the very next request.
    msg = problem("[Foo]")
    assert msg and "input" in msg


def test_home_excluded_shapes_are_refused():
    for name in ("{alle sanger}", "<motor>", "__start__", "🐾 subset", "🔈 haze"):
        assert problem(name), name


def test_empty_and_whitespace_names_are_refused():
    assert problem("")
    assert problem("   ")


def test_no_input_pattern_configured_skips_that_check():
    assert creatable_home_name_problem(
        "[Foo]", input_pattern=None, exclude_patterns=EXCLUDES, exclude_emoji=True
    ) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_create_home.py -v`
Expected: FAIL — `ImportError: cannot import name 'creatable_home_name_problem'`

- [ ] **Step 3: Implement in `sortify/folders.py`**

Place directly after `home_name_excluded`:

```python
def creatable_home_name_problem(
    name: str, input_pattern: str | None, exclude_patterns: list[str], exclude_emoji: bool
) -> str | None:
    """Why `name` cannot become a home playlist, or None if it can.

    Checked before the create call is spent: an input-shaped name would be
    re-read as an input by the pattern union on the very next request, and a
    home-excluded shape would be dropped from homes by every folder ingest —
    both would silently produce something other than what was asked for.
    """
    s = name.strip()
    if not s:
        return "the name is empty"
    if input_pattern and re.fullmatch(input_pattern, s):
        return (
            f"{s!r} matches the input name pattern — it would become an "
            "input, not a home"
        )
    if home_name_excluded(s, exclude_patterns, exclude_emoji):
        return (
            f"{s!r} has a shape that is excluded from homes "
            "(emoji prefix, or a marker like {…}, <…>, __…__)"
        )
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_create_home.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the whole suite, then commit**

Run: `.venv/bin/pytest -q` — expected all green.

```bash
git add sortify/folders.py tests/test_create_home.py
git commit -m "feat: refuse home names that would be re-read as inputs or excluded shapes"
```

---

### Task 2: `remember_playlist` and `create_playlist_full` (spotify.py)

`forget_playlists` exists so the cached listing stops advertising removed playlists; this is its inverse for created ones. Also: the endpoint needs the create response's `snapshot_id` if the Feb-2026 API sends one, but `create_playlist`'s three existing callers (sittings, materialiser) only want the id — so a `_full` variant returns the pair and `create_playlist` delegates to it, leaving those callers untouched.

**Files:**
- Modify: `sortify/spotify.py` (`create_playlist` ~line 680; new `remember_playlist` next to `forget_playlists` ~line 516)
- Test: `tests/test_create_home.py` (append)

**Interfaces:**
- Consumes: `_LIST_LOCK`, `self.store.cache()` / `save_cache`, the listing-item shape produced by `_fetch_my_playlists` (`id, name, owner, editable, total, snapshot_id, image, description`).
- Produces:
  - `Spotify.remember_playlist(item: dict) -> None` — zero calls; inserts `item` at the top of the cached listing; no-op if no listing is cached or the id is already present.
  - `Spotify.create_playlist_full(name: str, description: str = "", bulk: bool = False) -> tuple[str, str | None]` — one call; returns `(playlist_id, snapshot_id_or_None)`; raises `SpotifyError(502, ...)` on a response with no id (unchanged behaviour).
  - `Spotify.create_playlist(...)` keeps its exact current signature and return type (delegates to `_full`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_create_home.py`. Reuse the fake-transport style of `tests/test_playlist_cache.py`:

```python
import pytest

from sortify.spotify import Spotify
from sortify.store import Store


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.status_code = status
        self.content = b"{}"
        self.headers = {}
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def sp(tmp_path, monkeypatch):
    client = Spotify(Store(tmp_path))
    monkeypatch.setattr(client, "_access_token", lambda: "token")
    monkeypatch.setattr(client, "_last_call", 0.0)
    client.store.save_cache({
        "playlists": {}, "artists": {}, "me": {"id": "me"},
        "playlist_list": {"fetched_at": 1.0, "items": [
            {"id": "old1", "name": "Existing", "owner": "me", "editable": True,
             "total": 3, "snapshot_id": "snap-old1", "image": None, "description": ""},
        ]},
    })
    return client


def item(pid, name="New Home"):
    return {"id": pid, "name": name, "owner": "me", "editable": True,
            "total": 0, "snapshot_id": f"created:{pid}", "image": None,
            "description": ""}


def test_remember_playlist_appears_in_listing_with_no_http(sp, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("remember_playlist must never touch the network")
    monkeypatch.setattr(sp.http, "request", boom)
    sp.remember_playlist(item("new1"))
    ids = [p["id"] for p in sp.my_playlists()]
    assert ids == ["new1", "old1"]


def test_remember_then_forget_round_trips(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    sp.remember_playlist(item("new1"))
    sp.forget_playlists({"new1"})
    assert [p["id"] for p in sp.my_playlists()] == ["old1"]


def test_remember_is_idempotent(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    sp.remember_playlist(item("new1"))
    sp.remember_playlist(item("new1"))
    assert [p["id"] for p in sp.my_playlists()].count("new1") == 1


def test_remember_without_a_cached_listing_is_a_noop(tmp_path, monkeypatch):
    client = Spotify(Store(tmp_path))
    client.remember_playlist(item("new1"))  # must not raise, must not fetch
    assert (client.store.cache().get("playlist_list") or {}) in ({}, None) or \
        client.store.cache()["playlist_list"] is None


def test_create_playlist_full_returns_id_and_snapshot(sp, monkeypatch):
    sent = []
    def fake_request(method, url, **kwargs):
        sent.append((method, url))
        return FakeResponse({"id": "fresh", "snapshot_id": "snap-1"}, status=201)
    monkeypatch.setattr(sp.http, "request", fake_request)
    assert sp.create_playlist_full("Late Night") == ("fresh", "snap-1")
    assert sent == [("POST", "https://api.spotify.com/v1/me/playlists")]


def test_create_playlist_full_snapshot_is_none_when_absent(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "request",
                        lambda *a, **k: FakeResponse({"id": "fresh"}, status=201))
    assert sp.create_playlist_full("Late Night") == ("fresh", None)


def test_create_playlist_still_returns_bare_id(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "request",
                        lambda *a, **k: FakeResponse({"id": "fresh"}, status=201))
    assert sp.create_playlist("Late Night") == "fresh"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_create_home.py -v`
Expected: Task-1 tests PASS; new ones FAIL with `AttributeError: ... 'remember_playlist'` / `'create_playlist_full'`.

- [ ] **Step 3: Implement in `sortify/spotify.py`**

Rework `create_playlist` (keep its docstring's spirit and the loud no-id failure):

```python
    def create_playlist_full(
        self, name: str, description: str = "", bulk: bool = False
    ) -> tuple[str, str | None]:
        """Create a playlist; return (id, snapshot_id or None). One call.

        snapshot_id's presence in the Feb-2026 create response is unverified
        (spec §3) — callers that get None seed a sentinel instead. The full
        key list is logged so the first real creation settles the question
        without ever probing for it.
        """
        resp = self.request(
            "POST", "/me/playlists",
            json={"name": name, "description": description, "public": False},
            bulk=bulk,
        )
        playlist_id = (resp or {}).get("id")
        if not playlist_id:
            # A 200/201 with no id would otherwise flow straight into
            # add_to_playlist as f"/playlists/{None}/items" — a confusing
            # 404 far from the actual problem. The call already happened and
            # spent budget either way; fail loudly at the source instead.
            raise SpotifyError(502, "playlist creation returned no id")
        log.info("create response keys: %s", sorted(resp))
        return playlist_id, resp.get("snapshot_id")

    def create_playlist(self, name: str, description: str = "", bulk: bool = False) -> str:
        """Create a playlist and return its id. One call."""
        return self.create_playlist_full(name, description, bulk=bulk)[0]
```

(If `spotify.py` has no module logger, add `log = logging.getLogger(__name__)` beside its imports.)

Add next to `forget_playlists`:

```python
    def remember_playlist(self, item: dict) -> None:
        """Insert a just-created playlist into the cached listing.

        The inverse of forget_playlists, and for the inverse reason: the
        listing is only re-read on an explicit Refresh (~21 calls), so
        without this a playlist created app-side is invisible — and
        unusable — until the user pays for one. Costs nothing and touches
        no network. `item` must be shaped exactly as _fetch_my_playlists
        produces it, so nothing downstream can tell the difference.

        Same lock and same loser as forget_playlists: a refresh landing
        between this read and write wins, and correctly so — it has just
        asked Spotify what actually exists.
        """
        with _LIST_LOCK:
            cache = self.store.cache()
            entry = cache.get("playlist_list")
            if not entry or entry.get("items") is None:
                return  # nothing cached to keep current
            if any(p.get("id") == item["id"] for p in entry["items"]):
                return
            entry["items"].insert(0, item)
            self.store.save_cache(cache)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_create_home.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite, then commit**

Run: `.venv/bin/pytest -q` — the existing sitting/materialiser tests exercise `create_playlist` and must stay green.

```bash
git add sortify/spotify.py tests/test_create_home.py
git commit -m "feat: remember_playlist + create_playlist_full (listing stays current at zero calls)"
```

---

### Task 3: Sticky home ids (`/api/folders` union, `/api/config` intersection)

`/api/folders` re-derives `home_ids` from the desktop folder tree; an app-created playlist has no folder path, so today's ingest would silently demote it. New config key `sticky_home_ids`: the ingest unions it in (after the same `& editable - inputs` filters), and `/api/config` intersects it with the saved `home_ids` so switching Home off actually demotes instead of resurrecting on the next ingest.

**Files:**
- Modify: `sortify/app.py` (`ingest_folders` ~line 182; `set_config` ~line 224)
- Test: `tests/test_create_home.py` (append)

**Interfaces:**
- Consumes: config key `sticky_home_ids: list[str]` (absent means `[]`; Task 4 is its writer).
- Produces: ingest result `home_ids` ⊇ (sticky ∩ editable − inputs); `/api/config` persists `sticky_home_ids = sticky ∩ body.home_ids`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_create_home.py`. App-level tests use the module-scope app with a fake client, like `tests/test_playback.py`'s `appmod` fixture:

```python
from fastapi.testclient import TestClient

from sortify import app as appmod


@pytest.fixture
def client(monkeypatch):
    return TestClient(appmod.app, raise_server_exceptions=False)


LISTING = [
    {"id": "tree1", "name": "Hazy", "owner": "me", "editable": True,
     "total": 3, "snapshot_id": "s1", "image": None, "description": ""},
    {"id": "made1", "name": "Late Night", "owner": "me", "editable": True,
     "total": 0, "snapshot_id": "created:made1", "image": None, "description": ""},
]

TREE = {"type": "folder", "children": [
    {"type": "folder", "name": "ROOT", "children": [
        {"type": "playlist", "uri": "spotify:playlist:tree1"}]},
]}


def _seed_config(**extra):
    appmod.store.save_config({
        "client_id": "x", "input_ids": [], "home_ids": [],
        "home_folder_prefixes": ["ROOT"], "home_folder_exclude": [],
        "input_name_pattern": r"^\[.+\]$",
        "home_exclude_emoji_names": True,
        "home_name_exclude_patterns": [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"],
        **extra,
    })


def test_folder_ingest_keeps_sticky_homes_the_tree_never_saw(client, monkeypatch):
    _seed_config(home_ids=["made1"], sticky_home_ids=["made1"])
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    res = client.post("/api/folders", json=TREE)
    assert res.status_code == 200
    assert sorted(appmod.store.config()["home_ids"]) == ["made1", "tree1"]


def test_folder_ingest_still_filters_sticky_by_editable_and_inputs(client, monkeypatch):
    _seed_config(sticky_home_ids=["ghost", "made1"], input_ids=["made1"])
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    client.post("/api/folders", json=TREE)
    # "ghost" is not in the listing (not editable), "made1" is an input now.
    assert appmod.store.config()["home_ids"] == ["tree1"]


def test_switching_home_off_also_drops_sticky_so_ingest_cannot_resurrect(client, monkeypatch):
    _seed_config(home_ids=["made1", "tree1"], sticky_home_ids=["made1"])
    res = client.post("/api/config", json={
        "input_ids": [], "home_ids": ["tree1"], "home_hints": {}})
    assert res.status_code == 200
    assert appmod.store.config()["sticky_home_ids"] == []
    monkeypatch.setattr(appmod.sp, "my_playlists", lambda refresh=False: LISTING)
    client.post("/api/folders", json=TREE)
    assert appmod.store.config()["home_ids"] == ["tree1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_create_home.py -v -k "sticky or ingest or resurrect"`
Expected: FAIL — ingest drops `made1`; `/api/config` leaves `sticky_home_ids` untouched.

- [ ] **Step 3: Implement in `sortify/app.py`**

In `ingest_folders`, immediately after `home_ids = sorted((chosen & editable) - inputs)`:

```python
    # Homes created inside sortify have no folder path yet, so the tree
    # cannot see them. Union them back in (through the same editable/input
    # filters) — the tree keeps authority over playlists it can see, it just
    # stops deleting knowledge it never had. (Spec §2.)
    sticky = {s for s in (cfg.get("sticky_home_ids") or [])
              if s in editable and s not in inputs}
    home_ids = sorted(set(home_ids) | sticky)
```

In `set_config`, extend the `store.update_config(...)` call:

```python
    store.update_config(
        input_ids=body.input_ids, home_ids=body.home_ids,
        home_hints={k: v.strip() for k, v in body.home_hints.items() if v.strip()},
        # A sticky role must still be revocable: Home toggled off in the
        # Playlists view drops the id here too, or the next folder ingest
        # would resurrect it. (Spec §2.)
        sticky_home_ids=sorted(
            set(store.config().get("sticky_home_ids") or []) & set(body.home_ids)
        ),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_create_home.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite, then commit**

Run: `.venv/bin/pytest -q` — expected green (existing `/api/folders` and `/api/config` tests must not notice the new key).

```bash
git add sortify/app.py tests/test_create_home.py
git commit -m "feat: sticky_home_ids — app-created homes survive folder ingests, Home-off still demotes"
```

---

### Task 4: `POST /api/playlists/create` (endpoint + seeded snapshot + leak guard)

The endpoint: validate (zero calls) → create (1 call) → remember in the listing → seed the track cache with a matching non-empty `snapshot_id` → mark `home_ids` + `sticky_home_ids` → clear `_profile_state`. The seeded snapshot is the trap the spec names: `_cached_tracks` refetches on any falsy/mismatched snapshot, which for a known-empty playlist means one wasted call every profile rebuild, forever. The leak-guard test pins it.

**Files:**
- Modify: `sortify/app.py` (new model beside `ConfigIn` ~line 68; new endpoint after `set_config` ~line 236; import `creatable_home_name_problem` from `.folders`)
- Test: `tests/test_create_home.py` (append)

**Interfaces:**
- Consumes: `creatable_home_name_problem` (Task 1), `sp.create_playlist_full` / `sp.remember_playlist` (Task 2), `sticky_home_ids` semantics (Task 3), existing `_profile_state`, `store`, `LIKED_ID`-free listing shape.
- Produces: `POST /api/playlists/create`, body `{"name": str, "role": "home"}` →
  `{"playlist": {…listing item…, "role": "home", "folder": null, "split": null, "hints": ""}, "note": str | null}`. 400 on bad role or refused name.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_create_home.py`:

```python
def _wire_create(monkeypatch, snapshot="snap-new"):
    """Fake the two client methods the endpoint spends/uses; count creates."""
    calls = {"create": 0}
    def fake_full(name, description="", bulk=False):
        calls["create"] += 1
        return "made1", snapshot
    monkeypatch.setattr(appmod.sp, "create_playlist_full", fake_full)
    monkeypatch.setattr(appmod.sp, "my_playlists",
                        lambda refresh=False: list(LISTING[:1]))
    return calls


def _seed_cache_with_listing():
    appmod.store.save_cache({
        "playlists": {}, "artists": {}, "me": {"id": "me"},
        "playlist_list": {"fetched_at": 1.0, "items": list(LISTING[:1])},
    })


def test_create_refuses_bad_names_before_spending(client, monkeypatch):
    _seed_config()
    calls = _wire_create(monkeypatch)
    for bad in ("[Foo]", "{x}", "🐾 sub", "  "):
        res = client.post("/api/playlists/create", json={"name": bad, "role": "home"})
        assert res.status_code == 400, bad
    assert calls["create"] == 0


def test_create_refuses_non_home_roles(client, monkeypatch):
    _seed_config()
    calls = _wire_create(monkeypatch)
    res = client.post("/api/playlists/create", json={"name": "Ok", "role": "input"})
    assert res.status_code == 400
    assert calls["create"] == 0


def test_create_marks_home_and_sticky_and_seeds_the_track_cache(client, monkeypatch):
    _seed_config()
    _seed_cache_with_listing()
    _wire_create(monkeypatch, snapshot="snap-new")
    res = client.post("/api/playlists/create", json={"name": "Late Night", "role": "home"})
    assert res.status_code == 200
    cfg = appmod.store.config()
    assert "made1" in cfg["home_ids"] and "made1" in cfg["sticky_home_ids"]
    entry = appmod.store.cache()["playlists"]["made1"]
    assert entry["tracks"] == [] and entry["snapshot_id"] == "snap-new"
    # And the listing entry carries the same snapshot — the equality is the
    # whole point (spec §3).
    listed = next(p for p in appmod.store.cache()["playlist_list"]["items"]
                  if p["id"] == "made1")
    assert listed["snapshot_id"] == "snap-new"
    assert res.json()["playlist"]["role"] == "home"


def test_create_without_a_response_snapshot_seeds_the_sentinel(client, monkeypatch):
    _seed_config()
    _seed_cache_with_listing()
    _wire_create(monkeypatch, snapshot=None)
    client.post("/api/playlists/create", json={"name": "Late Night", "role": "home"})
    entry = appmod.store.cache()["playlists"]["made1"]
    listed = next(p for p in appmod.store.cache()["playlist_list"]["items"]
                  if p["id"] == "made1")
    assert entry["snapshot_id"] == listed["snapshot_id"] == "created:made1"


def test_duplicate_names_are_allowed_with_a_note(client, monkeypatch):
    _seed_config()
    _seed_cache_with_listing()
    _wire_create(monkeypatch)
    res = client.post("/api/playlists/create", json={"name": "Hazy", "role": "home"})
    assert res.status_code == 200
    assert res.json()["note"]  # "already exists" note, creation not refused


def test_create_clears_the_profile_cache(client, monkeypatch):
    _seed_config()
    _seed_cache_with_listing()
    _wire_create(monkeypatch)
    appmod._profile_state.update(built_at=9e12, profiles={"stale": None})
    client.post("/api/playlists/create", json={"name": "Late Night", "role": "home"})
    assert appmod._profile_state.get("built_at") == 0.0
    assert "profiles" not in appmod._profile_state


def test_leak_guard_created_home_costs_exactly_one_call_ever(client, monkeypatch):
    """THE test for the snapshot trap (spec §3, §6): create a home, then
    build profiles twice; total upstream spend is exactly the 1 create call.
    A falsy or mismatched seeded snapshot makes _cached_tracks refetch the
    empty playlist on every rebuild — 1 call per 10 minutes, forever."""
    _seed_config()
    _seed_cache_with_listing()
    calls = {"n": 0}

    class Resp:
        status_code = 201
        content = b"{}"
        headers = {}
        text = ""
        @staticmethod
        def json():
            return {"id": "made1"}  # deliberately NO snapshot_id → sentinel path

    def fake_request(method, url, **kwargs):
        calls["n"] += 1
        assert method == "POST" and url.endswith("/me/playlists"), (
            f"unexpected upstream call: {method} {url}")
        return Resp()

    monkeypatch.setattr(appmod.sp, "_access_token", lambda: "token")
    monkeypatch.setattr(appmod.sp, "_last_call", 0.0)
    monkeypatch.setattr(appmod.sp.http, "request", fake_request)

    res = client.post("/api/playlists/create", json={"name": "Late Night", "role": "home"})
    assert res.status_code == 200
    appmod._ensure_profiles(force=True)
    appmod._ensure_profiles(force=True)
    assert calls["n"] == 1
```

Note for the implementer: the leak guard also needs `tree1`'s tracks cached so the *other* home doesn't fetch — extend `_seed_cache_with_listing` or set `home_ids=["tree1"]` off. Simplest: seed `_seed_config(home_ids=[])` (the default above) so `tree1` is not a home; the only homes in play are the created one. Keep whichever variant makes `calls["n"] == 1` assert the created playlist alone.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_create_home.py -v -k create`
Expected: FAIL — 404 on `/api/playlists/create`.

- [ ] **Step 3: Implement in `sortify/app.py`**

Model beside `ConfigIn`:

```python
class CreatePlaylistIn(BaseModel):
    name: str
    # Only "home" exists today; explicit rather than implied because the two
    # deferred roles (inputs, subsets) differ in exactly this field. (Spec §1.)
    role: str = "home"
```

Endpoint after `set_config` (import `creatable_home_name_problem` in the existing `from .folders import ...` line):

```python
@app.post("/api/playlists/create")
def create_playlist_api(body: CreatePlaylistIn):
    """Create a home playlist from inside sortify. One Spotify call.

    Everything around the call is local bookkeeping: the listing entry
    (remember_playlist), a seeded empty track cache whose snapshot_id
    matches the listing's (else every profile rebuild refetches a playlist
    we know is empty — spec §3), the home + sticky role, and a profile
    cache clear so the new home is usable now, not in PROFILE_TTL.
    """
    if body.role != "home":
        raise HTTPException(400, f"unsupported role {body.role!r} — only homes can be created yet")
    cfg = store.config()
    problem = creatable_home_name_problem(
        body.name,
        input_pattern=cfg.get("input_name_pattern"),
        exclude_patterns=cfg.get("home_name_exclude_patterns") or [],
        exclude_emoji=bool(cfg.get("home_exclude_emoji_names")),
    )
    if problem:
        raise HTTPException(400, problem)
    name = body.name.strip()

    note = None
    if any(p["name"].strip() == name for p in sp.my_playlists()):
        note = "a playlist with this name already exists — Spotify allows duplicates, so now there are two"

    new_id, snapshot = sp.create_playlist_full(name)
    snapshot = snapshot or f"created:{new_id}"  # sentinel: only has to equal itself

    item = {
        "id": new_id, "name": name,
        "owner": (store.cache().get("me") or {}).get("id"),
        "editable": True, "total": 0, "snapshot_id": snapshot,
        "image": None, "description": "",
    }
    sp.remember_playlist(item)

    # Seed, don't fetch: we created it, it is empty by construction, and the
    # matching snapshot is what makes _cached_tracks serve this entry instead
    # of paying a call per profile rebuild to re-learn "empty".
    cache = store.cache()
    cache["playlists"][new_id] = {
        "snapshot_id": snapshot, "tracks": [], "fetched_at": time.time(),
    }
    store.save_cache(cache)

    cfg = store.config()
    store.update_config(
        home_ids=sorted(set(cfg.get("home_ids") or []) | {new_id}),
        sticky_home_ids=sorted(set(cfg.get("sticky_home_ids") or []) | {new_id}),
    )

    # Same move as set_config after a hints save, same reason: usable on the
    # next request, not up to PROFILE_TTL later.
    _profile_state.clear()
    _profile_state["built_at"] = 0.0

    return {
        "playlist": {**item, "role": "home", "folder": None, "split": None, "hints": ""},
        "note": note,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_create_home.py -v`
Expected: PASS — the leak guard's `calls["n"] == 1` is the one to watch.

- [ ] **Step 5: Run the whole suite, then commit**

Run: `.venv/bin/pytest -q` — expected green.

```bash
git add sortify/app.py tests/test_create_home.py
git commit -m "feat: POST /api/playlists/create — one call, sticky home role, seeded snapshot, no refresh needed"
```

---

### Task 5: UI — Playlists-view row and picker no-match creation

Two entry points, both stating their price (house rule). The picker path is the primary one: an empty home is never suggested (empty profile scores 0), so creating it *with* its first track is the gesture that matters.

**Files:**
- Modify: `sortify/static/index.html` (Playlists view header, ~line 87)
- Modify: `sortify/static/app.js` (`loadLists`/`renderLists` area ~line 109; `openPicker` ~line 915; `ordinaryCardBody`'s `btn-now-more` wiring ~line 827)
- Modify: `sortify/static/style.css` (one small block)

**Interfaces:**
- Consumes: `POST /api/playlists/create` (Task 4) via the existing `api()` helper; response `{playlist, note}`.
- Produces: none consumed by later tasks (this is the last one).

- [ ] **Step 1: Add the Playlists-view row (index.html)**

After the `pl-filter`/`btn-refresh-lists` row (~line 89), before `pl-age`:

```html
    <div id="pl-new-home" class="row">
      <input id="new-home-name" placeholder="New home playlist…" autocomplete="off">
      <button id="btn-new-home" title="Created in your Spotify account and marked Home here — no Refresh needed">Create (1 call)</button>
    </div>
```

- [ ] **Step 2: Wire it (app.js)**

Near the other Playlists-view handlers (after `$("btn-save-config").onclick`):

```javascript
// Creating a home from here is 1 call; the row appears in place, already
// marked Home, with no Refresh. The folder path stays blank until the next
// desktop-client folder export — not an error, homes work without one.
async function createHome(name) {
  const res = await api("/api/playlists/create", { name, role: "home" });
  const p = res.playlist;
  playlistData.unshift(p);
  roles[p.id] = "home";
  if (res.note) toast(res.note, 4000);
  return p;
}

$("btn-new-home").onclick = async () => {
  const name = $("new-home-name").value.trim();
  if (!name) return;
  const btn = $("btn-new-home");
  btn.disabled = true;
  try {
    const p = await createHome(name);
    $("new-home-name").value = "";
    renderLists();
    toast(`created home "${p.name}"`);
  } catch (e) { toast(e.message); } finally { btn.disabled = false; }
};
```

- [ ] **Step 3: Add the picker's no-match creation row (app.js)**

Extend `openPicker` with an optional third argument; only the Now card passes it, so the triage picker is unchanged:

```javascript
function openPicker(homesMap, onPick, onCreate) {
  const list = $("picker-list");
  const paint = (filter) => {
    list.innerHTML = "";
    const homes = [...homesMap.values()].sort((a, b) =>
      (a.folder || "").localeCompare(b.folder || "") || a.name.localeCompare(b.name));
    let shown = 0;
    for (const h of homes) {
      if (filter && !(h.name + " " + (h.folder || "")).toLowerCase().includes(filter)) continue;
      shown++;
      const b = document.createElement("button");
      b.className = "picker-row";
      const sub = [h.folder, h.total != null ? `${h.total} tracks` : ""].filter(Boolean).join(" · ");
      b.innerHTML = `<span class="p-name">${esc(h.name)}</span>` +
        (sub ? `<span class="p-sub">${esc(sub)}</span>` : "");
      b.onclick = () => { closePicker(); onPick(h.id); };
      list.appendChild(b);
    }
    // The moment of need: the right playlist doesn't exist yet. Create it
    // and file in one gesture — create + add, priced as such. (Spec §5.)
    if (!shown && filter && onCreate) {
      const b = document.createElement("button");
      b.className = "picker-row picker-create";
      b.innerHTML = `<span class="p-name">Create home “${esc(filter)}” and file this track there</span>` +
        `<span class="p-sub">2 calls</span>`;
      b.onclick = () => { closePicker(); onCreate($("picker-filter").value.trim()); };
      list.appendChild(b);
    }
  };
  paint("");
  $("picker-filter").value = "";
  $("picker-filter").oninput = (e) => paint(e.target.value.trim().toLowerCase());
  $("picker").hidden = false;
  $("picker-filter").focus();
}
```

(The create row uses the raw filter value for the name but the lowercased one for matching — pass `$("picker-filter").value.trim()` as shown so the created name keeps the user's casing.)

Then in `renderNow`'s wiring, change the `btn-now-more` line:

```javascript
    const more = $("btn-now-more");
    if (more) more.onclick = () => openPicker(nowState.homes, nowFile, async (name) => {
      try {
        const p = await createHome(name);
        nowState.homes.set(p.id, { id: p.id, name: p.name, image: null, total: 0, folder: null });
        await nowFile(p.id);  // lands the card in its ordinary ✓ filed state
      } catch (e) { toast(e.message); }
    });
```

- [ ] **Step 4: Style (style.css)**

```css
#pl-new-home input { flex: 1; }
.picker-create .p-name { color: var(--accent, #1db954); }
```

(Match the file's existing variable names — if there is no `--accent`, reuse whatever the suggestion buttons use.)

- [ ] **Step 5: Static sanity check (no JS toolchain)**

Run: `node --check sortify/static/app.js`
Expected: no output (parses clean).

Run: `.venv/bin/pytest -q`
Expected: green (`test_cache_busting`/`test_pwa` read the static files).

- [ ] **Step 6: Hand verification against the running server**

Zero *extra* cost beyond the feature's own price; each creation spends 1–2 real calls, so do it once, deliberately:

1. `systemctl --user restart sortify`, open the app, Playlists view.
2. Type a name, press `Create (1 call)` — the row must appear at the top, marked Home, without pressing Refresh.
3. Check the server log for `create response keys:` — **this settles spec §3**: if `snapshot_id` is listed, file a note to delete the sentinel branch; if not, the sentinel is load-bearing and stays.
4. On the Now card with a track playing, `Add to…`, type a name that matches nothing, press the create row — card must land in `✓ filed to …`.
5. `.venv/bin/spx budget` before and after — the delta must equal the creations you performed (1 + 2).

- [ ] **Step 7: Commit**

```bash
git add sortify/static/index.html sortify/static/app.js sortify/static/style.css
git commit -m "feat: create home playlists from the Playlists view and the Add-to picker"
```

---

## Self-review notes

- **Spec coverage:** §1 endpoint+validation → Tasks 1+4; §2 stickiness → Task 3; §3 remember+snapshot → Tasks 2+4 (leak guard); §4 profile clear → Task 4; §5 entry points → Task 5; §6 tests → Tasks 1–4 map one-to-one onto the spec's four groups (leak guard included). Non-goals untouched.
- **Spec §6 names `tests/test_no_proactive_work.py` as the leak guard's "neighbourhood"** — this plan puts all feature tests in one new `tests/test_create_home.py` instead, deliberately: the leak guard needs the create-endpoint fixtures, and splitting one test away from them buys nothing. The docstring on the test carries the spec reference.
- **Type consistency:** `create_playlist_full -> tuple[str, str | None]` consumed as `new_id, snapshot` (Task 4); `remember_playlist(item: dict)` item shape matches `_fetch_my_playlists` field-for-field; `createHome` (Task 5) is defined in step 2 and used in step 3.
