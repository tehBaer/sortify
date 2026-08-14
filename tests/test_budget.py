import time

import pytest

from sortify.spotify import (
    BACKGROUND_DAILY_CAP,
    DAILY_CAP,
    QUIET_AFTER_COOLDOWN,
    Spotify,
    SpotifyError,
)
from sortify.store import Store


def test_daily_budget_blocks_at_cap(tmp_path):
    sp = Spotify(Store(tmp_path))
    sp._spend_budget()
    assert sp.budget_spent() == 1

    usage = sp.store.usage()
    usage["count"] = DAILY_CAP
    sp.store.save_usage(usage)
    with pytest.raises(SpotifyError, match="daily budget"):
        sp._spend_budget()


def test_budget_resets_on_new_day(tmp_path):
    sp = Spotify(Store(tmp_path))
    sp.store.save_usage({"day": "1999-01-01", "count": DAILY_CAP})
    sp._spend_budget()  # stale day: counts from zero instead of raising
    assert sp.budget_spent() == 1


# ---- background allowance --------------------------------------------------


def test_background_calls_bill_both_ledgers(tmp_path):
    sp = Spotify(Store(tmp_path))
    sp._spend_budget(background=True)
    assert sp.background_spent() == 1
    assert sp.budget_spent() == 1  # background is carved out of the daily cap


def test_background_cap_blocks_while_interactive_still_runs(tmp_path):
    sp = Spotify(Store(tmp_path))
    sp.store.save_usage(
        {"day": time.strftime("%Y-%m-%d"), "count": BACKGROUND_DAILY_CAP,
         "background": BACKGROUND_DAILY_CAP}
    )
    with pytest.raises(SpotifyError, match="background budget"):
        sp._spend_budget(background=True)
    sp._spend_budget()  # the user's own traffic is unaffected


def test_background_cap_is_far_below_daily_cap():
    # A proactive job must never be able to spend the day on its own — that is
    # what earned the 2026-08-13 ban.
    assert BACKGROUND_DAILY_CAP * 4 < DAILY_CAP


def test_legacy_usage_file_without_background_key(tmp_path):
    store = Store(tmp_path)
    store.save_usage({"day": time.strftime("%Y-%m-%d"), "count": 5})  # pre-upgrade shape
    sp = Spotify(store)
    assert sp.background_spent() == 0
    sp._spend_budget(background=True)
    assert sp.background_spent() == 1
    assert sp.budget_spent() == 6


# ---- post-cooldown quiet period --------------------------------------------


def test_background_blocked_during_cooldown(tmp_path):
    store = Store(tmp_path)
    store.save_tokens({"cooldown_until": time.time() + 3600})
    sp = Spotify(store)
    assert "cooldown" in sp.background_block_reason()


def test_background_stays_quiet_after_cooldown_expires(tmp_path):
    """The exact 2026-08-13 regression: cooldown just lifted, enricher must
    not resume."""
    store = Store(tmp_path)
    store.save_tokens({"cooldown_until": time.time() - 60})  # ended a minute ago
    sp = Spotify(store)
    assert "quiet period" in sp.background_block_reason()


def test_background_resumes_after_quiet_period(tmp_path):
    store = Store(tmp_path)
    store.save_tokens({"cooldown_until": time.time() - QUIET_AFTER_COOLDOWN - 60})
    sp = Spotify(store)
    assert sp.background_block_reason() is None


def test_background_yields_once_day_is_half_spent(tmp_path):
    sp = Spotify(Store(tmp_path))
    sp.store.save_usage({"day": time.strftime("%Y-%m-%d"), "count": DAILY_CAP // 2, "background": 0})
    assert "real usage" in sp.background_block_reason()


def test_cooldown_earned_by_another_process_is_seen(tmp_path):
    """spx shares the ledger from its own process; the long-lived server must
    notice a cooldown it did not earn itself."""
    store = Store(tmp_path)
    sp = Spotify(store)
    assert sp.background_block_reason() is None
    store.save_tokens({"cooldown_until": time.time() + 3600})  # written by spx
    assert "cooldown" in sp.background_block_reason()
    with pytest.raises(SpotifyError, match="cooldown"):
        sp.request("GET", "/me")
