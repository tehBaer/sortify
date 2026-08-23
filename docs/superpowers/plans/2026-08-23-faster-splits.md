# Faster Splits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Materialise split piles with 100-track batched adds (50x fewer Spotify calls), add a top-level Split tab with a playlist picker, and name split outputs `{source} · pile`.

**Architecture:** The materialiser tick sends `missing[:100]` per call instead of one track, guarded by a reconcile-on-pickup read that rebuilds `record["added"]` from what actually landed (approach A from the design — Spotify permits duplicates, so a half-landed batch must never be blindly retried). All existing safety machinery (record-before-create, claim CAS, single-writer, readopt/abandon) is untouched. UI and naming are independent add-ons.

**Tech Stack:** Python/FastAPI, vanilla JS (no build step), pytest, `tests/ui_harness.mjs` (node, zero-dependency DOM stub).

**Spec:** `docs/superpowers/specs/2026-08-23-faster-splits-design.md` (read it first; the handoff `2026-08-23-faster-splits-handoff.md` has background).

## Global Constraints

- **Zero live Spotify calls during development.** Every test runs against fakes at the `Spotify.request` seam or method-level monkeypatches. Never call api.spotify.com; never run `.venv/bin/spx` except `spx budget`.
- **The batch limit is 100 uris per POST** (probed 2026-08-23; 150 → 400 "Too many ids requested"). Batch *add* only — there is still no batch delete.
- Tests must pass in **both orderings**: `.venv/bin/pytest -q` and `.venv/bin/pytest -q -p no:randomly $(ls -r tests/test_*.py)`, plus `node tests/ui_harness.mjs`.
- `data/queue.json` and `data/pacing.json` keep `version: 1` and every field boxdash reads: `state`, `stop_reason`, `progress.{pile_id, pile_index, pile_count, track, track_total, spent_today, bulk_today, daily_cap, reserve}`, `updated_at`; pacing `rate_per_min`, `ceiling`, `max_clean_rate`, `history_429`. No shape changes in this plan.
- **Do not restart `sortify.service` mid-plan without checking `git log` / other running sessions first** — the repo is under active development by other sessions.
- Never import `sortify.app` from a script outside pytest — it binds Store to the live `data/` dir (see memory: live-data clobber). Tests only.
- Spend classes: worker traffic is `bulk=True`; the rename action is interactive (no `bulk`). No new caps, no cap changes.
- The playlist-name cap is 100 characters; the source name always survives truncation whole.

---

### Task 1: Spotify client — batch add and bulk-aware playlist read

**Files:**
- Modify: `sortify/spotify.py:507-513` (`_paginate`), `sortify/spotify.py:666-682` (`playlist_tracks`), `sortify/spotify.py:735-739` (`add_to_playlist`)
- Test: `tests/test_spotify_batch.py` (new)

**Interfaces:**
- Produces: `Spotify.add_to_playlist(playlist_id, uris: str | list[str], bulk=False, spend_reserve=False) -> str | None` — accepts a single uri (back-compat) or a list of ≤100; raises `ValueError` on >100 **before** spending anything.
- Produces: `Spotify.playlist_tracks(playlist_id, bulk=False, spend_reserve=False) -> list[dict]` — same return shape as today; the new kwargs flow through `_paginate` → `get` → `request` so reads spend from the bulk class.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_spotify_batch.py
"""The Feb-2026 dev-mode API accepts up to 100 uris per playlist-add POST
(probed 2026-08-23 — docs/superpowers/specs/2026-08-23-faster-splits-design.md).
Pins the client's batch form and the bulk-accounting passthrough on the
paged read, at the request() seam — zero live calls."""

import pytest

import sortify.app as appmod
from liveguard import assert_not_live_data
assert_not_live_data(appmod.store.dir)

from sortify.spotify import Spotify
from sortify.store import Store


@pytest.fixture
def client(monkeypatch):
    sp = Spotify(Store())
    calls = []

    def fake_request(method, path, background=False, bulk=False,
                     spend_reserve=False, **kw):
        calls.append({"method": method, "path": path, "bulk": bulk,
                      "spend_reserve": spend_reserve, "json": kw.get("json"),
                      "params": kw.get("params")})
        if method == "GET" and path.endswith("/items"):
            return {"items": [
                {"item": {"uri": f"spotify:track:t{i}", "name": f"t{i}",
                          "artists": []}}
                for i in range(3)]}
        return {"snapshot_id": "snap"}

    monkeypatch.setattr(sp, "request", fake_request)
    sp.calls = calls
    return sp


def test_add_single_uri_still_sends_a_list(client):
    client.add_to_playlist("P1", "spotify:track:a")
    assert client.calls[-1]["json"] == {"uris": ["spotify:track:a"]}


def test_add_batch_sends_all_uris_in_one_call(client):
    uris = [f"spotify:track:b{i}" for i in range(100)]
    client.add_to_playlist("P1", uris, bulk=True, spend_reserve=True)
    assert len(client.calls) == 1
    assert client.calls[0]["json"] == {"uris": uris}
    assert client.calls[0]["bulk"] is True
    assert client.calls[0]["spend_reserve"] is True


def test_add_batch_over_100_refuses_before_spending(client):
    with pytest.raises(ValueError):
        client.add_to_playlist("P1", [f"spotify:track:c{i}" for i in range(101)])
    assert client.calls == []


def test_playlist_tracks_passes_bulk_through_to_request(client):
    tracks = client.playlist_tracks("P1", bulk=True, spend_reserve=True)
    assert [t["uri"] for t in tracks] == [f"spotify:track:t{i}" for i in range(3)]
    assert all(c["bulk"] for c in client.calls)
    assert all(c["spend_reserve"] for c in client.calls)


def test_playlist_tracks_default_stays_interactive(client):
    client.playlist_tracks("P1")
    assert all(not c["bulk"] for c in client.calls)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_spotify_batch.py -v`
Expected: FAIL — `test_add_batch_sends_all_uris_in_one_call` gets `{"uris": [uris]}` (list nested in list) or a TypeError, `test_playlist_tracks_passes_bulk_through_to_request` gets `TypeError: playlist_tracks() got an unexpected keyword argument 'bulk'`.

- [ ] **Step 3: Implement**

In `sortify/spotify.py`, replace `_paginate` (line 507):

```python
    def _paginate(self, path: str, params: dict | None = None, **kw) -> Iterator[dict]:
        page = self.get(path, params=params or {}, **kw)
        while True:
            yield from page.get("items", [])
            if not page.get("next"):
                return
            page = self.get(page["next"], **kw)
