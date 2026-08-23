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
        conditions the clean clock measured no longer hold, so any climb
        earned during them is unproven and the clock resets.

        But an interruption must never RAISE the rate. A rate 429 can be
        followed, within the same run, by an unrelated sleep (a quiet
        period, a reserve-cap wait, a cooldown check that spans the wait
        cap) — and if that later interruption reset the rate back up to
        START_RATE, the 429's halving would be silently undone without a
        single clean minute ever being earned back (ruling R-T8f). So this
        only ever pulls the rate DOWN to at most START_RATE; a rate already
        below it (freshly halved by note_429) is left alone.
        """
        self.rate = min(self.rate, START_RATE)
        self._clean_since = None

    def to_state(self) -> dict:
        return {"version": 1, "rate_per_min": round(self.rate, 1),
                "ceiling": CEILING_RATE, "clean_since": self._clean_since,
                "max_clean_rate": self._max_clean,
                "history_429": self._history, "updated_at": time.time()}
