import json
import time

import pytest

from sortify.account_ledger import ACCOUNT_DAILY_CAP, AccountLedger
from sortify.spotify import (
    BACKGROUND_DAILY_CAP,
    BULK_RESERVE,
    DAILY_CAP,
    QUIET_AFTER_COOLDOWN,
    Spotify,
    SpotifyError,
    _next_local_midnight,
)
from sortify.store import Store


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text="", json_body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json_body = json_body
        self.content = b"1" if json_body is not None else text.encode()

    def json(self):
        return self._json_body


@pytest.fixture
def sp_and_store(tmp_path):
    store = Store(tmp_path)
    sp = Spotify(store)
    return sp, store


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


# ---- the shared account ledger ---------------------------------------------


def test_sortify_calls_land_in_the_shared_ledger(tmp_path):
    sp = Spotify(Store(tmp_path))
    sp._spend_budget()
    assert AccountLedger("sortify").app_spent_today() == 1


def test_account_cap_binds_even_when_sortifys_own_share_is_free(tmp_path):
    """The failure the old per-app guards could not see: sortify is nowhere
    near its own 600, but the account backstop is spent by the siblings."""
    sp = Spotify(Store(tmp_path))
    sibling = AccountLedger("autoqueuer")
    # Seeded rather than spent 8000 times — this is the real on-disk shape.
    with open(sibling.path, "w") as fh:
        json.dump(
            {"day": time.strftime("%Y-%m-%d"), "count": ACCOUNT_DAILY_CAP,
             "by_app": {"autoqueuer": ACCOUNT_DAILY_CAP}, "cooldown_until": 0,
             "cooldown_source": "", "cooldown_reason": ""},
            fh,
        )
    assert sp.budget_spent() == 0  # sortify's own ledger is untouched
    with pytest.raises(SpotifyError, match="account daily budget"):
        sp._spend_budget()


def test_cooldown_recorded_by_a_sibling_app_stops_sortify(tmp_path):
    sp = Spotify(Store(tmp_path))
    assert sp.background_block_reason() is None
    AccountLedger("playlistener").note_cooldown(time.time() + 3600, reason="quota")
    assert "cooldown" in sp.background_block_reason()
    with pytest.raises(SpotifyError, match="cooldown"):
        sp.request("GET", "/me")


def test_sortifys_own_cooldown_is_published_to_the_siblings(tmp_path):
    sp = Spotify(Store(tmp_path))
    sp.ledger.note_cooldown(time.time() + 3600, reason="quota")
    until, source, reason = AccountLedger("playlistener").cooldown()
    assert until > time.time()
    assert source == "sortify"
    assert reason == "quota"


def test_quota_cooldown_runs_to_the_next_local_midnight():
    now = time.time()
    assert now < _next_local_midnight(now) <= now + 86400


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


# ---- the bulk spend class ---------------------------------------------------


def test_bulk_never_spends_the_interactive_reserve(sp_and_store):
    """DAILY_CAP-150 is the line: the last 150 calls of the day belong to the
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
