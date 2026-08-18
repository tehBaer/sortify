"""sortify makes no proactive Spotify calls.

The genre enricher was the only one, and it spent the full 40/day background
allowance backfilling a field the API stopped returning: `/artists/{id}` in the
Feb-2026 dev mode has no `genres` key at all (verified 2026-08-17 against Alice
Cooper). The `genres: []` seen throughout cache.json was our own
`a.get("genres", [])` default, not data.

The budget machinery it was paced by stays — BACKGROUND_DAILY_CAP, the
background branch of _spend_budget, get_background — because that is the guard
rail for any future proactive work, and CLAUDE.md is explicit that the layers
are set from observed damage. What goes is the job and the fetch, so every
call sortify now makes is one the user asked for.
"""

from sortify import app as appmod


def test_no_background_job_exists():
    """A resurrection should have to be deliberate, not a merge accident."""
    assert not hasattr(appmod, "_genre_enricher")
    assert not hasattr(appmod, "_start_enricher")


def test_the_dead_genre_fetch_is_gone():
    """artists_genres could only ever return empty genres, and it was called
    on the now-playing path too — a per-track cost for nothing."""
    assert not hasattr(appmod.sp, "artists_genres")


def test_the_background_guard_rail_survives():
    """Deleting the only background job must not delete the protection against
    the next one. These caps came from three multi-hour lockouts."""
    from sortify.spotify import BACKGROUND_DAILY_CAP

    assert BACKGROUND_DAILY_CAP == 40
    assert hasattr(appmod.sp, "background_block_reason")
    assert hasattr(appmod.sp, "get_background")


def test_the_queue_worker_cannot_self_start():
    """No thread at import; the only creator is _start_queue_worker; its only
    callers are the enqueue and resume endpoints. (Spec decision 4.)"""
    import inspect, threading
    assert appmod._queue_worker is None
    assert "queue-materialiser" not in [t.name for t in threading.enumerate()]
    src = inspect.getsource(appmod)
    calls = src.count("_start_queue_worker()")
    defs = src.count("def _start_queue_worker")
    # DEVIATION from the brief's literal "calls == 2": a zero-arg def line
    # ("def _start_queue_worker() -> None:") itself contains the substring
    # "_start_queue_worker()", so `calls` also counts that def line once,
    # in addition to the real invocation sites. `calls - defs` is the count
    # of actual call sites; the brief's raw `calls == 2` undercounts by
    # exactly `defs` for any zero-arg function of this name.
    assert defs == 1 and calls - defs == 2, (
        f"_start_queue_worker is invoked {calls - defs} times — enqueue and "
        "resume are the only two launch sites allowed; a third is a "
        "self-start waiting to happen")
