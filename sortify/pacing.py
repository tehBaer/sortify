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
