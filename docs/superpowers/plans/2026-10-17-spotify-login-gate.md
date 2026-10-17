# Public sortify behind a Sign in with Spotify gate: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve sortify on a second, public port (for Tailscale Funnel :10000) where only the owner, proven by signing in with Spotify, gets in; the tailnet port stays exactly as it is.

**Architecture:** One uvicorn server binds two sockets (8800 tailnet, 8801 public). An HTTP middleware gates every request that arrived on the public socket, identified by the ASGI `scope["server"]` port, not by headers. The gate does a PKCE login against Spotify with no scopes, calls `GET /me` once, and sets a signed 90-day sliding cookie if the id equals the pinned `gate_owner_id`. All cookie and path rules live in a pure `sortify/gate.py`.

**Tech Stack:** Python 3, FastAPI/starlette on uvicorn 0.52, stdlib `hmac`/`hashlib`/`secrets` (no new dependencies), pytest with `fastapi.testclient.TestClient`, the hand-run `tests/ui_harness.mjs`.

**Spec:** `docs/superpowers/specs/2026-10-16-spotify-login-gate-design.md`

## Global Constraints

- No new Python dependencies. Stdlib only for crypto and cookies.
- Public port 8801, Funnel `https://mbp.tail916fc5.ts.net:10000`, gate redirect URI `https://mbp.tail916fc5.ts.net:10000/gate/callback`.
- Cookies: `sortify_gate` (session, 90 days, sliding, re-issued when older than 1 day) and `sortify_gate_pending` (10 min, path `/gate`). Both HttpOnly, Secure, SameSite=Lax.
- `SORTIFY_GATE_SECRET` at least 32 bytes. Missing or short: the public socket is not opened, and the gate refuses everyone.
- `gate_owner_id` lives in `data/config.json`. Empty means nobody gets in.
- The gate never reads or writes `tokens.json` except through `sp.effective_cooldown_until()` (a read). The owner's token and the Reconnect flow's `pending` entry must be byte-identical after a gate sign-in.
- One `GET /me` per sign-in, through `Spotify.request` (ledger, throttle, cooldown guard). At most 20 gate verifications per day (in-memory).
- No Spotify scopes on the gate login.
- The Claude button (`rc-button.js` line) is served on the tailnet port only, never on the public port.
- Never call api.spotify.com from tests or by hand. Tests fake `sp.exchange_code` and `sp.whoami`, or `sp.http`.
- Tests: `.venv/bin/pytest -q` and `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs` (`node` is not on PATH) both green before every commit.

## Review Focus

1. **The user taps Cancel on Spotify's consent page.** Spotify redirects with `?error=access_denied`; expect a plain "cancelled" page, not a 500. Test in Task 3.
2. **The callback is reloaded or reached again** (pending cookie gone after a success, or older than 10 minutes); expect "expired, start again", not a 500 and no Spotify call. Test in Task 3.
3. **The secret disappears from the environment while a valid cookie exists** (unit edited, `.env` lost); expect the public port to refuse everyone, never to let anyone through. Test in Task 3.
4. **`gate_owner_id` is missing** (config rewritten, live-data clobber); expect nobody to get in and the callback to say why, without spending the `/me` call. Test in Task 3.
5. **A stale public tab POSTs after its cookie expired**; expect `401 {"gate": true}` (not a redirect a `fetch` would follow), and app.js going to `/gate/start` instead of showing the Spotify setup screen. Tests in Task 3 and Task 5.

---

## File structure

- Create `sortify/gate.py`: pure helpers (sign/verify, session and pending cookie values, the open-path predicate, the daily counter). No FastAPI, no network.
- Modify `sortify/spotify.py`: `build_auth_url` takes `redirect_uri` and `scope`; new `exchange_code`; `request` takes `bearer`; new `whoami`.
- Modify `sortify/app.py`: config constants, middleware, `/gate/start`, `/gate/callback`, button strip in the index, two-socket `main()`.
- Modify `sortify/static/app.js`: `api()` handles `401 {"gate": true}`.
- Tests: create `tests/test_gate.py`, `tests/test_gate_app.py`, `tests/test_gate_sockets.py`; modify `tests/test_playback.py` (whoami), `tests/test_cache_busting.py` (index signature), `tests/ui_harness.mjs`.
- Ops: `.gitignore`, `CLAUDE.md`, `~/.config/systemd/user/sortify.service` (outside the repo).

---

### Task 1: gate.py, the pure rules

**Files:**
- Create: `sortify/gate.py`
- Test: `tests/test_gate.py`