```

Replace `playlist_tracks` (line 666) — signature and the three `_paginate` calls only, body otherwise unchanged:

```python
    def playlist_tracks(self, playlist_id: str, bulk: bool = False,
                        spend_reserve: bool = False) -> list[dict]:
        kw = {"bulk": bulk, "spend_reserve": spend_reserve}
        if playlist_id == LIKED_ID:
            items = list(self._paginate("/me/tracks", {"limit": 50}, **kw))
        else:
            try:
                items = list(
                    self._paginate(
                        f"/playlists/{playlist_id}/items",
                        {"limit": 100, "fields": ITEM_FIELDS}, **kw,
                    )
                )
            except SpotifyError as e:
                # If the fields filter is rejected, refetch unfiltered.
                if e.status != 400:
                    raise
                items = list(self._paginate(f"/playlists/{playlist_id}/items",
                                            {"limit": 100}, **kw))
        return [t for t in (self._slim_track(i) for i in items) if t and t["uri"]]
```

Replace `add_to_playlist` (line 735):

```python
    def add_to_playlist(self, playlist_id: str, uris: str | list[str],
                        bulk: bool = False, spend_reserve: bool = False) -> str | None:
        """Add up to 100 tracks in ONE call — the batch limit probed
        2026-08-23 (150 uris → 400 "Too many ids requested"). A single uri
        is accepted for the interactive one-track callers."""
        batch = [uris] if isinstance(uris, str) else list(uris)
        if len(batch) > 100:
            raise ValueError(f"{len(batch)} uris — the batch add limit is 100")
        resp = self.request("POST", f"/playlists/{playlist_id}/items",
                            json={"uris": batch}, bulk=bulk, spend_reserve=spend_reserve)
        return (resp or {}).get("snapshot_id")
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_spotify_batch.py -v` — expected: all PASS.
Then the whole suite: `.venv/bin/pytest -q` — expected: green (the change is signature-compatible; existing callers pass a single str).

- [ ] **Step 5: Commit**

```bash
git add sortify/spotify.py tests/test_spotify_batch.py
git commit -m "feat: batch add up to 100 uris per call; bulk-aware playlist reads"
```

---

### Task 2: `split_output_name` — the `{source} · pile` title rule

**Files:**
- Modify: `sortify/naming.py` (append function + module constant)
- Test: `tests/test_naming.py` (append tests)

**Interfaces:**
- Produces: `naming.split_output_name(source_name: str | None, pile_name: str) -> str` — the composed title, ≤100 chars, source name always whole; bare (possibly truncated) pile name when source is None/empty.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_naming.py`)

```python
from sortify.naming import split_output_name, violations


def test_split_output_name_composes_with_the_pile_separator():
    assert split_output_name("{teh bomb}", "jazz · funk · Bossa Nova") == \
        "{teh bomb} · jazz · funk · Bossa Nova"


def test_split_output_name_without_source_is_the_bare_pile_name():
    assert split_output_name(None, "untagged") == "untagged"
    assert split_output_name("  ", "untagged") == "untagged"


def test_split_output_name_truncates_the_pile_half_not_the_source():
    source = "S" * 40
    out = split_output_name(source, "p" * 90)
    assert len(out) <= 100
    assert out.startswith(source + " · ")
    assert out.endswith("…")


def test_split_output_name_degenerate_source_survives_alone():
    # A source name so long no pile half fits: the source is the grouping
    # key, so it wins and the pile half is dropped entirely.
    out = split_output_name("S" * 99, "jazz")
    assert out == "S" * 99


def test_split_output_titles_trip_no_naming_rules():
    # Design §3: split outputs are created unmarked, and the title shape
    # must not fullmatch the input pattern. violations() must stay empty
    # even if someone marks one as input by hand? No — unmarked is the
    # contract; marked-as-home just gets the ordinary caps proposal.
    playlists = [{"id": "X1", "name": "{teh bomb} · jazz · funk", "editable": True}]
    assert violations(playlists, input_ids=set(), home_ids=set()) == []
    # And the input pattern does not swallow it either:
    rows = violations(playlists, input_ids={"X1"}, home_ids=set())
    assert rows and rows[0]["proposed"] == "[{teh bomb} · jazz · funk]"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_naming.py -v -k split_output`
Expected: FAIL with `ImportError: cannot import name 'split_output_name'`.

- [ ] **Step 3: Implement** (append to `sortify/naming.py`)

```python
MAX_PLAYLIST_NAME = 100   # Spotify's name cap
_SEP = " · "


def split_output_name(source_name: str | None, pile_name: str) -> str:
    """The title for a materialised pile: `{source} · pile`, ≤100 chars.

    The source name is what groups a split's outputs in the client, so under
    truncation it survives whole and the pile half gives way (design §3).
    Fixed at create time — a later rename of the source does not ripple.
    """
    pile = pile_name.strip()
    src = (source_name or "").strip()
    if not src:
        return pile[:MAX_PLAYLIST_NAME]
    title = f"{src}{_SEP}{pile}"
    if len(title) <= MAX_PLAYLIST_NAME:
        return title
    room = MAX_PLAYLIST_NAME - len(src) - len(_SEP) - 1   # -1 for the ellipsis
    if room < 1:
        return src[:MAX_PLAYLIST_NAME]
    return f"{src}{_SEP}{pile[:room]}…"
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_naming.py -v` — expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sortify/naming.py tests/test_naming.py
git commit -m "feat: split-output title rule — {source} · pile, source survives truncation"
```

---

### Task 3: Batched materialiser core — plan, claim, tick, reconcile

This is the load-bearing task. Read `sortify/app.py:1616-1932` (the materialise machinery and its comments) before touching anything — the record-before-create ordering, the claim CAS, and `_pending_materialise` are deliberate and stay exactly as they are.

**Files:**
- Modify: `sortify/app.py:32` (import line), `sortify/app.py:1649` (beside `_pending_materialise`), `sortify/app.py:1687-1720` (`_materialise_plan`), `sortify/app.py:1723-1768` (`_claim_materialisation`), `sortify/app.py:1794-1878` (`_materialise_tick`), `sortify/app.py:2245` (worker clean-credit check)
- Test: `tests/test_materialise.py` (new tests + updated cost assertions), `tests/test_queue_api.py` + `tests/test_queue_worker.py` (updated cost assertions)

**Interfaces:**
- Consumes: `Spotify.add_to_playlist(pid, uris: list[str], bulk=, spend_reserve=)` and `Spotify.playlist_tracks(pid, bulk=, spend_reserve=)` from Task 1; `naming.split_output_name` from Task 2.
- Produces: `_materialise_plan(split, pile, reconciled: bool = False) -> dict` — existing keys plus `"reconcile_calls": int`; `"calls"` becomes `reconcile_calls + ceil(len(missing)/100) + (1 if need_create else 0)`.
- Produces: `_claim_materialisation(..., added_uri=None, added_uris: list[str] | None = None, **fields)`.
- Produces: `_reconciled: set[tuple[str, str]]` — module-level, `(split_playlist_id, pile_id)` pairs this process has verified against the account; mutated only under `_split_lock`.
- Produces: `_source_playlist_name(playlist_id: str) -> str | None` — cached-listing lookup, zero Spotify calls.
- `_materialise_tick` keeps its `{"spent", "done", "gone"}` contract, but `spent` may now exceed 1 (a reconcile tick spends one call per 100 tracks read).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_materialise.py`; the existing `client` fixture already fakes `create_playlist`/`add_to_playlist` and records into `c.calls`)

