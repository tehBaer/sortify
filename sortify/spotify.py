"""Thin Spotify Web API client with PKCE auth.

PKCE means no client secret exists anywhere — only the public client ID.
The refresh token in tokens.json is the long-lived credential.

Spotify requires redirect URIs to be HTTPS or a loopback IP literal. The
app is reached over tailnet HTTPS, so the callback is a real route on this
server and the browser comes straight back after consenting.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from collections import deque
from typing import Iterator
from urllib.parse import urlencode

import httpx

from .account_ledger import (ACCOUNT_DAILY_CAP, AccountLedger, InCooldown,
                             LedgerFull, classify_429)
from .account_ledger import quota_cooldown_until
from .account_ledger import next_local_midnight as _next_local_midnight
from .store import Store

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
# Must byte-match a Redirect URI registered in the Spotify dashboard app.
# The tailnet HTTPS origin is how every real device reaches sortify
# (tailscale serve :9800 → 127.0.0.1:8800).
REDIRECT_URI = os.environ.get(
    "SORTIFY_REDIRECT_URI", "https://mbp.tail916fc5.ts.net:9800/auth/callback"
)
SCOPES = " ".join(
    [
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-private",
        "playlist-modify-public",
        "user-library-read",
        "user-library-modify",
        "user-read-currently-playing",
        # Skip and "play this input instead" are the only calls that change
        # playback rather than read it. Tokens issued before this was added
        # get 401 "Permissions missing" and need a fresh login to fix.
        "user-modify-playback-state",
    ]
)

LIKED_ID = "liked"  # pseudo-playlist id for the user's Liked Songs

# Local demand ceilings — the hard guarantee against ever earning another
# multi-hour 429 cooldown. Feb-2026 dev-mode quotas are undocumented and
# brutal, so these are set from observed damage rather than from the docs.
#
# 2026-08-13: the background enricher spent 678 calls in ~70 minutes and
# earned a ~23h ban. The old 1200/day and 30-per-60s were guesses, and both
# sat above what actually trips the limiter. Everything here is now below it.
# sortify's *share* of the account budget, unchanged at 600. Since Jul 2026
# Spotify counts quota per developer account, so this is now backed by
# ACCOUNT_DAILY_CAP in ~/kode/spotify-ledger, which all three apps spend from —
# and, more importantly, by the shared cooldown recorded there. This stays as a
# local guard so a missing ledger file still can't uncork us.
DAILY_CAP = 600       # api.spotify.com calls per local day, sortify's share
WINDOW_CAP = 12       # calls per rolling 60s

# Proactive background work (genre enrichment) draws from this much smaller
# allowance inside DAILY_CAP. Nothing the user did not ask for gets to spend
# more than a few dozen calls a day.
BACKGROUND_DAILY_CAP = 40

# The queued materialiser's spend class: user-initiated but unattended. It
# counts toward DAILY_CAP, but the day's LAST 150 calls are reserved for the
# user's own interactive clicks — the bulk job sleeps to local midnight
# instead of spending them. (Spec 2026-08-18, decision 3.)
BULK_RESERVE = 150

# Background work also yields once the day is half spent: at that point the
# user is clearly using the app, and the rest of the budget is theirs.
BACKGROUND_YIELD_FRACTION = 0.5

# After a 429 cooldown expires, proactive work stays silent this much longer.
# Resuming background fetches the instant a cooldown lifted is precisely what
# earned the 2026-08-13 ban ~70 minutes later; the user's own listening must
# be the first traffic Spotify sees from us.
QUIET_AFTER_COOLDOWN = 6 * 3600

# Feb 2026 dev-mode API: playlist entries are "items" containing an "item"
# (the old "tracks"/"track" naming is gone for apps created after 2026-02-11).
ITEM_FIELDS = (
    "items(added_at,is_local,item(uri,id,type,name,duration_ms,"
    "artists(id,name),album(name,images))),next"
)


class AuthNeeded(Exception):
    pass


class SpotifyError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Spotify API {status}: {message}")


class AuthFlowError(SpotifyError):
    """The login attempt itself is broken (no pending auth, stale state) —
    distinct from Spotify refusing the exchange, so the callback page can say
    "start again" instead of "try again"."""


# Serialises read-modify-write on cache["playlist_list"] between the refresh
# that replaces the listing and the prune that removes swept sittings from it.
_LIST_LOCK = threading.Lock()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, code_challenge(verifier)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_auth_url(client_id: str, challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }
    )


class Spotify:
    def __init__(self, store: Store):
        self.store = store
        # The account-wide budget, shared with playlistener and
        # spotify-autoqueuer. Spotify counts quota per developer account, so
        # sortify's own ledger below can only ever be a second opinion.
        self.ledger = AccountLedger("sortify")
        self.http = httpx.Client(timeout=25)
        # Dev-mode 429 cooldowns can last many hours; calling during one is
        # pointless (and rude), so fail fast until it ends.
        # Persisted: a server restart must not forget an active cooldown and
        # poke the API again (that can extend the penalty).
        self.cooldown_until = float(store.tokens().get("cooldown_until", 0))
        self._last_call = 0.0
        self._window: deque[float] = deque()  # timestamps of recent calls
        self._budget_lock = threading.Lock()
        # Several browser tabs poll concurrently; without a lock they race to
        # refresh and rotate each other's refresh token into invalidity.
        self._refresh_lock = threading.Lock()
        self._refresh_fail_until = 0.0
        # Last 429 seen by request(), including ones it retried through —
        # the queue governor reads this to know a tick was not clean.
        # Observation only; retry behaviour is unchanged (review finding I2).
        self.last_429: dict | None = None

    # ---- local demand limiting ---------------------------------------------

    def budget_spent(self) -> int:
        usage = self.store.usage()
        return usage["count"] if usage["day"] == time.strftime("%Y-%m-%d") else 0

    def background_spent(self) -> int:
        usage = self.store.usage()
        if usage["day"] != time.strftime("%Y-%m-%d"):
            return 0
        return usage.get("background", 0)

    def effective_cooldown_until(self) -> float:
        """Cooldown end, re-read from disk.

        `spx` runs in its own process and shares the same ledger, so a
        cooldown it earned would otherwise be invisible to the long-lived
        server until a restart — and we would keep poking a limiter that is
        already angry.
        """
        persisted = float(self.store.tokens().get("cooldown_until", 0))
        if persisted > self.cooldown_until:
            self.cooldown_until = persisted
        # A cooldown earned by playlistener or spotify-autoqueuer binds us too —
        # the 429 is against the developer account, not the client ID.
        account, _source, _reason = self.ledger.cooldown()
        if account > self.cooldown_until:
            self.cooldown_until = account
        return self.cooldown_until

    def quiet_until(self) -> float:
        """Timestamp until which proactive work must stay silent."""
        cd = self.effective_cooldown_until()
        return cd + QUIET_AFTER_COOLDOWN if cd else 0.0

    def background_block_reason(self) -> str | None:
        """Why proactive work must not run right now — None means it may.

        Checked before every background call rather than once per loop, so a
        cooldown earned mid-run stops the next one immediately.
        """
        now = time.time()
        if now < self.effective_cooldown_until():
            mins = int((self.cooldown_until - now) / 60) + 1
            return f"in rate-limit cooldown (~{mins} min left)"
        if now < self.quiet_until():
            mins = int((self.quiet_until() - now) / 60) + 1
            return f"post-cooldown quiet period (~{mins} min left)"
        if self.background_spent() >= BACKGROUND_DAILY_CAP:
            return f"background budget spent ({BACKGROUND_DAILY_CAP}/day) — resting until midnight"
        yield_at = int(DAILY_CAP * BACKGROUND_YIELD_FRACTION)
        if self.budget_spent() >= yield_at:
            return f"day already {yield_at}+ calls in — leaving the rest for real usage"
        return None

    def bulk_spent(self) -> int:
        usage = self.store.usage()
        if usage["day"] != time.strftime("%Y-%m-%d"):
            return 0
        return usage.get("bulk", 0)

    def bulk_block_reason(self) -> tuple[str, float] | None:
        """Why the bulk worker must not call right now — None means go.

        Returns (reason, resume_at). QUIET_AFTER_COOLDOWN applies here on
        purpose: this rail survived the enricher's deletion named for "the
        next proactive job", and the queued materialiser is that job.
        """
        now = time.time()
        cd = self.effective_cooldown_until()
        if now < cd:
            return ("cooldown", cd)
        if cd and now < cd + QUIET_AFTER_COOLDOWN:
            return ("quiet", cd + QUIET_AFTER_COOLDOWN)
        if self.budget_spent() >= DAILY_CAP - BULK_RESERVE:
            return ("reserve", _next_local_midnight(now))
        # The local usage.json check above only sees what THIS process wrote.
        # The shared account ledger can be further along — another process
        # (spx, a restarted server) spent against sortify's share since the
        # last time usage.json was written — and a LedgerFull raised out of
        # _spend_budget would otherwise surface as an unexplained SpotifyError
        # mid-tick and pause the run for a human. Read-only, and cheap: both
        # reads hit the same small on-disk file, lock-free (AccountLedger.read
        # tolerates a missing file as an empty ledger, i.e. "go").
        if self.ledger.app_spent_today() >= DAILY_CAP - BULK_RESERVE:
            return ("reserve", _next_local_midnight(now))
        # Account-cap analog: the account-wide ceiling can be reached by the
        # siblings alone even while sortify's own share is untouched (the same
        # gap test_account_cap_binds_even_when_sortifys_own_share_is_free
        # covers for _spend_budget). Checked against the full cap, not a
        # reserve fraction — there's no established "interactive reserve"
        # concept at the account level, only sortify's own share of it.
        if self.ledger.spent_today() >= ACCOUNT_DAILY_CAP:
            return ("reserve", _next_local_midnight(now))
        return None

    def _spend_budget(self, background: bool = False, bulk: bool = False) -> None:
        """One API call's worth of budget; blocks briefly to honor the rolling
        window, raises if the applicable cap is spent."""
        with self._budget_lock:
            today = time.strftime("%Y-%m-%d")
            usage = self.store.usage()
            if usage["day"] != today:
                usage = {"day": today, "count": 0, "background": 0, "bulk": 0}
            usage.setdefault("background", 0)
            usage.setdefault("bulk", 0)
            if usage["count"] >= DAILY_CAP:
                raise SpotifyError(
                    429, f"local daily budget ({DAILY_CAP} calls) spent — resting until midnight"
                )
            if background and usage["background"] >= BACKGROUND_DAILY_CAP:
                raise SpotifyError(
                    429,
                    f"background budget ({BACKGROUND_DAILY_CAP} calls) spent — resting until midnight",
                )
            if bulk and usage["count"] >= DAILY_CAP - BULK_RESERVE:
                raise SpotifyError(
                    429,
                    f"bulk budget: interactive reserve ({BULK_RESERVE} calls) "
                    "reached — sleeping until midnight",
                )
            now = time.time()
            while self._window and now - self._window[0] > 60:
                self._window.popleft()
            if len(self._window) >= WINDOW_CAP:
                time.sleep(max(0.0, 60 - (now - self._window[0]) + 0.05))
            self._window.append(time.time())
            # The account-wide ceiling, shared with the sibling apps. Checked
            # last so the cheap local guards and the window pacing have already
            # run, and before the local increment so a refusal here leaves both
            # ledgers agreeing.
            try:
                self.ledger.spend(app_cap=DAILY_CAP, account_cap=ACCOUNT_DAILY_CAP)
            except (InCooldown, LedgerFull) as exc:
                raise SpotifyError(429, str(exc)) from None
            usage["count"] += 1
            if background:
                usage["background"] += 1
            if bulk:
                usage["bulk"] += 1
            self.store.save_usage(usage)

    # ---- auth -------------------------------------------------------------

    def start_auth(self, client_id: str) -> str:
        # The client ID rides in the pending entry and is only persisted by
        # finish_auth on a successful exchange — an abandoned or mistyped
        # attempt must not overwrite the ID a working login was made with.
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        tokens = self.store.tokens()
        tokens["pending"] = {"verifier": verifier, "state": state, "client_id": client_id}
        self.store.save_tokens(tokens)
        return build_auth_url(client_id, challenge, state)

    def finish_auth(self, code: str, state: str) -> dict:
        tokens = self.store.tokens()
        pending = tokens.get("pending")
        if not pending:
            raise AuthFlowError(400, "no auth in progress — start over")
        if state != pending["state"]:
            raise AuthFlowError(400, "state mismatch — start the auth again")
        client_id = pending.get("client_id") or self.store.config()["client_id"]
        resp = self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": pending["verifier"],
            },
        )
        if resp.status_code != 200:
            raise SpotifyError(resp.status_code, resp.text)
        self.store.update_config(client_id=client_id)
        self._store_token_response(resp.json(), clear_pending=True)
        # The token exchange (accounts.spotify.com) can succeed while the API
        # (api.spotify.com) is rate-cooled — auth is done either way.
        cache = self.store.cache()
        try:
            me = self.get("/me")
            cache["me"] = {"id": me["id"], "name": me.get("display_name") or me["id"]}
            self.store.save_cache(cache)
        except SpotifyError:
            pass
        return cache.get("me") or {"id": None, "name": "connected"}

    def _store_token_response(self, payload: dict, clear_pending: bool = False) -> None:
        tokens = self.store.tokens()
        if clear_pending:
            # Only the authorization-code exchange consumes the pending PKCE
            # state — a routine refresh must not kill an in-flight login.
            tokens.pop("pending", None)
        tokens["access_token"] = payload["access_token"]
        tokens["expires_at"] = time.time() + payload.get("expires_in", 3600) - 60
        # Spotify rotates refresh tokens on PKCE refresh; keep the newest.
        if payload.get("refresh_token"):
            tokens["refresh_token"] = payload["refresh_token"]
        self.store.save_tokens(tokens)

    def _access_token(self) -> str:
        tokens = self.store.tokens()
        if not tokens.get("refresh_token"):
            raise AuthNeeded()
        if time.time() < tokens.get("expires_at", 0) and tokens.get("access_token"):
            return tokens["access_token"]
        with self._refresh_lock:
            # Re-read: another thread may have refreshed while we waited.
            tokens = self.store.tokens()
            if time.time() < tokens.get("expires_at", 0) and tokens.get("access_token"):
                return tokens["access_token"]
            # A failed refresh must not be retried per poll — that's a storm.
            if time.time() < self._refresh_fail_until:
                raise AuthNeeded()
            resp = self.http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": self.store.config()["client_id"],
                },
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 300))
                self._refresh_fail_until = time.time() + max(retry_after, 300)
                raise SpotifyError(429, "token endpoint rate limited — backing off")
            if resp.status_code != 200:
                self._refresh_fail_until = time.time() + 300
                raise AuthNeeded()
            self._store_token_response(resp.json())
            return self.store.tokens()["access_token"]

    def authed(self) -> bool:
        return bool(self.store.tokens().get("refresh_token"))

    # ---- request plumbing -------------------------------------------------

    def request(self, method: str, path: str, background: bool = False, bulk: bool = False,
                **kwargs) -> dict | None:
        if time.time() < self.effective_cooldown_until():
            mins = int(self.cooldown_until - time.time()) // 60 + 1
            raise SpotifyError(429, f"in Spotify rate-limit cooldown — try again in ~{mins} min")
        url = path if path.startswith("http") else API + path
        for attempt in range(3):
            self._spend_budget(background=background, bulk=bulk)
            # Mild throttle: bulk fetches shouldn't burst the rolling window.
            wait = self._last_call + 0.2 - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            resp = self.http.request(
                method, url, headers={"Authorization": f"Bearer {self._access_token()}"}, **kwargs
            )
            if resp.status_code == 401:
                # "Permissions missing" = insufficient scope: a refresh can't
                # fix that, and retrying one per poll hammers the token
                # endpoint until Spotify slow-walks us. Fail fast instead.
                if "Permissions missing" in resp.text:
                    raise SpotifyError(401, "Permissions missing (token lacks a scope)")
                if attempt == 0:
                    self.store.save_tokens({**self.store.tokens(), "expires_at": 0})
                    continue
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2))
                kind = classify_429(resp.text, retry_after)
                # time.monotonic(): the queue worker compares this against a
                # tick-start timestamp it also takes with monotonic() (ruling
                # R-T8c) — a wall-clock jump between the two must not make a
                # stale 429 look fresh, or a fresh one look stale.
                self.last_429 = {"ts": time.monotonic(), "kind": kind, "retry_after": retry_after}
                # Two different limiters with opposite remedies. A rate limit is
                # a rolling 30s window and wants a few seconds of patience; a
                # Development Mode quota trip wants the rest of the day, and
                # retrying into one is what extends it.
                if kind == "rate" and retry_after <= 60 and attempt < 2:
                    time.sleep(retry_after)
                    continue
                now = time.time()
                self.cooldown_until = (
                    quota_cooldown_until(now, retry_after) if kind == "quota"
                    else now + retry_after
                )
                self.store.save_tokens(
                    {**self.store.tokens(), "cooldown_until": self.cooldown_until}
                )
                # Tell the siblings: the account is resting, not just sortify.
                self.ledger.note_cooldown(self.cooldown_until, reason=kind)
                left = int(self.cooldown_until - now)
                hrs, rem = divmod(left, 3600)
                human = f"~{hrs}h {rem // 60}m" if hrs else f"~{max(rem // 60, 1)} min"
                label = "daily quota spent" if kind == "quota" else "rate limit hit"
                raise SpotifyError(
                    429, f"Spotify {label} — cooldown {human}. Let it rest; retrying extends it."
                )
            if resp.status_code >= 400:
                raise SpotifyError(resp.status_code, resp.text[:300])
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                # Playback endpoints acknowledge success without JSON (and not
                # always with a truly empty body either). Parsing that
                # unconditionally turned a working skip into a 500: the track
                # changed, the user saw an error, and since JSONDecodeError is
                # not a SpotifyError, callers' cleanup was skipped too.
                return None
        raise SpotifyError(429, "rate limited, gave up")

    def get(self, path: str, **kwargs) -> dict:
        return self.request("GET", path, **kwargs)

    def get_background(self, path: str, **kwargs) -> dict:
        """A GET that draws on the small proactive allowance, not the user's."""
        return self.request("GET", path, background=True, **kwargs)

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = self.get(path, params=params or {})
        while True:
            yield from page.get("items", [])
            if not page.get("next"):
                return
            page = self.get(page["next"])

    # ---- playlists --------------------------------------------------------

    def my_playlists(self, refresh: bool = False) -> list[dict]:
        """The user's playlist listing, served from disk unless asked to reload.

        A thousand playlists is ~21 paginated calls, and WINDOW_CAP turns that
        into a ~60s stall — far too expensive to repay on every Playlists view
        and every 10-minute profile rebuild for a list that changes rarely.
        Refreshing is therefore an explicit act (the Refresh button), which
        also means playlists edited in the Spotify client stay invisible here
        until then: snapshot_id comes from this listing, so the track caches
        keyed off it stay put too.
        """
        if not refresh:
            entry = self.store.cache().get("playlist_list")
            if entry and entry.get("items") is not None:
                return entry["items"]
        items = self._fetch_my_playlists()
        with _LIST_LOCK:
            cache = self.store.cache()
            cache["playlist_list"] = {"fetched_at": time.time(), "items": items}
            self.store.save_cache(cache)
        return items

    def forget_playlists(self, ids: set[str]) -> None:
        """Drop ids from the cached listing after they have been unfollowed.

        Costs nothing and touches no network. Without it the listing keeps
        advertising playlists that no longer exist, so a swept sitting would
        be re-offered as an orphan on every Playlists view until the next
        manual refresh — cleanup that visibly does nothing.

        Read-modify-write under a lock local to this file, which is the same
        lock the refresh path takes. A refresh that lands between this read
        and its own write still wins, and correctly so: it has just asked
        Spotify what actually exists.
        """
        if not ids:
            return
        with _LIST_LOCK:
            cache = self.store.cache()
            entry = cache.get("playlist_list")
            if not entry or entry.get("items") is None:
                return
            kept = [p for p in entry["items"] if p.get("id") not in ids]
            if len(kept) == len(entry["items"]):
                return
            entry["items"] = kept
            self.store.save_cache(cache)

    def rename_playlist(self, playlist_id: str, name: str) -> None:
        """One PUT, then patch the cached listing so the new name is visible
        without a paid Refresh. Same lock as the refresh path; a refresh
        landing in between still wins, and correctly so."""
        self.request("PUT", f"/playlists/{playlist_id}", json={"name": name})
        with _LIST_LOCK:
            cache = self.store.cache()
            entry = cache.get("playlist_list")
            if not entry or entry.get("items") is None:
                return
            for p in entry["items"]:
                if p["id"] == playlist_id:
                    p["name"] = name
                    break
            self.store.save_cache(cache)

    def _fetch_my_playlists(self) -> list[dict]:
        me = (self.store.cache().get("me") or {}).get("id")
        out = []
        for p in self._paginate("/me/playlists", {"limit": 50}):
            # Spotify returns partial (or null) objects for some algorithmic
            # playlists — every field except id is optional here.
            if not p or not p.get("id"):
                continue
            images = p.get("images") or []
            owner = (p.get("owner") or {}).get("id")
            meta = p.get("items") or p.get("tracks") or {}
            out.append(
                {
                    "id": p["id"],
                    "name": p.get("name") or "(untitled)",
                    "owner": owner,
                    "editable": owner == me or p.get("collaborative", False),
                    "total": meta.get("total"),
                    "snapshot_id": p.get("snapshot_id"),
                    "image": images[-1].get("url") if images else None,
                    # Kept so a sitting playlist can be recognised from the
                    # cached listing alone, at zero calls. It arrives in this
                    # same response either way; discarding it was what forced
                    # sitting cleanup to trust splits.json instead of the
                    # account. `or ""` because Spotify sends null here.
                    "description": p.get("description") or "",
                }
            )
        return out

    @staticmethod
    def _slim_track(item: dict) -> dict | None:
        # Playlist entries use "item" since Feb 2026; /me/tracks still says "track".
        t = item.get("item") or item.get("track")
        if not t:
            return None
        return {
            "uri": t.get("uri"),
            "id": t.get("id"),
            "name": t.get("name"),
            "type": t.get("type", "track"),
            "is_local": item.get("is_local", False),
            "duration_ms": t.get("duration_ms"),
            "artists": [
                {"id": a.get("id"), "name": a.get("name")} for a in t.get("artists", [])
            ],
            "album": (t.get("album") or {}).get("name"),
            "image": ((t.get("album") or {}).get("images") or [{}])[-1].get("url"),
            "added_at": item.get("added_at"),
        }

    def playlist_tracks(self, playlist_id: str) -> list[dict]:
        if playlist_id == LIKED_ID:
            items = list(self._paginate("/me/tracks", {"limit": 50}))
        else:
            try:
                items = list(
                    self._paginate(
                        f"/playlists/{playlist_id}/items",
                        {"limit": 100, "fields": ITEM_FIELDS},
                    )
                )
            except SpotifyError as e:
                # If the fields filter is rejected, refetch unfiltered.
                if e.status != 400:
                    raise
                items = list(self._paginate(f"/playlists/{playlist_id}/items", {"limit": 100}))
        return [t for t in (self._slim_track(i) for i in items) if t and t["uri"]]

    # ---- playback control -------------------------------------------------

    def skip_next(self) -> None:
        self.request("POST", "/me/player/next")

    def pause_playback(self) -> None:
        self.request("PUT", "/me/player/pause")

    def resume_playback(self) -> None:
        # No body: resume where playback stopped. A context_uri here would
        # restart the playlist from the top instead (that's play_context).
        self.request("PUT", "/me/player/play")

    def play_context(self, playlist_id: str) -> None:
        """Start a playlist from the top, leaving shuffle as the user set it."""
        self.request(
            "PUT", "/me/player/play", json={"context_uri": f"spotify:playlist:{playlist_id}"}
        )

    def currently_playing(self) -> dict | None:
        """The user's playing track, or None. Includes the playlist context
        when they're listening to a playlist."""
        data = self.request("GET", "/me/player/currently-playing")
        if not data or not data.get("item"):
            return None
        t = data["item"]
        ctx = data.get("context") or {}
        ctx_uri = ctx.get("uri") or ""
        return {
            "track": {
                "uri": t.get("uri"),
                "id": t.get("id"),
                "name": t.get("name"),
                "type": t.get("type", "track"),
                "is_local": t.get("is_local", False),
                "duration_ms": t.get("duration_ms"),
                "artists": [{"id": a.get("id"), "name": a.get("name")} for a in t.get("artists", [])],
                "album": (t.get("album") or {}).get("name"),
                # Largest-first list; the middle entry (~300px) matches what
                # the now card actually displays — [-1] is the 64px thumbnail,
                # which upscaled to card size as a blur.
                "image": (lambda imgs: imgs[(len(imgs) - 1) // 2].get("url"))(
                    (t.get("album") or {}).get("images") or [{}]),
            },
            "is_playing": data.get("is_playing", False),
            "progress_ms": data.get("progress_ms"),
            "context_playlist_id": ctx_uri.rsplit(":", 1)[-1] if ctx.get("type") == "playlist" else None,
        }

    # ---- mutations --------------------------------------------------------

    def add_to_playlist(self, playlist_id: str, uri: str, bulk: bool = False) -> str | None:
        resp = self.request("POST", f"/playlists/{playlist_id}/items",
                            json={"uris": [uri]}, bulk=bulk)
        return (resp or {}).get("snapshot_id")

    def remove_from_playlist(self, playlist_id: str, uri: str) -> str | None:
        if playlist_id == LIKED_ID:
            # Feb 2026: /me/tracks mutations became the URI-based /me/library.
            self.request("DELETE", "/me/library", json={"uris": [uri]})
            return None
        resp = self.request(
            "DELETE", f"/playlists/{playlist_id}/items", json={"items": [{"uri": uri}]}
        )
        return (resp or {}).get("snapshot_id")

    def save_to_liked(self, uri: str) -> None:
        self.request("PUT", "/me/library", json={"uris": [uri]})

    def create_playlist(self, name: str, description: str = "", bulk: bool = False) -> str:
        """Create a playlist and return its id. One call."""
        resp = self.request(
            "POST", "/me/playlists",
            json={"name": name, "description": description, "public": False},
            bulk=bulk,
        )
        playlist_id = (resp or {}).get("id")
        if not playlist_id:
            # A 200/201 with no id would otherwise flow straight into
            # add_to_playlist as f"/playlists/{None}/items" — a confusing
            # 404 far from the actual problem. The call already happened and
            # spent budget either way; fail loudly at the source instead.
            raise SpotifyError(502, "playlist creation returned no id")
        return playlist_id

    def unfollow_playlist(self, playlist_id: str) -> None:
        """Discard a whole playlist in one call.

        This is why a sitting is disposable: clearing a 22-track playlist
        track-by-track would cost 22 calls, since the Feb-2026 API has no
        batch delete.
        """
        self.request("DELETE", f"/playlists/{playlist_id}/followers")