**Interfaces:**
- Produces:
  - constants `SESSION_COOKIE = "sortify_gate"`, `PENDING_COOKIE = "sortify_gate_pending"`, `SESSION_MAX_AGE = 90*24*3600`, `RENEW_AFTER = 24*3600`, `PENDING_MAX_AGE = 600`, `SECRET_MIN = 32`, `DAILY_VERIFY_CAP = 20`
  - `secret_ok(secret: str | None) -> bool`
  - `session_value(secret: str, uid: str, now: float | None = None) -> str`
  - `session_uid(secret: str | None, value: str | None, owner_id: str | None) -> str | None`
  - `needs_renewal(secret: str | None, value: str | None, now: float | None = None) -> bool`
  - `pending_value(secret: str, state: str, verifier: str, now: float | None = None) -> str`
  - `pending_verifier(secret: str | None, value: str | None, state: str | None) -> str | None`
  - `is_open_path(path: str) -> bool`
  - `class DailyCounter(cap: int = DAILY_VERIFY_CAP)` with `take(today: str) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
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


def test_the_daily_counter_caps_and_resets_with_the_day():
    c = gate.DailyCounter(cap=2)

    assert c.take("2026-10-17") and c.take("2026-10-17")
    assert not c.take("2026-10-17")
    assert c.take("2026-10-18")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_gate.py`
Expected: FAIL, `ImportError: cannot import name 'gate' from 'sortify'`

- [ ] **Step 3: Write `sortify/gate.py`**

```python
"""The public port's lock: Sign in with Spotify, owner only.

Pure helpers: no FastAPI, no network, so every rule tests directly. The app
wires them up in its middleware and the two /gate routes. Spec:
docs/superpowers/specs/2026-10-16-spotify-login-gate-design.md

Every check here fails closed. A missing secret, a missing owner, a cookie
that does not parse: each means "not let in", never "let in".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

SESSION_COOKIE = "sortify_gate"
PENDING_COOKIE = "sortify_gate_pending"
SESSION_MAX_AGE = 90 * 24 * 3600
# Sliding: a cookie in use is re-issued once it is a day old, so a phone
# that opens sortify every few weeks is never logged out.
RENEW_AFTER = 24 * 3600
PENDING_MAX_AGE = 10 * 60
SECRET_MIN = 32
DAILY_VERIFY_CAP = 20

# The manifest and icons are fetched by the browser without cookies, so
# gating them breaks add-to-home-screen. They reveal nothing.
_OPEN_PATHS = {"/gate/start", "/gate/callback", "/static/manifest.webmanifest"}
_ICON = re.compile(r"^/static/[a-z0-9-]+\.png$")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def secret_ok(secret: str | None) -> bool:
    return bool(secret) and len(secret.encode()) >= SECRET_MIN


def _mac(secret: str, body: str) -> str:
    return _b64e(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def _sign(secret: str, payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_mac(secret, body)}"


def _unsign(secret: str | None, value: str | None, kind: str) -> dict | None:
    """The payload if the signature holds, the kind matches and it has not
    expired; None for anything else."""
    if not secret_ok(secret) or not value or value.count(".") != 1:
        return None
    body, mac = value.split(".")
    # Bytes, not str: compare_digest raises on non-ASCII str, and a cookie
    # is whatever the client sends.
    if not hmac.compare_digest(mac.encode(), _mac(secret, body).encode()):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("k") != kind:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp <= time.time():
        return None
    return payload


def session_value(secret: str, uid: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    return _sign(secret, {"k": "session", "uid": uid, "iat": int(now),
                          "exp": int(now) + SESSION_MAX_AGE})


def session_uid(secret: str | None, value: str | None, owner_id: str | None) -> str | None:
    """The owner's id if this cookie lets them in, else None."""
    if not owner_id:
        return None
    payload = _unsign(secret, value, "session")
    if not payload or payload.get("uid") != owner_id:
        return None
    return owner_id


def needs_renewal(secret: str | None, value: str | None, now: float | None = None) -> bool:
    payload = _unsign(secret, value, "session")
    now = time.time() if now is None else now
    return bool(payload) and now - payload.get("iat", 0) > RENEW_AFTER


def pending_value(secret: str, state: str, verifier: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    return _sign(secret, {"k": "pending", "state": state, "verifier": verifier,
                          "exp": int(now) + PENDING_MAX_AGE})


def pending_verifier(secret: str | None, value: str | None, state: str | None) -> str | None:
    """The PKCE verifier if this pending cookie belongs to `state`, else None."""
    payload = _unsign(secret, value, "pending")
    if not payload or not state:
        return None
    if not hmac.compare_digest(str(payload.get("state", "")).encode(), state.encode()):
        return None
    return payload.get("verifier")


def is_open_path(path: str) -> bool:
    return path in _OPEN_PATHS or bool(_ICON.match(path))


class DailyCounter:
    """At most `cap` gate verifications a day. In memory: a restart resets
    it, which is fine for a backstop against strangers, who cannot get past
    a Development Mode app's allowlist anyway."""

    def __init__(self, cap: int = DAILY_VERIFY_CAP):
        self.cap = cap
        self.day: str | None = None
        self.count = 0

    def take(self, today: str) -> bool:
        if today != self.day:
            self.day, self.count = today, 0
        if self.count >= self.cap:
            return False
        self.count += 1
        return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q tests/test_gate.py`