First extend the existing fixture (three edits inside `tests/test_materialise.py`):

1. In the `client` fixture, add a `playlist_tracks` fake and clear `_reconciled` in the teardown:

```python
    # inside the fixture, next to the other monkeypatches:
    monkeypatch.setattr(appmod.sp, "playlist_tracks",
                        lambda pid, bulk=False, spend_reserve=False:
                            calls.append(("read", pid)) or
                            [{"uri": u} for u in c.remote.get(pid, [])])
```

and give the client a `remote` dict plus teardown, in the try/finally:

```python
    c = TestClient(appmod.app)
    c.calls = calls
    c.remote = {}          # playlist_id -> uris "actually on Spotify"
    try:
        yield c
    finally:
        appmod._pending_materialise.clear()
        appmod._reconciled.clear()
        Store().save_splits(original_splits)
        Store().save_cache(original_cache)
```

2. In the fixture, seed the cached listing so `_source_playlist_name` finds the source (next to the existing `cache["playlists"]["PLM"] = ...`):

```python
    cache["playlist_list"] = {"items": [{"id": "PLM", "name": "{src}"}]}
```

Then add the new tests:

```python
def _tick(pid="PLM", pile="p2"):
    return appmod._materialise_tick(pid, pile)


def test_plan_prices_batched_adds():
    payload = _split()
    split = payload["splits"]["PLM"]
    pile = split["piles"][1]                       # 60 uris, no record
    plan = appmod._materialise_plan(split, pile)
    assert plan["calls"] == 2                      # 1 create + ceil(60/100)
    assert plan["reconcile_calls"] == 0


def test_plan_prices_reconciliation_for_a_resumed_partial_pile():
    payload = _split()
    split = payload["splits"]["PLM"]
    pile = split["piles"][1]
    split["materialised"] = {"p2": {
        "playlist_id": "OLDP", "pile_id": "p2", "name": "big one",
        "fingerprint": appmod._pile_fingerprint(pile),
        "track_count": 60, "added": BIG_URIS[:30], "claim": "c1",
        "created_at": "x", "updated_at": "x"}}
    plan = appmod._materialise_plan(split, pile)
    assert plan["reconcile_calls"] == 1            # ceil(30/100)
    assert plan["calls"] == 2                      # reconcile + 1 add batch
    assert appmod._materialise_plan(split, pile, reconciled=True)["calls"] == 1


def test_claim_added_uris_appends_without_duplicates(client):
    s = Store()
    payload = s.splits()
    payload["splits"]["PLM"]["materialised"] = {"p1": {
        "playlist_id": "NEWP", "pile_id": "p1", "name": "n",
        "fingerprint": "f", "track_count": 5,
        "added": [PILE_URIS[0]], "claim": "c9",
        "created_at": "x", "updated_at": "x"}}
    s.save_splits(payload)
    assert appmod._claim_materialisation("PLM", "p1", "c9",
                                         added_uris=[PILE_URIS[0], PILE_URIS[1]])
    added = Store().splits()["splits"]["PLM"]["materialised"]["p1"]["added"]
    assert added == [PILE_URIS[0], PILE_URIS[1]]


def test_fresh_pile_drains_in_create_plus_batches_and_never_reads(client):
    r1 = _tick()                                   # create
    assert (r1["spent"], r1["done"]) == (1, False)
    r2 = _tick()                                   # all 60 in one batch
    assert (r2["spent"], r2["done"]) == (1, True)
    kinds = [c[0] for c in client.calls]
    assert kinds == ["create", "add"]              # no ("read", ...) ever
    add = next(c for c in client.calls if c[0] == "add")
    assert add[2] == BIG_URIS                      # the whole batch, one call
    rec = Store().splits()["splits"]["PLM"]["materialised"]["p2"]
    assert rec["added"] == BIG_URIS


def test_created_playlist_is_named_source_dot_pile(client):
    _tick(pile="p1")
    create = next(c for c in client.calls if c[0] == "create")
    assert create[1] == "{src} · cumbia · latin · salsa"
    rec = Store().splits()["splits"]["PLM"]["materialised"]["p1"]
    assert rec["name"] == "{src} · cumbia · latin · salsa"


def test_resumed_pile_reconciles_before_adding(client):
    # Record says 30 landed; reality says 40 (a batch landed unrecorded).
    s = Store()
    payload = s.splits()
    pile = payload["splits"]["PLM"]["piles"][1]
    payload["splits"]["PLM"]["materialised"] = {"p2": {
        "playlist_id": "OLDP", "pile_id": "p2", "name": "big one",
        "fingerprint": appmod._pile_fingerprint(pile),
        "track_count": 60, "added": BIG_URIS[:30], "claim": "c1",
        "created_at": "x", "updated_at": "x"}}
    s.save_splits(payload)
    client.remote["OLDP"] = BIG_URIS[:40]

    r1 = _tick()                                   # the reconcile read
    assert (r1["spent"], r1["done"]) == (1, False)
    assert client.calls == [("read", "OLDP")]
    rec = Store().splits()["splits"]["PLM"]["materialised"]["p2"]
    assert rec["added"] == BIG_URIS[:40]           # truth, not the record

    r2 = _tick()                                   # the remaining 20, one call
    assert (r2["spent"], r2["done"]) == (1, True)
    add = client.calls[-1]
    assert add[0] == "add" and add[2] == BIG_URIS[40:]
    assert not any(u in add[2] for u in BIG_URIS[:40])   # no re-adds

    r3 = _tick()
    assert (r3["spent"], r3["done"]) == (0, True)  # complete, free


def test_reconcile_ignores_foreign_tracks_in_the_destination(client):
    s = Store()
    payload = s.splits()
    pile = payload["splits"]["PLM"]["piles"][1]
    payload["splits"]["PLM"]["materialised"] = {"p2": {
        "playlist_id": "OLDP", "pile_id": "p2", "name": "big one",
        "fingerprint": appmod._pile_fingerprint(pile),
        "track_count": 60, "added": BIG_URIS[:10], "claim": "c1",
        "created_at": "x", "updated_at": "x"}}
    s.save_splits(payload)
    # The user dropped two of their own tracks into the output by hand.
    client.remote["OLDP"] = BIG_URIS[:10] + ["spotify:track:foreign1",
                                             "spotify:track:foreign2"]
    _tick()
    rec = Store().splits()["splits"]["PLM"]["materialised"]["p2"]
    assert rec["added"] == BIG_URIS[:10]           # foreign uris not adopted


def test_reconcile_runs_once_per_process_pickup(client):
    s = Store()
    payload = s.splits()
    pile = payload["splits"]["PLM"]["piles"][1]
    payload["splits"]["PLM"]["materialised"] = {"p2": {
        "playlist_id": "OLDP", "pile_id": "p2", "name": "big one",
        "fingerprint": appmod._pile_fingerprint(pile),
        "track_count": 60, "added": BIG_URIS[:30], "claim": "c1",
        "created_at": "x", "updated_at": "x"}}
    s.save_splits(payload)
    client.remote["OLDP"] = BIG_URIS[:30]
    _tick(); _tick(); _tick()
    assert [c[0] for c in client.calls].count("read") == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_materialise.py -v -k "batched or reconcil or added_uris or named_source or drains"`
