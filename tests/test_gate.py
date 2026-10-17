"""The public port's lock, as pure rules (spec: 2026-10-16 login gate).

Everything here runs without a server or a network, because the gate's
failure modes (a forged cookie, an expired one, a missing secret, a missing
owner) must all end in "not let in", and each is easiest to pin directly.
"""

import time

from sortify import gate

SECRET = "s" * 40
OWNER = "owner-id"


def test_a_short_or_missing_secret_is_not_ok():
    assert not gate.secret_ok(None)
    assert not gate.secret_ok("")
    assert not gate.secret_ok("x" * 31)
    assert gate.secret_ok("x" * 32)


def test_the_owners_cookie_lets_the_owner_in():
    value = gate.session_value(SECRET, OWNER)

    assert gate.session_uid(SECRET, value, OWNER) == OWNER


def test_a_cookie_for_another_account_is_refused():
    value = gate.session_value(SECRET, "someone-else")

    assert gate.session_uid(SECRET, value, OWNER) is None


def test_no_owner_means_nobody_gets_in():
    value = gate.session_value(SECRET, OWNER)

    assert gate.session_uid(SECRET, value, None) is None
    assert gate.session_uid(SECRET, value, "") is None


def test_a_tampered_cookie_is_refused():
    value = gate.session_value(SECRET, OWNER)
    body, mac = value.split(".")
    forged = gate.session_value("other-secret-" + "x" * 32, OWNER)

    assert gate.session_uid(SECRET, body + "." + mac[:-2] + "AA", OWNER) is None
    assert gate.session_uid(SECRET, forged, OWNER) is None
    assert gate.session_uid(SECRET, "garbage", OWNER) is None
    assert gate.session_uid(SECRET, "a.b.c", OWNER) is None
    assert gate.session_uid(SECRET, "ünïcode.ünïcode", OWNER) is None


def test_an_expired_cookie_is_refused():
    old = time.time() - gate.SESSION_MAX_AGE - 10
    value = gate.session_value(SECRET, OWNER, now=old)

    assert gate.session_uid(SECRET, value, OWNER) is None


def test_without_a_secret_even_a_valid_cookie_is_refused():
    value = gate.session_value(SECRET, OWNER)

    assert gate.session_uid(None, value, OWNER) is None
    assert gate.session_uid("short", value, OWNER) is None


def test_a_pending_cookie_is_not_a_session():
    pending = gate.pending_value(SECRET, "state1", "verifier1")

    assert gate.session_uid(SECRET, pending, OWNER) is None


def test_a_session_cookie_is_not_a_pending_login():
    value = gate.session_value(SECRET, OWNER)

    assert gate.pending_verifier(SECRET, value, "state1") is None


def test_renewal_after_a_day_not_before():
    fresh = gate.session_value(SECRET, OWNER)
    day_old = gate.session_value(SECRET, OWNER, now=time.time() - gate.RENEW_AFTER - 5)

    assert not gate.needs_renewal(SECRET, fresh)
    assert gate.needs_renewal(SECRET, day_old)
    assert not gate.needs_renewal(SECRET, "garbage")


def test_the_pending_cookie_gives_back_the_verifier_for_its_state_only():
    value = gate.pending_value(SECRET, "state1", "verifier1")

    assert gate.pending_verifier(SECRET, value, "state1") == "verifier1"
    assert gate.pending_verifier(SECRET, value, "state2") is None
    assert gate.pending_verifier(SECRET, value, None) is None
    assert gate.pending_verifier(SECRET, None, "state1") is None


def test_a_pending_cookie_expires_after_ten_minutes():
    value = gate.pending_value(SECRET, "state1", "verifier1",
                               now=time.time() - gate.PENDING_MAX_AGE - 5)

    assert gate.pending_verifier(SECRET, value, "state1") is None


def test_only_the_gate_routes_and_the_install_files_are_open():
    for path in ("/gate/start", "/gate/callback", "/static/manifest.webmanifest",
                 "/static/icon-192.png", "/static/apple-touch-icon.png"):
        assert gate.is_open_path(path), path
    for path in ("/", "/api/now", "/static/app.js", "/static/style.css",
                 "/auth/callback", "/gate/startx", "/static/../data/tokens.png"):
        assert not gate.is_open_path(path), path


def test_is_open_path_refuses_any_character_outside_the_safe_set():
    for path in ("/gate/start#x", "/gate/start?x", "/gate/start%2e%2e",
                 "/static/manifest.webmanifest#/../app.js",
                 "/static/icon-192.png?/../style.css"):
        assert not gate.is_open_path(path), path


def test_the_daily_counter_caps_and_resets_with_the_day():
    c = gate.DailyCounter(cap=2)

    assert c.take("2026-10-17") and c.take("2026-10-17")
    assert not c.take("2026-10-17")
    assert c.take("2026-10-18")