Expected: PASS (14 passed)

- [ ] **Step 5: Commit**

```bash
git add sortify/gate.py tests/test_gate.py
git commit -m "feat(gate): the public port's cookie and path rules"
```

---

### Task 2: Spotify client, a login that is not the owner's token

**Files:**
- Modify: `sortify/spotify.py` (`build_auth_url` at ~line 150, `finish_auth` at ~line 351, `request` at ~line 440, new `exchange_code` and `whoami`)
- Test: `tests/test_playback.py` (append)

**Interfaces:**
- Produces:
  - `build_auth_url(client_id: str, challenge: str, state: str, *, redirect_uri: str | None = None, scope: str | None = None) -> str`. `None` means today's values (`REDIRECT_URI`, `SCOPES`); `scope=""` leaves the `scope` parameter out.
  - `Spotify.exchange_code(code: str, verifier: str, client_id: str, redirect_uri: str | None = None) -> dict` (the token endpoint's JSON; raises `SpotifyError` on non-200; persists nothing)
  - `Spotify.request(..., bearer: str | None = None, ...)`: with `bearer`, that token is sent instead of the owner's, and a 401 raises at once (no refresh retry, which would touch the owner's tokens)
  - `Spotify.whoami(access_token: str) -> str` (the `/me` id; one call through `request`)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_playback.py`)

```python
# ---- the public gate's login (spec: 2026-10-16 login gate) ----------------


def test_the_gate_login_asks_for_no_scopes_and_its_own_redirect():
    from urllib.parse import parse_qs, urlparse

    from sortify.spotify import REDIRECT_URI, build_auth_url

    url = build_auth_url("cid", "chal", "st", redirect_uri="https://x/gate/callback", scope="")
    q = parse_qs(urlparse(url).query)

    assert "scope" not in q
    assert q["redirect_uri"] == ["https://x/gate/callback"]
    # The owner's Reconnect login is unchanged.
    q2 = parse_qs(urlparse(build_auth_url("cid", "chal", "st")).query)
    assert q2["redirect_uri"] == [REDIRECT_URI]
    assert q2["scope"] == [SCOPES]


def test_exchange_code_returns_the_tokens_and_persists_nothing(sp, monkeypatch):
    before = sp.store.tokens()
    sent = {}

    def fake_post(url, data=None, **kw):
        sent.update(url=url, data=data)
        return FakeResponse(200, {"access_token": "visitor", "expires_in": 3600})

    monkeypatch.setattr(sp.http, "post", fake_post)

    out = sp.exchange_code("code1", "ver1", "cid", redirect_uri="https://x/gate/callback")

    assert out["access_token"] == "visitor"
    assert sent["data"]["redirect_uri"] == "https://x/gate/callback"
    assert sent["data"]["code_verifier"] == "ver1"
    assert sp.store.tokens() == before


def test_exchange_code_raises_on_a_refused_code(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "post",
                        lambda url, data=None, **kw: FakeResponse(400, None, text="invalid_grant"))

    with pytest.raises(SpotifyError):
        sp.exchange_code("used", "ver1", "cid")


def test_whoami_sends_the_visitors_token_not_the_owners(sp, monkeypatch):
    sent = {}

    def fake_request(method, url, headers=None, **kw):
        sent.update(method=method, url=url, auth=(headers or {}).get("Authorization"))
        return FakeResponse(200, {"id": "visitor-id"})

    monkeypatch.setattr(sp.http, "request", fake_request)

    assert sp.whoami("visitor-token") == "visitor-id"
    assert sent["url"].endswith("/me")
    assert sent["auth"] == "Bearer visitor-token"


def test_whoami_spends_one_call_on_the_ledger(sp, monkeypatch):
    monkeypatch.setattr(sp.http, "request",
                        lambda method, url, headers=None, **kw: FakeResponse(200, {"id": "v"}))
    before = sp.budget_spent()

    sp.whoami("visitor-token")

    assert sp.budget_spent() == before + 1


def test_a_401_on_the_visitors_token_does_not_touch_the_owners(sp, monkeypatch):
    sp.store.save_tokens({"refresh_token": "owner-rt", "expires_at": 9e12, "access_token": "owner"})
    before = sp.store.tokens()
    monkeypatch.setattr(sp.http, "request",
                        lambda method, url, headers=None, **kw: FakeResponse(401, None, text="bad token"))

    with pytest.raises(SpotifyError) as e:
        sp.whoami("visitor-token")

    assert e.value.status == 401
    assert sp.store.tokens() == before