Expected: FAIL — `AttributeError: module 'sortify.app' has no attribute '_reconciled'`, plans returning per-track costs, ticks adding one uri at a time.

- [ ] **Step 3: Implement**

All in `sortify/app.py`.

3a. Import (line 32):

```python
from .naming import split_output_name, violations as naming_violations
```

3b. Beside `_pending_materialise` (after line 1649):

```python
# (split playlist id, pile id) pairs whose record THIS process has verified
# against the account — either by creating/adding under its own CAS, or by
# the reconcile read below. Batching is what makes this necessary: a crash
# between a 100-track POST landing and the CAS recording it leaves up to 100
# landed tracks unrecorded, and Spotify permits duplicates, so a blind retry
# would double them (design §1). A pair NOT in this set whose record has
# tracks in `added` is treated as untrustworthy and re-read before anything
# is added. Mutated only under `_split_lock`; a fresh process starts empty,
# which is exactly the "never trust the record across an interruption" rule.
_reconciled: set[tuple[str, str]] = set()


def _batches(n: int) -> int:
    """Calls needed to move n tracks at the 100-per-call batch limit."""
    return -(-n // 100)


def _source_playlist_name(playlist_id: str) -> str | None:
    """The source playlist's display name, from the cached listing only —
    zero Spotify calls; None when the cache doesn't know it. The output
    title is fixed at create time (design §3): a later rename of the
    source does not ripple into existing outputs."""
    items = (store.cache().get("playlist_list") or {}).get("items") or []
    return next((p.get("name") for p in items if p.get("id") == playlist_id), None)
```

3c. `_materialise_plan` — new signature and cost lines; keep the docstring's single-place-for-cost argument and add one paragraph:

```python
def _materialise_plan(split: dict, pile: dict, reconciled: bool = False) -> dict:
```

After `need_create = ...` insert:

```python
    added_list = usable.get("added", []) if usable else []
    reconcile_calls = (
        _batches(max(len(added_list), 1))
        if (usable and usable.get("playlist_id") and added_list and missing
            and not reconciled)
        else 0
    )
```

and in the returned dict:

```python
        "reconcile_calls": reconcile_calls,
        "calls": reconcile_calls + _batches(len(missing)) + (1 if need_create else 0),
```

Docstring addition (verbatim, after the `stale` paragraph):

```
    `reconciled` is whether THIS process has already verified the record
    against the account (`_reconciled`). A resumable record (playlist_id
    set, some `added`, some missing) that hasn't been verified prices in a
    read of the destination first — ceil(len(added)/100) calls, a floor
    since the real playlist can hold more (duplicates from an earlier
    crash). Reconciliation is what makes a 100-track batch safe to retry.
```

3d. `_claim_materialisation` — signature and one loop:

```python
def _claim_materialisation(
    split_playlist_id: str, pile_id: str, claim: str, added_uri: str | None = None,
    added_uris: list[str] | None = None, **fields: Any
) -> bool:
```

after the existing `added_uri` branch:

```python
        for u in added_uris or []:
            if u not in record["added"]:
                record["added"].append(u)
```

3e. `_materialise_tick` — replace from the `plan = ...` line to the end of the function with:

```python
        plan = _materialise_plan(split, pile,
                                 reconciled=(playlist_id, pile_id) in _reconciled)
        if plan["calls"] == 0:
            return {"spent": 0, "done": True, "gone": False}
        if plan["stale"]:
            old = split["materialised"].pop(pile_id, None)
            if old:
                split.setdefault("materialised_history", []).append(old)
        record = (split.get("materialised") or {}).get(pile_id)
        if not record or plan["stale"] or not record.get("claim"):
            existing = plan["record"] if not plan["stale"] else None
            record = {
                "playlist_id": existing.get("playlist_id") if existing else None,
                "pile_id": pile_id, "name": pile["name"],
                "fingerprint": _pile_fingerprint(pile),
                "track_count": len(_unique(pile["uris"])),
                "added": list(existing.get("added", [])) if existing else [],
                "claim": uuid.uuid4().hex,
                "created_at": existing.get("created_at") if existing else _now_iso(),
                "updated_at": _now_iso(),
            }
            # Written BEFORE the create call — the create/record gap is where
            # this project's stray playlists have come from.
            split.setdefault("materialised", {})[pile_id] = record
            store.save_splits(payload)
        claim = record["claim"]
        need_create = not record.get("playlist_id")
        need_reconcile = not need_create and plan["reconcile_calls"] > 0
        batch = [] if (need_create or need_reconcile) else plan["missing"][:100]
        _pending_materialise.add((playlist_id, pile_id))

    def _mark_reconciled():
        # Every successful CAS proves this process owns the record and its
        # `added` list is truth — creates and adds included, so a pile this
        # process started never pays for a read it doesn't need.
        with _split_lock:
            _reconciled.add((playlist_id, pile_id))

    try:
        if need_create:
            new_name = split_output_name(_source_playlist_name(playlist_id),
                                         pile["name"])
            new_id = sp.create_playlist(new_name, MATERIALISE_DESCRIPTION, bulk=True,
                                        spend_reserve=spend_reserve)
            if _claim_materialisation(playlist_id, pile_id, claim,
                                      playlist_id=new_id, name=new_name):
                _mark_reconciled()
            else:
                try:
                    _abandon_unrecorded_playlist(playlist_id, pile_id, new_id, record)
                except HTTPException as exc:
                    raise SpotifyError(exc.status_code, exc.detail) from exc
            # A create call never finishes a pile on its own — every pile
            # has at least one track still to add afterwards.
            return {"spent": 1, "done": False, "gone": False}
        if need_reconcile:
            # First act on picking up a resumable pile: one read of the
            # destination, and `added` becomes what is ACTUALLY there. A
            # crash mid-batch can then neither duplicate nor skip (design
            # §1) — the record is never trusted across an interruption.
            actual = sp.playlist_tracks(record["playlist_id"], bulk=True,
                                        spend_reserve=spend_reserve)
            actual_uris = {t["uri"] for t in actual}
            landed = [u for u in _unique(pile["uris"]) if u in actual_uris]
            if _claim_materialisation(playlist_id, pile_id, claim, added=landed):
                _mark_reconciled()
            return {"spent": max(1, _batches(len(actual))), "done": False,
                    "gone": False}
        sp.add_to_playlist(record["playlist_id"], batch, bulk=True,
                           spend_reserve=spend_reserve)
        if _claim_materialisation(playlist_id, pile_id, claim, added_uris=batch):
            _mark_reconciled()
        else:
            try:
                _readopt_materialisation(playlist_id, pile_id, record,
                                         record["playlist_id"], batch)
            except HTTPException as exc:
                raise SpotifyError(exc.status_code, exc.detail) from exc
    finally:
        with _split_lock:
            _pending_materialise.discard((playlist_id, pile_id))
    remaining = len(plan["missing"]) - len(batch)
    return {"spent": 1, "done": remaining == 0, "gone": False}
```

