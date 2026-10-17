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