def test_whoami_makes_no_call_during_a_cooldown(sp, monkeypatch):
    import time as _time

    sp.cooldown_until = _time.time() + 3600
    called = []
    monkeypatch.setattr(sp.http, "request",
                        lambda *a, **kw: called.append(1) or FakeResponse(200, {"id": "v"}))

    with pytest.raises(SpotifyError) as e:
        sp.whoami("visitor-token")

    assert e.value.status == 429
    assert called == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_playback.py -k "gate or exchange or whoami or visitor"`
Expected: FAIL (`build_auth_url() got an unexpected keyword argument 'redirect_uri'`, `'Spotify' object has no attribute 'exchange_code'` / `'whoami'`)

- [ ] **Step 3: Implement**

Replace `build_auth_url`:

```python
def build_auth_url(client_id: str, challenge: str, state: str, *,
                   redirect_uri: str | None = None, scope: str | None = None) -> str:
    """The authorize URL. Defaults are the owner's Reconnect login; the public
    gate passes its own redirect URI and scope="" (identity only, no
    permissions), which leaves the scope parameter out altogether."""
    scope = SCOPES if scope is None else scope
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri or REDIRECT_URI,
    }
    if scope:
        params["scope"] = scope
    params.update(code_challenge_method="S256", code_challenge=challenge, state=state)
    return AUTH_URL + "?" + urlencode(params)
```

Add to `class Spotify`, in the auth section just above `finish_auth`:

```python
    def exchange_code(self, code: str, verifier: str, client_id: str,
                      redirect_uri: str | None = None) -> dict:
        """Trade an authorization code for tokens. Persists nothing: the
        owner's Reconnect stores them (finish_auth), the public gate only
        reads the access token once and drops it."""
        resp = self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri or REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        if resp.status_code != 200:
            raise SpotifyError(resp.status_code, resp.text)
        return resp.json()
```

In `finish_auth`, replace the inline `resp = self.http.post(...)` block and the `if resp.status_code != 200` check, and the later `resp.json()`, with:

```python
        payload = self.exchange_code(code, pending["verifier"], client_id)
        self.store.update_config(client_id=client_id)
        self._store_token_response(payload, clear_pending=True)
```

In `request`: add `bearer: str | None = None` to the signature after `spend_reserve`, and change the header and the 401 branch:

```python
            token = bearer if bearer is not None else self._access_token()
            resp = self.http.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
            if resp.status_code == 401:
                # Someone else's token (the public gate's visitor): a refresh
                # would rewrite the OWNER's tokens and cannot fix theirs.
                if bearer is not None:
                    raise SpotifyError(401, resp.text[:300])
                # "Permissions missing" = insufficient scope: ...(existing comment and code unchanged)
```

Add after `get_background`:

```python
    def whoami(self, access_token: str) -> str:
        """The Spotify user id behind a visitor's token: the public gate's
        one call per sign-in. Through `request`, so it is on the ledger and
        refused during a cooldown like any other call."""
        me = self.request("GET", "/me", bearer=access_token) or {}
        uid = me.get("id")
        if not uid:
            raise SpotifyError(502, "/me answered without an id")
        return uid