Update the tick's docstring first line: "Advance one pile's materialisation by ONE Spotify call — a create, a reconcile read (one call per 100 tracks in the destination, reported in `spent`), or a batch add of up to 100 tracks."

3f. Worker clean-credit (line 2245): change

```python
        elif result.get("spent") == 1:
```

to

```python
        elif result.get("spent", 0) >= 1:
```

(a reconcile tick can legitimately spend several paginated calls and still be clean).

- [ ] **Step 4: Sweep the existing cost assertions**

Run: `.venv/bin/pytest -q` and fix every failure that is a *cost expectation*, not a behaviour change. The transformation rule — old `len(missing) + need_create` becomes `reconcile_calls + ceil(len(missing)/100) + need_create`:

- 5-uri fresh pile: 6 → **2**; 60-uri fresh pile: 61 → **2**.
- A pile now finishes in `1 + ceil(n/100)` ticks, not `1 + n`.
- Find them with: `grep -n "materialise_calls\|expected_calls" tests/test_materialise.py tests/test_queue_api.py` and by reading each failure. Known sites: `tests/test_materialise.py` (2 hits), `tests/test_queue_api.py` (17 hits), plus tick-count loops in `tests/test_queue_worker.py`.
- Also update `tests/test_materialise.py`'s module docstring — its "no batch add, so a 309-track pile is 310 calls" sentence is the overturned premise; replace with "batch add moves 100 tracks per call (probed 2026-08-23), so a 309-track pile is 4 calls — and reconciliation on resume is what keeps a retried batch from duplicating tracks."
- Do NOT weaken assertions about *ordering* or *refusal* (echo-mismatch 409s, claim CAS, stray-playlist abandonment) — those are behaviour, and unchanged.

Expected after the sweep: `.venv/bin/pytest -q` fully green.

- [ ] **Step 5: Run both orderings**

Run: `.venv/bin/pytest -q` then `.venv/bin/pytest -q -p no:randomly $(ls -r tests/test_*.py)`
Expected: green twice.

- [ ] **Step 6: Commit**

```bash
git add sortify/app.py tests/test_materialise.py tests/test_queue_api.py tests/test_queue_worker.py
git commit -m "feat: batched materialisation with reconcile-on-pickup — ~50x fewer calls per split"
```

---

### Task 4: Queue integration — accurate prices and an end-to-end drain

**Files:**
- Modify: `sortify/app.py:841` (`_pile_progress`), `sortify/app.py:1147` and `sortify/app.py:1183` (its two callers), `sortify/app.py:2055` (`_queue_next_action`), `sortify/app.py:2325` (`enqueue_piles`)
- Test: `tests/test_queue_worker.py` (append)

**Interfaces:**
- Consumes: `_materialise_plan(split, pile, reconciled=...)` and `_reconciled` from Task 3.
- Produces: `_pile_progress(split: dict, playlist_id: str) -> list[dict]` (new second parameter).

- [ ] **Step 1: Write the failing test** (append to `tests/test_queue_worker.py`; that file drives the worker thread directly — its `worker_env` fixture fakes `create_playlist`/`add_to_playlist`, nulls `bulk_block_reason`, zeroes `Governor.interval`, and module helpers `start_queue(s, pending=...)` / `wait_done(s)` start and settle a run)

```python
def test_queue_drains_a_250_track_pile_in_four_calls(worker_env):
    # 250 uris: 1 create + 3 batches. The whole run must cost 4 Spotify
    # calls — the headline number of the 2026-08-23 design.
    calls, s = worker_env
    big = [f"spotify:track:q{i}" for i in range(250)]
    payload = s.splits()
    payload["splits"]["PLQ"]["piles"] = [
        {"id": "p1", "name": "big", "tags": [], "uris": big}]
    s.save_splits(payload)
    split = s.splits()["splits"]["PLQ"]
    assert appmod._materialise_plan(split, split["piles"][0])["calls"] == 4
    start_queue(s, pending=("p1",))
    q = wait_done(s)
    assert q["state"] == "done"
    assert [c[0] for c in calls] == ["create", "add", "add", "add"]
    assert [len(c[2]) for c in calls[1:]] == [100, 100, 50]
    added = s.splits()["splits"]["PLQ"]["materialised"]["p1"]["added"]
    assert added == big
```

Also add `appmod._reconciled.clear()` to the `worker_env` fixture's teardown (next to the existing `appmod._queue_wake.clear()`): these tests mint records for the shared "PLQ" id, and a leaked `_reconciled` entry would silently skip the reconcile another test expects.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_queue_worker.py -v -k 250_track`
Expected: with Task 3 done this may already PASS — that is fine, it pins the end-to-end contract. The reconciled-pricing edits below are still required either way.

- [ ] **Step 3: Implement the reconciled-aware pricing**

`_pile_progress` (line 841) — new parameter, passed through:

```python
def _pile_progress(split: dict, playlist_id: str) -> list[dict]:
    decided = split.get("decided", {})
    out = []
    for p in split["piles"]:
        plan = _materialise_plan(split, p,
                                 reconciled=(playlist_id, p["id"]) in _reconciled)
```

