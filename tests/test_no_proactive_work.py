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