```

- [ ] **Step 4: Run the tests to verify they pass, and the connect-flow tests still do**

Run: `.venv/bin/pytest -q tests/test_playback.py tests/test_connect_flow.py tests/test_reauth.py tests/test_store_and_auth.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sortify/spotify.py tests/test_playback.py
git commit -m "feat(gate): a Spotify login that never touches the owner's token"
```

---

### Task 3: The gate in the app

**Files:**
- Modify: `sortify/app.py` (imports ~line 3-65; constants after `sp = Spotify(store)` at line 75; middleware and routes after the `/auth/callback` route ~line 238; `index()` ~line 4569)
- Modify: `tests/test_cache_busting.py` (4 calls of `appmod.index()`)
- Test: `tests/test_gate_app.py`

**Interfaces:**
- Consumes: everything Task 1 produces; `build_auth_url(..., redirect_uri=, scope=)`, `pkce_pair()`, `sp.exchange_code(code, verifier, client_id, redirect_uri=)`, `sp.whoami(token)` from Task 2.
- Produces:
  - `appmod.PUBLIC_PORT: int` (0 = off), `appmod.PUBLIC_ORIGIN: str`, `appmod.GATE_REDIRECT_URI: str`, `appmod._gate_counter: gate.DailyCounter`
  - `appmod._gate_secret() -> str | None`, `appmod._arrived_public(scope: dict) -> bool`
  - `appmod._index_response(public: bool) -> HTMLResponse`; route `index(request: Request)`
  - routes `GET /gate/start`, `GET /gate/callback`; responses `401 {"detail": ..., "gate": true}` from the middleware

- [ ] **Step 1: Write the failing tests** (`tests/test_gate_app.py`)

```python
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
```

In `tests/test_cache_busting.py`, replace each `appmod.index()` with `appmod._index_response(public=False)` (4 places):

```bash
sed -i 's/appmod\.index()/appmod._index_response(public=False)/g' tests/test_cache_busting.py
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_gate_app.py tests/test_cache_busting.py`
Expected: FAIL (`module 'sortify.app' has no attribute 'PUBLIC_PORT'` / `'_index_response'`)

- [ ] **Step 3: Implement in `sortify/app.py`**

Imports: add `import secrets` to the stdlib block; change `from fastapi import Body, FastAPI, HTTPException` to `from fastapi import Body, FastAPI, HTTPException, Request`; change `from fastapi.responses import HTMLResponse, RedirectResponse` to `from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse`; add `from . import gate` beside `from . import filing`; extend the `from .spotify import (...)` list with `build_auth_url` and `pkce_pair`.

After `sp = Spotify(store)`:

```python
# ---- the public port (spec: 2026-10-16 login gate) -------------------------
#
# A second socket for Tailscale Funnel. Requests that arrive on it go through
# the Sign in with Spotify gate; the tailnet socket is untouched. Which socket
# a request came in on is read from scope["server"] (the local address of the
# connection), never from headers, which a client can forge. Both routes
# reach us from 127.0.0.1 via tailscaled, so the address cannot tell them
# apart either.
PUBLIC_PORT = int(os.environ.get("SORTIFY_PUBLIC_PORT", "0") or 0)
PUBLIC_ORIGIN = os.environ.get(
    "SORTIFY_PUBLIC_ORIGIN", "https://mbp.tail916fc5.ts.net:10000"
).rstrip("/")
GATE_REDIRECT_URI = PUBLIC_ORIGIN + "/gate/callback"
_gate_counter = gate.DailyCounter()


def _gate_secret() -> str | None:
    # Read per request, not at import: tests set it, and a missing one must
    # close the gate the moment it is gone.
    return os.environ.get("SORTIFY_GATE_SECRET")


def _arrived_public(scope: dict) -> bool:
    server = scope.get("server") or (None, None)
    return bool(PUBLIC_PORT) and server[1] == PUBLIC_PORT
```

After the `/auth/callback` route:

```python
# ---- the public gate -------------------------------------------------------

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _set_session(response, secret: str, uid: str) -> None:
    response.set_cookie(
        gate.SESSION_COOKIE, gate.session_value(secret, uid),
        max_age=gate.SESSION_MAX_AGE, httponly=True, secure=True, samesite="lax", path="/",
    )


@app.middleware("http")
async def public_gate(request: Request, call_next):
    """Owner only on the public port; a no-op on the tailnet one."""
    if not _arrived_public(request.scope):
        return await call_next(request)
    path = request.url.path
    if gate.is_open_path(path):
        return await call_next(request)
    secret = _gate_secret()
    value = request.cookies.get(gate.SESSION_COOKIE)
    owner = store.config().get("gate_owner_id")
    if not gate.session_uid(secret, value, owner):
        # fetch() follows redirects silently, so an API call (or any write)
        # gets a 401 that app.js turns into a trip to /gate/start.
        if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "sign in with Spotify", "gate": True}, status_code=401)
        return RedirectResponse("/gate/start", status_code=302)
    # SameSite=Lax already keeps the cookie off cross-site writes; this is
    # the second layer. Browsers send Origin on every POST, same-origin too.
    if request.method in _UNSAFE_METHODS and request.headers.get("origin") != PUBLIC_ORIGIN:
        return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
    response = await call_next(request)
    if gate.needs_renewal(secret, value):
        _set_session(response, secret, owner)
    return response


@app.get("/gate/start")
def gate_start(request: Request):
    secret = _gate_secret()
    if not _arrived_public(request.scope) or not gate.secret_ok(secret):
        raise HTTPException(404)
    client_id = store.config().get("client_id")
    if not client_id:
        return _auth_page(503, "sortify has no Spotify app set up yet. Connect it over the tailnet first.")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    resp = RedirectResponse(
        build_auth_url(client_id, challenge, state, redirect_uri=GATE_REDIRECT_URI, scope=""),
        status_code=302,
    )
    # The pending login lives in this cookie, not in tokens.json, where the
    # owner's own Reconnect keeps its pending state.
    resp.set_cookie(
        gate.PENDING_COOKIE, gate.pending_value(secret, state, verifier),
        max_age=gate.PENDING_MAX_AGE, httponly=True, secure=True, samesite="lax", path="/gate",
    )
    return resp