and its two callers become `_pile_progress(split, playlist_id)` (both already have `playlist_id` in scope — verify at `app.py:1147` and `app.py:1183`).

`_queue_next_action` (line 2055):

```python
            if pile is None or _materialise_plan(
                    split, pile,
                    reconciled=(q["playlist_id"], pid) in _reconciled)["calls"] == 0:
```

`enqueue_piles` (line 2325):

```python
        plans = {p["id"]: _materialise_plan(
            split, p, reconciled=(playlist_id, p["id"]) in _reconciled)
            for p in piles}
```

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest -q` — expected: green. The displayed price and the spent price come from the same function with the same `reconciled` argument, so the echo contract holds.

- [ ] **Step 5: Commit**

```bash
git add sortify/app.py tests/test_queue_worker.py
git commit -m "feat: reconcile-aware pricing everywhere a materialise cost is shown or echoed"
```

---

### Task 5: Retire the pacing docstring's ambition; correct CLAUDE.md's batch line

Doc-only, zero behaviour change (design §4 and the "correction worth carrying" rule: a docstring that describes a dead ambition misleads the next reader).

**Files:**
- Modify: `sortify/pacing.py:1-13` (module docstring only — no constant, no method changes)
- Modify: `CLAUDE.md` (the Conventions bullet)

- [ ] **Step 1: Replace the pacing docstring**

```python
"""Rate governor for the queued materialiser.

Historical note: this module was built as a measuring instrument — 678
calls @ 9.7/min earned a ~23h quota ban, 1208 @ 1.8/min was penalty-free,
and the ~1240 calls of one-track-per-POST materialisation traffic were
meant to map the band between. Batched adds (2026-08-23: 100 uris per
POST) removed that traffic — a ~24-call split never survives the 15 clean
minutes a single escalation rung needs — so the measuring ambition is
retired. `data/pacing.json`'s max_clean_rate (4.0) stands as what WAS
measured before every new pile's worker reset the ladder to START_RATE.

What remains is what it also always was: a rate limiter. Start at the
known-good 1.8/min, escalate 15% per 15 clean minutes, cap at 7.0/min
(28% under known-bad), halve on any 429. All still enforced, all still
correct for the calls that remain.

Pure logic: injected clocks, no I/O, no imports from the rest of sortify.
The worker owns persistence (store.save_pacing) and all actual sleeping.
"""
```

- [ ] **Step 2: Correct CLAUDE.md's Conventions bullet**

Change:

```
- The client speaks the Feb-2026 dev-mode API (`items`/`item`, `/me/library`,
  no batch endpoints) — do not "fix" it back to pre-2026 shapes.
```

to:

```
- The client speaks the Feb-2026 dev-mode API (`items`/`item`, `/me/library`)
  — do not "fix" it back to pre-2026 shapes. Batch ADD exists: up to 100 uris
  per playlist-items POST (probed 2026-08-23; 150 → 400). There is still no
  batch delete.
```

- [ ] **Step 3: Run the pacing tests**

Run: `.venv/bin/pytest tests/test_pacing.py -q` — expected: green, untouched.

- [ ] **Step 4: Commit**

```bash
git add sortify/pacing.py CLAUDE.md
git commit -m "docs: retire the pacing ladder's measuring ambition; record the real batch-add limit"
```

---

### Task 6: Split as a top-level tab with a picker

**Files:**
- Modify: `sortify/static/index.html:23-27` (nav) and before line 122 (new section)
- Modify: `sortify/static/app.js:4` (views), `app.js:47-54` (`show`), `app.js:529-530` area (nav wiring), `app.js:1910` (back button), plus one new function
- Test: `tests/ui_harness.mjs` (append a check block)

**Interfaces:**
- Consumes: `openSplit(id, name)` (`app.js:1104`), `splitDisabledReason(p)` (`app.js:165`), `api()`, `show()`, `esc()` — all existing.
- Produces: `showSplitPicker()` — renders eligible playlists into `#splitpick-list`; the split view's `‹ Back` now returns here.

- [ ] **Step 1: Write the failing harness checks** (append to `tests/ui_harness.mjs` before the final results/exit block, following the existing `{ ... }` check-block pattern)

```js
// ============================================================================
// C-tab — the Split tab: picker lists eligible playlists, Back returns here
// ============================================================================
{
  resetLog();
  routes["GET /api/playlists"] = { playlists: [
    { id: "liked", name: "Liked Songs", total: 900, editable: false, role: null },
    { id: "PB", name: "big", total: 250, editable: true, role: null },
    { id: "PS", name: "small", total: 30, editable: true, role: null },
    { id: "PB2", name: "bigger", total: 400, editable: true, role: null },
  ], fetched_at: 0 };
  // GET queue is deliberately global regardless of the path id (M4).
  routes["GET /api/split/_/queue"] = {
    queue: { state: "paused", playlist_id: "PB2", pending: ["p2"], current: null },
    pacing: {} };
  run("showSplitPicker()");
  await tick();
  const rows = $$("splitpick-list").children;
  check("C-tab picker shows only 100+-track, non-Liked playlists",
        rows.length === 2, `${rows.length} rows`);
  check("C-tab the in-progress split is pinned first",
        /bigger/.test(rows[0]?.innerHTML || ""),
        (rows[0]?.innerHTML || "").slice(0, 60));
  check("C-tab the picker view is the visible one",
        $$("view-splitpick").hidden === false);
  check("C-tab opening the picker costs zero Spotify-priced POSTs",
        log.every((c) => c.method === "GET"), JSON.stringify(log));

  routes["GET /api/split/PB2"] = { status: 200, body: splitBody(null) };
  routes["GET /api/split/PB2/queue"] = { queue: { state: "done", playlist_id: "PB2",
    pending: [], current: null }, pacing: {} };
  rows[0].onclick();
  await tick();
  check("C-tab clicking a row opens the split view",
        $$("view-split").hidden === false);
  run(`$("btn-split-back").onclick()`);
  await tick();
  check("C-tab Back returns to the picker, not Playlists",
        $$("view-splitpick").hidden === false && $$("view-lists").hidden === true);
}
```

- [ ] **Step 2: Run to verify failure**

Run: `node tests/ui_harness.mjs`
Expected: FAIL (or a ReferenceError on `showSplitPicker`) on the new C-tab checks; every pre-existing check still PASS. If a pre-existing check broke, stop and fix before proceeding.

- [ ] **Step 3: Implement**

`sortify/static/index.html` — in `<nav>` (line 23-27), after the Playlists link:

```html
    <a id="nav-split">Split</a>
```

