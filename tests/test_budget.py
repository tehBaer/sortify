import pytest

from sortify.spotify import DAILY_CAP, Spotify, SpotifyError
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