@app.get("/gate/callback")
def gate_callback(request: Request, code: str | None = None, state: str | None = None,
                  error: str | None = None):
    secret = _gate_secret()
    if not _arrived_public(request.scope) or not gate.secret_ok(secret):
        raise HTTPException(404)
    if error:
        return _auth_page(400, "Spotify sign-in was cancelled.")
    verifier = gate.pending_verifier(secret, request.cookies.get(gate.PENDING_COOKIE), state)
    if not code or not verifier:
        return _auth_page(400, "That sign-in expired or didn't match. Please start again.")
    owner = store.config().get("gate_owner_id")
    if not owner:
        return _auth_page(403, "This sortify has no owner set, so nobody can sign in here.")
    cooldown = sp.effective_cooldown_until()
    if time.time() < cooldown:
        until = datetime.fromtimestamp(cooldown).strftime("%H:%M")
        return _auth_page(503, f"Spotify is cooling down. Sign-in works again at {until}, "
                               "or use the tailnet address.")
    if not _gate_counter.take(time.strftime("%Y-%m-%d")):
        return _auth_page(429, "Too many sign-ins today. Try again tomorrow, or use the tailnet address.")
    try:
        tokens = sp.exchange_code(code, verifier, store.config()["client_id"],
                                  redirect_uri=GATE_REDIRECT_URI)
        uid = sp.whoami(tokens["access_token"])
    except SpotifyError as e:
        log.warning("gate: sign-in failed: %s", e)
        return _auth_page(502, "Couldn't complete the Spotify sign-in. Please try again.")
    if uid != owner:
        log.warning("gate: refused a sign-in by another Spotify account")
        return _auth_page(403, "This sortify belongs to someone else.")
    log.info("gate: owner signed in on the public port")
    resp = RedirectResponse("/", status_code=303)
    _set_session(resp, secret, owner)
    resp.delete_cookie(gate.PENDING_COOKIE, path="/gate")
    return resp
```

Replace the `index()` route (keep its docstring on `_index_response`):

```python
# The Claude remote-session button: tailnet only, never on a public page
# (standing rule; decided for sortify 2026-10-17).
_RC_BUTTON = re.compile(r'^<script src="[^"]*/rc-button\.js"[^>]*></script>\n', re.M)


