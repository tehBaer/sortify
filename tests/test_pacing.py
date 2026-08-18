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
