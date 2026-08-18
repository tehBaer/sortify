# Queued Materialiser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One click enqueues all piles of a split as permanent Spotify playlists; a paced worker drains one track per tick with a self-escalating rate governor (1.8→7.0/min), a "bulk" budget class with a 150-call interactive reserve, instant pause/cancel, and a read-only boxdash card.

**Architecture:** The unmerged `pile-materialise` branch's machinery (plan/claim/record helpers, all reviewed) is kept verbatim; only its blocking one-shot delivery loop is replaced. A new `_materialise_tick()` advances a pile by at most ONE Spotify call; a worker thread (started only by enqueue/resume endpoints, never at boot) calls it on a schedule owned by a `Governor` in a new pure module `sortify/pacing.py`. Queue and pacing state persist as versioned JSON files that boxdash reads directly.

**Tech Stack:** Python 3 / FastAPI / pytest (existing), vanilla JS + `tests/ui_harness.mjs` (existing), boxdash's stdlib `server.py` + single-file `static/index.html`.

**Spec:** `docs/superpowers/specs/2026-08-18-queued-materialiser-design.md` — read it first; it argues every number below.

## Global Constraints

- **Zero live Spotify calls, ever.** Every test fakes at or below `Spotify` method level; tests that assert cost count calls on the fake. Do not run `spx` or curl api.spotify.com. Do not write to `data/` in the worktree — it is a **symlink to the live tree's data**; tests already isolate via `tests/conftest.py` (temp `SORTIFY_DATA_DIR` + temp account ledger).
- Suite must pass **both orderings** after every task that touches Python: `.venv/bin/pytest -q` AND `.venv/bin/pytest -q -p no:randomly $(ls -r tests/test_*.py)`. Frontend tasks also run `node tests/ui_harness.mjs`.
- Caps unchanged: `DAILY_CAP = 600`, `WINDOW_CAP = 12`, `BACKGROUND_DAILY_CAP = 40`, `QUIET_AFTER_COOLDOWN = 6*3600`. New: `BULK_RESERVE = 150`. Governor: start 1.8/min, ceiling **7.0/min**, +15% interval shrink per 15 clean minutes, halve on rate-429, permanent stop on `QUOTA_EXCEEDED`.
- Do NOT change `Spotify.request` retry behaviour (review finding I2 is disclosure-only). Additive observation (`last_429`) is allowed.
- Nothing blocking may run while `_split_lock` is held. `sortify/tags.py` must never import `sortify.spotify`. `sortify/pacing.py` must import neither (pure logic).
- No new runtime dependencies.
- Worktree: `~/kode/sortify-lastfm`, branch `queued-materialiser`. Merges to master use `git merge-tree` to verify first, then `git merge --ff-only` where possible, `--no-ff` otherwise — master may move under you (other sessions exist).
- Commit after every task, message in house style (imperative, why-first body).

---

### Task 1: Land the stoplist fix on master (sequencing decision 6a)

The pile-naming stoplist fix is commit `297fe5b` on `pile-materialise` — self-contained (`sortify/tags.py` + `tests/test_tag_hygiene.py`), zero API cost. It must reach master and the piles must be re-clustered BEFORE any playlist is materialised, or positional pile ids will drift under the records.

**Files:**
- No new files; cherry-pick moves `sortify/tags.py`, `tests/test_tag_hygiene.py`.

**Interfaces:**
- Produces: master containing the stoplist; the live 9-pile split becomes 8 piles after the (attended) re-cluster.

- [ ] **Step 1: Branch and cherry-pick**

```bash
cd ~/kode/sortify-lastfm
git checkout -b stoplist-fix master
git cherry-pick 297fe5b
```

Expected: clean cherry-pick (the commit predates the machinery; it touches nothing the other pile-materialise commits touch).

- [ ] **Step 2: Verify both orderings + harness**

```bash
.venv/bin/pytest -q
.venv/bin/pytest -q -p no:randomly $(ls -r tests/test_*.py)
node tests/ui_harness.mjs
```