def _index_response(public: bool) -> HTMLResponse:
    """(existing index() docstring, unchanged)"""
    html = _ASSET_REF.sub(
        lambda m: f"/static/{m.group(1)}?v={int((STATIC / m.group(1)).stat().st_mtime)}",
        (STATIC / "index.html").read_text(),
    )
    if public:
        html = _RC_BUTTON.sub("", html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/")
def index(request: Request):
    return _index_response(_arrived_public(request.scope))
```

- [ ] **Step 4: Run the tests to verify they pass, then the whole suite**

Run: `.venv/bin/pytest -q tests/test_gate_app.py tests/test_cache_busting.py && .venv/bin/pytest -q`
Expected: PASS, and the full suite still green (1002 + the new tests)

- [ ] **Step 5: Commit**

```bash
git add sortify/app.py tests/test_gate_app.py tests/test_cache_busting.py
git commit -m "feat(gate): owner-only public port behind Sign in with Spotify"
```

---

### Task 4: Two sockets in one server

**Files:**
- Modify: `sortify/app.py` (`main()` at the end; add `import socket` to the stdlib imports)
- Test: `tests/test_gate_sockets.py`

**Interfaces:**
- Consumes: `appmod.PUBLIC_PORT`, `appmod._gate_secret()`, `gate.secret_ok`, `gate.SECRET_MIN`
- Produces: `appmod._bind(host: str, port: int) -> socket.socket`, `appmod._sockets(host: str, port: int, public_port: int, secret: str | None) -> list[socket.socket]`

- [ ] **Step 1: Write the failing tests** (`tests/test_gate_sockets.py`)

```python
"""One server, two sockets: the gate depends on uvicorn reporting the port a
connection ARRIVED on in scope["server"]. Proven here end to end on two
ephemeral loopback ports. No Spotify calls: only "/" is requested."""

import asyncio
import http.client
import threading
import time

import uvicorn

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

SECRET = "k" * 48


def test_no_public_socket_without_a_good_secret():
    socks = appmod._sockets("127.0.0.1", 0, 1, None)
    try:
        assert len(socks) == 1
    finally:
        for s in socks:
            s.close()


def test_no_public_socket_when_the_port_is_off():
    socks = appmod._sockets("127.0.0.1", 0, 0, SECRET)
    try:
        assert len(socks) == 1
    finally:
        for s in socks:
            s.close()


def test_each_socket_is_told_apart(monkeypatch):
    tail = appmod._bind("127.0.0.1", 0)
    pub = appmod._bind("127.0.0.1", 0)
    monkeypatch.setattr(appmod, "PUBLIC_PORT", pub.getsockname()[1])
    monkeypatch.setenv("SORTIFY_GATE_SECRET", SECRET)
    server = uvicorn.Server(uvicorn.Config(appmod.app, log_level="warning", lifespan="off"))
    t = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[tail, pub])), daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)

        def get(sock):
            c = http.client.HTTPConnection("127.0.0.1", sock.getsockname()[1], timeout=5)
            c.request("GET", "/")
            r = c.getresponse()
            return r.status, r.getheader("location"), r.read().decode()

        status, _, body = get(tail)
        assert status == 200 and "rc-button.js" in body
        status, location, _ = get(pub)
        assert status == 302 and location == "/gate/start"
    finally:
        server.should_exit = True
        t.join(timeout=5)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_gate_sockets.py`
Expected: FAIL (`module 'sortify.app' has no attribute '_sockets'`)

- [ ] **Step 3: Implement**

Replace `main()`:

```python
def _bind(host: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    return s


def _sockets(host: str, port: int, public_port: int, secret: str | None) -> list[socket.socket]:
    """The tailnet socket always; the public one only with a usable secret,
    so a lost secret closes the public door instead of opening it."""
    socks = [_bind(host, port)]
    if public_port:
        if gate.secret_ok(secret):
            socks.append(_bind(host, public_port))
        else:
            log.error("public port %s NOT opened: SORTIFY_GATE_SECRET missing or under %s bytes",
                      public_port, gate.SECRET_MIN)
    return socks


def main():
    socks = _sockets(
        os.environ.get("SORTIFY_HOST", "0.0.0.0"),
        int(os.environ.get("SORTIFY_PORT", "8800")),
        PUBLIC_PORT,
        _gate_secret(),
    )
    uvicorn.Server(uvicorn.Config(app)).run(sockets=socks)
```

- [ ] **Step 4: Run the tests, then the whole suite**

Run: `.venv/bin/pytest -q tests/test_gate_sockets.py && .venv/bin/pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sortify/app.py tests/test_gate_sockets.py
git commit -m "feat(gate): one server, a tailnet socket and a public one"
```

---

### Task 5: The frontend follows the gate

**Files:**
- Modify: `sortify/static/app.js:39` (`api()`)
- Test: `tests/ui_harness.mjs` (the `vm.createContext({...})` at ~line 202, and a new block before the summary)

**Interfaces:**
- Consumes: the middleware's `401 {"detail": "sign in with Spotify", "gate": true}` from Task 3.

- [ ] **Step 1: Write the failing check**

In `tests/ui_harness.mjs`, add `location` to the context object passed to `vm.createContext` (after `Audio: AudioStub,`):

```js
  // The public gate sends the page to /gate/start by assigning this.
  location: { href: "" },
```

Then add a block before the final summary (read the `ui-harness append-anchor` memory first: append as its own `{ ... }` block, keep the summary lines intact):

```js
// ============================================================================
// GT — the public port's gate. A 401 that says `gate` means the visitor is
// not signed in on the public port: go sign in with Spotify. It must not be
// mistaken for the owner's Spotify login having lapsed (needs_auth), which
// shows the setup view instead.
{
  run(`show("now"); location.href = ""`);
  routes["GET /api/status"] = { status: 401, body: { detail: "sign in with Spotify", gate: true } };
  await run(`api("/api/status").catch(() => {})`);
  check("GT a gate 401 sends the page to /gate/start",
        run("location.href") === "/gate/start", `href=${run("location.href")}`);
  check("GT ...and does not show the Spotify setup view",
        $$("view-setup").hidden === true, `setup hidden=${$$("view-setup").hidden}`);
  delete routes["GET /api/status"];
}
```

- [ ] **Step 2: Run the harness to verify the new checks fail**

Run: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | grep -E "^(FAIL|PASS)  GT|checks passed"`
Expected: `FAIL  GT a gate 401 sends the page to /gate/start`

- [ ] **Step 3: Implement** — in `api()`, before the `needs_auth` line:

```js
  // The public port's gate (not the owner's Spotify login): sign in with
  // Spotify, then come back. A redirect would be followed silently by
  // fetch, so the server answers API calls with this 401 instead.
  if (resp.status === 401 && data.gate) { location.href = "/gate/start"; throw new Error("sign-in needed"); }
```

- [ ] **Step 4: Run the harness and pytest**

Run: `~/.nvm/versions/node/v22.23.1/bin/node tests/ui_harness.mjs 2>&1 | tail -1 && .venv/bin/pytest -q 2>&1 | tail -1`
Expected: `527/527 checks passed` (525 + 2) and pytest green

- [ ] **Step 5: Commit**

```bash
git add sortify/static/app.js tests/ui_harness.mjs
git commit -m "feat(gate): a gate 401 sends the page to Sign in with Spotify"
```

---

### Task 6: Docs, config and rollout

**Files:**
- Modify: `.gitignore`, `CLAUDE.md`
- Outside the repo: `~/.config/systemd/user/sortify.service`, `.env` (git-ignored), `data/config.json` (`gate_owner_id`)

Code-wise this task only touches docs; the rest is deployment, with three stops for the human.

- [ ] **Step 1: `.gitignore`** — add a line `.env`.

- [ ] **Step 2: `CLAUDE.md`** — under `## Run`, after the Tailscale paragraph, add:

```markdown
- **Public route (since 2026-10):** Tailscale Funnel `https://mbp.tail916fc5.ts.net:10000`
  → `127.0.0.1:8801`, a second socket of the same process
  (`SORTIFY_PUBLIC_PORT`). Everything on it is behind Sign in with Spotify,
  owner only (`gate_owner_id` in config; `sortify/gate.py`). The port a request
  arrived on (`scope["server"]`) is what separates the routes — never headers.
  The Claude button is stripped from public pages and stays on the tailnet
  port. Each sign-in costs one `/me` call. Secret: `SORTIFY_GATE_SECRET` in
  the git-ignored `.env`; without it the public socket stays closed. Spec:
  `docs/superpowers/specs/2026-10-16-spotify-login-gate-design.md`.
```

- [ ] **Step 3: Commit**

```bash
git add .gitignore CLAUDE.md
git commit -m "docs: sortify's public route and its gate"
```

- [ ] **Step 4 (human): add the redirect URI.** Ask the user to open their app on developer.spotify.com, Settings → Redirect URIs, add `https://mbp.tail916fc5.ts.net:10000/gate/callback`, and save. Wait for their confirmation.

- [ ] **Step 5: secret and owner** (no API calls)

```bash
cd ~/kode/spotify/sortify && umask 077 && .venv/bin/python -c "import secrets; print('SORTIFY_GATE_SECRET=' + secrets.token_urlsafe(48))" > .env && .venv/bin/python -c "from sortify.store import Store; s = Store(); s.update_config(gate_owner_id=s.cache()['me']['id']); print('owner set')"
```

- [ ] **Step 6: unit file** — in `~/.config/systemd/user/sortify.service`, under `[Service]` after the `SORTIFY_HOST` line:

```ini
# the public (Funnel :10000) socket, gated by Sign in with Spotify
Environment=SORTIFY_PUBLIC_PORT=8801
EnvironmentFile=/home/mbp2016/kode/spotify/sortify/.env
```

Then `systemctl --user daemon-reload`.

- [ ] **Step 6b: update the device list.** `~/.claude/CLAUDE.md` still names
  port `:10000` as skannonser's archived Funnel route (its "web-public"
  guest copy, stopped 2026-10-08). Once this plan reassigns `:10000` to
  sortify's gated public route, update that entry so the device list
  doesn't point two different services at the same port.