New section, immediately before `<section id="view-split" hidden>` (line 122):

```html
  <section id="view-splitpick" hidden>
    <h2>Split a playlist</h2>
    <p class="hint">Playlists with at least 100 tracks can be split into piles.
       A split reads the playlist once, tags it via Last.fm, and saving piles
       now takes a couple of minutes.</p>
    <div id="splitpick-list"></div>
  </section>
```

`sortify/static/app.js`:

Line 4:

```js
const views = ["setup", "lists", "triage", "now", "split", "splitpick"];
```

`show()` (lines 51-52) — split moves off the Playlists highlight onto its own tab:

```js
  $("nav-now").classList.toggle("active", view === "now");
  $("nav-lists").classList.toggle("active", view === "lists" || view === "triage");
  $("nav-split").classList.toggle("active", view === "split" || view === "splitpick");
```

New function (place next to `openSplit`, `app.js:1104`):

```js
// The Split tab opens on a picker (design §2): same eligibility rule as the
// per-row button (not Liked, 100+ tracks — splitDisabledReason handles
// not-owned), any in-progress split pinned first. Both reads are free:
// /api/playlists serves the cached listing and GET queue reads queue.json.
async function showSplitPicker() {
  stopQueuePolling();
  stopNowPolling();
  stopNowTicker();
  split = null;
  show("splitpick");
  const wrap = $("splitpick-list");
  wrap.innerHTML = '<p class="hint">Loading playlists…</p>';
  try {
    const data = await api("/api/playlists");
    // "_" is fine: GET queue is deliberately global regardless of path id.
    const qs = await api("/api/split/_/queue").catch(() => null);
    const q = qs?.queue;
    const activeId = q && (q.pending?.length || q.current) ? q.playlist_id : null;
    const eligible = data.playlists.filter(
      (p) => p.id !== "liked" && (p.total ?? 0) >= 100);
    eligible.sort((a, b) => (b.id === activeId) - (a.id === activeId));
    wrap.innerHTML = "";
    if (!eligible.length) {
      wrap.innerHTML = '<p class="hint">No playlist here has 100+ tracks — nothing needs splitting.</p>';
      return;
    }
    for (const p of eligible) {
      const row = document.createElement("div");
      row.className = "pl-row";
      const reason = splitDisabledReason(p);
      const sub = [p.folder, `${p.total} tracks`,
                   p.id === activeId ? "split in progress" :
                     p.split ? `split into ${p.split.piles} pile${p.split.piles === 1 ? "" : "s"}` : null,
                   reason]
        .filter(Boolean).join(" · ");
      row.innerHTML = `<div class="pl-meta"><div class="name">${esc(p.name)}</div>
        <div class="sub">${esc(sub)}</div></div>`;
      if (!reason) row.onclick = () => openSplit(p.id, p.name);
      wrap.appendChild(row);
    }
  } catch (e) {
    if (e.message === "auth needed") return;
    wrap.innerHTML = `<p class="hint">Couldn't load playlists: ${esc(e.message)}</p>
       <button id="btn-retry-splitpick">Retry</button>`;
    $("btn-retry-splitpick").onclick = showSplitPicker;
  }
}
```

Nav wiring (next to line 529-530):

```js
$("nav-split").onclick = showSplitPicker;
```

Back button (line 1910) — returns to the picker, not Playlists (design §2):

```js
$("btn-split-back").onclick = () => { stopQueuePolling(); split = null; showSplitPicker(); };
```

- [ ] **Step 4: Run the harness and suite**

Run: `node tests/ui_harness.mjs` — expected: all checks PASS, C-tab included.
Run: `.venv/bin/pytest -q` — expected: green (server untouched).

- [ ] **Step 5: Commit**

```bash
git add sortify/static/index.html sortify/static/app.js tests/ui_harness.mjs
git commit -m "feat: Split as a top-level tab — picker of splittable playlists, Back returns to it"
```

---

### Task 7: Rename existing outputs — a separate, explicit, priced action

The eight `{teh bomb}` outputs predate the naming rule and keep their bare pile names by default (design §3). This adds the opt-in rename: an endpoint with the house echo-the-price contract, and an understated button in the split view.

**Files:**
- Modify: `sortify/app.py` (new endpoint after `enqueue_piles`, ~line 2361)
- Modify: `sortify/static/index.html:140` area (button beside `#queue-panel`), `sortify/static/app.js:1117` area (hook in `openSplit`)
- Test: `tests/test_rename_outputs.py` (new)

**Interfaces:**
- Consumes: `split_output_name`, `_source_playlist_name`, `_claim_materialisation` (Tasks 2-3); `Spotify.rename_playlist(playlist_id, name)` (`spotify.py:598`, exists).
- Produces: `POST /api/split/{playlist_id}/rename_outputs` with body `{"expected_calls": int}` → `{"ok": true, "renamed": [{"pile_id", "name"}]}`; 409 on price mismatch with nothing spent.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rename_outputs.py
"""Renaming a split's materialised playlists to `{source} · pile` form —
explicit, priced (one rename call each), never automatic (design §3)."""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from liveguard import assert_not_live_data
assert_not_live_data(appmod.store.dir)

from sortify.store import Store

URIS = [f"spotify:track:r{i}" for i in range(3)]


def _seed(source_name="{src}"):
    s = Store()
    payload = s.splits()
    payload["splits"]["PLR"] = {
        "created_at": "x", "snapshot_id": None,
        "params": {"resolution": 1.0, "min_pile": 15, "tag_floor": 10,
                   "max_tags_per_artist": 8},
        "piles": [{"id": "p1", "name": "jazz · funk", "tags": ["jazz"],
                   "uris": URIS}],
        "decided": {}, "active_sitting": None,
        "materialised": {
            "p1": {"playlist_id": "SP1", "pile_id": "p1", "name": "jazz · funk",
                   "fingerprint": "f", "track_count": 3, "added": list(URIS),
                   "claim": "c1", "created_at": "x", "updated_at": "x"},
        }}
    s.save_splits(payload)
    cache = s.cache()
    if source_name is not None:
        cache["playlist_list"] = {"items": [{"id": "PLR", "name": source_name}]}
    else:
        cache["playlist_list"] = {"items": []}
    s.save_cache(cache)


@pytest.fixture
def client(monkeypatch):
    renames = []
    monkeypatch.setattr(appmod.sp, "rename_playlist",
                        lambda pid, name: renames.append((pid, name)))
    s = Store()
    original_splits = s.splits()
    original_cache = s.cache()
    c = TestClient(appmod.app)
    c.renames = renames
    try:
        yield c
    finally:
        Store().save_splits(original_splits)
        Store().save_cache(original_cache)