Expected: all green (master is 254 tests + this commit's hygiene tests; harness 40/40).

- [ ] **Step 3: Merge to master (guarded)**

```bash
git merge-tree $(git merge-base master stoplist-fix) master stoplist-fix
```

Inspect output: no conflict markers. Then:

```bash
git checkout master && git merge --ff-only stoplist-fix || git merge --no-ff stoplist-fix
git checkout queued-materialiser
```

If `--ff-only` fails because master moved, re-run merge-tree against the new master before `--no-ff`.

- [ ] **Step 4: CHECKPOINT — ask the user before touching the live service**

The live tree (`~/kode/sortify`) has uncommitted static-file changes from the paused split-progress-bar session (task_8cb1a2a1). Merging master there and restarting `sortify.service` is a shared-state action. **Stop and ask the user** to approve: `cd ~/kode/sortify && git merge --ff-only master-branch-tip && systemctl --user restart sortify`, then a free re-cluster of `{teh bomb}` (Split view → re-cluster, or `curl -s -X POST localhost:8800/api/split/3km9EmUcfrlQKKqRincV6T -H 'content-type: application/json' -d '{}'` — 0 Spotify calls, cached tracks + tags). Do not proceed with the restart without the user's yes; the rest of the plan does not depend on it and may continue.

- [ ] **Step 5: Commit state** — nothing extra to commit; record the merge hash in the SDD ledger.

---

### Task 2: Merge master and the materialise machinery into `queued-materialiser`

**Files:**
- Merge brings: `sortify/app.py` (+370 machinery lines), `sortify/static/app.js`, `sortify/static/style.css`, `tests/test_materialise.py`, `tests/ui_harness.mjs` additions, spec doc.

**Interfaces:**
- Produces (used by Tasks 6–9, verbatim from the branch): `_materialise_plan(split, pile) -> dict` with keys `record/stale/missing/need_create/calls/record_view`; `_claim_materialisation(split_playlist_id, pile_id, claim, added_uri=None, **fields) -> bool`; `_rerecord_materialisation(split_playlist_id, pile_id, record) -> bool`; `_pile_fingerprint(pile) -> str`; `_unique(uris) -> list`; `_pending_materialise: set`; `MATERIALISE_DESCRIPTION`; endpoint `POST /api/split/{playlist_id}/materialise` (removed in Task 7).

- [ ] **Step 1: Merge**

```bash
cd ~/kode/sortify-lastfm && git checkout queued-materialiser
git merge --no-ff master          # picks up Task 1's stoplist
git merge --no-ff pile-materialise
```

The stoplist commit exists on both sides (cherry-pick + original); git merges identical hunks silently. Resolve any conflicts by taking both sides' intent; nothing on the two branches edits the same lines except possibly `tests/ui_harness.mjs` fixture counts.

- [ ] **Step 2: Verify both orderings + harness** (same three commands as Task 1 Step 2). Expected: ~284 pytest tests green, harness green (the branch's harness has more than 40 checks — all must pass).

- [ ] **Step 3: Commit** (the merge commits are the commits).

---

### Task 3: `store.py` — queue and pacing files, versioned, guard-on-read

**Files:**
- Modify: `sortify/store.py` (append after `save_splits`)
- Test: `tests/test_store_and_auth.py` (append)

**Interfaces:**
- Produces: `Store.queue() -> dict`, `Store.save_queue(payload)`, `Store.pacing() -> dict`, `Store.save_pacing(payload)`. Empty defaults: `{"version": 1, "playlist_id": None, "pending": [], "current": None, "state": "stopped", "stop_reason": None, "progress": {}, "enqueued_at": None, "updated_at": None}` and `{"version": 1, "rate_per_min": 1.8, "ceiling": 7.0, "clean_since": null, "max_clean_rate": None, "history_429": [], "updated_at": None}`. A file with the wrong `version` is treated as absent (guard-on-read, like `tag_artists()`); `_atomic_write` already yields 0600 files (mkstemp default) — boxdash contract satisfied for free.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_store_and_auth.py`):

```python
def test_queue_and_pacing_default_when_missing(tmp_path):
    s = Store(tmp_path)
    assert s.queue()["state"] == "stopped" and s.queue()["version"] == 1
    assert s.pacing()["rate_per_min"] == 1.8 and s.pacing()["ceiling"] == 7.0


def test_queue_and_pacing_round_trip_and_are_private(tmp_path):
    s = Store(tmp_path)
    q = s.queue(); q.update(playlist_id="PL", pending=["p2"], state="running")
    s.save_queue(q)
    assert s.queue()["pending"] == ["p2"]
    import os, stat
    mode = stat.S_IMODE(os.stat(tmp_path / "queue.json").st_mode)
    assert mode == 0o600  # boxdash reads these; nobody else should


def test_wrong_version_reads_as_default(tmp_path):
    s = Store(tmp_path)
    (tmp_path / "pacing.json").write_text('{"version": 99, "rate_per_min": 40}')
    assert s.pacing()["rate_per_min"] == 1.8  # a v99 file must not set our pace
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest -q tests/test_store_and_auth.py` → FAIL: `AttributeError: 'Store' object has no attribute 'queue'`.

- [ ] **Step 3: Implement** (append to `Store`):

```python
    # queue.json / pacing.json: the queued materialiser's persisted state.
    # boxdash reads BOTH files directly (its house pattern), so their shape is
    # a published contract: versioned envelopes, guard-on-read like tags.json,
    # written atomically (0600 via mkstemp) so a half-written file is never
    # visible and the card keeps working while sortify is down.
    QUEUE_DEFAULT = {"version": 1, "playlist_id": None, "pending": [],
                     "current": None, "state": "stopped", "stop_reason": None,
                     "progress": {}, "enqueued_at": None, "updated_at": None}
    PACING_DEFAULT = {"version": 1, "rate_per_min": 1.8, "ceiling": 7.0,
                      "clean_since": None, "max_clean_rate": None,
                      "history_429": [], "updated_at": None}

    def _versioned(self, name: str, default: dict) -> dict:
        data = self._load(name, default)
        return data if isinstance(data, dict) and data.get("version") == default["version"] else dict(default)

    def queue(self) -> dict:
        return self._versioned("queue.json", self.QUEUE_DEFAULT)

    def save_queue(self, payload: dict) -> None:
        self._save("queue.json", payload)

    def pacing(self) -> dict:
        return self._versioned("pacing.json", self.PACING_DEFAULT)

    def save_pacing(self, payload: dict) -> None:
        self._save("pacing.json", payload)
```

(Return `dict(default)` copies so callers can't mutate the class constant; note `QUEUE_DEFAULT`/`PACING_DEFAULT` are class attributes — a mutation bug here would poison every Store, hence the copy.)

- [ ] **Step 4: Run to pass**, then both orderings. **Step 5: Commit** — `git add sortify/store.py tests/test_store_and_auth.py && git commit`.

---

### Task 4: `sortify/pacing.py` — the Governor (pure, no I/O)

**Files:**
- Create: `sortify/pacing.py`
- Test: `tests/test_pacing.py` (new)

**Interfaces:**
- Produces: `Governor(state: dict | None)`; `.rate -> float` (calls/min); `.interval() -> float` (seconds); `.note_success(now: float)`; `.note_429(kind: str, retry_after: int, now: float)`; `.note_interruption()`; `.to_state() -> dict` (exact pacing.json shape from Task 3). Module constants `START_RATE = 1.8`, `CEILING_RATE = 7.0`, `SHRINK = 0.85`, `CLEAN_SECONDS = 15 * 60`, `MIN_RATE = 0.9`.
- Consumes: nothing from the rest of sortify (imports only stdlib) — that is a test.

- [ ] **Step 1: Write the failing tests** (`tests/test_pacing.py`):

```python
"""The governor is the measuring instrument: it must climb exactly the
approved ladder, retreat on a rate 429, stop dead on a quota trip (the caller
does the stopping — the governor just records), and never exceed 7.0/min.
All time is injected; nothing here sleeps or calls anything."""

import time

from sortify.pacing import (CEILING_RATE, CLEAN_SECONDS, MIN_RATE, SHRINK,
                            START_RATE, Governor)


def test_pacing_imports_nothing_from_sortify():
    """Pure logic: the governor must be testable and reviewable in isolation."""
    import inspect
    import sortify.pacing as p
    src = inspect.getsource(p)
    assert "from .spotify" not in src and "from .store" not in src
    assert "import httpx" not in src


def test_starts_at_known_good_rate():
    g = Governor(None)
    assert g.rate == START_RATE == 1.8
    assert abs(g.interval() - 60 / 1.8) < 0.01


def test_climbs_the_approved_ladder_and_stops_at_the_ceiling():
    g = Governor(None)
    t = 1_000_000.0
    seen = [g.rate]
    for _ in range(15):
        # a clean 15-minute block: successes at t and t + CLEAN_SECONDS
        g.note_success(t)
        t += CLEAN_SECONDS
        g.note_success(t)
        seen.append(round(g.rate, 1))
    ladder = [1.8, 2.1, 2.5, 2.9, 3.4, 4.0, 4.7, 5.5, 6.5, 7.0]
    assert seen[: len(ladder)] == ladder
    assert max(seen) == CEILING_RATE == 7.0


def test_max_clean_rate_records_the_rate_that_survived_not_the_new_one():
    g = Governor(None)
    g.note_success(0.0)
    g.note_success(CLEAN_SECONDS)          # 1.8 survived 15 clean minutes
    assert g.to_state()["max_clean_rate"] == 1.8
    assert round(g.rate, 1) == 2.1


def test_rate_429_halves_and_resets_the_clean_clock():
    g = Governor({"version": 1, "rate_per_min": 4.0, "ceiling": 7.0,
                  "clean_since": 100.0, "max_clean_rate": 3.4,
                  "history_429": [], "updated_at": None})
    g.note_429("rate", 12, now=200.0)
    assert g.rate == 2.0
    st = g.to_state()
    assert st["clean_since"] is None
    assert st["history_429"][-1] == {"when": 200.0, "kind": "rate",
                                     "rate": 4.0, "retry_after": 12}
    assert st["max_clean_rate"] == 3.4     # history survives the retreat


def test_halving_floors_at_min_rate():
    g = Governor(None)
    for i in range(6):
        g.note_429("rate", 5, now=float(i))
    assert g.rate == MIN_RATE == 0.9


def test_quota_trip_is_recorded_but_rate_untouched():
    """Stopping is the worker's job (permanent, human-only resume); the
    governor only keeps the evidence."""
    g = Governor(None)
    g.note_429("quota", 86400, now=50.0)
    assert g.to_state()["history_429"][-1]["kind"] == "quota"
    assert g.rate == START_RATE


def test_interruption_resets_to_start_rate_and_clears_the_clock():
    """After a pause, a midnight sleep, a quiet period or a process restart
    the world has changed; re-climb from the known-good floor. The climb to
    ceiling costs ~2.25h — noise against a ~3-day job."""
    g = Governor(None)
    g.note_success(0.0); g.note_success(CLEAN_SECONDS)
    assert g.rate > START_RATE
    g.note_interruption()
    assert g.rate == START_RATE and g.to_state()["clean_since"] is None
    assert g.to_state()["max_clean_rate"] == 1.8   # the measurement survives


def test_state_round_trip_matches_pacing_json_shape():
    g = Governor(None)
    g.note_success(10.0)
    st = g.to_state()
    assert set(st) == {"version", "rate_per_min", "ceiling", "clean_since",
                       "max_clean_rate", "history_429", "updated_at"}
    assert Governor(st).rate == g.rate
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: sortify.pacing`.

- [ ] **Step 3: Implement** (`sortify/pacing.py`):

```python
"""Rate governor for the queued materialiser — the measuring instrument.

Evidence base (spec §Evidence): 678 calls @ 9.7/min earned a ~23h quota ban;
1208 @ 1.8/min was penalty-free. The band between is unmeasured, and this
job's ~1240 calls of real traffic are the one legitimate chance to measure
it. So: start at the known-good 1.8/min, and after every 15 CLEAN minutes
shrink the interval 15% — 1.8, 2.1, 2.5, 2.9, 3.4, 4.0, 4.7, 5.5, 6.5 —
capped at 7.0/min, 28% under known-bad: a probe allowed to touch the
boundary has learned nothing and paid full price.

Pure logic: injected clocks, no I/O, no imports from the rest of sortify.
The worker owns persistence (store.save_pacing) and all actual sleeping.
"""

from __future__ import annotations

import time

START_RATE = 1.8          # calls/min — spotify-autoqueuer's proven pace
CEILING_RATE = 7.0        # never probe closer than 28% to the known ban rate
SHRINK = 0.85             # interval *= 0.85 == rate /= 0.85, ≈ +18% rate
CLEAN_SECONDS = 15 * 60   # a rate must survive this long before escalating
MIN_RATE = 0.9            # halving floor; below this we are pathologically shy


class Governor:
    def __init__(self, state: dict | None = None):
        state = state or {}
        self.rate: float = float(state.get("rate_per_min") or START_RATE)
        self._clean_since: float | None = state.get("clean_since")
        self._max_clean: float | None = state.get("max_clean_rate")
        self._history: list[dict] = list(state.get("history_429") or [])

    def interval(self) -> float:
        return 60.0 / self.rate

    def note_success(self, now: float) -> None:
        if self._clean_since is None:
            self._clean_since = now
            return
        if now - self._clean_since >= CLEAN_SECONDS:
            # This rate survived a full clean block: it is the new measured
            # maximum, and we earn one step up the ladder.
            self._max_clean = max(self._max_clean or 0.0, round(self.rate, 1))
            self.rate = round(min(self.rate / SHRINK, CEILING_RATE), 1)
            self._clean_since = now

    def note_429(self, kind: str, retry_after: int, now: float) -> None:
        self._history.append({"when": now, "kind": kind,
                              "rate": round(self.rate, 1),
                              "retry_after": int(retry_after)})
        self._clean_since = None
        if kind == "rate":
            self.rate = round(max(self.rate / 2.0, MIN_RATE), 1)
        # kind == "quota": the worker stops permanently; leaving the rate
        # alone keeps the evidence of what we were doing when it tripped.

    def note_interruption(self) -> None:
        """Pause, midnight sleep, quiet period, or process restart: the
        conditions the clean clock measured no longer hold. Re-climb."""
        self.rate = START_RATE
        self._clean_since = None

    def to_state(self) -> dict:
        return {"version": 1, "rate_per_min": round(self.rate, 1),
                "ceiling": CEILING_RATE, "clean_since": self._clean_since,
                "max_clean_rate": self._max_clean,
                "history_429": self._history, "updated_at": time.time()}
```

Check the ladder arithmetic against the test before running: 1.8/0.85=2.118→2.1, 2.1/0.85=2.47→2.5, 2.5/0.85=2.94→2.9, 2.9/0.85=3.41→3.4, 3.4/0.85=4.0, 4.0/0.85=4.71→4.7, 4.7/0.85=5.53→5.5, 5.5/0.85=6.47→6.5, 6.5/0.85=7.65→7.0. Matches the spec's ladder exactly.

- [ ] **Step 4: Run to pass**, both orderings. **Step 5: Commit.**

---

### Task 5: `spotify.py` — the "bulk" budget class + 429 observation

**Files:**
- Modify: `sortify/spotify.py`
- Test: `tests/test_budget.py` (append)

**Interfaces:**
- Produces: `BULK_RESERVE = 150`; `Spotify.request(..., bulk: bool = False)`; `_spend_budget(background=False, bulk=False)` refusing bulk past `DAILY_CAP - BULK_RESERVE`; `usage["bulk"]` counter; `Spotify.bulk_spent() -> int`; `Spotify.bulk_block_reason() -> tuple[str, float] | None` returning `(reason, resume_at_ts)` with reason ∈ {"cooldown", "quiet", "reserve"}; `Spotify.last_429: dict | None` (`{"ts", "kind", "retry_after"}`); `add_to_playlist(pid, uri, bulk=False)` and `create_playlist(name, description="", bulk=False)` pass-throughs.
- Consumes: `next_local_midnight` already imported as `_next_local_midnight` from `account_ledger`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_budget.py`, matching its existing fixture style — it builds `Spotify(Store(tmp_path))` and manipulates `usage.json` directly):

```python
def test_bulk_never_spends_the_interactive_reserve(sp_and_store):
    """DAILY_CAP−150 is the line: the last 150 calls of the day belong to the
    user's own clicks, not to the unattended job."""
    sp, store = sp_and_store
    store.save_usage({"day": time.strftime("%Y-%m-%d"),
                      "count": DAILY_CAP - BULK_RESERVE, "background": 0})
    with pytest.raises(SpotifyError) as e:
        sp._spend_budget(bulk=True)
    assert "reserve" in str(e.value)
    # …and an interactive call at the same spend level still goes through.
    sp._spend_budget()
    assert sp.budget_spent() == DAILY_CAP - BULK_RESERVE + 1


def test_bulk_spend_is_its_own_bucket(sp_and_store):
    sp, store = sp_and_store
    sp._spend_budget(bulk=True)
    u = store.usage()
    assert u["bulk"] == 1 and u["count"] == 1 and u.get("background", 0) == 0
    assert sp.bulk_spent() == 1


def test_bulk_block_reason_orders_cooldown_quiet_reserve(sp_and_store, monkeypatch):
    sp, store = sp_and_store
    now = time.time()
    # cooldown active
    monkeypatch.setattr(sp, "effective_cooldown_until", lambda: now + 100)
    reason, until = sp.bulk_block_reason()
    assert reason == "cooldown" and until == pytest.approx(now + 100, abs=2)
    # cooldown over, quiet running — QUIET_AFTER_COOLDOWN applies to bulk:
    # this is exactly "the next proactive job" that rail was kept for.
    monkeypatch.setattr(sp, "effective_cooldown_until", lambda: now - 10)
    reason, until = sp.bulk_block_reason()
    assert reason == "quiet" and until == pytest.approx(now - 10 + QUIET_AFTER_COOLDOWN, abs=2)
    # quiet over, reserve line reached → sleep till local midnight
    monkeypatch.setattr(sp, "effective_cooldown_until", lambda: 0.0)
    store.save_usage({"day": time.strftime("%Y-%m-%d"),
                      "count": DAILY_CAP - BULK_RESERVE, "background": 0})
    reason, until = sp.bulk_block_reason()
    assert reason == "reserve" and until > now
    # clear day: no block
    store.save_usage({"day": time.strftime("%Y-%m-%d"), "count": 0, "background": 0})
    assert sp.bulk_block_reason() is None


def test_request_records_the_last_429_without_changing_retries(sp_and_store, monkeypatch):
    """Additive observation only (finding I2 forbids touching retry
    behaviour): a transient rate 429 that request retries internally must
    still be visible to the governor afterwards."""
    sp, _ = sp_and_store
    responses = [FakeResponse(429, headers={"Retry-After": "1"},
                              text='{"error": {"status": 429}}'),
                 FakeResponse(200, json_body={"ok": True})]
    monkeypatch.setattr(sp.http, "request", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(sp, "_access_token", lambda: "tok")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = sp.request("GET", "/ping")
    assert out == {"ok": True}
    assert sp.last_429 and sp.last_429["kind"] == "rate" and sp.last_429["retry_after"] == 1
```

(Adopt the file's actual fixture/fake names — it already has a fake-response pattern for `classify_429` tests; reuse it rather than inventing `FakeResponse` if one exists.)

- [ ] **Step 2: Run to verify failure** — `TypeError: _spend_budget() got an unexpected keyword argument 'bulk'` etc.

- [ ] **Step 3: Implement.** In `sortify/spotify.py`:

Constant, after `BACKGROUND_DAILY_CAP`:

```python
# The queued materialiser's spend class: user-initiated but unattended. It
# counts toward DAILY_CAP, but the day's LAST 150 calls are reserved for the
# user's own interactive clicks — the bulk job sleeps to local midnight
# instead of spending them. (Spec 2026-08-18, decision 3.)
BULK_RESERVE = 150
```

In `__init__`, after `self._refresh_fail_until = 0.0`:

```python
        # Last 429 seen by request(), including ones it retried through —
        # the queue governor reads this to know a tick was not clean.
        # Observation only; retry behaviour is unchanged (review finding I2).
        self.last_429: dict | None = None
```

`bulk_spent` + `bulk_block_reason`, next to `background_block_reason`:

```python
    def bulk_spent(self) -> int:
        usage = self.store.usage()
        if usage["day"] != time.strftime("%Y-%m-%d"):
            return 0
        return usage.get("bulk", 0)

    def bulk_block_reason(self) -> tuple[str, float] | None:
        """Why the bulk worker must not call right now — None means go.

        Returns (reason, resume_at). QUIET_AFTER_COOLDOWN applies here on
        purpose: this rail survived the enricher's deletion named for "the
        next proactive job", and the queued materialiser is that job.
        """
        now = time.time()
        cd = self.effective_cooldown_until()
        if now < cd:
            return ("cooldown", cd)
        if cd and now < cd + QUIET_AFTER_COOLDOWN:
            return ("quiet", cd + QUIET_AFTER_COOLDOWN)
        if self.budget_spent() >= DAILY_CAP - BULK_RESERVE:
            return ("reserve", _next_local_midnight(now))
        return None
```

(Note `quiet_until()` returns `cd + QUIET` only when `cd` truthy — mirror that: a never-cooled client has no quiet period.)

`_spend_budget(self, background: bool = False, bulk: bool = False)` — add after the background cap check:

```python
            usage.setdefault("bulk", 0)
            if bulk and usage["count"] >= DAILY_CAP - BULK_RESERVE:
                raise SpotifyError(
                    429,
                    f"bulk budget: interactive reserve ({BULK_RESERVE} calls) "
                    "reached — sleeping until midnight",
                )
```

and before `self.store.save_usage(usage)`:

```python
            if bulk:
                usage["bulk"] += 1
```

(Also update the day-rollover reset dict to `{"day": today, "count": 0, "background": 0, "bulk": 0}`.)

`request(self, method, path, background=False, bulk=False, **kwargs)`: pass `self._spend_budget(background=background, bulk=bulk)`; in the 429 branch, immediately after `kind = classify_429(...)`:

```python
                self.last_429 = {"ts": time.time(), "kind": kind,
                                 "retry_after": retry_after}
```

Mutation pass-throughs:

```python
    def add_to_playlist(self, playlist_id: str, uri: str, bulk: bool = False) -> str | None:
        resp = self.request("POST", f"/playlists/{playlist_id}/items",
                            json={"uris": [uri]}, bulk=bulk)
        return (resp or {}).get("snapshot_id")
```

and the same `bulk: bool = False` → `self.request(..., bulk=bulk)` on `create_playlist`.

- [ ] **Step 4: Run to pass**, both orderings. **Step 5: Commit.**

---

### Task 6: Sweep orphaned materialisation records on re-cluster (finding I3)

A re-cluster that shrinks 9 piles to 8 leaves a record keyed `p9` that no pile row will ever show again — a playlist sortify made becomes untraceable. Sweep such records to `materialised_history` when `create_split` carries records forward. (Fingerprint-stale records under a still-existing id stay put — the existing stale flow prices and sweeps those at materialise time.)

**Files:**
- Modify: `sortify/app.py` — `create_split`'s carry-forward block (search for `"materialised": prev.get("materialised", {})`)
- Test: `tests/test_materialise.py` (append)

**Interfaces:**
- Consumes: Task 2's carried-forward keys. Produces: no new API; `materialised_history` entries gain `"swept": "recluster"`.

- [ ] **Step 1: Failing test** (append; use the file's existing `client` fixture and `_split` helper):

```python
def test_recluster_sweeps_records_for_vanished_pile_ids_to_history(client):
    """9 piles → 8 must not orphan p9's record (finding I3): the playlist is
    real, and history is the only place it stays traceable."""
    s = Store()
    payload = s.splits()
    payload["splits"]["PLM"]["materialised"] = {
        "p9": {"playlist_id": "OLD9", "pile_id": "p9", "name": "gone pile",
               "fingerprint": "beef", "track_count": 3,
               "added": ["spotify:track:x"], "claim": "c",
               "created_at": "t", "updated_at": "t"}}
    s.save_splits(payload)
    r = client.post("/api/split/PLM", json={})     # re-cluster, 0 calls
    assert r.status_code == 200
    split = Store().splits()["splits"]["PLM"]
    assert "p9" not in split.get("materialised", {})
    hist = split["materialised_history"]
    assert hist and hist[-1]["playlist_id"] == "OLD9" and hist[-1]["swept"] == "recluster"
    assert client.calls == []                       # free, like every re-cluster
```

(Adapt the POST path/body to `create_split`'s real signature — it is `POST /api/split/{playlist_id}` with `SplitParams`; check the file.)

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — in `create_split`, replace the plain carry-forward with:

```python
            # Carried forward for the same reason `decided` is (see below) —
            # but a record whose pile id no longer exists after this
            # re-cluster would never be shown or priced again. Sweep those to
            # history so a playlist sortify made never stops being traceable
            # to the pile it came from (review finding I3).
            new_ids = {p["id"] for p in piles}
            carried, history = {}, list(prev.get("materialised_history", []))
            for pid, rec in (prev.get("materialised") or {}).items():
                if pid in new_ids:
                    carried[pid] = rec
                else:
                    history.append({**rec, "swept": "recluster"})
```

and use `"materialised": carried, "materialised_history": history` in the payload dict.

- [ ] **Step 4: Run to pass**, both orderings. **Step 5: Commit.**

---

### Task 7: `_materialise_tick` — one call per invocation; the one-shot endpoint dies

This is the delivery replacement: the reviewed machinery stays; the blocking loop and its endpoint go.

**Files:**
- Modify: `sortify/app.py` — extract from `materialise_pile`; delete `MaterialiseIn` and the `@app.post(".../materialise")` endpoint; keep `_abandon_unrecorded_playlist`, `_readopt_materialisation`, `_claim_materialisation`, `_rerecord_materialisation`, `_materialise_plan`, `_pile_fingerprint`, `_unique`, `_pending_materialise` unchanged.
- Test: `tests/test_materialise.py` (rework the endpoint-driven tests; hazard-helper tests stand).

**Interfaces:**
- Produces: `_materialise_tick(playlist_id: str, pile_id: str) -> dict` returning `{"spent": 0 | 1, "done": bool, "gone": bool}` — `gone` means split/pile vanished; `done` means nothing left to spend. Raises `SpotifyError` upward (the worker classifies). At most ONE Spotify call per invocation, `bulk=True` on both mutation kinds.

- [ ] **Step 1: Write the failing tests.** Rework `tests/test_materialise.py`: delete tests that POST `/api/split/PLM/materialise` (the endpoint is gone — Task 9's queue endpoint re-covers the echo/refusal semantics); keep every test of `_materialise_plan` / `_claim_materialisation` / `_rerecord_materialisation` / fingerprints untouched; add tick tests:

```python
def tick(pile_id="p1"):
    return appmod._materialise_tick("PLM", pile_id)


def test_first_tick_stamps_record_then_creates_only(client):
    out = tick()
    assert out == {"spent": 1, "done": False, "gone": False}
    assert client.calls == [("create", "cumbia · latin · salsa")]
    rec = record()
    assert rec["playlist_id"] == "NEWP" and rec["added"] == []
    assert rec["fingerprint"] and rec["claim"]


def test_each_following_tick_adds_exactly_one_track_in_order(client):
    tick()
    for i, uri in enumerate(PILE_URIS):
        out = tick()
        assert out["spent"] == 1
        assert client.calls[-1] == ("add", "NEWP", uri)
        assert record()["added"] == PILE_URIS[: i + 1]
    assert tick() == {"spent": 0, "done": True, "gone": False}
    assert len(client.calls) == len(PILE_URIS) + 1   # cost identical to the old loop


def test_tick_resumes_a_partial_record_without_a_second_create(client):
    tick(); tick(); tick()                    # create + 2 adds
    out = tick()
    assert out["spent"] == 1 and client.calls.count(("create", "cumbia · latin · salsa")) == 1


def test_tick_on_a_stale_record_sweeps_it_and_starts_fresh(client):
    tick(); tick()
    s = Store(); payload = s.splits()
    payload["splits"]["PLM"]["piles"][0]["uris"] = ["spotify:track:changed"]
    s.save_splits(payload)
    out = tick()
    assert out["spent"] == 1 and client.calls[-1][0] == "create"
    hist = Store().splits()["splits"]["PLM"]["materialised_history"]
    assert hist[-1]["playlist_id"] == "NEWP"


def test_tick_spends_from_the_bulk_bucket(client, monkeypatch):
    """The unattended job must never masquerade as interactive traffic."""
    seen = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="", bulk=False: seen.append(bulk) or "NEWP")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False: seen.append(bulk) or "snap")
    tick(); tick()
    assert seen == [True, True]


def test_a_vanished_pile_reports_gone_and_spends_nothing(client):
    assert tick("nope") == {"spent": 0, "done": True, "gone": True}
    assert client.calls == []


def test_tick_holds_nothing_blocking_under_split_lock(client, monkeypatch):
    """Same rule the sitting path pins: the Spotify call happens outside
    _split_lock, or every /api/now poll stalls behind the worker."""
    def slow_create(name, description="", bulk=False):
        assert not appmod._split_lock.locked()
        return "NEWP"
    monkeypatch.setattr(appmod.sp, "create_playlist", slow_create)
    tick()
```

(The `client` fixture's monkeypatched fakes need `bulk=False` kwargs added — update the fixture. The existing double-click/409 endpoint tests translate to Task 9's queue endpoint; note them in a comment for that task.)

- [ ] **Step 2: Run to verify failure** — `AttributeError: _materialise_tick`.

- [ ] **Step 3: Implement.** Replace the endpoint with (keeping the machinery section's long comment block, updated to say delivery is now the queue):

```python
def _materialise_tick(playlist_id: str, pile_id: str) -> dict:
    """Advance one pile's materialisation by at most ONE Spotify call.

    The one-shot endpoint this replaces spent a whole pile in a blocking
    loop — measured at 12.4 calls/min for 25 minutes, above the rate that
    earned the 2026-08-13 ban. Same machinery, same records, same claims;
    the only change is that the loop now lives in the queue worker, which
    owns the pacing between calls. Returns {"spent", "done", "gone"};
    SpotifyError propagates to the caller, which classifies it.
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        pile = next((p for p in (split or {}).get("piles", []) if p["id"] == pile_id), None)
        if not split or not pile:
            return {"spent": 0, "done": True, "gone": True}
        if (playlist_id, pile_id) in _pending_materialise:
            return {"spent": 0, "done": False, "gone": False}
        plan = _materialise_plan(split, pile)
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
        next_uri = None if need_create else plan["missing"][0]
        _pending_materialise.add((playlist_id, pile_id))

    try:
        if need_create:
            new_id = sp.create_playlist(pile["name"], MATERIALISE_DESCRIPTION, bulk=True)
            if not _claim_materialisation(playlist_id, pile_id, claim, playlist_id=new_id):
                _abandon_unrecorded_playlist(playlist_id, pile_id, new_id, record)
        else:
            sp.add_to_playlist(record["playlist_id"], next_uri, bulk=True)
            if not _claim_materialisation(playlist_id, pile_id, claim, added_uri=next_uri):
                _readopt_materialisation(playlist_id, pile_id, record,
                                         record["playlist_id"], [next_uri])
    finally:
        with _split_lock:
            _pending_materialise.discard((playlist_id, pile_id))
    remaining = len(plan["missing"]) - (0 if need_create else 1)
    return {"spent": 1, "done": remaining == 0 and not need_create, "gone": False}
```

One subtlety to preserve: the tick re-mints the claim only when it has to stamp a fresh record; on a resumed pile it reuses the persisted claim, which is fine because the worker is the only writer and `_claim_materialisation` still CASes. Delete `MaterialiseIn`, the `materialise_pile` endpoint, and the frontend's POST wiring compiles later (Task 10) — for now `app.js` may still reference the dead route; the harness is updated in Task 10, so run only pytest orderings for this task and note the harness is expected red until Task 10.

- [ ] **Step 4: Run to pass** — both pytest orderings green; `node tests/ui_harness.mjs` known-red (record which checks). **Step 5: Commit.**

---

### Task 8: Queue decision logic + worker loop (no endpoints yet)

**Files:**
- Modify: `sortify/app.py` (new section after the materialise machinery)
- Test: `tests/test_queue_worker.py` (new)

**Interfaces:**
- Consumes: `_materialise_tick` (Task 7), `Governor` (Task 4), `store.queue()/save_queue/pacing/save_pacing` (Task 3), `sp.bulk_block_reason()` + `sp.last_429` (Task 5).
- Produces: `_queue_next_action(now: float) -> tuple` — one of `("stop", reason)`, `("sleep", seconds, state_label)`, `("tick", playlist_id, pile_id)`; `_drain_queue() -> None` (the thread target); `_queue_wake: threading.Event`; `_queue_lock: threading.Lock`; `_set_queue_state(state, stop_reason=None)`; module global `_queue_worker: threading.Thread | None = None`. Task 9 wires endpoints to these.

- [ ] **Step 1: Write the failing tests** (`tests/test_queue_worker.py`) — fixture mirrors `test_materialise.py`'s (fake `create_playlist`/`add_to_playlist` with `bulk` kwargs, capture/restore splits+cache+queue+pacing files in try/finally; conftest's temp data dir isolates the rest):

```python
"""The queue worker: one call per tick, governor-paced, and every way it must
STOP spending. Threads here are real (house rule: verify by execution);
intervals are forced to ~0 so a test runs in milliseconds."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
import sortify.pacing as pacing
from sortify.spotify import SpotifyError
from sortify.store import Store

PILE_URIS = [f"spotify:track:m{i}" for i in range(3)]
TINY = [f"spotify:track:t{i}" for i in range(1)]


@pytest.fixture
def worker_env(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="", bulk=False: calls.append(("create", name)) or f"NEW-{name}")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False: calls.append(("add", pid, uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "bulk_block_reason", lambda: None)
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 0.0)
    s = Store()
    originals = {f: getattr(s, f)() for f in ("splits", "queue", "pacing")}
    s.save_splits({"version": 1, "splits": {"PLQ": {
        "created_at": "t", "snapshot_id": None, "params": {},
        "piles": [
            {"id": "p1", "name": "big", "tags": [], "uris": list(PILE_URIS)},
            {"id": "p2", "name": "tiny", "tags": [], "uris": list(TINY)},
        ],
        "decided": {}, "active_sitting": None}}})
    yield calls, s
    appmod._queue_wake.set()
    if appmod._queue_worker:
        appmod._queue_worker.join(timeout=5)
    appmod._queue_worker = None
    appmod._queue_wake.clear()
    s.save_splits(originals["splits"])
    s.save_queue(originals["queue"])
    s.save_pacing(originals["pacing"])


def start_queue(s, pending=("p2", "p1")):
    q = s.queue()
    q.update(playlist_id="PLQ", pending=list(pending), current=None,
             state="running", stop_reason=None)
    s.save_queue(q)
    appmod._queue_wake.clear()
    appmod._queue_worker = threading.Thread(target=appmod._drain_queue, daemon=True)
    appmod._queue_worker.start()


def wait_done(s, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if s.queue()["state"] in ("done", "stopped", "paused"):
            appmod._queue_worker.join(timeout=2)
            return s.queue()
        time.sleep(0.01)
    raise AssertionError(f"queue never settled: {s.queue()}")


def test_drains_every_pile_smallest_first_one_call_per_tick(worker_env):
    calls, s = worker_env
    start_queue(s)
    q = wait_done(s)
    assert q["state"] == "done"
    assert calls[0] == ("create", "tiny")               # smallest pile first
    assert calls[1] == ("add", "NEW-tiny", TINY[0])
    assert calls[2] == ("create", "big")
    assert [c[2] for c in calls[3:]] == PILE_URIS
    assert len(calls) == len(TINY) + len(PILE_URIS) + 2  # exact total, no probes


def test_pause_is_instant_and_the_thread_exits(worker_env, monkeypatch):
    calls, s = worker_env
    gate = threading.Event()
    monkeypatch.setattr(pacing.Governor, "interval", lambda self: 30.0)
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri, bulk=False: gate.set() or calls.append(("add", pid, uri)) or "snap")
    start_queue(s, pending=("p1",))
    assert gate.wait(5)
    q = s.queue(); q["state"] = "paused"; s.save_queue(q)
    appmod._queue_wake.set()                             # what the endpoint does
    t0 = time.time()
    appmod._queue_worker.join(timeout=5)
    assert not appmod._queue_worker.is_alive() and time.time() - t0 < 5
    assert s.queue()["state"] == "paused"                # resumable at the price of what's left


def test_quota_trip_stops_permanently_with_reason(worker_env, monkeypatch):
    calls, s = worker_env
    def quota_create(name, description="", bulk=False):
        appmod.sp.last_429 = {"ts": time.time(), "kind": "quota", "retry_after": 86400}
        raise SpotifyError(429, "Spotify daily quota spent — cooldown ~23h.")
    monkeypatch.setattr(appmod.sp, "create_playlist", quota_create)
    start_queue(s, pending=("p2",))
    q = wait_done(s)
    assert q["state"] == "stopped" and q["stop_reason"] == "quota"
    hist = s.pacing()["history_429"]
    assert hist and hist[-1]["kind"] == "quota"
    assert calls == []                                   # no retry into a quota trip


def test_rate_429_halves_and_keeps_going(worker_env, monkeypatch):
    calls, s = worker_env
    first = {"burned": False}
    def flaky_add(pid, uri, bulk=False):
        if not first["burned"]:
            first["burned"] = True
            appmod.sp.last_429 = {"ts": time.time(), "kind": "rate", "retry_after": 1}
            raise SpotifyError(429, "rate limit hit — cooldown ~1 min")
        calls.append(("add", pid, uri)); return "snap"
    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky_add)
    start_queue(s, pending=("p2",))
    q = wait_done(s)
    assert q["state"] == "done"
    assert [c[0] for c in calls].count("add") == 1       # the failed add was re-driven
    assert s.pacing()["history_429"][-1]["kind"] == "rate"
    assert s.pacing()["rate_per_min"] == pacing.MIN_RATE  # 1.8 halved, floored


def test_block_reason_sleeps_with_the_labelled_state(worker_env, monkeypatch):
    calls, s = worker_env
    blocked = {"on": True}
    monkeypatch.setattr(appmod.sp, "bulk_block_reason",
                        lambda: ("reserve", time.time() + 60) if blocked["on"] else None)
    start_queue(s, pending=("p2",))
    deadline = time.time() + 5
    while time.time() < deadline and s.queue()["state"] != "sleeping":
        time.sleep(0.01)
    assert s.queue()["state"] == "sleeping" and calls == []
    blocked["on"] = False
    appmod._queue_wake.set(); appmod._queue_wake.clear()
    q = wait_done(s)
    assert q["state"] == "done" and len(calls) == 2


def test_progress_snapshot_is_written_for_boxdash(worker_env):
    calls, s = worker_env
    start_queue(s)
    wait_done(s)
    prog = s.queue()["progress"]
    assert prog["pile_count"] == 2 and prog["spent_today"] >= 0
    assert prog["daily_cap"] == 600 and prog["reserve"] == 150
```

- [ ] **Step 2: Run to verify failure** — `AttributeError: _drain_queue`.

- [ ] **Step 3: Implement** (new section in `app.py`; import `Governor` from `.pacing` and `BULK_RESERVE, DAILY_CAP` names via the existing `from .spotify import (...)` block):

```python
# ---- the queued materialiser: one paced call per tick ----------------------
#
# The worker thread is created ONLY by the enqueue/resume endpoints (Task 9);
# there is no code path from boot to Spotify traffic, and
# tests/test_no_proactive_work.py pins that. Pacing belongs to the Governor
# (sortify/pacing.py); stopping belongs to _queue_next_action; the single
# Spotify call per tick belongs to _materialise_tick. queue.json and
# pacing.json are read directly by boxdash, so both are rewritten (atomic,
# versioned) after every state change, not just at exit.

_queue_lock = threading.Lock()
_queue_wake = threading.Event()          # set() = "wake now and re-decide"
_queue_worker: threading.Thread | None = None


def _set_queue_state(state: str, stop_reason: str | None = None) -> None:
    with _queue_lock:
        q = store.queue()
        q["state"] = state
        q["stop_reason"] = stop_reason
        q["updated_at"] = _now_iso()
        store.save_queue(q)


def _queue_progress(q: dict) -> dict:
    """The boxdash snapshot: everything the card shows, in one read."""
    split = store.splits()["splits"].get(q.get("playlist_id") or "", {})
    piles = {p["id"]: p for p in split.get("piles", [])}
    cur = piles.get(q.get("current") or "")
    rec = (split.get("materialised") or {}).get(q.get("current") or "", {})
    total = len(q.get("pending", [])) + (1 if q.get("current") else 0)
    done = (q.get("pile_count_at_enqueue") or total) - total
    return {"pile_id": q.get("current"), "pile_index": done + (1 if cur else 0),
            "pile_count": q.get("pile_count_at_enqueue") or total,
            "track": len(rec.get("added", [])),
            "track_total": len(_unique(cur["uris"])) if cur else 0,
            "spent_today": sp.budget_spent(), "bulk_today": sp.bulk_spent(),
            "daily_cap": DAILY_CAP, "reserve": BULK_RESERVE}


def _queue_next_action(now: float) -> tuple:
    """Decide, without doing: ("stop", reason) | ("sleep", secs, state) |
    ("tick", playlist_id, pile_id). Mutates queue.json only to advance
    current/pending as piles finish (free, local)."""
    with _queue_lock:
        q = store.queue()
        if q["state"] in ("paused", "stopped", "done"):
            return ("stop", q["state"])
        block = sp.bulk_block_reason()
        if block:
            reason, until = block
            state = "quiet" if reason == "quiet" else "sleeping"
            return ("sleep", max(until - now, 1.0), state)
        while True:
            pid = q.get("current")
            if not pid:
                if not q["pending"]:
                    q.update(state="done", updated_at=_now_iso())
                    q["progress"] = _queue_progress(q)
                    store.save_queue(q)
                    return ("stop", "done")
                q["current"] = q["pending"].pop(0)
                store.save_queue(q)
                continue
            split = store.splits()["splits"].get(q["playlist_id"], {})
            pile = next((p for p in split.get("piles", []) if p["id"] == pid), None)
            if pile is None or _materialise_plan(split, pile)["calls"] == 0:
                q["current"] = None          # vanished or finished: next pile
                store.save_queue(q)
                continue
            return ("tick", q["playlist_id"], pid)


def _drain_queue() -> None:
    gov = Governor(store.pacing())
    gov.note_interruption()                  # every (re)start re-climbs from 1.8
    store.save_pacing(gov.to_state())
    _set_queue_state("running")
    while True:
        action = _queue_next_action(time.time())
        if action[0] == "stop":
            return
        if action[0] == "sleep":
            _set_queue_state(action[2])
            gov.note_interruption()
            store.save_pacing(gov.to_state())
            # Wake early on pause/cancel/resume; re-check at most every 60s so
            # a cooldown shortened by another process is noticed.
            _queue_wake.wait(min(action[1], 60))
            if store.queue()["state"] in ("paused", "stopped"):
                return
            _set_queue_state("running")
            continue
        _, playlist_id, pile_id = action
        tick_started = time.time()
        try:
            _materialise_tick(playlist_id, pile_id)
        except SpotifyError as e:
            info = sp.last_429 or {}
            if e.status == 429 and info.get("ts", 0) >= tick_started:
                gov.note_429(info["kind"], info.get("retry_after", 60), time.time())
                store.save_pacing(gov.to_state())
                if info["kind"] == "quota":
                    # Permanent: request() already published note_cooldown to
                    # the account ledger; resuming is a human's call (spec §2).
                    _set_queue_state("stopped", stop_reason="quota")
                    return
                continue                     # rate: cooldown sleep happens above
            # Auth failures, 5xx, local budget refusals: stop spending and
            # surface the reason rather than grind a broken loop.
            log.error("queue worker paused by error: %s", e)
            _set_queue_state("paused", stop_reason=str(e))
            return
        info = sp.last_429 or {}
        if info.get("ts", 0) >= tick_started:
            gov.note_429(info["kind"], info.get("retry_after", 60), time.time())
        else:
            gov.note_success(time.time())
        store.save_pacing(gov.to_state())
        with _queue_lock:
            q = store.queue()
            q["progress"] = _queue_progress(q)
            q["updated_at"] = _now_iso()
            store.save_queue(q)
        if _queue_wake.wait(gov.interval()):
            _queue_wake.clear()              # pause/cancel: next loop decides
```

Two invariants to keep while implementing: **no Spotify call under `_queue_lock` or `_split_lock`** (the tick already guarantees the latter), and **every state change rewrites queue.json** so boxdash never reads a stale state for long.

- [ ] **Step 4: Run to pass**, both pytest orderings. **Step 5: Commit.**

---

### Task 9: Queue endpoints, thread lifecycle, and the no-self-start pin

**Files:**
- Modify: `sortify/app.py` (endpoints below the worker), `tests/test_no_proactive_work.py` (append)
- Test: `tests/test_queue_api.py` (new)

**Interfaces:**
- Consumes: everything from Tasks 7–8.
- Produces:
  - `POST /api/split/{playlist_id}/queue` body `{"pile_ids": [...] | null, "expected_calls": N}` — echo gate (same semantics the one-shot endpoint had: exact match or 409 having spent nothing), orders piles smallest-remaining-first, writes queue.json, starts the worker. Free.
  - `GET /api/split/{playlist_id}/queue` → `{"queue": <queue.json with effective state>, "pacing": <pacing.json>}`. Free.
  - `POST /api/split/{playlist_id}/queue/pause`, `POST .../queue/resume`, `DELETE .../queue` (cancel). All free.
  - `_start_queue_worker()` — the ONLY function that creates the thread, called from exactly two places: enqueue and resume.

- [ ] **Step 1: Write the failing tests** (`tests/test_queue_api.py`). Define a `client` fixture combining Task 8's `worker_env` fakes with a `TestClient(appmod.app)` carrying a `.calls` list (the `test_materialise.py` pattern), plus copies of Task 8's `wait_done` helper — this file cannot import from `test_queue_worker` (pytest test modules are not import targets here). Representative set:

```python
def test_enqueue_requires_the_exact_echoed_price(client):
    """Same contract the one-shot endpoint had (finding I1): the number on
    the button is the number the server verifies. 4 tracks + 2 creates = 6."""
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 3})
    assert r.status_code == 409 and "6" in r.json()["detail"]
    assert Store().queue()["state"] == "stopped" and client.calls == []


def test_enqueue_orders_smallest_first_and_starts_the_worker(client):
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 6})
    assert r.status_code == 200 and r.json()["queued"] == ["p2", "p1"]
    q = wait_done(Store())
    assert q["state"] == "done"


def test_enqueue_is_free(client, monkeypatch):
    """The click costs 0: pricing comes from _materialise_plan (local), and
    the response returns before the worker's first tick can land."""
    hold = threading.Event()
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda *a, **k: hold.wait(5) or "NEWP")
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 6})
    assert client.calls == []
    hold.set()


def test_second_enqueue_while_active_is_refused(client):
    client.post("/api/split/PLQ/queue", json={"pile_ids": ["p1"], "expected_calls": 4})
    r = client.post("/api/split/PLQ/queue", json={"pile_ids": ["p2"], "expected_calls": 2})
    assert r.status_code == 409


def test_pause_resume_round_trip(client):
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 6})
    assert client.post("/api/split/PLQ/queue/pause").status_code == 200
    q = Store().queue()
    assert q["state"] == "paused"
    r = client.post("/api/split/PLQ/queue/resume")
    assert r.status_code == 200
    assert wait_done(Store())["state"] == "done"
    assert len(client.calls) == 6        # pause+resume cost nothing extra


def test_cancel_clears_pending_but_keeps_records(client):
    client.post("/api/split/PLQ/queue", json={"pile_ids": None, "expected_calls": 6})
    client.post("/api/split/PLQ/queue/pause")
    r = client.delete("/api/split/PLQ/queue")
    assert r.status_code == 200
    q = Store().queue()
    assert q["state"] == "stopped" and q["stop_reason"] == "cancelled" and q["pending"] == []
    # whatever landed stays resumable at the price of what's left:
    # re-enqueue prices only the remainder (may be 0..6 depending on timing).


def test_restart_shows_a_running_file_as_paused_and_starts_nothing(client):
    """The one-click promise ends with the process: after a restart the queue
    loads paused with a Resume button — no code path from boot to traffic."""
    s = Store(); q = s.queue()
    q.update(playlist_id="PLQ", pending=["p1"], state="running")
    s.save_queue(q)                       # what a crash mid-run leaves behind
    r = client.get("/api/split/PLQ/queue")
    assert r.json()["queue"]["state"] == "paused"
    assert appmod._queue_worker is None and client.calls == []
```

Append to `tests/test_no_proactive_work.py`:

```python
def test_the_queue_worker_cannot_self_start():
    """No thread at import; the only creator is _start_queue_worker; its only
    callers are the enqueue and resume endpoints. (Spec decision 4.)"""
    import inspect, threading
    assert appmod._queue_worker is None
    assert "queue-materialiser" not in [t.name for t in threading.enumerate()]
    src = inspect.getsource(appmod)
    calls = src.count("_start_queue_worker()")
    defs = src.count("def _start_queue_worker")
    assert defs == 1 and calls == 2, (
        f"_start_queue_worker is called {calls} times — enqueue and resume "
        "are the only two launch sites allowed; a third is a self-start "
        "waiting to happen")
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement:**

```python
class QueueIn(BaseModel):
    pile_ids: list[str] | None = None    # None = every pile in the split
    # The echo, not a flag — same argument as the endpoint this replaces:
    # the caller must state the price it was shown (finding I1).
    expected_calls: int = Field(..., ge=0)


def _start_queue_worker() -> None:
    global _queue_worker
    with _queue_lock:
        if _queue_worker and _queue_worker.is_alive():
            return
        _queue_wake.clear()
        _queue_worker = threading.Thread(
            target=_drain_queue, name="queue-materialiser", daemon=True)
        _queue_worker.start()


def _effective_queue() -> dict:
    """queue.json with the state a reader should act on: a file that says
    "running" while no worker thread is alive is a restart's leftover —
    paused, resumable, and starting nothing by itself."""
    q = store.queue()
    if q["state"] in ("running", "sleeping", "quiet") and not (
            _queue_worker and _queue_worker.is_alive()):
        q["state"] = "paused"
    return q


@app.post("/api/split/{playlist_id}/queue")
def enqueue_piles(playlist_id: str, body: QueueIn):
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        if not split:
            raise HTTPException(404, "no split for that playlist")
        wanted = body.pile_ids or [p["id"] for p in split["piles"]]
        piles = [p for p in split["piles"] if p["id"] in wanted]
        if len(piles) != len(set(wanted)):
            raise HTTPException(404, "unknown pile id in the request")
        plans = {p["id"]: _materialise_plan(split, p) for p in piles}
        total = sum(pl["calls"] for pl in plans.values())
        if body.expected_calls != total:
            raise HTTPException(
                409,
                f"cost has changed: saving these piles now spends {total} "
                f"Spotify calls, not the {body.expected_calls} you confirmed. "
                "Nothing was spent. Re-open the pile list and confirm the new "
                "number.")
        order = sorted((pid for pid in plans if plans[pid]["calls"] > 0),
                       key=lambda pid: plans[pid]["calls"])
    if total == 0:
        return {"ok": True, "queued": [], "total_calls": 0, "complete": True}
    with _queue_lock:
        q = _effective_queue()
        if q["state"] in ("running", "sleeping", "quiet"):
            raise HTTPException(409, "a queue is already draining — pause or cancel it first")
        store.save_queue({"version": 1, "playlist_id": playlist_id,
                          "pending": order, "current": None, "state": "running",
                          "stop_reason": None, "progress": {},
                          "pile_count_at_enqueue": len(order),
                          "enqueued_at": _now_iso(), "updated_at": _now_iso()})
    _start_queue_worker()
    return {"ok": True, "queued": order, "total_calls": total, "complete": False}


@app.get("/api/split/{playlist_id}/queue")
def queue_status(playlist_id: str):
    return {"queue": _effective_queue(), "pacing": store.pacing()}


@app.post("/api/split/{playlist_id}/queue/pause")
def pause_queue(playlist_id: str):
    _set_queue_state("paused")
    _queue_wake.set()
    return {"ok": True}


@app.post("/api/split/{playlist_id}/queue/resume")
def resume_queue(playlist_id: str):
    q = store.queue()
    if not q.get("pending") and not q.get("current"):
        raise HTTPException(409, "nothing queued")
    _set_queue_state("running")
    _start_queue_worker()
    return {"ok": True}


@app.delete("/api/split/{playlist_id}/queue")
def cancel_queue(playlist_id: str):
    with _queue_lock:
        q = store.queue()
        q.update(pending=[], current=None, state="stopped",
                 stop_reason="cancelled", updated_at=_now_iso())
        store.save_queue(q)
    _queue_wake.set()
    return {"ok": True}
```

(Resume after a quota stop is allowed — the endpoint IS the human-only resume; the worker's `bulk_block_reason` then holds it through the cooldown and the 6h quiet before any call lands.)

- [ ] **Step 4: Run to pass**, both orderings. **Step 5: Commit.**

---

### Task 10: Frontend — save-all, queue panel, price-floor disclosure

**Files:**
- Modify: `sortify/static/app.js`, `sortify/static/index.html`, `sortify/static/style.css`
- Test: `tests/ui_harness.mjs` (rework the materialise section)

**Interfaces:**
- Consumes: Task 9's endpoints; `_pile_progress`'s existing `materialise_calls` / `materialised` row fields (unchanged).
- Produces: `queuePiles(pileIds, expectedCalls)` (replaces `materialisePile`), `pauseQueue()`, `resumeQueue()`, `cancelQueue()`, `renderQueuePanel(status)`; per-pile Save buttons and one "Save all piles (N calls)" header button, both routing through `queuePiles`.

- [ ] **Step 1: Rework the harness first (failing).** In `tests/ui_harness.mjs`, delete the `materialisePile` checks (noted in Task 7) and add, in the same fetch-stub style:

```js
// The queue replaces the one-shot save. Same misclick contract: the number
// POSTed is the number the button displayed, now summed across piles.
resetLog();
routes["POST /api/split/PL8/queue"] = { ok: true, queued: ["p2", "p1"], total_calls: 5 };
run(`queuePiles(null, 5)`);          // "Save all" — null means every pile
check("save-all posts the summed price it displayed",
      bodies("/api/split/PL8/queue")[0]?.expected_calls === 5,
      JSON.stringify(bodies("/api/split/PL8/queue")[0]));

resetLog();
run(`queuePiles(["p1"], 3)`);        // single pile goes through the same gate
check("single-pile save is a one-pile queue",
      JSON.stringify(bodies("/api/split/PL8/queue")[0]?.pile_ids) === '["p1"]');

// Finding I2: every displayed price is a floor, not a ceiling — request()
// retries a transient 429 up to 3x and each attempt is charged.
check("the price label discloses it is a floor",
      /at least|minst|floor/i.test(renderSaveAllLabel(5)),
      renderSaveAllLabel(5));

// Pause is one click and free; the button reflects the effective state.
resetLog();
routes["POST /api/split/PL8/queue/pause"] = { ok: true };
run(`pauseQueue()`);
check("pause posts exactly once and nowhere else",
      posts("/api/split/PL8/queue/pause") === 1 && log.length === 1);

check("a restart's leftover running state renders as paused with Resume",
      renderQueuePanel({ queue: { state: "paused", stop_reason: null,
        progress: { pile_index: 1, pile_count: 8, track: 40, track_total: 309 } },
        pacing: { rate_per_min: 2.5, ceiling: 7.0, max_clean_rate: 2.1 } })
        .includes("Resume"));
```

Run `node tests/ui_harness.mjs` → the new checks FAIL (and Task 7's known-red disappears with the deleted checks).

- [ ] **Step 2: Implement** in `app.js`: replace `materialisePile` with `queuePiles(pileIds, expectedCalls)` POSTing `{pile_ids: pileIds, expected_calls: expectedCalls}`; on 409 re-fetch `/api/split/{id}` and re-render (same behaviour the one-shot had). Add the queue panel to the split view: state badge, `pile i/n · track j/m`, `rate r/min (ceiling 7.0)`, `max clean rate`, last 429 if any, Pause/Resume/Cancel buttons. Poll `GET /api/split/{id}/queue` only while the panel is open AND state is active — it is free and local, but keep it modest (every 10s) and stop when hidden; this endpoint never touches Spotify so the `/api/now` polling rule is not in play. Price labels everywhere (per-pile buttons, save-all button) render `≥ N calls` with a title-text: "at least — a retried 429 is charged per attempt". Keep the disabled-while-in-flight behaviour the harness already pins.

- [ ] **Step 3: Run** `node tests/ui_harness.mjs` → all green; both pytest orderings still green. **Step 4: Commit.**

---

### Task 11: boxdash card (read-only)

**Files:**
- Modify: `~/kode/boxdash/server.py`, `~/kode/boxdash/static/index.html`
- Test: `~/kode/boxdash/tests/test_spotify.py` (append)

This is a separate repo with its own conventions — read its `server.py` spotify section and `tests/test_spotify.py` before writing. boxdash's house pattern: read state FILES directly, absolute paths as module constants, every reader tolerates a missing/garbled file.

**Interfaces:**
- Consumes: `~/kode/sortify/data/queue.json` and `pacing.json` (Task 3's shapes, version 1).
- Produces: `sortify_bulk` key in the `/api/spotify` payload; a card section in the spotify tab. **No POST endpoints — read-only is the contract** (spec decision 5).

- [ ] **Step 1: Failing test** (append to `tests/test_spotify.py`, using its tmp-file pattern):

```python
def test_sortify_bulk_reads_queue_and_pacing(tmp_path, monkeypatch):
    import server
    (tmp_path / "queue.json").write_text(json.dumps({
        "version": 1, "playlist_id": "PL", "pending": ["p3"], "current": "p2",
        "state": "running", "stop_reason": None,
        "progress": {"pile_index": 2, "pile_count": 8, "track": 40,
                     "track_total": 309, "spent_today": 120, "bulk_today": 100,
                     "daily_cap": 600, "reserve": 150},
        "enqueued_at": "t", "updated_at": "t"}))
    (tmp_path / "pacing.json").write_text(json.dumps({
        "version": 1, "rate_per_min": 2.5, "ceiling": 7.0, "clean_since": None,
        "max_clean_rate": 2.1,
        "history_429": [{"when": 1.0, "kind": "rate", "rate": 4.0, "retry_after": 12}],
        "updated_at": 2.0}))
    monkeypatch.setattr(server, "SORTIFY_DATA", str(tmp_path))
    d = server.sortify_bulk()
    assert d["state"] == "running" and d["rate"] == 2.5 and d["ceiling"] == 7.0
    assert d["max_clean_rate"] == 2.1 and d["last_429"]["kind"] == "rate"
    assert d["progress"]["track"] == 40 and d["progress"]["reserve"] == 150


def test_sortify_bulk_absent_or_wrong_version_is_none(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, "SORTIFY_DATA", str(tmp_path))
    assert server.sortify_bulk() is None
    (tmp_path / "queue.json").write_text('{"version": 2}')
    assert server.sortify_bulk() is None
```

- [ ] **Step 2: Run to verify failure**, in the boxdash repo: `python -m pytest tests/test_spotify.py -q` (check how boxdash runs tests — no venv assumptions).

- [ ] **Step 3: Implement.** `SORTIFY_DATA = os.path.join(HOME, "kode", "sortify", "data")` beside the other path constants; a `sortify_bulk()` reader returning `None` unless both files parse with `version == 1`, else `{"state", "stop_reason", "rate", "ceiling", "max_clean_rate", "last_429", "progress", "updated_at"}`; add `"sortify_bulk": sortify_bulk()` to `spotify_usage()`'s payload; in `static/index.html`'s spotify render, a card row shown only when `d.sortify_bulk` is truthy: state badge, `pile i/n · track j/m`, `rate 2.5/min → ceiling 7.0`, `max clean 2.1/min`, spend `120/450 (+150 reserved)`, last 429 `rate @4.0/min, 12s`. Files outlive the process — the card must render fine (state as stored, no liveness claim) when sortify's port 8090-probe says down.

- [ ] **Step 4: Run boxdash tests to pass. Step 5: Commit in the boxdash repo** (its own git history, its own style).

---

### Task 12: Docs, ledger, final verification

**Files:**
- Modify: `CLAUDE.md` (budget layers bullet), `.superpowers/sdd/2026-08-17-playlist-splitting/progress.md` (ledger entry)

- [ ] **Step 1:** Add one line to CLAUDE.md's budget-layers bullet: `BULK_RESERVE 150` — the queued materialiser never spends the day's last 150 calls; governor ceiling 7.0/min; `data/pacing.json` holds the measured `max_clean_rate`. Keep it to 2–3 lines in the existing bullet's voice.

- [ ] **Step 2: Full verification, stated with output:**

```bash
cd ~/kode/sortify-lastfm
.venv/bin/pytest -q
.venv/bin/pytest -q -p no:randomly $(ls -r tests/test_*.py)
node tests/ui_harness.mjs
cd ~/kode/boxdash && python -m pytest -q
```

All green, both orderings, or the task is not done.

- [ ] **Step 3: Commit**, update the SDD ledger with what shipped and what was ruled.

- [ ] **Step 4: CHECKPOINT — hand back to the user.** Merging `queued-materialiser` to master, restarting the live service, and the **attended first run** (spec decision 6c: user watching the dashboard, `spx budget` stated before and after) are the user's calls, not this plan's. Present: the branch, the test counts, the merge command, and the runbook: enqueue all piles from the UI → watch the boxdash card → first sleep at `DAILY_CAP−150` → expect ~3 days; a quota stop is permanent until the user resumes it.

---

## Execution notes for the orchestrating session

- Tasks 3, 4, 5, 6 are independent of each other (after Task 2) and may be dispatched in parallel worktrees if desired; 7 needs 2+5; 8 needs 3+4+5+7; 9 needs 8; 10 needs 9; 11 needs 3's shapes only; 12 last.
- Task 1's Step 4 and Task 12's Step 4 are the only user checkpoints. Everything else runs without a single Spotify call.
- If any test fails in only ONE ordering, that is a conftest shared-data-dir isolation leak (four found so far) — fix with try/finally restores in the offending fixture, not by reordering.