- [ ] **Step 7 (human-coordinated): restart.** Follow the restart memory: check `data/queue.json` for an active materialiser run, announce to every live sortify session and wait for each, then `systemctl --user restart sortify`. Confirm both sockets: `ss -ltn | grep -E ':880[01]'` shows 8800 and 8801 on 127.0.0.1. Confirm the commit predates the start: `systemctl --user show sortify -p ExecMainStartTimestamp`.

- [ ] **Step 8 (human approval): open Funnel.** This publishes the port to the internet; ask before running:

```bash
tailscale funnel --bg --https=10000 http://127.0.0.1:8801 && tailscale funnel status
```

- [ ] **Step 9: verify.** From the box, unauthenticated (no API calls), through
  the Funnel URL:

```bash
.venv/bin/python -c "import urllib.request as u; r=u.urlopen(u.Request('https://mbp.tail916fc5.ts.net:10000/api/status')); print(r.status)" 2>&1 | tail -1
```

Expected: an HTTP 401 error. Also check, still unauthenticated and still
through the Funnel URL (no API calls — these are all redirects or refusals,
never a completed request):

- `GET /static/app.js` → 302 (to `/gate/start`), not the raw script.
- `GET /static/manifest.webmanifest%23/../app.js` → anything but 200 — this
  spelling must not resolve to app.js without a cookie (the encoded-`#`
  bypass this branch fixed).
- `GET /static/index.html` → 302, not the raw file with the Claude button.
- `POST` with no `Origin` header → 401, not a bare write going through.

Then ask the user to open `https://mbp.tail916fc5.ts.net:10000` on the phone
**with Tailscale off**, sign in (1 API call), and see the Now card without
the Claude button. Verify with the **installed home-screen app**, not only a
browser tab — iOS keeps a separate cookie jar for a home-screen-installed
PWA, so a tab that is already signed in proves nothing about the app icon
the user actually taps. Report `spx budget` before and after.

Rollback at any point: `tailscale funnel --https=10000 off`.
