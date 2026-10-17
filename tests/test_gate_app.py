"""The gate wired into the app (spec: 2026-10-16 login gate).

A TestClient whose base URL carries the public port acts as Funnel traffic:
starlette puts the URL's port in scope["server"], which is exactly what the
real two-socket server does. The default client (port 80) is tailnet traffic.
Spotify is never called: exchange_code and whoami are faked.
"""

import time

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from sortify import gate

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

SECRET = "k" * 48
OWNER = "owner-id"
PUBLIC = "https://testserver:8801"
ORIGIN = "https://testserver:8801"


@pytest.fixture(autouse=True)
def gate_on(monkeypatch):
    monkeypatch.setattr(appmod, "PUBLIC_PORT", 8801)
    monkeypatch.setattr(appmod, "PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setattr(appmod, "GATE_REDIRECT_URI", ORIGIN + "/gate/callback")
    monkeypatch.setattr(appmod, "_gate_counter", gate.DailyCounter())
    monkeypatch.setenv("SORTIFY_GATE_SECRET", SECRET)
    prev_client = appmod.store.config().get("client_id")
    appmod.store.update_config(gate_owner_id=OWNER, client_id="a" * 32)
    yield
    appmod.store.update_config(gate_owner_id=None, client_id=prev_client)


@pytest.fixture
def spotify_says(monkeypatch):
    """Fake the two Spotify calls; returns the list of calls made."""
    calls = []

    def answer(uid):
        monkeypatch.setattr(appmod.sp, "exchange_code",
                            lambda code, verifier, client_id, redirect_uri=None:
                            calls.append(("exchange", code, verifier, redirect_uri))
                            or {"access_token": "visitor"})
        monkeypatch.setattr(appmod.sp, "whoami",
                            lambda token: calls.append(("whoami", token)) or uid)
        return calls

    return answer


def public():
    return TestClient(appmod.app, base_url=PUBLIC, follow_redirects=False)


def tailnet():
    return TestClient(appmod.app, follow_redirects=False)


def signed_in(c):
    c.cookies.set(gate.SESSION_COOKIE, gate.session_value(SECRET, OWNER))
    return c


def sign_in(c):
    """Run /gate/start and /gate/callback; returns the callback response."""
    start = c.get("/gate/start")
    state = start.headers["location"].split("state=")[1].split("&")[0]
    return c.get(f"/gate/callback?code=c1&state={state}")


# ---- who is let through ----------------------------------------------------


def test_the_tailnet_port_is_untouched():
    r = tailnet().get("/")

    assert r.status_code == 200
    assert "rc-button.js" in r.text


def test_a_public_page_without_a_cookie_goes_to_the_gate():
    r = public().get("/")

    assert r.status_code == 302
    assert r.headers["location"] == "/gate/start"


def test_a_public_api_call_without_a_cookie_gets_a_gate_401():
    r = public().get("/api/status")

    assert r.status_code == 401
    assert r.json()["gate"] is True


def test_a_public_post_without_a_cookie_gets_a_401_not_a_redirect():
    r = public().post("/api/act", json={}, headers={"Origin": ORIGIN})

    assert r.status_code == 401
    assert r.json()["gate"] is True


def test_static_scripts_are_gated_but_the_install_files_are_not():
    c = public()

    assert c.get("/static/app.js").status_code == 302
    assert c.get("/static/manifest.webmanifest").status_code == 200
    assert c.get("/static/icon-192.png").status_code == 200


def test_the_raw_static_index_html_never_reaches_the_public_port_signed_in():
    r = signed_in(public()).get("/static/index.html")

    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_the_raw_static_index_html_is_unchanged_on_the_tailnet():
    r = tailnet().get("/static/index.html")

    assert r.status_code == 200
    assert "rc-button.js" in r.text


def test_the_owners_cookie_opens_the_public_page_without_the_button():
    r = signed_in(public()).get("/")

    assert r.status_code == 200
    assert "rc-button.js" not in r.text
    assert "/static/app.js?v=" in r.text


def test_without_a_secret_the_owners_cookie_is_refused(monkeypatch):
    c = signed_in(public())
    monkeypatch.delenv("SORTIFY_GATE_SECRET")

    assert c.get("/api/status").status_code == 401


def test_without_an_owner_the_owners_cookie_is_refused():
    c = signed_in(public())
    appmod.store.update_config(gate_owner_id=None)

    assert c.get("/api/status").status_code == 401


def test_a_cross_origin_post_is_refused_even_signed_in():
    c = signed_in(public())

    assert c.post("/api/act", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/act", json={}).status_code == 403


def test_a_day_old_cookie_is_renewed():
    c = public()
    c.cookies.set(gate.SESSION_COOKIE,
                  gate.session_value(SECRET, OWNER, now=time.time() - gate.RENEW_AFTER - 5))

    r = c.get("/api/status")

    assert r.status_code == 200
    assert gate.SESSION_COOKIE in r.headers.get("set-cookie", "")


# ---- signing in ------------------------------------------------------------


def test_start_sends_to_spotify_with_no_scopes_and_the_public_callback():
    r = public().get("/gate/start")

    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.spotify.com/authorize?")
    assert "scope=" not in loc
    assert "redirect_uri=https%3A%2F%2Ftestserver%3A8801%2Fgate%2Fcallback" in loc
    assert gate.PENDING_COOKIE in r.headers["set-cookie"]


def test_the_gate_routes_do_not_exist_on_the_tailnet():
    assert tailnet().get("/gate/start").status_code == 404
    assert tailnet().get("/gate/callback?code=c&state=s").status_code == 404


def test_the_owner_signs_in_and_gets_the_cookie(spotify_says):
    calls = spotify_says(OWNER)
    c = public()

    r = sign_in(c)

    assert r.status_code == 303 and r.headers["location"] == "/"
    assert [k[0] for k in calls] == ["exchange", "whoami"]
    assert calls[0][3] == ORIGIN + "/gate/callback"
    assert c.get("/api/status").status_code == 200


def test_another_account_is_refused(spotify_says):
    spotify_says("someone-else")
    c = public()

    r = sign_in(c)

    assert r.status_code == 403
    assert c.get("/api/status").status_code == 401


def test_signing_in_leaves_the_owners_tokens_alone(spotify_says):
    spotify_says(OWNER)
    appmod.store.save_tokens({**appmod.store.tokens(),
                              "pending": {"state": "reconnect-in-flight", "verifier": "v",
                                          "client_id": "b" * 32}})
    before = appmod.store.tokens()

    sign_in(public())

    assert appmod.store.tokens() == before


def test_cancelling_on_spotify_gets_a_page_not_an_error(spotify_says):
    calls = spotify_says(OWNER)

    r = public().get("/gate/callback?error=access_denied&state=x")

    assert r.status_code == 400
    assert "cancelled" in r.text
    assert calls == []


def test_a_reloaded_callback_says_start_again_and_calls_nothing(spotify_says):
    calls = spotify_says(OWNER)
    c = public()
    sign_in(c)
    calls.clear()

    r = c.get("/gate/callback?code=c1&state=whatever")

    assert r.status_code == 400
    assert "start again" in r.text
    assert calls == []


def test_no_owner_set_refuses_without_calling_spotify(spotify_says):
    calls = spotify_says(OWNER)
    appmod.store.update_config(gate_owner_id=None)

    r = sign_in(public())

    assert r.status_code == 403
    assert "no owner" in r.text
    assert calls == []


def test_during_a_cooldown_sign_in_says_so_and_calls_nothing(spotify_says, monkeypatch):
    calls = spotify_says(OWNER)
    monkeypatch.setattr(appmod.sp, "effective_cooldown_until", lambda: time.time() + 3600)

    r = sign_in(public())

    assert r.status_code == 503
    assert "cooling down" in r.text
    assert calls == []


def test_sign_ins_are_capped_per_day(spotify_says, monkeypatch):
    calls = spotify_says("someone-else")
    monkeypatch.setattr(appmod, "_gate_counter", gate.DailyCounter(cap=1))
    c = public()

    sign_in(c)
    r = sign_in(c)

    assert r.status_code == 429
    assert [k[0] for k in calls] == ["exchange", "whoami"]


def test_a_refused_code_gets_a_page_not_an_error(monkeypatch):
    def refuse(*a, **kw):
        raise appmod.SpotifyError(400, "invalid_grant")

    monkeypatch.setattr(appmod.sp, "exchange_code", refuse)

    r = sign_in(public())

    assert r.status_code == 502