def test_rename_prefixes_and_records(client):
    _seed()
    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 1})
    assert r.status_code == 200
    assert client.renames == [("SP1", "{src} · jazz · funk")]
    rec = Store().splits()["splits"]["PLR"]["materialised"]["p1"]
    assert rec["name"] == "{src} · jazz · funk"


def test_rename_skips_already_prefixed_outputs(client):
    _seed()
    s = Store()
    payload = s.splits()
    payload["splits"]["PLR"]["materialised"]["p1"]["name"] = "{src} · jazz · funk"
    s.save_splits(payload)
    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 0})
    assert r.status_code == 200
    assert r.json()["renamed"] == []
    assert client.renames == []


def test_rename_refuses_a_stale_price_without_spending(client):
    _seed()
    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 3})
    assert r.status_code == 409
    assert client.renames == []


def test_rename_refuses_when_the_source_name_is_unknown(client):
    _seed(source_name=None)
    r = client.post("/api/split/PLR/rename_outputs", json={"expected_calls": 1})
    assert r.status_code == 409
    assert client.renames == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_rename_outputs.py -v`
Expected: FAIL with 404s (endpoint doesn't exist).

- [ ] **Step 3: Implement the endpoint** (in `sortify/app.py`, after `enqueue_piles`)

```python
class RenameOutputsIn(BaseModel):
    # The echo, not a flag — the caller states the price it was shown (I1).
    expected_calls: int = Field(..., ge=0)


@app.post("/api/split/{playlist_id}/rename_outputs")
def rename_outputs(playlist_id: str, body: RenameOutputsIn):
    """Rename this split's materialised playlists to `{source} · pile` form.

    Explicit and priced, never automatic (design §3): outputs created before
    the naming rule keep their bare pile names until the user asks. One
    rename call per playlist, interactive budget — this is a user click.
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        if not split:
            raise HTTPException(404, "no split for that playlist")
        source = _source_playlist_name(playlist_id)
        if not source:
            raise HTTPException(
                409, "the source playlist's name isn't in the cached listing — "
                     "Refresh the Playlists view first (free if unchanged)")
        todo = []
        for pid, rec in (split.get("materialised") or {}).items():
            if not rec.get("playlist_id"):
                continue
            cur = rec.get("name") or ""
            if cur.startswith(f"{source} · "):
                continue
            todo.append((pid, rec["playlist_id"], rec.get("claim"),
                         split_output_name(source, cur)))
    if body.expected_calls != len(todo):
        raise HTTPException(
            409, f"cost has changed: renaming now spends {len(todo)} Spotify "
                 f"calls, not the {body.expected_calls} you confirmed. "
                 "Nothing was spent.")
    renamed = []
    for pid, spotify_id, claim, target in todo:
        sp.rename_playlist(spotify_id, target)
        # CAS so a concurrent re-cluster's fresh record isn't stamped with a
        # stale name; a refused write just means the record moved on — the
        # playlist itself is renamed either way, which is what was asked.
        _claim_materialisation(playlist_id, pid, claim, name=target)
        renamed.append({"pile_id": pid, "name": target})
    return {"ok": True, "renamed": renamed}
```

- [ ] **Step 4: Run the endpoint tests**

Run: `.venv/bin/pytest tests/test_rename_outputs.py -v` — expected: PASS.

- [ ] **Step 5: Add the understated UI button**

`sortify/static/index.html` — next to `<div id="queue-panel" hidden></div>` (line 140):

```html
    <button id="btn-rename-outputs" hidden></button>
```

`sortify/static/app.js` — three edits in `openSplit` and one new function. In `openSplit`, next to the existing `$("queue-panel").hidden = true;` reset (line 1107) add `$("btn-rename-outputs").hidden = true;` (otherwise a previous split's offer survives into a view that 404s), and after `applySplitData(data);` (line 1117) add `renderRenameOffer(data);`. Then add the function beside it:

```js
// Understated by design (design §2 judgement): shown only when this split
// has saved playlists still carrying bare pile names. The count is computed
// client-side from record names; the server re-derives it and 409s if they
// disagree — same echo contract as every other priced action.
function renderRenameOffer(data) {
  const btn = $("btn-rename-outputs");
  const prefix = split.name + " · ";
  const todo = (data.piles || []).filter(
    (p) => p.materialised?.playlist_id && p.materialised.name
           && !p.materialised.name.startsWith(prefix));
  btn.hidden = !todo.length;
  if (!todo.length) return;
  btn.textContent =
    `Rename ${todo.length} saved playlist${todo.length === 1 ? "" : "s"} to “${split.name} · …” (${todo.length} calls)`;
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      await api(`/api/split/${split.id}/rename_outputs`,
                { expected_calls: todo.length });
      toast("Renamed");
      btn.hidden = true;
    } catch (e) {
      if (e.message !== "auth needed") toast(e.message);
    } finally {
      btn.disabled = false;
    }
  };
}
```

- [ ] **Step 6: Run everything**

Run: `.venv/bin/pytest -q` and `node tests/ui_harness.mjs` — expected: green. (The harness's existing split scenarios route `GET /api/split/PL1` with piles whose `materialised` is null, so the button stays hidden there — no existing check should move.)

- [ ] **Step 7: Commit**

```bash
git add sortify/app.py sortify/static/index.html sortify/static/app.js tests/test_rename_outputs.py
git commit -m "feat: explicit priced rename of existing split outputs to {source} · pile form"
```

---

### Task 8: Final verification

- [ ] **Step 1: Full suite, both orderings, plus the harness**

```bash
.venv/bin/pytest -q
```

```bash
.venv/bin/pytest -q -p no:randomly $(ls -r tests/test_*.py)
```

```bash
node tests/ui_harness.mjs
```

Expected: all green. Pay attention to `tests/test_no_proactive_work.py` — the reconcile read must only ever happen inside a worker a click created, and that suite pins boot-to-traffic silence.

- [ ] **Step 2: Confirm zero API spend**

```bash
.venv/bin/spx budget
```

Expected: numbers unchanged from the start of the work (state them in the report). Development spent nothing; the first real batched run is attended, with `spx budget` stated before and after — that run is a **user decision**, not part of this plan.

- [ ] **Step 3: Restart and hand over — coordinated**

Do NOT restart `sortify.service` unilaterally: check `git log` for other sessions' commits since this plan started and merge/rebase first if any. Then, with the user's go-ahead:

```bash
systemctl --user restart sortify
```

and report: what changed, the new cost of a split (`1 create + ceil(n/100)` per pile), and that boxdash's card will now jump in steps of up to 100 and may show a whole split finishing inside one 20-second poll — expected, not a bug.
