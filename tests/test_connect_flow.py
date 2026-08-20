"""The autoqueuer-style connection flow.

Three properties ported from spotify-autoqueuer's auth routes:
- nothing persists until the callback exchange succeeds (an abandoned or
  mistyped attempt must not overwrite the stored client ID),
- the browser returns to sortify via a real /auth/callback route instead of
  the copy-the-failed-URL dance,
- a typed client ID is validated (32 alphanumerics) before we send the user
  off to Spotify with it.
"""

import pytest

from sortify import app as appmod

VALID_ID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"  # 32 alnum
OTHER_ID = "ffffffffffffffffffffffffffffffff"


@pytest.fixture(autouse=True)
def clean_auth_state():
    appmod.store.update_config(client_id=None)
    tokens = appmod.store.tokens()
    tokens.pop("pending", None)
    appmod.store.save_tokens(tokens)


def start(client_id=VALID_ID):
    return appmod.auth_start(appmod.AuthStart(client_id=client_id))


def pending():
    return appmod.store.tokens().get("pending")


# ---- start: nothing persists yet -------------------------------------------


def test_starting_auth_does_not_touch_the_stored_client_id():
    appmod.store.update_config(client_id=OTHER_ID)

    start(VALID_ID)

    assert appmod.store.config()["client_id"] == OTHER_ID


def test_the_typed_client_id_rides_in_the_pending_entry():
    start(VALID_ID)

    assert pending()["client_id"] == VALID_ID


def test_a_malformed_client_id_is_refused_before_spotify_sees_it():
    with pytest.raises(appmod.HTTPException) as e:
        start("not a client id")
    assert e.value.status_code == 400


def test_blank_still_falls_back_to_the_stored_id_even_a_legacy_shaped_one():
    # IDs stored before validation existed must keep working on reconnect.
    appmod.store.update_config(client_id="stored-id")

    url = start("")["auth_url"]

    assert "client_id=stored-id" in url


# ---- callback: the browser comes back on its own ---------------------------


class FakeExchange:
    """Stands in for the accounts.spotify.com token POST."""

    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, url, data=None, **kw):
        self.calls.append(data)
        import httpx

        payload = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        return httpx.Response(self.status, json=payload if self.status == 200 else {"error": "bad"})


@pytest.fixture
def exchange(monkeypatch):
    fake = FakeExchange()
    monkeypatch.setattr(appmod.sp.http, "post", fake)
    monkeypatch.setattr(appmod.sp, "get", lambda path: {"id": "u1", "display_name": "Bjørn"})
    return fake


def callback(**params):
    return appmod.auth_callback(
        code=params.get("code"), state=params.get("state"), error=params.get("error")
    )


def test_a_successful_callback_persists_the_client_id_and_goes_home(exchange):
    state = pending_state_after_start()

    resp = callback(code="AQD-x", state=state)

    assert appmod.store.config()["client_id"] == VALID_ID
    assert resp.status_code in (302, 303, 307) and resp.headers["location"] == "/"


def test_the_exchange_uses_the_pending_client_id_not_the_stored_one(exchange):
    appmod.store.update_config(client_id=OTHER_ID)
    state = pending_state_after_start()

    callback(code="AQD-x", state=state)

    assert exchange.calls[0]["client_id"] == VALID_ID


def test_a_cancelled_signin_shows_a_friendly_page_and_persists_nothing(exchange):
    pending_state_after_start()

    resp = callback(error="access_denied")

    assert resp.status_code == 400
    assert b"cancelled" in resp.body.lower()
    assert appmod.store.config()["client_id"] is None
    assert exchange.calls == []


def test_a_stale_or_mismatched_state_is_refused(exchange):
    pending_state_after_start()

    resp = callback(code="AQD-x", state="wrong-state")

    assert resp.status_code == 400
    assert appmod.store.config()["client_id"] is None
    assert exchange.calls == []


def test_a_failed_exchange_leaves_the_stored_client_id_alone(monkeypatch):
    appmod.store.update_config(client_id=OTHER_ID)
    fake = FakeExchange(status=400)
    monkeypatch.setattr(appmod.sp.http, "post", fake)
    state = pending_state_after_start()

    resp = callback(code="AQD-x", state=state)

    assert resp.status_code == 502
    assert appmod.store.config()["client_id"] == OTHER_ID


def pending_state_after_start():
    start(VALID_ID)
    return pending()["state"]


# ---- the wizard's dashboard step -------------------------------------------


def test_the_ui_can_ask_which_redirect_uri_to_paste_into_the_dashboard():
    got = appmod.auth_redirect_uri()["redirect_uri"]

    assert got.endswith("/auth/callback")
