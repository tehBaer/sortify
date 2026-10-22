"""FastAPI app: serves the web UI and orchestrates triage."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import posixpath
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import filing
from . import foldermove
from . import gate
from . import quickadds
from . import rootlist
from . import suggest as sugg
from . import splitparts as spp
from . import splitparts_api as spa
from . import tabletshare
from .deezer import Deezer
from . import inputsets
from . import playlist_roles as roles
from .skip_ledger import Observation, SkipDetector, SkipLedger
from .folders import (
    creatable_home_name_problem,
    extract_folder_map,
    folder_paths,
    home_name_excluded,
    select_home_ids,
)
from .naming import violations as naming_violations
from .spotify import (
    BACKGROUND_DAILY_CAP,
    BULK_RESERVE,
    DAILY_CAP,
    LIKED_ID,
    REDIRECT_URI,
    REMOVE_BATCH,
    AuthFlowError,
    AuthNeeded,
    Spotify,
    SpotifyError,
    _LIST_LOCK,
    build_auth_url,
    pkce_pair,
)
from .store import TAGS_VERSION, Store
from .tags import LastFm, LastFmError, enrich, fetch_track, load_key, track_key

LIKED_TTL = 120  # seconds; Liked Songs has no snapshot_id to validate against

# uvicorn owns the handlers; borrowing its logger is what puts these lines in
# the journal next to the access log.
log = logging.getLogger("uvicorn.error")

app = FastAPI(title="sortify")
store = Store()
sp = Spotify(store)
undo_stack: list[dict] = []


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
# Two separate counters: _exchange_counter protects the token endpoint
# itself (taken before exchange_code, so garbage codes spend only this),
# _gate_counter protects the account check (taken only after a successful
# exchange, right before whoami) — a bogus code must never be able to
# spend the owner's own daily sign-in allowance.
_gate_counter = gate.DailyCounter()
_exchange_counter = gate.DailyCounter(cap=gate.DAILY_EXCHANGE_CAP)


def _gate_secret() -> str | None:
    # Read per request, not at import: tests set it, and a missing one must
    # close the gate the moment it is gone.
    return os.environ.get("SORTIFY_GATE_SECRET")


def _arrived_public(scope: dict) -> bool:
    server = scope.get("server") or (None, None)
    return bool(PUBLIC_PORT) and server[1] == PUBLIC_PORT


class AuthStart(BaseModel):
    client_id: str


class ConfigIn(BaseModel):
    input_ids: list[str]
    home_ids: list[str] = []
    # {playlist_id: "ambient, piano"} — the user's own matching hints per
    # home, free text split on commas at profile-build time.
    home_hints: dict[str, str] = {}


class CreatePlaylistIn(BaseModel):
    name: str
    # "home", "subset" or "input". Explicit rather than implied because the
    # roles differ in what gets marked and in which name rules apply. An
    # input is defined by its NAME matching a set's pattern (or by the folder
    # it is created in, for a folder-defined set), so creating one means
    # honouring that rule rather than writing the role alone.
    role: str = "home"
    # Where to file it. An existing folder path, or None for the top level.
    # OMITTING the field is the third case: it means "the default for this
    # role" (config's `create_folders`), which is what the UI's dropdown
    # shows pre-selected. `model_fields_set` is what tells the three apart.
    folder: str | None = None


class PlayIn(BaseModel):
    input_id: str


class SeekIn(BaseModel):
    position_ms: int


class RepeatIn(BaseModel):
    state: str  # "off" | "context" | "track"


class ActIn(BaseModel):
    action: str  # "move" | "remove"
    uri: str
    from_id: str | None = None  # None = just add to the home, no removal
    to_id: str | None = None
    # Opt-in: also empty every OTHER input holding this uri (see `act`). Off by
    # default so the split decide path and anything else filing through here
    # keeps its old, single-playlist shape.
    sweep_inputs: bool = False


@app.exception_handler(AuthNeeded)
def _auth_needed(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=401, content={"detail": "auth needed", "needs_auth": True})


@app.exception_handler(SpotifyError)
def _spotify_error(request, exc: SpotifyError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=502, content={"detail": str(exc)})


# ---- auth & status ---------------------------------------------------------


@app.get("/api/status")
def status():
    cfg = store.config()
    now = time.time()
    return {
        "has_client_id": bool(cfg.get("client_id")),
        "authed": sp.authed(),
        "me": store.cache().get("me"),
        "input_ids": cfg.get("input_ids", []),
        "home_ids": cfg.get("home_ids", []),
        "budget": {
            "spent_today": sp.budget_spent(),
            "cap": DAILY_CAP,
            "background_spent": sp.background_spent(),
            "background_cap": BACKGROUND_DAILY_CAP,
            "bulk": {"spent_today": sp.bulk_spent(), "reserve": BULK_RESERVE},
        },
        "cooldown_min_left": max(0, int((sp.effective_cooldown_until() - now) / 60)),
        # Cooldown is over but proactive work is still holding its breath.
        "quiet_min_left": max(0, int((sp.quiet_until() - now) / 60)),
    }


# What a Spotify Client ID looks like. Typos get caught here, before the
# user is bounced to accounts.spotify.com and back for an opaque error.
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9]{32}$")


def _auth_page(status: int, msg: str) -> HTMLResponse:
    """Full-page notice for the callback route — it renders in a bare browser
    tab mid-redirect, so there is no app shell to toast into."""
    body = (
        "<!doctype html><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>sortify</title>"
        '<body style="font-family:system-ui,sans-serif;background:#121212;color:#ededed;'
        'max-width:26rem;margin:0 auto;padding:2rem;text-align:center">'
        f'<h2 style="font-size:1.15rem;font-weight:500;line-height:1.4">{msg}</h2>'
        '<p style="margin-top:1.75rem"><a style="color:#08210f;background:#1db954;'
        "text-decoration:none;padding:.65rem 1.3rem;border-radius:999px;font-weight:500;"
        'display:inline-block" href="/">Back to sortify</a></p></body>'
    )
    return HTMLResponse(body, status_code=status)


@app.get("/api/auth/redirect-uri")
def auth_redirect_uri():
    # The setup wizard shows this so the user can paste it into the Spotify
    # dashboard app's Redirect URIs — it must byte-match what start_auth sends.
    return {"redirect_uri": REDIRECT_URI}


@app.post("/api/auth/start")
def auth_start(body: AuthStart):
    # Re-authorising to pick up a new scope is now the common case, and the
    # client ID is already on file — only first-time setup should have to go
    # and fetch it. Falling back also stops a blank field from overwriting the
    # stored id with "".
    typed = body.client_id.strip()
    if typed and not CLIENT_ID_RE.match(typed):
        raise HTTPException(400, "that doesn't look like a Client ID — it's 32 letters and numbers")
    client_id = typed or (store.config().get("client_id") or "")
    if not client_id:
        raise HTTPException(400, "paste the Client ID from your Spotify dashboard app")
    return {"auth_url": sp.start_auth(client_id)}


@app.get("/auth/callback")
def auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error == "access_denied":
        return _auth_page(400, "You cancelled the Spotify connection.")
    if error:
        return _auth_page(
            400,
            f"Spotify returned an error: {html.escape(error)}. If it mentions the redirect URI, "
            f"paste this exact value into your dashboard app's settings: {html.escape(REDIRECT_URI)}",
        )
    if not code or not state:
        return _auth_page(400, "That sign-in link was incomplete. Please start again.")
    try:
        me = sp.finish_auth(code, state)
    except AuthFlowError:
        return _auth_page(400, "That sign-in expired or didn't match. Please start again.")
    except SpotifyError as e:
        log.warning("auth callback: exchange failed: %s", e)
        return _auth_page(502, "Couldn't complete the Spotify sign-in. Please try again.")
    log.info("auth callback: connected as %s", me.get("name"))
    return RedirectResponse("/", status_code=303)


# ---- the public gate -------------------------------------------------------

# An allow-list, not a block-list: any method outside this set (not just
# the common write verbs) needs a matching Origin on the public port.
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _is_raw_index(path: str) -> bool:
    """True for every spelling of /static/index.html that StaticFiles (no
    html=True) would still resolve to that file: doubled slashes, a "."
    or ".." segment, a trailing slash. An exact string match on the raw
    path missed all of these, so this normalizes the same way StaticFiles
    does before comparing — collapse empty segments (handles doubled and
    trailing slashes), then posixpath.normpath (resolves "." and "..")."""
    collapsed = "/" + "/".join(p for p in path.split("/") if p)
    return posixpath.normpath(collapsed) == "/static/index.html"


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
    # scope["path"] is the decoded path the router and StaticFiles route on.
    # request.url.path is NOT the same thing: Starlette's Request.url is
    # built by re-parsing a reassembled URL string, so a decoded '#' or '?'
    # in the path is read back as a fragment/query separator and truncates
    # it there. Matching on the truncated value let a path with an encoded
    # '#'/'?' read as an open path (or not-the-raw-index) while StaticFiles
    # went on to resolve the untruncated path to a different file.
    path = request.scope["path"]
    if gate.is_open_path(path):
        return await call_next(request)
    secret = _gate_secret()
    value = request.cookies.get(gate.SESSION_COOKIE)
    owner = store.config().get("gate_owner_id")
    signed_in = gate.session_uid(secret, value, owner)
    if _is_raw_index(path) and signed_in:
        # StaticFiles would serve the raw file, rc-button.js line and all —
        # the strip in _index_response only runs for "/". An unauthenticated
        # request falls through to the ordinary gate check below instead,
        # so this never tells a stranger which path spellings resolve to it.
        return RedirectResponse("/", status_code=302)
    if not signed_in:
        # fetch() follows redirects silently, so an API call (or any write)
        # gets a 401 that app.js turns into a trip to /gate/start.
        if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "sign in with Spotify", "gate": True}, status_code=401)
        return RedirectResponse("/gate/start", status_code=302)
    # SameSite=Lax already keeps the cookie off cross-site writes; this is
    # the second layer. Browsers send Origin on every POST, same-origin too.
    if request.method not in _SAFE_METHODS and request.headers.get("origin") != PUBLIC_ORIGIN:
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
        build_auth_url(client_id, challenge, state, redirect_uri=GATE_REDIRECT_URI, scope="",
                       show_dialog=True),
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
    today = time.strftime("%Y-%m-%d")
    if not _exchange_counter.take(today):
        return _auth_page(429, "Too many sign-ins today. Try again tomorrow, or use the tailnet address.")
    try:
        tokens = sp.exchange_code(code, verifier, store.config()["client_id"],
                                  redirect_uri=GATE_REDIRECT_URI)
        if not _gate_counter.take(today):
            return _auth_page(429,
                              "Too many sign-ins today. Try again tomorrow, or use the tailnet address.")
        uid = sp.whoami(tokens["access_token"])
    except (SpotifyError, httpx.HTTPError, KeyError) as e:
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


# ---- playlists & config ----------------------------------------------------


@app.get("/api/playlists")
def playlists():
    cfg = store.config()
    folders = store.folders()
    items = sp.my_playlists()
    liked = {"id": LIKED_ID, "name": "Liked Songs", "owner": "you", "editable": False,
             "total": None, "image": None}
    out = [liked] + items
    inputs = _effective_input_ids(cfg, items)
    _sets = inputsets.resolve_sets(cfg)
    hints = cfg.get("home_hints") or {}
    for p in out:
        p["folder"] = (folders.get(p["id"]) or {}).get("path")
        role = roles.role_of(
            p["name"], is_input=p["id"] in inputs,
            is_home=p["id"] in cfg.get("home_ids", []),
            editable=bool(p.get("editable")) and p["id"] != LIKED_ID)
        p["archived"] = role == "archived"
        p["role"] = None if role == "archived" else role
        # The Subset chip renames: the paw on, the leading emoji off. Only on
        # our own playlists, and never on a home or input — those roles win
        # anyway. An archived list keeps it: marking takes the 🗄️ off, and
        # that chip is the only way back from the archive on the phone.
        p["subset_eligible"] = (bool(p.get("editable")) and p["id"] != LIKED_ID
                                and role in (None, "subset", "archived"))
        p["input_set"] = (
            inputsets.set_of(p["name"], p.get("folder"), _sets) or inputsets.DEFAULT_KEY
        ) if p["role"] == "input" else None
        p["hints"] = hints.get(p["id"], "")
    entry = store.cache().get("playlist_list") or {}
    return {"playlists": out, "fetched_at": entry.get("fetched_at"),
            # The create row's folder dropdown and its pre-selection. Both
            # ride along here rather than on an endpoint of their own: the
            # view that creates playlists is the view that loads this.
            "folder_paths": folder_paths(folders),
            "create_folders": cfg.get("create_folders") or {}}


@app.post("/api/folders")
def ingest_folders(tree: Any = Body(...)):
    """Ingest the spotify-folders JSON from the desktop client.

    Homes are re-marked from the tree: playlists under the configured
    `home_folder_prefixes` (minus `home_folder_exclude` segments and inputs).
    With no prefixes configured, falls back to ALL-CAPS folders.
    """
    mapping = extract_folder_map(tree)
    if not mapping:
        raise HTTPException(400, "no playlists found in that JSON — is it spotify-folders output?")
    return _apply_folder_mapping(mapping)


def _apply_folder_mapping(mapping: dict) -> dict:
    """Store a folder mapping and re-mark homes from it.

    Shared tail of `POST /api/folders` (tree pasted from another machine)
    and `POST /api/folders/refresh` (tree extracted from this box's client
    cache). Reads the cached playlist listing — zero Spotify calls.
    """
    store.save_folders(mapping)
    cfg = store.config()
    all_playlists = sp.my_playlists()
    inputs = _effective_input_ids(cfg, all_playlists)
    editable = {p["id"] for p in all_playlists if p["editable"]}
    prefixes = cfg.get("home_folder_prefixes") or []
    if prefixes:
        chosen = select_home_ids(mapping, prefixes, cfg.get("home_folder_exclude") or [])
        rule = f"under {', '.join(prefixes)}"
    else:
        chosen = {pid for pid, info in mapping.items() if info["caps"]}
        rule = "ALL-CAPS folders"
    patterns = cfg.get("home_name_exclude_patterns") or []
    emoji = bool(cfg.get("home_exclude_emoji_names"))
    if patterns or emoji:
        name_by_id = {p["id"]: p["name"] for p in all_playlists}
        chosen = {
            pid for pid in chosen
            if not home_name_excluded(name_by_id.get(pid, ""), patterns, emoji)
        }
        rule += ", minus marker/derived names"
    home_ids = sorted((chosen & editable) - inputs)
    # Homes created inside sortify have no folder path yet, so the tree
    # cannot see them. Union them back in (through the same editable/input
    # filters) — the tree keeps authority over playlists it can see, it just
    # stops deleting knowledge it never had. (Spec §2.)
    sticky = {s for s in (cfg.get("sticky_home_ids") or [])
              if s in editable and s not in inputs}
    home_ids = sorted(set(home_ids) | sticky)
    store.update_config(home_ids=home_ids)
    return {
        "playlists_in_folders": len(mapping),
        "homes_marked": len(home_ids),
        "rule": rule,
        "home_folders": sorted({mapping[pid]["path"] for pid in home_ids if pid in mapping}),
    }


# One refresh at a time: the sync step runs the desktop client for
# ~SYNC_SECONDS, and two concurrent clients fighting over one cache would
# help nobody. Non-blocking so the second click gets a clear answer.
_folders_refresh_lock = threading.Lock()


@app.post("/api/folders/refresh")
def refresh_folders(body: dict = Body(default={})):
    """Re-import the folder tree from this box's own Spotify client.

    Runs the client headless so its cache catches up (skippable with
    {"sync": false}), extracts the rootlist, and applies it through the
    same path as POST /api/folders. Zero Web API calls — the client sync
    is Spotify's desktop protocol, outside the dev-mode quota.
    """
    if not _folders_refresh_lock.acquire(blocking=False):
        raise HTTPException(409, "a folder refresh is already running")
    try:
        before = store.folders()
        if body.get("sync", True):
            rootlist.sync_client()
        try:
            tree = rootlist.extract_tree()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        mapping = extract_folder_map(tree)
        if not mapping:
            raise HTTPException(503, "the client cache produced an empty tree — not applied")
        result = _apply_folder_mapping(mapping)
        result["tree_as_of"] = rootlist.cache_mtime()
        result["added"] = sum(1 for k in mapping if k not in before)
        result["moved"] = sum(
            1 for k in mapping if k in before and mapping[k]["path"] != before[k]["path"])
        result["dropped"] = sum(1 for k in before if k not in mapping)
        return result
    finally:
        _folders_refresh_lock.release()


@app.get("/api/naming")
def naming():
    """Naming-convention violations among marked playlists. Reads the cached
    listing (free once the listing has been fetched), so it can run on every
    Playlists-view open."""
    cfg = store.config()
    items = sp.my_playlists()
    inputs = _effective_input_ids(cfg, items)
    return {"violations": naming_violations(
        items, inputs, set(cfg.get("home_ids") or []),
        cfg.get("input_name_pattern"),
        sets=inputsets.resolve_sets(cfg), folders=store.folders(),
    )}


@app.post("/api/naming/{playlist_id}/rename")
def apply_naming_rename(playlist_id: str):
    """Apply one approved rename — exactly one Spotify call.

    The proposal is recomputed here from the cached listing rather than
    trusted from the client: a stale tab posting an old violation gets a
    409, not a rename based on a name that no longer exists.
    """
    cfg = store.config()
    items = sp.my_playlists()
    inputs = _effective_input_ids(cfg, items)
    rows = naming_violations(items, inputs, set(cfg.get("home_ids") or []),
                             cfg.get("input_name_pattern"),
                             sets=inputsets.resolve_sets(cfg), folders=store.folders())
    row = next((r for r in rows if r["playlist_id"] == playlist_id), None)
    if row is None:
        raise HTTPException(
            409, "that playlist has no naming issue any more — the list was "
                 "stale. Reopen the Playlists view to see the current state.")
    sp.rename_playlist(playlist_id, row["proposed"])
    return {"renamed": {"playlist_id": playlist_id,
                        "from": row["current"], "to": row["proposed"]}}


class SubsetMarkIn(BaseModel):
    on: bool


@app.post("/api/playlists/{playlist_id}/subset")
def mark_subset(playlist_id: str, body: SubsetMarkIn):
    """Make a playlist a subset, or stop it being one — by renaming it.

    A subset is its name (an emoji first, 🗄️ excepted; playlist_roles.py), so
    marking puts 🐾 on and unmarking takes the leading emoji off. One Spotify
    call, none when the name already says so. Reads the cached listing only.
    Unmarking only ever renames a subset name: a plain or archived name is
    returned unchanged, so "unmark" can never silently un-archive a list.
    """
    cfg = store.config()
    listing = (store.cache().get("playlist_list") or {}).get("items") or []
    p = next((x for x in listing if x["id"] == playlist_id), None)
    if p is None:
        raise HTTPException(404, "that playlist is not in the cached listing — Refresh first")
    if not p.get("editable"):
        raise HTTPException(400, "not yours to rename, so it cannot be marked a subset")
    inputs = _effective_input_ids(cfg, listing)
    # An archived name is no role at all, so an id left in home_ids under a
    # 🗄️ name is not a home here — the chip that marks it is the way back.
    in_home_ids = playlist_id in (cfg.get("home_ids") or [])
    is_home = in_home_ids and not roles.is_archived(p["name"])
    if body.on and (playlist_id in inputs or is_home):
        raise HTTPException(
            409, f"{p['name']!r} is {'an input' if playlist_id in inputs else 'a home'} — "
                 "that role wins over subset, so a 🐾 would change nothing")
    if body.on:
        new = roles.mark_subset_name(p["name"])
    elif roles.is_subset_name(p["name"]):
        new = roles.unmark_subset_name(p["name"])
    else:
        new = p["name"]
    if not new.strip():
        raise HTTPException(400, f"{p['name']!r} is only an emoji — removing it would leave no name")
    if new != p["name"]:
        sp.rename_playlist(playlist_id, new)
    # The role of the NEW name: for a pattern set the name is the membership,
    # so unmarking "🐾 [x]" makes "[x]" an input the old name was not.
    is_input = (playlist_id in (cfg.get("input_ids") or []) or bool(inputsets.set_of(
        new, (store.folders().get(playlist_id) or {}).get("path"), inputsets.resolve_sets(cfg))))
    role = roles.role_of(new, is_input=is_input, is_home=in_home_ids, editable=True)
    return {"playlist_id": playlist_id, "name": new, "role": None if role == "archived" else role}


# ---- split into parts ------------------------------------------------------
#
# Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md. Before
# Done, the only Spotify calls are reads: the snapshot (1) and the track
# list when that moved (~1 per 100). Suggest runs on a thread because the
# Last.fm fill-in for never-seen songs is paced at ~4 requests a second.

_splitparts_job: dict = {}
_splitparts_job_lock = threading.Lock()
# Tests flip this to run jobs inline.
_splitparts_threaded = True


class SplitSuggestIn(BaseModel):
    n: int


class SplitMoveIn(BaseModel):
    uri: str
    to: str


class SplitRemoveIn(BaseModel):
    dry: bool = False
    # A part key: every song goes there, instead of each to its best fit.
    to: str | None = None


_SPLITPARTS_DONE_STARTED_MSG = "Done has already started for this split — press Done again to finish it"
_SPLITPARTS_CHANGED_MSG = ("the playlist changed in Spotify since the suggestion — "
                           "re-read it to continue")


def _splitparts_home(pid: str) -> dict:
    cfg = store.config()
    items = sp.my_playlists()
    homes = _resolve_homes(cfg, items, "", _effective_input_ids(cfg, items))
    for p in homes:
        if p["id"] == pid:
            return p
    raise HTTPException(404, "not one of your homes")


@app.get("/api/splitparts/homes")
def splitparts_homes():
    cfg = store.config()
    items = sp.my_playlists()
    homes = _resolve_homes(cfg, items, "", _effective_input_ids(cfg, items))
    drafts = store.splitparts()["drafts"]
    folders = store.folders()
    out = []
    # A split in progress first: it is the one you came back for.
    for p in sorted(homes, key=lambda p: (p["id"] not in drafts, (p.get("name") or "").lower())):
        total = p.get("total") or 0
        reason = None
        if total < spp.MIN_TRACKS:
            reason = f"too small to split — fewer than {spp.MIN_TRACKS} songs"
        elif total > spp.MAX_TRACKS:
            reason = f"too big to split here — more than {spp.MAX_TRACKS} songs"
        out.append({"id": p["id"], "name": p.get("name") or "", "total": total,
                    "folder": (folders.get(p["id"]) or {}).get("path"),
                    "splittable": reason is None, "reason": reason,
                    "draft": p["id"] in drafts})
    return {"homes": out}


@app.get("/api/splitparts/progress")
def splitparts_last_job():
    """The job slot without knowing its pid: which home it was for, what
    kind of job, and how it ended. Registered before the `{pid}` routes so
    "progress" is never read as a playlist id. The Split tab asks this on
    entry, because a finished subset-mode Done's original is no longer a
    home — this is the only way back to its summary."""
    with _splitparts_job_lock:
        job = dict(_splitparts_job)
    if not job:
        return {"pid": None, "kind": None, "state": "idle", "summary": None}
    return {k: job.get(k) for k in ("pid", "kind", "state", "summary")}


# A split sits "running" (409-ing every other Suggest) for as long as this
# fill takes; on a genuine Last.fm outage each call pays its own 15s timeout,
# so without a breaker a single bad patch of network turns one Suggest into
# hours. One counter spans the track and artist-similar phases below — a
# success anywhere resets it — so a string of failures straddling the two
# phases still trips it exactly once, not twice as long.
FILL_FAILURE_LIMIT = 10


def _splitparts_fill(tracks: list[dict], progress) -> None:
    """Fetch Last.fm data for never-seen songs; never raises. A missing key
    or a Last.fm failure just leaves those songs without data ("?")."""
    fm = _lastfm_client()
    if fm is None:
        return
    need_tags, need_tracks, need_similar = spa.missing_lastfm(
        tracks, store.tag_artists(), store.lastfm_track_map(), store.lastfm_artist_map())
    total = len(need_tags) + len(need_tracks) + len(need_similar)
    done = 0
    progress(phase="looking up songs on Last.fm", done=0, total=total)
    if need_tags:
        cached = store.tag_artists()
        try:
            merged = enrich(need_tags, cached, fm, _now_iso(),
                            on_progress=lambda d, t: progress(done=d))
        except LastFmError as exc:
            log.warning("split: tag fetch stopped early: %s", exc)
            merged = exc.partial if exc.partial is not None else cached
            if len(merged) > len(cached):
                _merge_save_tag_artists(merged)
            # The tag fetch itself failed — Last.fm is failing outright, not
            # just missing this one artist, so the track and artist-similar
            # phases would just pay the same timeout per song for nothing.
            skipped = len(need_tracks) + len(need_similar)
            if skipped:
                log.warning("split: Last.fm is failing — skipping %d further lookups", skipped)
            progress(done=total)
            return
        if len(merged) > len(cached):
            _merge_save_tag_artists(merged)
        done = len(need_tags)
    failures = 0
    for artist, title in need_tracks:
        if failures >= FILL_FAILURE_LIMIT:
            log.warning("split: stopping after %d consecutive Last.fm failures, "
                        "%d lookups left undone", FILL_FAILURE_LIMIT, total - done)
            progress(done=total)
            return
        try:
            record = fetch_track(fm, artist, title, time.time())
            _merge_save_lastfm_tracks({track_key(artist, title): record})
            failures = 0
        except Exception:
            log.warning("split: track record failed for %r — %r", artist, title, exc_info=True)
            failures += 1
        done += 1
        progress(done=done)
    for aid, name in need_similar:
        if failures >= FILL_FAILURE_LIMIT:
            log.warning("split: stopping after %d consecutive Last.fm failures, "
                        "%d lookups left undone", FILL_FAILURE_LIMIT, total - done)
            progress(done=total)
            return
        try:
            similar = fm.artist_similar(name)
            _merge_save_lastfm_artists({aid: {"name": name, "similar": similar or [],
                                              "fetched_at": _now_iso(), "miss": similar is None}})
            failures = 0
        except Exception:
            log.warning("split: artist-similar failed for %r", name, exc_info=True)
            failures += 1
        done += 1
        progress(done=done)


def _sync_listing_snapshot(pid: str, snapshot_id: str | None, total: int | None = None) -> None:
    """Patch the cached playlist LISTING's own snapshot_id (and total, when
    given) to match a fresher read taken elsewhere — e.g. Suggest's own
    `sp.snapshot` call. The profile rebuild's staleness check keys off the
    listing (`cache["playlist_list"]`), not the per-playlist track cache
    `_cached_tracks` updates — leaving the listing behind made every later
    read see a mismatch and re-fetch, and the next Suggest would mismatch
    again the same way. Same lock as `Spotify.rename_playlist`; a refresh
    landing in between still wins, and correctly so. No-op when there is no
    snapshot_id to record, or the playlist is not (yet) in the listing.
    """
    if snapshot_id is None:
        return
    with _LIST_LOCK:
        cache = store.cache()
        entry = cache.get("playlist_list")
        if not entry or entry.get("items") is None:
            return
        found = False
        for p in entry["items"]:
            if p.get("id") == pid:
                p["snapshot_id"] = snapshot_id
                if total is not None:
                    p["total"] = total
                found = True
                break
        if found:
            store.save_cache(cache)


def _splitparts_run_suggest(playlist: dict, n: int) -> None:
    pid = playlist["id"]

    def progress(**fields):
        with _splitparts_job_lock:
            _splitparts_job.update(fields)

    try:
        progress(phase="reading the playlist", done=0, total=0)
        snap = sp.snapshot(pid)
        tracks = _cached_tracks(pid, snap)
        _sync_listing_snapshot(pid, snap, total=len(tracks))
        _splitparts_fill(tracks, progress)
        progress(phase="grouping", done=0, total=0)
        sugg = spp.suggest(tracks, n, store.tag_artists(), store.lastfm_track_map(),
                           store.lastfm_artist_map())
        spa.put_draft(store, spa.draft_from(playlist, snap, tracks, sugg, n))
        progress(state="done", phase="done", message=None)
    except spp.SplitError as exc:
        progress(state="error", message=str(exc))
    except SpotifyError as exc:
        progress(state="error", message=str(exc))
    except Exception as exc:  # never leave the job "running" forever
        log.exception("split: suggest failed")
        progress(state="error", message=f"suggest failed: {exc}")


@app.post("/api/splitparts/{pid}/suggest")
def splitparts_suggest(pid: str, body: SplitSuggestIn):
    if not spp.MIN_PARTS <= body.n <= spp.MAX_PARTS:
        raise HTTPException(400, f"choose between {spp.MIN_PARTS} and {spp.MAX_PARTS} parts")
    playlist = _splitparts_home(pid)
    existing = spa.get_draft(store, pid)
    if existing is not None and existing.get("done") is not None:
        raise HTTPException(
            409, "Done has already started for this split — press Done again to finish it")
    with _splitparts_job_lock:
        if _splitparts_job.get("state") == "running":
            raise HTTPException(409, "already running")
        _splitparts_job.clear()
        _splitparts_job.update({"pid": pid, "state": "running", "phase": "starting", "kind": "suggest",
                                "done": 0, "total": 0, "message": None})
    if _splitparts_threaded:
        threading.Thread(target=_splitparts_run_suggest, args=(playlist, body.n),
                         daemon=True).start()
    else:
        _splitparts_run_suggest(playlist, body.n)
    return {"started": True}


@app.get("/api/splitparts/{pid}/progress")
def splitparts_progress(pid: str):
    with _splitparts_job_lock:
        job = dict(_splitparts_job)
    if job.get("pid") != pid:
        return {"state": "idle", "phase": "", "done": 0, "total": 0, "message": None,
                "summary": None, "kind": None}
    return {k: job.get(k) for k in ("state", "phase", "done", "total", "message", "summary", "kind")}


@app.get("/api/splitparts/{pid}")
def splitparts_get(pid: str):
    draft = spa.get_draft(store, pid)
    if draft is None:
        raise HTTPException(404, "no split in progress for this playlist")
    return spa.without_tracks(draft)


@app.delete("/api/splitparts/{pid}")
def splitparts_discard(pid: str):
    draft = spa.get_draft(store, pid)
    if draft is not None and draft.get("done") is not None:
        raise HTTPException(409, _SPLITPARTS_DONE_STARTED_MSG)
    spa.drop_draft(store, pid)
    return {"ok": True}


@app.post("/api/splitparts/{pid}/abandon")
def splitparts_abandon(pid: str):
    """The way out of a started Done that keeps failing (a created part
    deleted in Spotify, the original unfollowed, a refused batch): every
    other action 409s once Done has started. Zero Spotify calls — the draft
    goes, and what was already made stays as it is and is reported back."""
    with _splitparts_job_lock:
        running = _splitparts_job.get("state") == "running" and _splitparts_job.get("pid") == pid
    with spa._lock:
        draft = spa.get_draft(store, pid)
        if draft is None:
            raise HTTPException(404, "no split in progress for this playlist")
        if draft.get("done") is None:
            raise HTTPException(409, "Done has not started for this split — discard it instead")
        if running:
            raise HTTPException(409, "Done is running for this split — wait for it to stop")
        report = spa.abandon_report(draft)
        spa.drop_draft(store, pid)
    return report


def _splitparts_draft(pid: str) -> dict:
    draft = spa.get_draft(store, pid)
    if draft is None:
        raise HTTPException(404, "no split in progress for this playlist")
    return draft


@app.post("/api/splitparts/{pid}/move")
def splitparts_move(pid: str, body: SplitMoveIn):
    with spa._lock:
        draft = _splitparts_draft(pid)
        if draft.get("done") is not None:
            raise HTTPException(409, _SPLITPARTS_DONE_STARTED_MSG)
        try:
            spa.move(draft, body.uri, body.to)
        except KeyError as exc:
            raise HTTPException(400, exc.args[0])
        spa.put_draft(store, draft)
    return spa.without_tracks(draft)


@app.post("/api/splitparts/{pid}/parts")
def splitparts_add_part(pid: str):
    with spa._lock:
        draft = _splitparts_draft(pid)
        if draft.get("done") is not None:
            raise HTTPException(409, _SPLITPARTS_DONE_STARTED_MSG)
        try:
            spa.add_part(draft)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        spa.put_draft(store, draft)
    return spa.without_tracks(draft)


@app.post("/api/splitparts/{pid}/parts/{key}/remove")
def splitparts_remove_part(pid: str, key: str, body: SplitRemoveIn):
    with spa._lock:
        draft = _splitparts_draft(pid)
        if draft.get("done") is not None:
            raise HTTPException(409, _SPLITPARTS_DONE_STARTED_MSG)
        if len(draft["parts"]) <= spp.MIN_PARTS:
            raise HTTPException(409, f"at least {spp.MIN_PARTS} parts must remain")
        if body.to is not None:
            if body.to == key or not any(p["key"] == body.to for p in draft["parts"]):
                raise HTTPException(400, "pick another part for its songs")
            leaving = next((p for p in draft["parts"] if p["key"] == key), None)
            if leaving is None:
                raise HTTPException(400, f"no part {key}")
            moves = {u: body.to for u in leaving["uris"]}
        else:
            try:
                moves = spa.removal_moves(draft, key, store.tag_artists(),
                                          store.lastfm_track_map(), store.lastfm_artist_map())
            except KeyError as exc:
                raise HTTPException(400, exc.args[0])
        if body.dry:
            counts: dict[str, int] = {}
            for dest in moves.values():
                counts[dest] = counts.get(dest, 0) + 1
            return {"moves": counts}
        spa.remove_part(draft, key, moves)
        spa.put_draft(store, draft)
    return spa.without_tracks(draft)


class SplitNameIn(BaseModel):
    name: str


@app.post("/api/splitparts/{pid}/parts/{key}/name")
def splitparts_name_part(pid: str, key: str, body: SplitNameIn):
    """Name a part while editing; the name is kept in the draft, so it
    survives leaving and coming back, and Done uses it. Zero calls. Whether
    the name can become a home is Done's check, as for a name typed there."""
    with spa._lock:
        draft = _splitparts_draft(pid)
        if draft.get("done") is not None:
            raise HTTPException(409, _SPLITPARTS_DONE_STARTED_MSG)
        try:
            spa.rename_part(draft, key, body.name)
        except KeyError as exc:
            raise HTTPException(400, exc.args[0])
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        spa.put_draft(store, draft)
    return spa.without_tracks(draft)


class SplitPlayIn(BaseModel):
    uri: str


@app.post("/api/splitparts/{pid}/play")
def splitparts_play(pid: str, body: SplitPlayIn):
    """A hold on a song: play it in Spotify, in the playlist being split, so
    the music carries on through that playlist after it, and (client side)
    open it in Now to be filed. One call; nothing in the draft changes.

    Arms the same settle window as Next: the Now view repolls a moment
    after this, and if Spotify still reports the song from before, that
    answer must get the short settle TTL rather than be cached for the old
    song's whole remaining runtime — or the card would stay on it."""
    with spa._lock:
        draft = _splitparts_draft(pid)
        if not spa.has_song(draft, body.uri):
            raise HTTPException(400, "that song is not in this split")
    with _now_lock:
        prev_uri = ((_now_cache["value"] or {}).get("track") or {}).get("uri")
    out = _playback_call(sp.play_in_context, pid, body.uri)
    # Already that song: nothing to settle, and arming it would only
    # shorten every answer for the next 15 seconds.
    if prev_uri and prev_uri != body.uri:
        with _now_lock:
            _skip_settle.update(uri=prev_uri, until=time.time() + NOW_SKIP_SETTLE_WINDOW)
    return out


def _splitparts_targets(pid: str) -> list[dict]:
    # The cached listing, never sp.my_playlists(): a cold one is ~21 calls.
    listing = (store.cache().get("playlist_list") or {}).get("items") or []
    return spa.move_targets(listing, pid, store.folders(), LIKED_ID,
                            store.config().get("home_ids") or [])


def _splitparts_buffer_targets(pid: str) -> list[dict]:
    """The inputs a song of this split can go back to, from the cached
    listing and config (zero calls) — the playlists the Now card's "Move to
    buffer…" offers, each with its input set."""
    listing = (store.cache().get("playlist_list") or {}).get("items") or []
    cfg = store.config()
    inputs = _effective_input_ids(cfg, listing)
    sets, folders = inputsets.resolve_sets(cfg), store.folders()
    set_keys = {p["id"]: (inputsets.set_of(p.get("name") or "", (folders.get(p["id"]) or {}).get("path"),
                                           sets) or inputsets.DEFAULT_KEY)
                for p in listing if p.get("id") in inputs}
    # Homeless is left out, as the Now card's "Move to buffer…" leaves it
    # out: "no home fits" is its own decision, not an inbox to go back to.
    homeless = cfg.get("homeless_id") or cfg.get("needs_home_id")
    inputs = [i for i in inputs if i != homeless]
    return spa.buffer_targets(listing, pid, inputs, set_keys, LIKED_ID)


@app.get("/api/splitparts/{pid}/targets")
def splitparts_targets(pid: str, kind: str = "home"):
    """The playlists a song of this split can be moved to instead. Zero
    calls. `kind=home` (the magnifier) lists other homes only; `kind=buffer`
    (the tray) lists the inputs, for a song going back to an inbox."""
    if kind == "home":
        return {"targets": _splitparts_targets(pid)}
    if kind == "buffer":
        return {"targets": _splitparts_buffer_targets(pid)}
    raise HTTPException(400, "kind is home or buffer")


class SplitElsewhereIn(BaseModel):
    uri: str
    to_id: str


@app.post("/api/splitparts/{pid}/elsewhere")
def splitparts_elsewhere(pid: str, body: SplitElsewhereIn):
    """Move one song out of the split's playlist into another playlist,
    right away (3 calls: the snapshot, the add, the removal; when the
    destination's cache holds the song, its snapshot is read instead of
    the add when it proves the cache current — still 3). The playlist must still be the
    one the draft knows — otherwise the draft would adopt the removal's
    snapshot over changes it never saw, and Done's "the playlist changed"
    guard would wave them through. The add goes first, so a failure can
    never lose the song: an add that fails changes nothing, and a removal
    that fails after it leaves the song in both playlists and says so (a
    retry then skips the add and only removes)."""
    # Held across the Spotify calls on purpose: a move, a part change or a
    # Done started mid-move waits for it rather than interleaving with it.
    # One user on one phone, so the wait (a second or so) is acceptable.
    with spa._lock:
        draft = _splitparts_draft(pid)
        if draft.get("done") is not None:
            raise HTTPException(409, _SPLITPARTS_DONE_STARTED_MSG)
        with _splitparts_job_lock:
            running = (_splitparts_job.get("state") == "running"
                       and _splitparts_job.get("pid") == pid)
        if running:
            raise HTTPException(409, "already running")
        uri = body.uri
        if not spa.has_song(draft, uri):
            raise HTTPException(400, "that song is not in this split")
        if ((draft.get("songs") or {}).get(uri) or {}).get("local"):
            raise HTTPException(400, "a local file cannot be added through Spotify's API")
        if body.to_id == pid:
            raise HTTPException(400, "that is the playlist being split")
        # Either list: another home (the magnifier) or an input (the tray).
        target = next((t for t in _splitparts_targets(pid) + _splitparts_buffer_targets(pid)
                       if t["id"] == body.to_id), None)
        if target is None:
            raise HTTPException(400, "not a playlist this song can be moved to")
        if sp.snapshot(pid) != draft["snapshot_id"]:
            raise HTTPException(409, _SPLITPARTS_CHANGED_MSG)
        # A song the destination already holds is not added a second time
        # (the retry after a "now in both playlists" error) — but only when
        # the cached track list is proven current: it can be days old, and a
        # song since removed from the destination in Spotify would otherwise
        # be skipped here, removed below, and end up in neither playlist.
        # Its snapshot is the proof (1 call); on a mismatch, or nothing
        # cached to compare, add as usual — a duplicate is the safe side.
        dest = store.cache()["playlists"].get(body.to_id) or {}
        held = any(t.get("uri") == uri for t in dest.get("tracks") or [])
        current = (held and bool(dest.get("snapshot_id"))
                   and sp.snapshot(body.to_id) == dest["snapshot_id"])
        if not current:
            _apply_snapshot(body.to_id, sp.add_to_playlist(body.to_id, [uri]))
        try:
            snap = sp.remove_from_playlist(pid, uri)
        except Exception as exc:
            # The add landed: mirror that much, it is true.
            _cache_move(uri, [], body.to_id)
            _sync_membership(uri, add_to=body.to_id, remove_from=[])
            log.warning("split: elsewhere removal failed after the add", exc_info=True)
            raise HTTPException(
                502, f"added to “{target['name']}”, but removing it from “{draft['name']}” "
                     f"failed ({exc}) — the song is now in both playlists")
        _apply_snapshot(pid, snap)
        _cache_move(uri, [pid], body.to_id)
        _sync_membership(uri, add_to=body.to_id, remove_from=[pid])
        entry = store.cache()["playlists"].get(pid)
        _sync_listing_snapshot(pid, snap, total=len(entry["tracks"]) if entry else None)
        spa.forget_song(draft, uri)
        if snap:
            draft["snapshot_id"] = snap
        spa.put_draft(store, draft)
    return {**spa.without_tracks(draft), "moved_to": {"id": target["id"], "name": target["name"]}}


class SplitDoneIn(BaseModel):
    names: dict[str, str] = Field(default_factory=dict)
    folder: str | None = None
    original: str = "subset"
    part: str | None = None
    # {part key: home id}: that part's songs are added to an existing home
    # instead of becoming a new playlist.
    into: dict[str, str] = Field(default_factory=dict)


def _splitparts_into_homes(into: dict[str, str]) -> dict[str, dict]:
    """What the cache knows of the homes parts go into, for the plan: name,
    the songs its cached track list holds, and whether it has one at all.
    Zero calls. Named from the whole cached listing, not just the offered
    targets, so a resumed Done whose home stopped being offered still
    names it."""
    cache = store.cache()
    listing = {p.get("id"): p for p in (cache.get("playlist_list") or {}).get("items") or []}
    out = {}
    for to_id in set(into.values()):
        entry = cache["playlists"].get(to_id)
        item = listing.get(to_id) or {}
        out[to_id] = {"name": item.get("name") or to_id, "total": item.get("total") or 0,
                      "cached": entry is not None,
                      "uris": {t.get("uri") for t in (entry or {}).get("tracks") or []}}
    return out


def _splitparts_into_problem(pid: str, into: dict[str, str]) -> str | None:
    """Why a part cannot go into this playlist: it must be one of the
    split's move targets — another home, editable, not archived, not the
    split playlist and not Liked Songs."""
    offered = {t["id"] for t in _splitparts_targets(pid)}
    names = {p.get("id"): p.get("name") for p in
             (store.cache().get("playlist_list") or {}).get("items") or []}
    for key, to_id in into.items():
        if to_id == pid:
            return "a part cannot go into the playlist being split"
        if to_id not in offered:
            return f"{names.get(to_id) or to_id!r} is not a home a part can go into"
    return None


def _splitparts_parse_into(pairs: list[str]) -> dict[str, str]:
    """The plan's `into=p2:ID` query pairs as a mapping. Part keys and
    Spotify ids never hold a colon, so the first one splits them."""
    out = {}
    for pair in pairs:
        key, sep, to_id = pair.partition(":")
        if not sep or not key or not to_id:
            raise HTTPException(400, f"into wants part:home pairs, not {pair!r}")
        out[key] = to_id
    return out


def _splitparts_name_problem(names: list[str]) -> str | None:
    """Why one of these part names cannot become a home. Input sets count
    as much as the single `input_name_pattern`: with sets configured that
    pattern is usually absent, and a "[x]" part would be read back as a
    buffer on the very next request."""
    cfg = store.config()
    sets = inputsets.resolve_sets(cfg)
    for name in names:
        problem = creatable_home_name_problem(
            name,
            input_pattern=cfg.get("input_name_pattern"),
            exclude_patterns=cfg.get("home_name_exclude_patterns") or [],
            exclude_emoji=bool(cfg.get("home_exclude_emoji_names")),
        )
        if not problem and inputsets.set_of(name, None, sets):
            problem = (f"{name.strip()!r} matches an input set's name rule — it would "
                       "become an input, not a home")
        if problem:
            return f"{name!r}: {problem}"
    return None


def _splitparts_input_set_folder(folder: str | None) -> str | None:
    """Why a part cannot be filed into this folder: a folder-defined input
    set claims everything inside it, so a part there would be an input."""
    if not folder:
        return None
    segments = folder.split(" / ")
    for s in inputsets.resolve_sets(store.config()):
        seg = s.get("path_segment")
        if seg and seg in segments:
            return (f"{folder!r} is inside {seg!r}, which defines an input set — "
                    "a part filed there would be an input, not a home")
    return None


@app.get("/api/splitparts/{pid}/plan")
def splitparts_plan(pid: str, original: str = "subset", part: str | None = None,
                    into: list[str] = Query(default=[])):
    """The price of a Done choice, zero calls. `into` comes as repeated
    `into=p2:ID` pairs: a GET with nothing to post, and the pairs keep the
    callers that send no `into` at all exactly as they were."""
    draft = _splitparts_draft(pid)
    into_map = _splitparts_parse_into(into)
    try:
        plan = spa.done_plan(draft, original, part, remove_batch=REMOVE_BATCH,
                             into=into_map, homes=_splitparts_into_homes(into_map))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # The locked finish screen prices the STORED choice, which a resume
    # keeps even once its home stopped being offered (see the done route).
    if draft.get("done") is None:
        problem = _splitparts_into_problem(pid, into_map)
        if problem:
            raise HTTPException(400, problem)
    choices, default = _home_folder_choices(store.config(), pid)
    for c in choices:
        c["blocked"] = c["blocked"] or _splitparts_input_set_folder(c["path"])
    if default and any(c["path"] == default and c["blocked"] for c in choices):
        default = None
    return {"calls": plan["calls"], "creates": plan["creates"], "empty": plan["empty"],
            "local": plan["local"], "removals": len(plan["removals"]),
            "into_parts": [{k: p[k] for k in ("key", "to_id", "to_name", "count")}
                           for p in plan["into_parts"]],
            "folders": choices, "folder": default}


def _splitparts_seed_tracks(pid: str, snapshot_id: str | None, uris: list[str],
                            full: dict[str, dict]) -> None:
    """Mirror a Done step in the local track cache (no call), so the new
    homes take part in filing suggestions at once. `full` maps uri to the
    richest track dict we hold (the original's cached tracks)."""
    if not snapshot_id:
        return
    cache = store.cache()
    entry = cache["playlists"].get(pid) or {}
    entry.update({"snapshot_id": snapshot_id, "fetched_at": time.time(),
                  "tracks": [full[u] for u in uris if u in full]})
    cache["playlists"][pid] = entry
    store.save_cache(cache)


def _splitparts_file(created: list[dict], folder: str) -> None:
    """File the new parts one after another: the mover drives one desktop
    client, so the jobs must not overlap. The listing comes from the cache
    (zero calls), not `sp.my_playlists()` (~21 unpriced calls on a cold
    one) — and it is re-read for every part, not once up front, because
    remembering a part (sp.remember_playlist, called for each one in turn)
    changes it."""
    def run():
        for c in created:
            items = (store.cache().get("playlist_list") or {}).get("items") or []
            filing.start(c["id"], c["name"], folder, items=items,
                         on_success=lambda path, p=c["id"]: store.set_folder_path(p, path),
                         threaded=False)
    if _splitparts_threaded:
        threading.Thread(target=run, daemon=True).start()
    else:
        run()


def _splitparts_run_done(pid: str, opts: dict) -> None:
    def progress(**fields):
        with _splitparts_job_lock:
            _splitparts_job.update(fields)

    try:
        draft = spa.get_draft(store, pid)
        if draft is None:
            progress(state="error", message="no split in progress for this playlist")
            return
        first_attempt = draft.get("done") is None
        state = draft.get("done") or {"opts": opts, "created": {}, "filled": [],
                                      "original_done": False}
        opts = state["opts"]   # a resumed Done keeps the choices it started with
        draft["done"] = state

        def save():
            spa.put_draft(store, draft)

        if first_attempt:
            # Only before anything was written: once parts exist, a resume
            # must finish the job rather than stop on our own changes.
            progress(phase="checking the playlist")
            if sp.snapshot(pid) != draft["snapshot_id"]:
                draft["done"] = None
                progress(state="changed", message=_SPLITPARTS_CHANGED_MSG)
                return
        into = opts.get("into") or {}   # absent from opts stored before into existed
        plan = spa.done_plan(draft, opts["original"], opts.get("part"),
                             names=opts.get("names"), remove_batch=REMOVE_BATCH,
                             into=into, homes=_splitparts_into_homes(into))
        if first_attempt:
            # The route checked this already; checked again here because the
            # runner is what writes. A resume does not: see the done route.
            problem = _splitparts_into_problem(pid, into)
            if problem:
                draft["done"] = None
                progress(state="error", message=problem)
                return
        steps = len(plan["new_parts"]) + len(plan["into_parts"])
        # Not saved yet on a first attempt: `done` locks the split, and
        # nothing is worth locking it for until the first write has landed
        # (a quota trip on create #1 must leave moves and Suggest open).
        # The first save is the one recording the first created id.
        full = {t["uri"]: t for t in
                (store.cache()["playlists"].get(pid) or {}).get("tracks") or []}
        for t in draft["tracks"]:
            full.setdefault(t["uri"], t)
        me = (store.cache().get("me") or {}).get("id")
        created = []
        for i, part in enumerate(plan["new_parts"]):
            key, name = part["key"], part["name"]
            progress(phase=f"creating “{name}” ({i + 1} of {len(plan['new_parts'])})", done=i)
            new_id = state["created"].get(key)
            if not new_id:
                new_id, _ = sp.create_playlist_full(name)
                # Record it before marking it a home below: a crash between
                # the two must not leave this id unrecorded — that would
                # make resume create a second playlist for the same part
                # and orphan the first, forever untracked.
                state["created"][key] = new_id
                save()
            # Idempotent, and run on every pass — not only when the id was
            # just created above — so a crash in the config write on a
            # prior attempt (new_id already in state["created"]) still
            # gets the id marked a home on the next attempt, instead of
            # skipping this forever because `if not new_id:` was already
            # false. Skip the write once both sets already have it, so a
            # fully-resolved resume doesn't pay a needless config save.
            cfg = store.config()
            home_ids = set(cfg.get("home_ids") or [])
            sticky_home_ids = set(cfg.get("sticky_home_ids") or [])
            if new_id not in home_ids or new_id not in sticky_home_ids:
                store.update_config(
                    home_ids=sorted(home_ids | {new_id}),
                    sticky_home_ids=sorted(sticky_home_ids | {new_id}))
            if key not in state["filled"]:
                uris = part["uris"]
                added = state.setdefault("added", {})
                snaps = state.setdefault("snap", {})
                offset = added.get(key, 0)
                snap = snaps.get(key)
                # Resume from the batch that failed, not batch 0 — `added`
                # and a per-batch save() are what make a part's add calls
                # resumable without re-adding (and duplicating) songs
                # already landed in a part bigger than one batch.
                for b in range(offset, len(uris), 100):
                    batch = uris[b:b + 100]
                    snap = sp.add_to_playlist(new_id, batch) or snap
                    added[key] = b + len(batch)
                    snaps[key] = snap
                    save()
                snap = snap or f"created:{new_id}"
                sp.remember_playlist({"id": new_id, "name": name, "owner": me,
                                      "editable": True, "total": len(uris),
                                      "snapshot_id": snap, "image": None, "description": ""})
                _splitparts_seed_tracks(new_id, snap, uris, full)
                state["filled"].append(key)
                save()
            created.append({"id": new_id, "name": name, "count": len(part["uris"])})

        # Into an existing home: only adds, nothing created, marked or filed.
        # What the home already holds is read from a list PROVEN current —
        # its snapshot now, the cached tracks only when that matches, a
        # fresh read otherwise — every time, on a resume too. A song wrongly
        # believed present would be skipped here and then, in "becomes a
        # part" mode, removed from the original: lost. Re-deciding on every
        # pass is also what makes a resume exact: the batches that landed
        # are in the list (mirrored into the cache with the add's own
        # snapshot, or found by the re-read when that answer never came
        # back), so they are neither added twice nor skipped.
        into_done = []
        for j, part in enumerate(plan["into_parts"]):
            key, to_id = part["key"], part["to_id"]
            progress(phase=f"adding to “{part['to_name']}” ({j + 1} of {len(plan['into_parts'])})",
                     done=len(plan["new_parts"]) + j)
            counts = state.setdefault("into_added", {})
            already = state.setdefault("into_held", {})
            if key not in state["filled"]:
                snap = sp.snapshot(to_id)
                home_tracks = _cached_tracks(to_id, snap)
                present = {t.get("uri") for t in home_tracks}
                todo = [u for u in part["uris"] if u not in present]
                # Counted on the first pass only (on a resume this part's own
                # landed batches are "present" too), and saved with the first
                # batch, not before it: nothing is worth locking the split
                # for until a write has landed.
                if key not in already:
                    already[key] = len(part["uris"]) - len(todo)
                uris_now = [t["uri"] for t in home_tracks if t.get("uri")]
                known = {**full, **{t["uri"]: t for t in home_tracks if t.get("uri")}}
                for b in range(0, len(todo), 100):
                    batch = todo[b:b + 100]
                    snap = sp.add_to_playlist(to_id, batch)
                    uris_now += batch
                    counts[key] = counts.get(key, 0) + len(batch)
                    _splitparts_seed_tracks(to_id, snap, uris_now, known)
                    _sync_listing_snapshot(to_id, snap, total=len(uris_now))
                    save()
                state["filled"].append(key)
                save()
            into_done.append({"key": key, "id": to_id, "name": part["to_name"],
                              "count": counts.get(key, 0), "already": already.get(key, 0)})

        if not state["original_done"]:
            progress(phase="updating the original", done=steps)
            if opts["original"] == "subset":
                new_name = roles.mark_subset_name(draft["name"])
                if new_name != draft["name"]:
                    sp.rename_playlist(pid, new_name)
                cfg = store.config()
                store.update_config(
                    home_ids=[h for h in cfg.get("home_ids") or [] if h != pid],
                    sticky_home_ids=[h for h in cfg.get("sticky_home_ids") or [] if h != pid])
            else:
                snap = sp.remove_many(pid, plan["removals"])
                gone = set(plan["removals"])
                kept = [t["uri"] for t in draft["tracks"] if t["uri"] not in gone]
                _splitparts_seed_tracks(pid, snap, kept, full)
                _sync_listing_snapshot(pid, snap, total=len(kept))
            state["original_done"] = True
            save()

        _profile_state.clear()
        _profile_state["built_at"] = 0.0
        if opts.get("folder"):
            _splitparts_file(created, opts["folder"])
        spa.drop_draft(store, pid)
        progress(state="done", phase="done", done=steps, message=None,
                 summary={"created": created, "into": into_done, "original": opts["original"],
                          "empty": plan["empty"], "local": plan["local"],
                          # The client watches filing per created id, and in
                          # subset mode the original is no longer a home, so
                          # the summary itself must say what it was called.
                          "folder": opts.get("folder"), "name": draft["name"]})
    except SpotifyError as exc:
        # Retrying into a 429 is what extends it: say to wait, not "again".
        tail = ("press Done again after the cooldown" if exc.status == 429
                else "press Done again to continue")
        progress(state="error", message=f"{exc} — your split is saved; {tail}")
    except Exception as exc:
        log.exception("split: done failed")
        progress(state="error", message=f"done failed: {exc} — your split is saved; press Done again to continue")


@app.post("/api/splitparts/{pid}/done")
def splitparts_done(pid: str, body: SplitDoneIn):
    draft = _splitparts_draft(pid)
    # A resumed Done (draft["done"] already set) ignores the request body —
    # _splitparts_run_done keeps the choices it started with — so validation
    # must check the SAME thing the runner will actually do, or a retry whose
    # body disagrees with the stored opts (e.g. the screen reopened with
    # fresh editable defaults instead of the locked finish screen) could pass
    # here against "subset" while the runner goes on to remove songs for a
    # stored "part" choice.
    started = draft.get("done")
    check = started["opts"] if started is not None else {
        "names": body.names, "folder": body.folder, "original": body.original, "part": body.part,
        "into": body.into}
    into = check.get("into") or {}
    try:
        plan = spa.done_plan(draft, check["original"], check.get("part"),
                             names=check.get("names"), remove_batch=REMOVE_BATCH,
                             into=into, homes=_splitparts_into_homes(into))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # The into targets, like the folder below, are checked on a FIRST
    # attempt only. A resume keeps the target it started with even once it
    # stopped being offered (unmarked as a home, renamed 🗄️): refusing
    # would strand the split, since moves, Suggest and Discard all 409
    # once Done has started, and finishing only ever ADDS songs to the
    # playlist the user confirmed — nothing can be lost by it. The locked
    # finish screen names that playlist, and Abandon stays the way out.
    problem = _splitparts_into_problem(pid, into) if started is None else None
    problem = problem or _splitparts_name_problem([p["name"] for p in plan["new_parts"]])
    if not problem and started is None:
        # First attempt only: on a resume the parts already created are in
        # the listing under these very names.
        listing = (store.cache().get("playlist_list") or {}).get("items") or []
        # Kept as a subset, the original is renamed with the subset mark
        # before anything is filed, so its current name is no twin.
        renamed = (check["original"] == "subset"
                   and roles.mark_subset_name(draft["name"]) != draft["name"])
        problem = spa.twin_name_problem([p["name"] for p in plan["new_parts"]], listing,
                                        skip_id=pid if renamed else None)
    if problem:
        raise HTTPException(400, problem)
    # The folder check is for a FIRST attempt only. On a resume it would
    # refuse forever with no escape — moves, suggest and discard all 409
    # once Done has started, so a folder that stopped being offered (the
    # tree changed, a re-import dropped it) would strand the split with no
    # way to finish it. Filing is post-hoc and best-effort: it fails
    # without touching the created playlists or the original, so skipping
    # this check on a resume costs nothing but an unfiled playlist.
    if started is None and check.get("folder"):
        choices, _ = _home_folder_choices(store.config(), pid)
        if not any(c["path"] == check["folder"] and not c["blocked"] for c in choices):
            raise HTTPException(400, f"{check['folder']!r} is not an offered folder")
        problem = _splitparts_input_set_folder(check["folder"])
        if problem:
            raise HTTPException(400, problem)
    with _splitparts_job_lock:
        if _splitparts_job.get("state") == "running":
            raise HTTPException(409, "already running")
        _splitparts_job.clear()
        _splitparts_job.update({"pid": pid, "state": "running", "phase": "starting", "kind": "done",
                                "done": 0, "total": plan["creates"] + len(plan["into_parts"]),
                                "message": None, "summary": None})
    # The body's own opts still seed a FIRST attempt (the runner has nothing
    # stored yet to prefer); a resume ignores them regardless, inside the
    # runner itself.
    opts = {"names": body.names, "folder": body.folder, "original": body.original,
            "part": body.part, "into": body.into}
    if _splitparts_threaded:
        threading.Thread(target=_splitparts_run_done, args=(pid, opts), daemon=True).start()
    else:
        _splitparts_run_done(pid, opts)
    return {"started": True}


@app.post("/api/splitparts/{pid}/reread")
def splitparts_reread(pid: str):
    draft0 = _splitparts_draft(pid)
    if draft0.get("done") is not None:
        raise HTTPException(409, _SPLITPARTS_DONE_STARTED_MSG)
    snap = sp.snapshot(pid)               # network outside the draft lock
    tracks = _cached_tracks(pid, snap)
    _sync_listing_snapshot(pid, snap, total=len(tracks))
    with spa._lock:
        draft = _splitparts_draft(pid)
        if draft.get("done") is not None:
            raise HTTPException(409, _SPLITPARTS_DONE_STARTED_MSG)
        spa.reconcile(draft, tracks, snap, store.tag_artists(),
                      store.lastfm_track_map(), store.lastfm_artist_map())
        spa.put_draft(store, draft)
    return spa.without_tracks(draft)


@app.post("/api/config")
def set_config(body: ConfigIn):
    # Subsets are not saved here: a subset is its name (the Subset chip renames).
    cfg = store.config()
    # An archived playlist loads in the Lists view with no role and no
    # Buffer/Home chips, so a Save never lists it. Its marks are kept, not
    # read as "toggled off": they are what it gets back when the 🗄️ comes
    # off. Cached listing only — with none cached, nothing is known archived.
    listing = (store.cache().get("playlist_list") or {}).get("items") or []
    archived = {p["id"] for p in listing if roles.is_archived(p.get("name", ""))}

    def kept(saved: list[str], key: str) -> list[str]:
        return saved + [i for i in (cfg.get(key) or []) if i in archived and i not in saved]

    home_ids = kept(body.home_ids, "home_ids")
    store.update_config(
        input_ids=kept(body.input_ids, "input_ids"), home_ids=home_ids,
        home_hints={k: v.strip() for k, v in body.home_hints.items() if v.strip()},
        # A sticky role must still be revocable: Home toggled off in the
        # Playlists view drops the id here too, or the next folder ingest
        # would resurrect it. (Spec §2.)
        sticky_home_ids=sorted(set(cfg.get("sticky_home_ids") or []) & set(home_ids)),
    )
    # Hints feed the tag profiles, which otherwise sit cached for PROFILE_TTL —
    # a save should be visible on the very next suggestion, not 10 min later.
    _profile_state.clear()
    _profile_state["built_at"] = 0.0
    return {"ok": True}


@app.post("/api/playlists/create")
def create_playlist_api(body: CreatePlaylistIn):
    """Create a home or subset playlist from inside sortify. One Spotify call.

    Everything around the call is local bookkeeping: the listing entry
    (remember_playlist), a seeded empty track cache whose snapshot_id
    matches the listing's (else every profile rebuild refetches a playlist
    we know is empty — spec §3), the role marking, and a profile cache clear
    so the new playlist is usable now, not in PROFILE_TTL.

    A subset is never a filing destination, so it is never marked
    home/sticky, and it is created with an emoji-led name — which is what
    makes it a subset (playlist_roles.py). The home name rules are not applied
    to it; the input pattern still is.
    """
    if body.role not in ("home", "subset", "input"):
        raise HTTPException(
            400, f"unsupported role {body.role!r} — only homes, subsets and inputs "
                 "can be created")
    subset = body.role == "subset"
    is_input = body.role == "input"
    cfg = store.config()

    # Folder first: for a folder-defined input set ("everything inside THE
    # BOMB") the destination is what decides whether the name is legal, so
    # the name checks below need it resolved already.
    defaults = cfg.get("create_folders") or {}
    chosen_folder = (body.folder if "folder" in body.model_fields_set
                     else defaults.get(body.role))
    if chosen_folder is not None:
        known = folder_paths(store.folders())
        if chosen_folder not in known:
            raise HTTPException(
                400, f"unknown folder {chosen_folder!r} — sortify only knows the "
                     "folders that already hold a playlist, from the last folder "
                     "re-import")

    name = body.name.strip()
    if subset:
        # A subset is its name (playlist_roles.py): created with 🐾 unless it
        # already starts with an emoji.
        name = roles.mark_subset_name(name)
    if is_input:
        # For a pattern set the NAME is the membership. Marking `input_ids`
        # without it would produce an input that belongs to no set at all,
        # and the only way to move it into one afterwards is a rename.
        sets = inputsets.resolve_sets(cfg)
        if not name:
            raise HTTPException(400, "the name is empty")
        if not inputsets.set_of(name, chosen_folder, sets):
            rules = ", ".join(
                s.get("pattern") or f"inside {s['path_segment']}" for s in sets) or "none"
            raise HTTPException(
                400, f"{name!r} matches no input set rule ({rules}) — it would be "
                     "marked an input belonging to no set")
    else:
        # The TYPED name is what is checked: marking blanks to "🐾" and hides
        # an input shape ("[Foo]" -> "🐾 [Foo]") that the user did not mean as
        # a subset name.
        problem = creatable_home_name_problem(
            body.name,
            input_pattern=cfg.get("input_name_pattern"),
            exclude_patterns=[] if subset else (cfg.get("home_name_exclude_patterns") or []),
            exclude_emoji=False if subset else bool(cfg.get("home_exclude_emoji_names")),
        )
        if problem:
            raise HTTPException(400, problem)

    # Cached-only: my_playlists() would silently pay ~21 paginated calls on a
    # cold/absent playlist_list cache. When there is nothing cached, skip the
    # note rather than fetch to produce one — the same coupling remember_playlist
    # has: with no cached listing, it is a no-op and the new home stays
    # invisible until the next Refresh (degraded, not corrupt).
    cached_items = (store.cache().get("playlist_list") or {}).get("items") or []
    note = None
    if any(p["name"].strip() == name for p in cached_items):
        note = "a playlist with this name already exists — Spotify allows duplicates, so now there are two"

    new_id, snapshot = sp.create_playlist_full(name)
    snapshot = snapshot or f"created:{new_id}"  # sentinel: only has to equal itself

    item = {
        "id": new_id, "name": name,
        "owner": (store.cache().get("me") or {}).get("id"),
        "editable": True, "total": 0, "snapshot_id": snapshot,
        "image": None, "description": "",
    }
    # Also seeds the track cache atomically under the same lock (spec §3's
    # snapshot trap) — see Spotify.remember_playlist.
    sp.remember_playlist(item)

    cfg = store.config()
    if is_input:
        store.update_config(
            input_ids=sorted(set(cfg.get("input_ids") or []) | {new_id}))
    elif not subset:
        store.update_config(
            home_ids=sorted(set(cfg.get("home_ids") or []) | {new_id}),
            sticky_home_ids=sorted(set(cfg.get("sticky_home_ids") or []) | {new_id}),
        )

    # Last folder used per role becomes that role's default, top level
    # included — the dropdown is pre-selected from this next time.
    store.update_config(create_folders={**defaults, body.role: chosen_folder})

    # Same move as set_config after a hints save, same reason: usable on the
    # next request, not up to PROFILE_TTL later.
    _profile_state.clear()
    _profile_state["built_at"] = 0.0

    # Filing drives the desktop client for about a minute, so it runs behind
    # the response: the row is usable immediately, and the folder fills in
    # when the move lands. A failure leaves the playlist at the top level —
    # degraded, never lost — and says so through /api/playlists/filing.
    if chosen_folder is not None:
        filing.start(
            new_id, name, chosen_folder,
            items=list((store.cache().get("playlist_list") or {}).get("items") or []),
            on_success=lambda path, pid=new_id: store.set_folder_path(pid, path),
        )

    return {
        "playlist": {**item, "role": body.role, "folder": chosen_folder,
                     "split": None, "hints": ""},
        "note": note,
        "filing": chosen_folder is not None,
    }


@app.get("/api/playlists/filing/{playlist_id}")
def filing_status(playlist_id: str):
    """How the background folder move for this playlist is going. 0 calls."""
    return filing.status(playlist_id)


def _parse_hints(cfg: dict) -> dict[str, list[str]]:
    """config's `home_hints` ({id: "a, b"}) as {id: ["a", "b"]}, lowercased."""
    out = {}
    for pid, text in (cfg.get("home_hints") or {}).items():
        tags = [t.strip().lower() for t in str(text).split(",") if t.strip()]
        if tags:
            out[pid] = tags
    return out


def _effective_input_ids(cfg: dict, playlists: list[dict]) -> set[str]:
    """Explicitly marked inputs plus everything matching any input SET, minus
    anything archived (the archive marker first: no role at all, see
    playlist_roles.py).

    Set rules (name patterns like ^\\[.+\\]$, or a folder segment such as
    THE BOMB) live in config so a stale browser tab saving roles can never
    un-mark the real inputs. See sortify/inputsets.py.
    """
    ids = set(cfg.get("input_ids", [])) | inputsets.matched_ids(playlists, store.folders(), cfg)
    archived = {p["id"] for p in playlists if roles.is_archived(p.get("name", ""))}
    return ids - archived


def _effective_subset_ids(cfg: dict, playlists: list[dict]) -> set[str]:
    """The subsets: playlists of ours whose name starts with an emoji other
    than the archive marker (playlist_roles.py, shared with spotify-autoqueuer).

    Since 2026-10-01 the NAME is the whole definition. It used to be an
    opt-in id list (`subset_ids`) because the Lists chip could only mark the
    ~200 rows it draws; marking is now a rename the chip does itself, so the
    name reaches every playlist and other tools can see it without reading
    sortify's config. Inputs and homes still win, and archived is no role.
    """
    inputs = _effective_input_ids(cfg, playlists)
    homes = set(cfg.get("home_ids") or [])
    return {
        p["id"] for p in playlists
        if roles.role_of(p.get("name", ""), is_input=p["id"] in inputs,
                         is_home=p["id"] in homes, editable=bool(p.get("editable"))) == "subset"
    }


# ---- cache helpers ---------------------------------------------------------


def _unplayable(t: dict) -> bool:
    """A song the Spotify app greys out: Spotify said so (`playable` False,
    read with a market since 2026-10-20), or it came back with no title,
    which is how the dev-mode API returns a song pulled from the catalogue.
    The title test also covers caches read before the flag existed."""
    return t.get("playable") is False or not t.get("name")


def _cached_tracks(pid: str, snapshot_id: str | None) -> list[dict]:
    cache = store.cache()
    entry = cache["playlists"].get(pid)
    if entry:
        if pid == LIKED_ID:
            if time.time() - entry.get("fetched_at", 0) < LIKED_TTL:
                return entry["tracks"]
        elif snapshot_id and entry.get("snapshot_id") == snapshot_id:
            return entry["tracks"]
    tracks = sp.playlist_tracks(pid)
    cache = store.cache()
    cache["playlists"][pid] = {
        "snapshot_id": snapshot_id,
        "tracks": tracks,
        "fetched_at": time.time(),
    }
    store.save_cache(cache)
    return tracks


def _resolve_homes(cfg: dict, playlists: list[dict], exclude: str, input_ids: set[str]) -> list[dict]:
    # An archived name (🗄️) has no role at all, so it is never a home even
    # when its id is still listed in home_ids.
    home_ids = cfg.get("home_ids") or []
    live = [p for p in playlists if not roles.is_archived(p.get("name", ""))]
    if home_ids:
        chosen = [p for p in live if p["id"] in home_ids and p["id"] not in input_ids]
    else:
        chosen = [p for p in live if p["editable"] and p["id"] not in input_ids]
    return [p for p in chosen if p["id"] != exclude]


# ---- home profiles (shared by triage and now-playing) ----------------------

PROFILE_TTL = 600  # rebuild at most every 10 min; snapshot cache does the rest
_profile_state: dict = {"built_at": 0.0}
_profile_lock = threading.Lock()


def _ensure_profiles(force: bool = False) -> dict:
    """The cached profiles, rebuilding them if stale.

    The rebuild runs OUTSIDE `_profile_lock` and is swapped in under it. The
    lock used to wrap the whole thing, which meant it was held across every
    home re-read — and a Refresh measured on 2026-09-18 held it for ~5.5
    minutes while it spent 70 calls, during which /api/now/suggest never
    returned. Suggestions are a polling endpoint; they must never queue
    behind a rebuild's network I/O.

    A caller that arrives mid-rebuild is served the profile already in hand.
    That is deliberately a stale read: a profile a few minutes old answers
    "which home does this track belong in" just as well, and the alternative
    is the freeze this replaced. Only the first-ever build, with nothing
    cached to serve, still makes callers wait.
    """
    with _profile_lock:
        fresh = (_profile_state.get("profiles")
                 and time.time() - _profile_state["built_at"] < PROFILE_TTL)
        if fresh and not force:
            return _profile_state
        # Someone else is already paying for this one. With a profile in hand
        # we serve it; without one there is nothing to serve, so fall through
        # and let the lock below serialise us.
        if _profile_state.get("building") and _profile_state.get("profiles"):
            return _profile_state
        if not _profile_state.get("building"):
            _profile_state["building"] = True
            mine = True
        else:
            mine = False

    if not mine:
        # First-ever build, contended: wait for the winner rather than start
        # a second full fetch of the same thing.
        with _profile_lock:
            return _profile_state

    try:
        built = _build_profiles()
    finally:
        with _profile_lock:
            _profile_state["building"] = False

    with _profile_lock:
        _profile_state.update(built)
        return _profile_state


def _build_profiles() -> dict:
    """Fetch and compute a fresh profile set. Holds no lock; may take minutes.

    Returns the fields `_profile_state` is updated with, rather than mutating
    it, so the swap is a single dict update under the lock.
    """
    now = time.time()
    cfg = store.config()
    all_playlists = sp.my_playlists()
    input_ids = _effective_input_ids(cfg, all_playlists)
    homes = _resolve_homes(cfg, all_playlists, exclude="", input_ids=input_ids)
    if not cfg.get("home_ids") and len(homes) > 40:
        raise HTTPException(
            400,
            f"You own {len(homes)} candidate home playlists — scanning them all would trip "
            "Spotify's rate cooldown (which can last hours). Mark the playlists you actually "
            "sort into as Home first.",
        )
    home_tracks = {}
    for h in homes:
        home_tracks[h["id"]] = _cached_tracks(h["id"], h["snapshot_id"])
    # Guard-on-read, not fail-hard: a bad tags.json shape must degrade
    # suggestions to artist-overlap-only, not break profile building (and
    # therefore /api/now, a polling endpoint) on every rebuild.
    tag_artists = store.tag_artists()
    hints = _parse_hints(cfg)
    profiles = {
        h["id"]: sugg.build_profile(home_tracks[h["id"]], tag_artists, hints=hints.get(h["id"]))
        for h in homes
    }

    # Co-occurrence corpus: EVERY cached playlist (homes, inputs, and
    # anything else already on disk) — a pure cache.json read, zero API
    # calls, refreshed on the same PROFILE_TTL cadence as the profiles and
    # with the same accepted staleness (see the freshness-asymmetry pin in
    # tests/test_suggest.py).
    playlist_artists = sugg.playlist_artist_index(
        {pid: p.get("tracks") or []
         for pid, p in store.cache().get("playlists", {}).items()
         if isinstance(p, dict)}
    )

    # Subsets deliberately build NO profile. They were scored like homes
    # until 2026-08-28, when the user decided they did not want to be
    # suggested into them — and reading a playlist is the only reason
    # marking one ever cost anything. Without profiles a subset is purely a
    # destination you chose, so marking is free, there is no warm budget to
    # police, and nothing subset-shaped touches the poll path's spend.

    # Input contents too, so the capture chips can show membership.
    by_id = {p["id"]: p for p in all_playlists}
    inputs = []
    for iid in sorted(input_ids):
        if iid != LIKED_ID and iid not in by_id:
            continue
        name = "Liked Songs" if iid == LIKED_ID else by_id[iid]["name"]
        tracks = _cached_tracks(iid, by_id.get(iid, {}).get("snapshot_id"))
        inputs.append({
            "id": iid, "name": name, "uris": {t["uri"] for t in tracks},
            # Kept out of the switcher's count only (see _now_payload); they
            # are still members for every other purpose.
            "unplayable": {t["uri"] for t in tracks if _unplayable(t)},
            "set": (inputsets.set_of(name, (store.folders().get(iid) or {}).get("path"),
                                     inputsets.resolve_sets(cfg))
                    or inputsets.DEFAULT_KEY),
        })

    return dict(
        built_at=time.time(), profiles=profiles, homes=homes, inputs=inputs,
        playlists=all_playlists, input_ids=input_ids,
        playlist_artists=playlist_artists,
        last_added={hid: _last_added_at(tracks) for hid, tracks in home_tracks.items()},
    )


def _last_added_at(tracks: list[dict]) -> str | None:
    """Latest `added_at` among a home's cached tracks, or None.

    ISO-8601 Zulu strings compare correctly as strings, so max() needs no
    parsing. Powers the picker's recency sort — pure cache read, zero API
    cost, and exactly as fresh as the cache itself (which refetches a home
    whenever its snapshot moves, i.e. whenever something was added)."""
    stamps = [t["added_at"] for t in tracks if t.get("added_at")]
    return max(stamps) if stamps else None


def _homes_payload(state: dict, exclude: str = "") -> list[dict]:
    folders = store.folders()
    last_added = state.get("last_added") or {}
    return [
        {"id": h["id"], "name": h["name"], "image": h["image"], "total": h["total"],
         "folder": (folders.get(h["id"]) or {}).get("path"),
         "last_added_at": last_added.get(h["id"])}
        for h in state["homes"] if h["id"] != exclude
    ]


def _subset_targets_payload() -> list[dict]:
    """The subsets — the Add-to-subset picker's list: every playlist of ours
    named with an emoji (the archive marker excepted). See
    `_effective_subset_ids`.

    Built from the cached listing, the same input /api/act's subset guard
    reads, so the picker and the guard cannot disagree — and a mark (a
    rename, which updates that listing) shows at once instead of after
    PROFILE_TTL, as it did when this read the profile snapshot. A cold
    listing gives an empty picker, never a fetch.
    """
    listing = (store.cache().get("playlist_list") or {}).get("items") or []
    ids = _effective_subset_ids(store.config(), listing)
    folders = store.folders()
    return [
        {"id": p["id"], "name": p["name"], "total": p.get("total"),
         "folder": (folders.get(p["id"]) or {}).get("path")}
        for p in listing if p["id"] in ids
    ]


def _quick_adds_payload(state: dict, uri: str | None) -> list[dict]:
    """The Now card's one-tap buttons (config's `quick_adds`).

    Sits beside the subset picker's list because that is what it skips: a
    quick add IS a subset add, with the destination decided in advance.
    Membership comes off the cached track list, so the badge that stops a
    double-press duplicating a song costs nothing.
    """
    cached = store.cache().get("playlists") or {}

    def tracks_of(pid):
        entry = cached.get(pid)
        return None if entry is None else (entry.get("tracks") or [])

    return quickadds.payload(store.config(), state.get("playlists", []), tracks_of, uri)


@app.post("/api/refresh")
def refresh_profiles():
    """The Refresh button: the one place the playlist listing is re-read.

    The listing and nothing else. Its cost is bounded and knowable in
    advance — one page per 50 playlists, ~20 calls for this account — which
    is what lets the UI promise a duration it can keep.

    It used to force a profile rebuild in the same request, and that is what
    made the button unusable. Measured 2026-09-18: **424.7 seconds and 90
    Spotify calls**, against a UI promising "about a minute". Only ~20 of
    those calls were the listing; the other ~70 were a re-read of every home
    whose snapshot_id had moved in the 19 days the list sat frozen. Those
    re-reads are real work, but they belong to whoever next needs a profile,
    paced by PROFILE_TTL — not to a button press the user is watching.

    Invalidating (rather than rebuilding) is what keeps the button honest:
    the next read rebuilds off the fresh listing, and since that rebuild no
    longer holds `_profile_lock` while it fetches, it blocks nobody.
    """
    before = sp.budget_spent()
    items = sp.my_playlists(refresh=True)
    # The fresh listing carries new snapshot_ids, so every cached profile is
    # now suspect. Clearing built_at (the same move set_config makes) hands
    # the rebuild to the next reader instead of doing it here.
    _profile_state["built_at"] = 0.0
    return {
        "ok": True,
        "playlists": len(items),
        "calls_spent": sp.budget_spent() - before,
    }


# ---- triage ----------------------------------------------------------------


@app.get("/api/triage/{playlist_id}")
def triage(playlist_id: str):
    state = _ensure_profiles()
    by_id = {p["id"]: p for p in state["playlists"]}
    if playlist_id != LIKED_ID and playlist_id not in by_id:
        raise HTTPException(404, "unknown playlist")

    input_tracks = _cached_tracks(playlist_id, by_id.get(playlist_id, {}).get("snapshot_id"))
    # Guard-on-read (see _build_profiles) — triage must stay usable
    # even with a stale/bad-version tags.json; the split flow is where that
    # fails loud. lastfm_track_map() gets the same fresh-on-every-request
    # treatment as tag_artists, for the same reason: it's a local JSON read
    # (zero API cost), so there is no case for leaning on the profile cache
    # and letting a just-fetched neighbour record sit invisible for up to
    # PROFILE_TTL.
    tag_artists = store.tag_artists()
    track_map = store.lastfm_track_map()
    artist_map = store.lastfm_artist_map()

    tracks_out = []
    for t in input_tracks:
        sortable = t["type"] == "track" and not t["is_local"] and t.get("id")
        tracks_out.append(
            {
                **t,
                "sortable": bool(sortable),
                "suggestions": sugg.suggest(
                    t, state["profiles"], tag_artists, track_map, artist_map,
                    state.get("playlist_artists"),
                ) if sortable else [],
            }
        )

    name = "Liked Songs" if playlist_id == LIKED_ID else by_id[playlist_id]["name"]
    return {
        "playlist": {"id": playlist_id, "name": name},
        "homes": _homes_payload(state, exclude=playlist_id),
        "tracks": tracks_out,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lastfm_client() -> LastFm | None:
    key = load_key()
    return LastFm(key) if key else None


# Guards every save of tags.json against the lost-update race between two
# writers that both start from a read taken before their (slow) network work:
# two concurrent now-playing on-demand fetches (below) can each hold a
# snapshot that predates the other's save, and a bare
# `store.save_tag_artists(mine)` after that is last-writer-wins — silently
# dropping the other writer's freshly fetched artists, which is permanent,
# unrecoverable work (tags.json is never re-fetched). Both writers must
# funnel their saves through `_merge_save_tag_artists` for the guarantee to
# hold; a lone caller of `store.save_tag_artists` still races.
_tags_save_lock = threading.Lock()


def _merge_save_tag_artists(new_entries: dict) -> None:
    """Merge `new_entries` into tags.json without losing a concurrent writer.

    Re-reads `store.tag_artists()` fresh *inside* the lock, right before
    saving — so the merge always starts from whatever the other writer most
    recently landed, not the snapshot `new_entries` was computed against.
    Existing entries always win (write-once, same rule `enrich` itself
    follows): a key already on disk is never replaced by one from
    `new_entries`, so this is also safe to call with `new_entries` being a
    caller's whole known map rather than strictly the delta. The critical
    section is local dict/disk work only, never a network call, so this can
    never block a request on another request's Last.fm round trip.

    Clobber guard: `store.tag_artists()` degrades a malformed-but-valid-JSON
    envelope to `{}` (guard-on-read, so other readers fail toward "no tags"
    instead of crashing) rather than raising — but that same behaviour would
    make this function treat a ~1400-entry permanent cache as legitimately
    empty (or truncated) and overwrite it with just `new_entries`. `baseline`
    is captured before the lock's own fresh read; if that later read comes
    back with FEWER artists than the baseline — not just zero — that is a
    malformed or truncated re-read, not a real shrink, and the save is
    refused. tags.json is append-only by design: no code path anywhere ever
    deletes an entry from it (write-once, the same rule `enrich` follows),
    so a shrink of ANY size between two reads taken moments apart is
    anomalous, not a legitimate race — a real concurrent writer only ever
    ADDS entries, so a genuine race can grow the count between the two reads
    but never shrink it, and the fetch that would have written here simply
    retries on the next request.
    """
    baseline = len(store.tag_artists())
    with _tags_save_lock:
        current = store.tag_artists()
        if len(current) < baseline:
            log.error(
                "refusing to save tags.json: fresh re-read shrank from %d to %d artists "
                "(envelope likely malformed or truncated); save skipped, "
                "the fetch will retry on the next request",
                baseline, len(current),
            )
            return
        merged = {**new_entries, **current}
        store.save_tag_artists(merged)


# Same lost-update guard as `_merge_save_tag_artists`, its own lock and its
# own baseline, for `lastfm_tracks.json` instead of `tags.json`. Currently
# has exactly one writer in this process (`_fetch_missing_now_tags`'s track-
# record step below), but the guard costs nothing to have in place before a
# second one exists, and it keeps the two envelopes' save paths symmetric —
# a future backfill-triggered-from-the-app writer gets the same guarantee
# for free. Unlike `tags.json`, `lastfm_tracks.json` is rebuildable (see
# `Store.LASTFM_TRACKS_DEFAULT`'s docstring), so a refused save here is
# lower-stakes than the tags.json case — but still worth refusing rather
# than guessing, for the same reason `scripts/backfill_similar.py`'s
# `merge_save` refuses: a malformed re-read must not be mistaken for a real
# shrink and clobber the file with just this call's `new_entries`.
_lastfm_tracks_save_lock = threading.Lock()


def _merge_save_lastfm_tracks(new_entries: dict) -> None:
    """Merge `new_entries` into lastfm_tracks.json without losing a
    concurrent writer. Mirrors `_merge_save_tag_artists` exactly — see that
    function's docstring for the full reasoning.

    `Store.save_lastfm_tracks` writes the WHOLE envelope (unlike
    `save_tag_artists`, which wraps the inner map for its caller), so this
    wraps `merged` in `{"version": 1, "tracks": ...}` itself before saving.
    """
    baseline = len(store.lastfm_track_map())
    with _lastfm_tracks_save_lock:
        current = store.lastfm_track_map()
        if len(current) < baseline:
            log.error(
                "refusing to save lastfm_tracks.json: fresh re-read shrank from %d to %d "
                "tracks (envelope likely malformed or truncated); save skipped, "
                "the fetch will retry on the next request",
                baseline, len(current),
            )
            return
        merged = {**new_entries, **current}
        store.save_lastfm_tracks({"version": 1, "tracks": merged})


# Same lost-update guard as `_merge_save_lastfm_tracks`, its own lock and its
# own baseline, for `lastfm_artists.json` instead — a rename-copy, not a
# reuse, because the two envelopes are unrelated caches with independent
# writers (this one from `_fetch_missing_now_tags`'s artist-similar step
# below, plus `scripts/backfill_artist_similar.py` in its own process).
# Unlike `tags.json`, `lastfm_artists.json` is rebuildable (see
# `Store.LASTFM_ARTISTS_DEFAULT`'s docstring), so a refused save here is
# lower-stakes than the tags.json case — but still worth refusing rather
# than guessing, for the same reason `scripts/backfill_artist_similar.py`'s
# `merge_save` refuses: a malformed re-read must not be mistaken for a real
# shrink and clobber the file with just this call's `new_entries`.
_lastfm_artists_save_lock = threading.Lock()


def _merge_save_lastfm_artists(new_entries: dict) -> None:
    """Merge `new_entries` into lastfm_artists.json without losing a
    concurrent writer. Mirrors `_merge_save_lastfm_tracks` exactly — see that
    function's docstring for the full reasoning.

    `Store.save_lastfm_artists` writes the WHOLE envelope (unlike
    `save_tag_artists`, which wraps the inner map for its caller), so this
    wraps `merged` in `{"version": 1, "artists": ...}` itself before saving.
    """
    baseline = len(store.lastfm_artist_map())
    with _lastfm_artists_save_lock:
        current = store.lastfm_artist_map()
        if len(current) < baseline:
            log.error(
                "refusing to save lastfm_artists.json: fresh re-read shrank from %d to %d "
                "artists (envelope likely malformed or truncated); save skipped, "
                "the fetch will retry on the next request",
                baseline, len(current),
            )
            return
        merged = {**new_entries, **current}
        store.save_lastfm_artists({"version": 1, "artists": merged})


# ---- now playing -----------------------------------------------------------

# All tabs share one upstream currently-playing call, and the cached answer
# lives exactly as long as the playing track has left to run — nothing can
# change before then except a manual skip, and those come in through the
# forced refresh below. A 3-4 minute track therefore costs one call instead of
# one every six seconds.
#
# The old pairing was a 5s TTL against a 6s client poll, so every single poll
# missed the cache: ~600 calls/hour just to watch one track play. The client
# no longer picks an interval at all; the server hands it one (poll_after_ms),
# because only the server knows when the answer can next change.
NOW_TTL_IDLE = 60    # paused or silent: nothing advances on its own
NOW_TTL_MIN = 20     # floor, so a track ending in 2s doesn't mean a call in 2s
NOW_TTL_MAX = 300    # ceiling, so a very long track still gets re-checked
NOW_FORCE_MIN_INTERVAL = 10   # fastest a user-triggered refresh may hit upstream
NOW_ERROR_POLL_MS = 300_000   # cooldown/reauth: sit still, there is nothing to see

_now_cache: dict = {"at": 0.0, "value": None, "ttl": NOW_TTL_IDLE}
_now_lock = threading.Lock()

# Skip settle: right after /api/player/next, Spotify can still report the track
# that was just skipped AWAY from. Caching that answer normally would pin the
# wrong track for its remaining runtime (minutes), with NOW_FORCE_MIN_INTERVAL
# blocking any correction — so while the pre-skip uri keeps coming back, the
# answer only gets a short TTL and the client's server-paced poll re-checks.
NOW_SKIP_SETTLE_TTL = 3.0     # cache life for a not-yet-settled post-skip answer
NOW_SKIP_SETTLE_WINDOW = 15.0  # how long after a skip the old uri stays suspect
_skip_settle: dict = {"uri": None, "until": 0.0}


def _now_ttl(value: dict | None) -> float:
    """How long this answer stays true — the track's remaining runtime."""
    if not value or not value.get("is_playing"):
        return NOW_TTL_IDLE
    duration = (value.get("track") or {}).get("duration_ms")
    progress = value.get("progress_ms")
    if not duration or progress is None:
        return NOW_TTL_MIN
    remaining = (duration - progress) / 1000
    return max(NOW_TTL_MIN, min(remaining + 1, NOW_TTL_MAX))


RELINK_DURATION_SLACK_MS = 2000


def _title_key(name: str | None) -> str:
    """A title with case, brackets, dashes and other punctuation folded away."""
    return " ".join(re.sub(r"[^\w]+", " ", (name or "").casefold()).split())


def _lead_artist(track: dict) -> set[str]:
    """The first artist, by id and by folded name: either may differ."""
    a = (track.get("artists") or [{}])[0] or {}
    return {x for x in (a.get("id"), (a.get("name") or "").casefold() or None) if x}


def _own_copy(value: dict | None) -> dict | None:
    """The now answer with a relinked stand-in read as the context
    playlist's own copy.

    Spotify can play a stand-in of a playlist's song — same name, artists
    and length under another track id — and /me/player reports the stand-in.
    Membership, filing and removal all need the playlist's own uri, so a
    song playing from [FILTER Y] read as having dropped out of it
    (2026-10-22). Matched from the cached track list only, zero calls; one
    unambiguous candidate or nothing. `played_uri` keeps what was reported."""
    track = (value or {}).get("track") or {}
    ctx = (value or {}).get("context_playlist_id")
    uri = track.get("uri")
    if not ctx or not uri or track.get("is_local"):
        return value
    tracks = ((store.cache().get("playlists") or {}).get(ctx) or {}).get("tracks") or []
    if not tracks or any(t.get("uri") == uri for t in tracks):
        return value
    # Loose on the title's punctuation and on featured artists' ids — the
    # stand-in for "Crossing Beyond - Govinda Remix" was "Crossing Beyond
    # (Govinda Remix)", its remixer under another artist id — strict on the
    # lead artist and the length.
    name, lead = _title_key(track.get("name")), _lead_artist(track)
    dur = track.get("duration_ms")
    cands = {t["uri"]: t for t in tracks
             if t.get("uri") and name and _title_key(t.get("name")) == name
             and lead and _lead_artist(t) & lead
             and dur is not None and t.get("duration_ms") is not None
             and abs(t["duration_ms"] - dur) <= RELINK_DURATION_SLACK_MS}
    if len(cands) != 1:
        return value
    own = next(iter(cands.values()))
    return {**value, "track": {**track, "uri": own["uri"], "id": own.get("id"), "played_uri": uri}}


def _currently_playing_shared(force: bool = False) -> tuple[dict | None, float]:
    """(value, seconds until it goes stale). N open tabs still cost one call.

    `force` is for explicit user action — opening the view, coming back to the
    tab — where the cheap prediction may be wrong because the user skipped.
    It ignores the TTL but never outruns NOW_FORCE_MIN_INTERVAL.
    """
    with _now_lock:
        age = time.time() - _now_cache["at"]
        if age < (NOW_FORCE_MIN_INTERVAL if force else _now_cache["ttl"]):
            return _now_cache["value"], max(0.0, _now_cache["ttl"] - age)
        value = _own_copy(sp.currently_playing())
        ttl = _now_ttl(value)
        if _skip_settle["uri"] and time.time() < _skip_settle["until"]:
            if ((value or {}).get("track") or {}).get("uri") == _skip_settle["uri"]:
                ttl = min(ttl, NOW_SKIP_SETTLE_TTL)
            else:
                _skip_settle.update(uri=None, until=0.0)  # settled: back to normal
        fetched_at = time.time()
        _now_cache.update(at=fetched_at, value=value, ttl=ttl)
    _skip_observe(value, fetched_at)
    return value, ttl


# ---- skip counting ---------------------------------------------------------
#
# Every fresh now-playing answer feeds the skip counter shared with
# spotify-autoqueuer (~/state/spotify/skips.json, see skip_ledger.py): a song
# that changed before min(60 s, 90% of it) had played counts as skipped, and one
# that changed before its halfway mark counts in a second tally — once each,
# whichever app saw it. Zero extra Spotify calls — it only reads answers we
# already fetched. This app polls about once per track, so on its own it would
# rarely see a skip early enough to be sure; player_next closes that gap by
# telling the detector exactly where the song was when Next was pressed, and
# that the skip was ours ("sortify" rather than "spotify" in by_source).

_skip_detector = SkipDetector()
_skip_detector_lock = threading.Lock()


def _skip_observe(value: dict | None, at: float, progress_ms: float | None = None,
                  left: bool = False) -> None:
    """Feed one playback snapshot to the skip counter. Never raises.

    `left` marks a reading taken as the song was skipped away from: it stops
    advancing there, so the detector must not credit it the time until the
    next answer arrives."""
    try:
        track = (value or {}).get("track") or {}
        is_track = track.get("type", "track") == "track" and not track.get("is_local")
        obs = Observation(
            track_id=track.get("id") if is_track else None,
            progress_ms=progress_ms if progress_ms is not None else (value or {}).get("progress_ms"),
            duration_ms=track.get("duration_ms"),
            is_playing=bool((value or {}).get("is_playing")) and not left,
            at=at,
            name=track.get("name"),
            artists=[a.get("name") for a in track.get("artists") or [] if a.get("name")],
            left_by="sortify" if left else None,
        )
        with _skip_detector_lock:
            ev = _skip_detector.observe(obs)
        if ev:
            SkipLedger("sortify", now=lambda: time.time()).record(ev)
    except Exception:  # a counter must never break now-playing
        log.warning("skip counter failed", exc_info=True)


def _now_fetched_ago_ms() -> int:
    """How old the answer /api/now just served is — milliseconds since the
    upstream fetch behind it. Display-only honesty for the client ("updated
    Ns ago"), so blind mode can tell a live answer from a cached one."""
    with _now_lock:
        return int(max(0.0, time.time() - _now_cache["at"]) * 1000)


def _poll_after_ms(stale_in: float) -> int:
    """When the client should come back — just after the cache goes stale, so
    its next poll is a fetch rather than a wasted round trip."""
    return max(1000, int(stale_in * 1000) + 500)


NOW_FETCH_MAX_ARTISTS = 3  # per /api/now?force=1 call — an explicit user action, not a poll

# Bound for the third piggyback step (artist-similar, lastfm_artists.json),
# same 3-credited-artists cap as the artist-tags step but its OWN constant —
# the two steps are independent write-once caches (tags.json vs
# lastfm_artists.json) with independent "already known" sets, so one being
# fully known must not shrink the other's slice of `track["artists"]`.
NOW_FETCH_MAX_SIMILAR_ARTISTS = 3  # per /api/now?force=1 call, same as NOW_FETCH_MAX_ARTISTS

# FastAPI's sync routes run in a threadpool, so two overlapping `?force=1`
# requests (a double-click, two tabs refocused at once) genuinely execute
# _fetch_missing_now_tags concurrently — this is not a hypothetical. Both
# would otherwise read tags.json as "artist unknown" and both call Last.fm
# for the same artist. Acquired non-blocking: a fetch already in flight means
# this round simply fetches nothing and the response renders with whatever
# tags already exist — never block one request's /api/now on another
# request's Last.fm round trip.
_now_fetch_lock = threading.Lock()

# Fix round, Important 1: worst case this fetch is up to NOW_FETCH_MAX_ARTISTS
# sequential Last.fm requests, each paced by tags.MIN_INTERVAL and (until this
# change) able to hang for the splitter's full 15s timeout — ~46s inline in a
# request the user is sitting on. NOW_FETCH_TIMEOUT bounds each request in
# this path alone; the splitter's own `top_tags` call is untouched.
NOW_FETCH_TIMEOUT = 5.0  # seconds per Last.fm request, this path only

# A non-code-6 Last.fm failure (rate limit, outage, bad key) used to be
# retried on every single subsequent `?force=1` with no floor at all — the
# same "retry into a rate limit" pattern CLAUDE.md forbids for Spotify,
# aimed at Last.fm instead. NOW_FETCH_MIN_INTERVAL floors how often an
# *attempt* happens at all, success or failure, independent of
# NOW_FORCE_MIN_INTERVAL (which paces the whole /api/now response).
NOW_FETCH_MIN_INTERVAL = 60  # seconds between fetch attempts, this path only

# Monotonic timestamp of the last fetch *attempt* (i.e. the last time `enrich`
# was actually called), guarded by `_now_fetch_lock` like everything else in
# this function — never read or written outside it.
_now_fetch_last_attempt = 0.0


def _fetch_missing_now_tags(track: dict, *, clock=time.monotonic) -> int:
    """Fetch Last.fm tags for up to `NOW_FETCH_MAX_ARTISTS` unknown artists on
    the currently-playing track, THEN — same lock, same floor, same bounded
    client — fetch its `lastfm_tracks.json` record (getSimilar + track top
    tags) if it has none yet, THEN — same lock, same floor, same bounded
    client again — fetch `lastfm_artists.json`'s artist-similar record for up
    to `NOW_FETCH_MAX_SIMILAR_ARTISTS` of its credited artists. Returns how
    many artists' TAGS were fetched; the track-record and artist-similar
    steps are side effects on `lastfm_tracks.json`/`lastfm_artists.json`,
    checked by tests via the store rather than this return value (mirroring
    how `_merge_save_tag_artists`'s own writes aren't reflected in it either).

    Callable ONLY from `now_playing`'s `?force=1` branch — opening or
    refocusing the view, an explicit user action — never the passive poll,
    which must never reach Last.fm at all. Reuses `_lastfm_client()`/`enrich`
    as-is — the same client construction, `MIN_INTERVAL` pacing and
    tags.json envelope shape — rather than building a second one its own
    way; no key configured means `_lastfm_client()` returns None and this
    does nothing, silently.

    Two bounds on top of that reuse: the `LastFm` instance this path gets
    back from `_lastfm_client()` has its transport swapped for one built with
    `timeout=NOW_FETCH_TIMEOUT` — a fresh `httpx.Client`, only on this
    instance, never touching `_lastfm_client()`'s own default-timeout build —
    *and* `fm._timeout` is set to the same value, since `LastFm.top_tags`
    passes `timeout=self._timeout` on every request, which in httpx overrides
    whatever the client itself was constructed with. Setting only the client
    would leave every actual request still bounded by the default 15s
    timeout; both need setting for this path to actually be bounded at 5s —
    and the SAME swapped instance is reused for the track-record step below, so that step is
    bounded exactly the same way without a second swap. `NOW_FETCH_MIN_INTERVAL`
    floors how often an attempt happens at all, checked (and skipped on)
    before any client is even touched — ONE floor shared by both fetch
    kinds, not two independent ones, so a request that only needs a track
    record still respects the same 60s spacing an artist-tags fetch would.
    `clock` is injectable so tests can move time without sleeping.

    Write-once: an artist already in tags.json — a hit or a recorded
    `miss: true` alike — is never handed to `enrich` again. `enrich` raises
    `LastFmError` on anything other than a genuine "not found" (code 6,
    folded into `miss: true` by `top_tags` returning None); that must not
    break this response, so it's caught here and only `.partial` — whatever
    was verified before the failure — is persisted. Every artist after the
    failing one in this batch is simply left absent and retryable, bounded by
    the floor above rather than retried on the very next force. Persisting
    goes through `_merge_save_tag_artists`, so a concurrent `enrich()` walk
    from another now-playing fetch can't lose (or be lost by) this write.

    The track-record step follows the same write-once rule via
    `Store.lastfm_track_map()`, but checked under EVERY credited artist's
    `track_key`, not just the first — `sortify.suggest._track_record`'s own
    convention for where a collab's record lives, and the same rule
    `scripts/backfill_similar.py`'s `tracks_to_fetch` follows for the
    backfill's skip-known check. The record itself is always fetched (and,
    if fetched, stored) under the FIRST credited artist's key, matching that
    script's fetch-key convention too, so a later backfill run recognises it
    as already known instead of re-fetching under the same key. `fetch_track`
    makes two Last.fm requests (getSimilar, then track top tags) and does
    NOT wrap a bare transport exception into `LastFmError` the way `enrich`
    does — its own docstring says any exception from either call propagates
    untouched — so the catch below is deliberately broad, exactly like
    `scripts/backfill_similar.py`'s run loop. A failure here leaves the
    track's key absent (retried on a later force, once the floor allows) and
    never raises out of this function — a bonus enrichment must never break
    the response it rides on.
    """
    global _now_fetch_last_attempt
    if not _now_fetch_lock.acquire(blocking=False):
        return 0
    try:
        if clock() - _now_fetch_last_attempt < NOW_FETCH_MIN_INTERVAL:
            return 0
        fm = _lastfm_client()
        if fm is None:
            return 0
        if isinstance(fm, LastFm):
            # This path's own bounded transport — see NOW_FETCH_TIMEOUT above.
            # Both the client AND `_timeout` must be set: `top_tags` passes
            # `timeout=self._timeout` explicitly on every request, which
            # overrides the client's own configured default in httpx.
            fm._client = httpx.Client(timeout=NOW_FETCH_TIMEOUT)
            fm._timeout = NOW_FETCH_TIMEOUT

        attempted = False
        fetched = 0

        # Fix round 1, Critical (C1): everything from the first possible
        # spend (the artist-tags `enrich` call) to the last (the
        # track-record `fetch_track`/save) now lives inside ONE try/finally.
        # The bug this fixes: `store.lastfm_track_map()` below is a bare
        # file read that raises `json.JSONDecodeError` on a truly corrupt
        # lastfm_tracks.json (not just a malformed-but-valid-JSON envelope,
        # which `_versioned` already degrades gracefully) — that used to sit
        # OUTSIDE any try, so it could escape past the `if attempted:` floor
        # stamp below even after the artist-tags step had already spent up
        # to NOW_FETCH_MAX_ARTISTS calls. Proven repro: a corrupt
        # lastfm_tracks.json meant the floor never advanced, so every
        # `?force=1` re-spent those calls indefinitely, ~10s apart. The
        # `finally` here stamps the floor whenever `attempted` was set True
        # BEFORE the exception, regardless of where in this block the raise
        # happened — and the `except Exception` means neither a corrupt
        # file nor any other failure in either fetch kind can ever escape
        # this function (a bonus enrichment must never break the response
        # it rides on, exactly like the narrower catches this replaces).
        try:
            names: dict[str, str] = {}
            for a in track.get("artists") or []:
                aid = a.get("id")
                if aid and aid not in names:
                    names[aid] = a.get("name") or ""
            if names:
                cached = store.tag_artists()
                unknown: dict[str, str] = {}
                for aid, name in names.items():
                    if aid in cached:
                        continue
                    unknown[aid] = name
                    if len(unknown) == NOW_FETCH_MAX_ARTISTS:
                        break
                if unknown:
                    attempted = True
                    try:
                        merged = enrich(unknown, cached, fm, _now_iso())
                    except LastFmError as exc:
                        log.warning("now-playing tag fetch stopped early: %s", exc)
                        merged = exc.partial if exc.partial is not None else cached
                    fetched = len(merged) - len(cached)
                    if fetched > 0:
                        _merge_save_tag_artists(merged)

            # ---- track-level record: getSimilar + track top tags --------
            title = track.get("name") or ""
            artist_names = [a.get("name") for a in (track.get("artists") or []) if a.get("name")]
            if title and artist_names:
                keys = [track_key(n, title) for n in artist_names]
                # See the block comment above: this read can raise on a
                # truly corrupt file, and now does so INSIDE the guarded
                # region instead of past the floor stamp.
                track_map = store.lastfm_track_map()
                # M1: a presence check alone (`k in track_map`) would treat
                # a non-dict value at that key (defensive only — nothing
                # legitimate ever writes one) as "known"; require it to
                # actually be a record, the same predicate
                # `suggest._track_record` and
                # `backfill_similar.tracks_to_fetch` both use.
                known = any(isinstance(track_map.get(k), dict) for k in keys)
                if not known:
                    attempted = True
                    record = fetch_track(fm, artist_names[0], title, time.time())
                    _merge_save_lastfm_tracks({keys[0]: record})

            # ---- artist-similar: lastfm_artists.json ---------------------
            # Unlike the artist-tags step above (which scans every credited
            # artist and stops once NOW_FETCH_MAX_SIMILAR_ARTISTS unknowns
            # are found), this takes the first NOW_FETCH_MAX_SIMILAR_ARTISTS
            # CREDITED artists outright (id present), then skips whichever
            # of those are already known — hit or `miss: true` alike, same
            # write-once rule `backfill_artist_similar.py` follows. A track
            # with more than the cap's worth of unknown artists simply never
            # reaches the later ones from this path; the backfill script is
            # the place for full coverage.
            similar_targets = [a for a in (track.get("artists") or []) if a.get("id")][
                :NOW_FETCH_MAX_SIMILAR_ARTISTS
            ]
            if similar_targets:
                known_similar = store.lastfm_artist_map()
                to_fetch = [
                    (a["id"], a.get("name") or "") for a in similar_targets
                    if not isinstance(known_similar.get(a["id"]), dict)
                ]
                if to_fetch:
                    attempted = True
                    for aid, name in to_fetch:
                        # `LastFm.artist_similar`, unlike `enrich`/`top_tags`,
                        # does not wrap a non-Last.fm transport failure into
                        # `LastFmError` — so this catches broadly, exactly
                        # like `backfill_artist_similar.run_backfill`'s own
                        # per-artist loop. A failure here leaves this one
                        # artist's key absent (retried on a later force, once
                        # the floor allows) and never stops the rest of this
                        # batch — a bonus enrichment must never lose more
                        # progress than the single artist that actually failed.
                        try:
                            similar = fm.artist_similar(name)
                        except Exception:
                            log.warning(
                                "now-playing artist-similar fetch failed for %r", name,
                                exc_info=True,
                            )
                            continue
                        record = {
                            "name": name,
                            "similar": similar or [],
                            "fetched_at": _now_iso(),
                            "miss": similar is None,  # code 6 only
                        }
                        _merge_save_lastfm_artists({aid: record})
        except Exception:
            log.warning("now-playing tag/track fetch failed", exc_info=True)
        finally:
            if attempted:
                # Recorded for success and failure alike, and for either
                # fetch kind — the floor above is about "was an attempt
                # just made", not "did it work" or "which of the two
                # kinds". Runs even when the try block above raised.
                _now_fetch_last_attempt = clock()
        return fetched
    finally:
        _now_fetch_lock.release()


# ---- Deezer client (preview clips) ------------------------------------------
#
# Deezer is not Spotify — none of the budget ledger applies (its own limit is
# 50 req/5s per IP, far above anything the preview path can produce).

_deezer = None


def _deezer_client() -> Deezer:
    global _deezer
    if _deezer is None:
        _deezer = Deezer(timeout=5.0)
    return _deezer


# ---- picker hold-to-preview -------------------------------------------------
#
# Spotify's dev-mode API lost preview_url, so the audio bite comes from
# Deezer's free 30s clips instead — resolved live per hold (the CDN URLs
# carry expiring tokens, so nothing here is persisted to disk). Track
# PICKING is a pure cache.json read: zero Spotify calls on this path, ever.

PREVIEW_CLIPS = 3          # clips per medley
PREVIEW_MAX_ATTEMPTS = 6   # Deezer searches per resolve before giving up
PREVIEW_LIST_N = 10        # text-fallback tracks in the payload
PREVIEW_TTL = 600          # seconds a resolved medley is served from memory
_preview_cache: dict[str, tuple[float, dict]] = {}
_preview_lock = threading.Lock()


def _preview_page(candidates: list[dict], offset: int, total: int) -> dict:
    """One medley page over `candidates` (track dicts with a name and a
    first artist, in play order), from candidate `offset`: up to
    PREVIEW_CLIPS Deezer clips, trying at most PREVIEW_MAX_ATTEMPTS songs.
    Shared by the playlist medley and a split part's player. Call with
    `_preview_lock` held; caching is the caller's."""
    rejects = store.preview_rejects()
    clips = []
    consumed = 0
    for t in candidates[offset:offset + PREVIEW_MAX_ATTEMPTS]:
        if len(clips) >= PREVIEW_CLIPS:
            break
        consumed += 1
        artist = t["artists"][0]["name"]
        # What the user has already marked as the wrong recording for
        # this track. Passed INTO the search so it reaches the next
        # candidate rather than filtering the answer away afterwards.
        skip = (rejects.get(t.get("uri")) or {}).get("rejected") or []
        try:
            rec = _deezer_client().fetch_preview(artist, t["name"], exclude=skip)
        except Exception:
            log.warning("deezer preview failed for %r — skipping", t["name"])
            continue
        if not rec.get("miss"):
            # `uri` and `deezer_id` are what let the player say WHICH
            # recording was wrong when the user marks it — without them
            # a reject could only name a title, which is the one thing
            # already known to be ambiguous.
            clips.append({"name": t["name"], "artist": artist, "url": rec["url"],
                          "uri": t.get("uri"), "deezer_id": rec.get("deezer_id")})
    nxt = offset + consumed
    return {
        "clips": clips,
        "next_offset": nxt if nxt < len(candidates) else None,
        "tracks": [{"name": t["name"], "artist": t["artists"][0]["name"]}
                   for t in candidates[:PREVIEW_LIST_N]],
        "total": total,
    }


def _previewable(t: dict) -> bool:
    return bool(t.get("name") and (t.get("artists") or [{}])[0].get("name"))


@app.get("/api/playlist_preview/{playlist_id}")
def playlist_preview(playlist_id: str, offset: int = 0) -> dict:
    """One PAGE of a hold-to-preview medley: up to PREVIEW_CLIPS Deezer 30s
    clip URLs over the playlist's cached tracks, newest additions first
    (they jog memory best), starting at candidate `offset`. `next_offset`
    is the cursor for the next page, or None when the playlist is walked —
    the client chains pages for as long as the user keeps holding, so the
    medley's length is controlled by the thumb, not by a setting. Also
    carries a text fallback list so the hold always answers even when
    Deezer resolves nothing. Pages are cached for PREVIEW_TTL (repeated
    holds cost zero Deezer requests); a Deezer error just skips that track.
    The audio itself is never proxied here — the browser streams straight
    from Deezer's CDN.
    """
    entry = store.cache().get("playlists", {}).get(playlist_id)
    if not isinstance(entry, dict):
        raise HTTPException(404, "playlist not cached — open it once in sortify first")
    with _preview_lock:
        cached = _preview_cache.get((playlist_id, offset))
        if cached and time.monotonic() - cached[0] < PREVIEW_TTL:
            return cached[1]
        candidates = sorted(
            (t for t in entry.get("tracks") or [] if _previewable(t)),
            key=lambda t: t.get("added_at") or "", reverse=True,
        )
        payload = _preview_page(candidates, offset, len(entry.get("tracks") or []))
        _preview_cache[(playlist_id, offset)] = (time.monotonic(), payload)
        return payload


@app.get("/api/splitparts/{pid}/preview")
def splitparts_preview(pid: str, key: str, offset: int = 0, start: str | None = None) -> dict:
    """The house player over one part of a split: the part's songs in the
    order the part lists them, from the tapped song (`start`) on the first
    page. Local files and songs without a name or artist are left out. Zero
    Spotify calls — the songs come from the draft. A page is cached under
    the part's exact song list, so a song moved out of the part can never
    come back from a page resolved before the move."""
    draft = spa.get_draft(store, pid)
    if draft is None:
        raise HTTPException(404, "no split in progress for this playlist")
    part = next((p for p in draft["parts"] if p["key"] == key), None)
    if part is None:
        raise HTTPException(404, f"no part {key!r} in this split")
    by_uri = {t["uri"]: t for t in draft.get("tracks") or []}
    candidates = [by_uri[u] for u in part["uris"]
                  if u in by_uri and not by_uri[u].get("is_local") and _previewable(by_uri[u])]
    if not offset and start:
        # A tapped song that cannot play (a local file, no name or artist)
        # starts the walk at the next one that can, in the part's order.
        pos = {u: i for i, u in enumerate(part["uris"])}
        at = pos.get(start)
        offset = next((i for i, t in enumerate(candidates)
                       if at is not None and pos[t["uri"]] >= at), 0)
    digest = hashlib.sha1("\n".join(part["uris"]).encode()).hexdigest()
    cache_key = ("split", pid, key, offset, digest)
    with _preview_lock:
        now = time.monotonic()
        cached = _preview_cache.get(cache_key)
        if cached and now - cached[0] < PREVIEW_TTL:
            return cached[1]
        payload = _preview_page(candidates, offset, len(part["uris"]))
        now = time.monotonic()   # resolving the page took Deezer round trips
        # Every move makes new keys, so the old pages are let go here.
        for k in [k for k, (at_, _) in _preview_cache.items() if now - at_ >= PREVIEW_TTL]:
            del _preview_cache[k]
        _preview_cache[cache_key] = (now, payload)
        return payload


class ExploreIn(BaseModel):
    uri: str
    # Sent by the client from the card it is already showing, the same way
    # /api/preview_reject takes the names it stores. Nothing is fetched to
    # confirm them: this is a record of a press, not of Spotify's catalogue.
    artist: str = ""
    artist_id: str | None = None
    title: str = ""


@app.post("/api/explore")
def explore(body: ExploreIn):
    """Resolve the Explore artist button's playlist, creating it once.

    Two jobs, both of which have to happen before the add: make sure there
    IS a playlist (the first press creates it — one Spotify call — names it
    as a subset (🐾) so it can never become a filing home, and writes its id back
    into `quick_adds`), and record the artist in data/explore.json, which
    is what the exploring still to come will read.

    The add itself is not done here: the client sends it through /api/act
    like every other subset add, so the undo stack and the "a subset add
    does not spend the song's decision" guard keep working unchanged.
    """
    cfg = store.config()
    entry = quickadds.entry_for(cfg, "explore")
    if entry is None:
        raise HTTPException(
            400, "no explore button is configured — add one to `quick_adds` "
                 "in data/config.json")
    pid = entry.get("playlist_id")
    created = False
    name = entry.get("create_name") or ""
    if not pid:
        if not name:
            raise HTTPException(
                400, "the explore button has neither a playlist nor a name to "
                     "create one with")
        name = roles.mark_subset_name(name)
        pid, snapshot = sp.create_playlist_full(name)
        created = True
        sp.remember_playlist({
            "id": pid, "name": name,
            "owner": (store.cache().get("me") or {}).get("id"),
            "editable": True, "total": 0,
            "snapshot_id": snapshot or f"created:{pid}",
            "image": None, "description": "",
        })
        cfg = store.config()
        store.update_config(
            quick_adds=quickadds.with_target(cfg, "explore", pid),
        )
        _profile_state.clear()
        _profile_state["built_at"] = 0.0
    else:
        listed = next((p for p in (store.cache().get("playlist_list") or {}).get("items") or []
                       if p["id"] == pid), None)
        name = (listed or {}).get("name") or name

    store.save_explore(quickadds.note_artist(
        store.explore(), body.artist_id, body.artist or None, body.uri))
    return {"playlist_id": pid, "name": name, "created": created}


class PreviewRejectIn(BaseModel):
    uri: str
    deezer_id: int
    # Stored, never matched on: the file is meant to be readable by whoever
    # opens it to correct an entry by hand.
    artist: str = ""
    title: str = ""


@app.post("/api/preview_reject")
def preview_reject(body: PreviewRejectIn) -> dict:
    """"That is not the song" — mark a Deezer recording wrong for this track.

    Costs nothing upstream: it writes a local file and drops some memoised
    pages. The next resolve for this track searches again with the id
    excluded, which usually lands on the recording the user wanted, since
    the free-text fallback that returns a remix normally has the original
    sitting right behind it.
    """
    rejects = store.preview_rejects()
    entry = rejects.setdefault(
        body.uri, {"artist": body.artist, "title": body.title, "rejected": []})
    # Names can improve (an empty one filled in by a later mark); the list is
    # the part that must only ever grow, and never with duplicates.
    if body.artist:
        entry["artist"] = body.artist
    if body.title:
        entry["title"] = body.title
    if body.deezer_id not in entry["rejected"]:
        entry["rejected"].append(body.deezer_id)
    store.save_preview_rejects(rejects)
    # Without this the rejected clip keeps playing for the rest of
    # PREVIEW_TTL — ten minutes of the app ignoring what it was just told.
    # Every page is dropped, not just the one holding this track: pages are
    # keyed by (playlist, offset) and the track can sit in any of them, in
    # any playlist. They are pure memoisation of a free API, so rebuilding
    # them costs a Deezer search and nothing else.
    with _preview_lock:
        _preview_cache.clear()
    return {"ok": True, "rejected": entry["rejected"]}


# On phones the OS gives the preview audio focus and pauses the Spotify app;
# it does not resume on its own. This endpoint spends exactly ONE budgeted
# call to un-pause, explicitly user-caused (the preview gesture), debounced
# client-side per preview session and floored here so a burst of holds can
# never turn into a burst of playback calls.
PREVIEW_RESUME_MIN_INTERVAL = 5.0
_preview_resume_last = -1e9


@app.post("/api/preview_resume")
def preview_resume() -> dict:
    global _preview_resume_last
    now = time.monotonic()
    if now - _preview_resume_last < PREVIEW_RESUME_MIN_INTERVAL:
        return {"ok": False, "error": "resume already sent"}
    _preview_resume_last = now
    try:
        sp.resume_playback()
    except Exception as e:
        # No active device / already playing / cooldown — the preview flow
        # must never surface a hard failure over a convenience resume.
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _idle_inputs_payload() -> list[dict]:
    """Inputs for the not-playing state's "start an input…" CTA — names only.

    Deliberately from config plus the cached listing, NOT _ensure_profiles:
    the idle branch of a polling endpoint must never fetch a missing listing
    upstream, and must never trade "nothing playing" for the >40-homes 400
    a profile build is allowed to raise. An empty cache just means no CTA.
    """
    cache = store.cache()
    items = (cache.get("playlist_list") or {}).get("items") or []
    cached = cache.get("playlists") or {}
    by_id = {p["id"]: p for p in items}
    out = []
    for iid in sorted(_effective_input_ids(store.config(), items)):
        if iid == LIKED_ID:
            # No count: the library's size is not in the listing, and reading
            # it is a call this branch is not allowed to make.
            out.append({"id": iid, "name": "Liked Songs", "has_track": False,
                        "total": None})
        elif iid in by_id:
            # The listing's own total, NOT a membership set — there is none to
            # count here, and building one is exactly what this branch must
            # never do. Same number in practice, staler by a refresh. Less the
            # unplayable songs the cached track list already knows of (a disk
            # read, no call), so it matches the playing switcher's count.
            total = by_id[iid].get("total")
            entry = cached.get(iid)
            if isinstance(total, int) and isinstance(entry, dict):
                total = max(0, total - len({t["uri"] for t in entry.get("tracks") or []
                                            if _unplayable(t)}))
            out.append({"id": iid, "name": by_id[iid]["name"], "has_track": False,
                        "total": total})
    return out


def _now_sortable(track: dict) -> bool:
    return bool(track["type"] == "track" and not track["is_local"] and track.get("id"))


def _light_context(ctx_id: str | None) -> dict | None:
    """Context for the light payload — cached listing + config only, zero API
    calls, the same discipline as `_idle_inputs_payload`: the light branch
    must never be able to trigger a profile build (a missing-listing fetch,
    the >40-homes 400) just to name the playing context."""
    if not ctx_id:
        return None
    items = (store.cache().get("playlist_list") or {}).get("items") or []
    p = next((x for x in items if x["id"] == ctx_id), None)
    return {"id": ctx_id, "name": p["name"] if p else None,
            "is_input": ctx_id in _effective_input_ids(store.config(), items)}


def _home_folder_choices(cfg: dict, top_home_id: str | None) -> tuple[list[dict], str | None]:
    """The folders a new home can be filed into from the Now card, and which
    one to pre-select. 0 calls: folders.json only.

    The choices are the folders today's homes live in, and only those that
    are home folders by config (under `home_folder_prefixes`, no
    `home_folder_exclude` segment) — a sticky home filed somewhere else does
    not make that folder a place for new homes. One the desktop client's
    folder search cannot single out (subfolders, or a name inside another
    folder's) is listed with the reason instead of offered — the mover would
    refuse it after the create (foldermove._check_leaf_unique).
    Pre-selected: the top guess's folder, else the last folder used for
    homes; None when neither is an offered folder (the client then selects
    the top level, so nothing invisible applies).
    """
    folders = store.folders()
    every = folder_paths(folders)
    prefixes = cfg.get("home_folder_prefixes") or []
    excludes = cfg.get("home_folder_exclude") or []

    def home_folder(path: str) -> bool:
        if not prefixes:       # the ALL-CAPS fallback has no prefix to check
            return not any(seg.upper() in {e.upper() for e in excludes}
                           for seg in path.split(" / "))
        return bool(select_home_ids({"_": {"path": path}}, prefixes, excludes))

    homes = sorted(p for p in {(folders.get(h) or {}).get("path")
                               for h in cfg.get("home_ids") or []} - {None, ""}
                   if home_folder(p))
    choices = []
    for path in homes:
        hits = foldermove.leaf_collisions(every, path)
        choices.append({"path": path, "blocked": (
            f"the client's folder search would also offer {', '.join(hits[:3])} — "
            "file this one by hand") if hits else None})
    ok = {c["path"] for c in choices if not c["blocked"]}
    for cand in ((folders.get(top_home_id) or {}).get("path") if top_home_id else None,
                 (cfg.get("create_folders") or {}).get("home")):
        if cand in ok:
            return choices, cand
    return choices, None


# A song playing from an inbox that our copy of the inbox does not hold is
# either autoplay or a song added in Spotify since the copy was read — the
# listing that would say so is only refreshed by hand. One snapshot read tells
# them apart (2026-10-22, "Splitting An Atom" in [FILTER Y]). Keyed by
# (inbox, song): once each, whatever the answer, so a poll never repeats it.
_context_rechecked: dict[tuple[str, str], float] = {}
_context_recheck_lock = threading.Lock()
CONTEXT_RECHECK_KEEP = 64


def _recheck_context(np: dict, state: dict) -> None:
    """Re-read the playing inbox when it may have changed under our copy.

    1 call (the snapshot) for a song the copy does not hold, plus the read
    (~1 per 100 songs) only when the snapshot moved. Updates the track cache,
    the listing and the profile's membership set, so the card's chips — and
    the drift banner that keys off them — see the song."""
    ctx = np.get("context_playlist_id")
    uri = (np.get("track") or {}).get("uri")
    if not ctx or not uri or ctx == LIKED_ID or ctx not in (state.get("input_ids") or ()):
        return
    entry = next((l for l in state.get("inputs") or [] if l["id"] == ctx), None)
    if entry is None or uri in entry["uris"]:
        return
    with _context_recheck_lock:
        if (ctx, uri) in _context_rechecked:
            return
        _context_rechecked[(ctx, uri)] = time.time()
        while len(_context_rechecked) > CONTEXT_RECHECK_KEEP:
            _context_rechecked.pop(next(iter(_context_rechecked)))
    try:
        snap = sp.snapshot(ctx)
        cached = (store.cache()["playlists"].get(ctx) or {}).get("snapshot_id")
        if not snap or snap == cached:
            return   # the song really is not in it: autoplay
        tracks = _cached_tracks(ctx, snap)
    except SpotifyError as e:
        log.info("now: recheck of %s skipped (%s)", ctx, e)
        return
    _sync_listing_snapshot(ctx, snap, total=len(tracks))
    with _profile_lock:
        for l in _profile_state.get("inputs") or []:
            if l["id"] == ctx:
                l["uris"] = {t["uri"] for t in tracks}
                l["unplayable"] = {t["uri"] for t in tracks if _unplayable(t)}


def _suggestion_payload(np: dict) -> dict:
    """The suggestion side of the now card — everything that needs profiles,
    tag maps, or CPU beyond naming the track. Shared verbatim by the one-shot
    /api/now and the two-phase /api/now/suggest so the pair cannot drift."""
    state = _ensure_profiles()
    _recheck_context(np, state)
    track = np["track"]
    sortable = _now_sortable(track)
    # Guard-on-read, fresh on every request — same reasoning as `triage`'s own
    # re-read below `_ensure_profiles`: `_profile_state`'s cached profiles are
    # a profile-build-time snapshot that can sit stale for up to PROFILE_TTL,
    # so an enrichment fetch just before this (or any other write to
    # tags.json) would otherwise be invisible for up to 10 minutes. Deliberate
    # freshness/cost trade-off: a local JSON read on every request (zero API
    # cost) buys always-current tags instead of leaning on the profile cache.
    # lastfm_track_map() gets the same treatment, and for the same reason —
    # the force-path fetch can land a brand-new neighbour/track-tag record
    # mid-poll.
    tag_artists = store.tag_artists()
    track_map = store.lastfm_track_map()
    artist_map = store.lastfm_artist_map()
    ctx_id = np["context_playlist_id"]
    ctx = next((p for p in state["playlists"] if p["id"] == ctx_id), None)
    suggestions = sugg.suggest(
        track, state["profiles"], tag_artists, track_map, artist_map,
        state.get("playlist_artists"),
    ) if sortable else []
    home_folders, home_folder_default = _home_folder_choices(
        store.config(), suggestions[0]["playlist_id"] if suggestions else None)
    return {
        "context": (
            {"id": ctx_id, "name": ctx["name"] if ctx else None,
             "is_input": ctx_id in state["input_ids"]}
            if ctx_id else None
        ),
        "suggestions": suggestions,
        "home_folders": home_folders,
        "home_folder_default": home_folder_default,
        "subset_targets": _subset_targets_payload(),
        "quick_adds": _quick_adds_payload(state, track["uri"]),
        "homes": _homes_payload(state),
        "inputs": [
            {"id": l["id"], "name": l["name"], "has_track": track["uri"] in l["uris"],
             "set": l.get("set", inputsets.DEFAULT_KEY),
             # Free, and live: `uris` is the membership set `_sync_membership`
             # updates on every filing, so the switcher's count falls as the
             # inbox empties instead of waiting out PROFILE_TTL. Deduped by
             # construction, which is why it can differ by a track or two from
             # the listing's `total` on a playlist holding the same song twice.
             # Songs that can no longer be played are left out: the count is
             # what there is to work through, and the Spotify app hides them.
             "total": len(l["uris"] - l.get("unplayable", set()))}
            for l in state["inputs"]
        ],
        "homeless_id": _homeless_id(state),
    }


def _homeless_id(state: dict) -> str | None:
    """The "no home fits this" destination, or None if there isn't a live one.

    Config names it by id rather than by name: `[Homeless]` is a buffer input
    only because of what it happens to be called, and a rename must move the
    playlist between sets — not silently redirect the button. Which is also
    why `needs_home_id` is still read: the concept was renamed to Homeless in
    the UI and the config key followed, but a config written before that
    rename still names the same playlist and must keep working.

    Withheld unless it resolves as an input right now. A deleted or renamed
    destination would otherwise reach the client as a button that 404s on the
    add, and this key is the only thing between the two.
    """
    cfg = store.config()
    nid = cfg.get("homeless_id") or cfg.get("needs_home_id")
    if not nid:
        return None
    return nid if any(l["id"] == nid for l in state["inputs"]) else None


def _now_fetch_tags_safe(track: dict) -> None:
    try:
        _fetch_missing_now_tags(track)
    except Exception:
        # A fetch failure must never break the response it rides on — this is
        # a bonus enrichment, not the payload the client actually needs.
        log.exception("now-playing tag fetch failed")


@app.get("/api/now")
def now_playing(force: bool = False, light: bool = False):
    """`light=1` is the fast half of the two-phase card: just the track and
    context — no profile build, no Last.fm, no tag-map parsing.
    The client renders the card from it immediately and asks /api/now/suggest
    for the rest. The full (default) shape is unchanged."""
    try:
        np, stale_in = _currently_playing_shared(force=force)
    except SpotifyError as e:
        # Spotify answers 401 "Permissions missing" (or 403) when the token
        # predates the user-read-currently-playing scope.
        if e.status in (401, 403):
            return {"playing": False, "needs_reauth": True, "poll_after_ms": NOW_ERROR_POLL_MS}
        if e.status == 429:
            return {"playing": False, "cooldown": str(e), "poll_after_ms": NOW_ERROR_POLL_MS}
        raise
    if not np:
        return {"playing": False, "poll_after_ms": _poll_after_ms(stale_in),
                "fetched_ago_ms": _now_fetched_ago_ms(),
                "inputs": _idle_inputs_payload()}

    track = np["track"]
    sortable = _now_sortable(track)
    ctx_id = np["context_playlist_id"]
    base = {
        "playing": True,
        "poll_after_ms": _poll_after_ms(stale_in),
        "fetched_ago_ms": _now_fetched_ago_ms(),
        "is_playing": np["is_playing"],
        "progress_ms": np["progress_ms"],
        "track": {**track, "sortable": sortable},
        # None when the token cannot see it — the toggle draws "unknown"
        # rather than guessing "off" (see Spotify.currently_playing).
        "repeat": np.get("repeat"),
    }
    if light:
        # Even `force=1` spends nothing extra here: force is forwarded to the
        # suggest phase by the client, where the Last.fm gate has moved.
        #
        # The quick adds ride along because they are furniture: which
        # playlists they point at comes from the config and the cached
        # listing, never from a profile build. Sending them only with the
        # suggestion half meant the first card of a session drew its buttons
        # in pieces — the tiles said "rows are coming", and the row of
        # destinations underneath appeared a second later as if it were an
        # answer too. Same discipline as _light_context: cached listing only.
        cached_items = (store.cache().get("playlist_list") or {}).get("items") or []
        return {**base, "light": True, "context": _light_context(ctx_id),
                "quick_adds": _quick_adds_payload({"playlists": cached_items},
                                                  track.get("uri"))}
    # A targeted artist fetch used to sit here to sharpen the card's genre
    # reasons; it cost a call per unseen artist to learn nothing, since the
    # dev-mode API stopped returning Spotify genres at all. `_fetch_missing_now_tags`
    # is that fetch's replacement — Last.fm instead of Spotify, bounded, and
    # gated on `force` so the passive poll still spends nothing.
    if force and sortable:
        _now_fetch_tags_safe(track)
    return {**base, **_suggestion_payload(np)}


@app.get("/api/now/suggest")
def now_suggest(force: bool = False):
    """Phase two of the card: suggestions for the track the now-cache already
    holds. Never calls Spotify's currently-playing itself — the light poll
    that preceded it owns that call — so a stale or empty cache just answers
    `playing: false` and the client's next poll sorts it out."""
    with _now_lock:
        np = _now_cache["value"] if _now_cache["at"] else None
    if not np:
        return {"playing": False}
    track = np["track"]
    # The same gate the one-shot endpoint has always had, relocated: only an
    # explicit user action (`force=1`) may spend bounded Last.fm calls.
    if force and _now_sortable(track):
        _now_fetch_tags_safe(track)
    return {"playing": True, "track_uri": track["uri"], **_suggestion_payload(np)}


# ---- playback control ------------------------------------------------------


def _playback_call(fn, *args) -> dict:
    """Run a playback call, translating the two failures that actually happen.

    Both are ordinary states rather than faults — a token issued before the
    playback scope existed, and nothing currently playing anywhere — and both
    are actionable, so neither should reach the user as a raw Spotify error.
    """
    try:
        fn(*args)
    except SpotifyError as e:
        if e.status in (401, 403):
            raise HTTPException(
                400,
                "Spotify hasn't granted playback control yet — log in again "
                "(the scope is new) and retry.",
            )
        if e.status == 404:
            raise HTTPException(
                400,
                "No active Spotify device — start playing something in Spotify first.",
            )
        raise
    # The now-cache predicts what Spotify would say; we just made that
    # prediction wrong, and force=1 alone would not help — it still serves the
    # cache inside NOW_FORCE_MIN_INTERVAL.
    with _now_lock:
        _now_cache["at"] = 0.0
    return {"ok": True}


@app.post("/api/player/next")
def player_next():
    # Remember what we skipped away from BEFORE the cache is dropped: if the
    # settle-poll still gets this uri back, it must not be cached for the old
    # track's remaining runtime (see NOW_SKIP_SETTLE_TTL).
    with _now_lock:
        prev_uri = ((_now_cache["value"] or {}).get("track") or {}).get("uri")
        cached, cached_at, cached_ttl = _now_cache["value"], _now_cache["at"], _now_cache["ttl"]
    out = _playback_call(sp.skip_next)
    # The skip counter's exact reading: where the song is now, extrapolated
    # from the cached answer (only ever too far along, so it can only
    # undercount). An expired answer says nothing — the song may have played
    # out and been replaced since.
    now = time.time()
    if cached and cached_at and now - cached_at <= cached_ttl:
        progress = cached.get("progress_ms")
        if progress is not None and cached.get("is_playing"):
            progress += (now - cached_at) * 1000
        _skip_observe(cached, now, progress, left=True)
    if prev_uri:
        with _now_lock:
            _skip_settle.update(uri=prev_uri, until=time.time() + NOW_SKIP_SETTLE_WINDOW)
    return out


@app.post("/api/player/previous")
def player_previous():
    # Mirrors player_next, settle window included: Spotify can report the
    # pre-press track for a moment in either direction.
    with _now_lock:
        prev_uri = ((_now_cache["value"] or {}).get("track") or {}).get("uri")
    out = _playback_call(sp.skip_previous)
    if prev_uri:
        with _now_lock:
            _skip_settle.update(uri=prev_uri, until=time.time() + NOW_SKIP_SETTLE_WINDOW)
    return out


@app.post("/api/player/seek")
def player_seek(body: SeekIn):
    """Move within the playing track — one call, and no poll behind it.

    `_playback_call` drops the now-cache, which is right for a skip (nobody
    can predict the next track) and wrong here: the only thing that changed is
    a number we chose ourselves. So the answer goes back with the new position
    and a TTL recomputed from it, and the client re-anchors its own ticker the
    same way. A seek therefore costs exactly the one call it makes, and the
    poll schedule — whose whole basis is the track's remaining runtime — stays
    honest across it.
    """
    if body.position_ms < 0:
        raise HTTPException(400, "position_ms must not be negative")
    with _now_lock:
        before = _now_cache["value"]
    out = _playback_call(sp.seek, body.position_ms)
    if before and before.get("track"):
        patched = {**before, "progress_ms": body.position_ms}
        with _now_lock:
            _now_cache.update(value=patched, at=time.time(), ttl=_now_ttl(patched))
    return out


REPEAT_STATES = ("off", "context", "track")


@app.post("/api/player/repeat")
def player_repeat(body: RepeatIn):
    """Loop the list, loop the song, or neither.

    Like seeking, the answer is known in advance — we are the ones setting it
    — so the now-cache is repositioned rather than dropped. Dropping it would
    make a press of the loop button cost the poll behind it as well, and the
    poll would only tell us the thing we just said.
    """
    if body.state not in REPEAT_STATES:
        raise HTTPException(400, f"repeat state must be one of {', '.join(REPEAT_STATES)}")
    with _now_lock:
        before = _now_cache["value"]
        ttl = _now_cache["ttl"]
        at = _now_cache["at"]
    out = _playback_call(sp.set_repeat, body.state)
    if before and before.get("track"):
        with _now_lock:
            # `at` and the TTL are left exactly as they were: repeat changes
            # nothing about how long the rest of the answer stays true.
            _now_cache.update(value={**before, "repeat": body.state}, at=at, ttl=ttl)
    return out


@app.post("/api/player/pause")
def player_pause():
    return _playback_call(sp.pause_playback)


@app.post("/api/player/resume")
def player_resume():
    return _playback_call(sp.resume_playback)


@app.post("/api/player/play")
def player_play(body: PlayIn):
    if body.input_id == LIKED_ID:
        # Liked Songs has no playlist id, so there is no context_uri for it.
        raise HTTPException(400, "Liked Songs can't be started as a playlist — play it from Spotify.")
    return _playback_call(sp.play_context, body.input_id)


# ---- tablet share (Spotify Messages via the Android tablet) ----------------
# No Web API endpoint exists for Messages and the desktop client has no
# Messages UI, so this drives the tablet's Spotify app over adb
# (sortify/tabletshare.py). Zero Spotify Web API quota.


class ShareIn(BaseModel):
    # title+artist, not a track id: the tablet flow enters via the search
    # deep link, because any track VIEW intent autoplays and steals the
    # user's playback via Connect (measured live 2026-08-24).
    title: str
    artist: str
    friend: str


def _share_cache() -> dict:
    path = store.dir / "tabletshare.json"
    if not path.exists():
        return {"targets": [], "updated": None, "aliases": {}}
    data = json.loads(path.read_text())
    data.setdefault("aliases", {})
    return data


@app.post("/api/share/track")
def share_track_endpoint(body: ShareIn):
    # The picker speaks display names ("Mara"); the tablet's share sheet
    # only knows the raw thread names, so resolve before driving.
    aliases = _share_cache()["aliases"]
    friend = tabletshare.resolve_target(body.friend, aliases)
    try:
        targets = tabletshare.share_track(body.title, body.artist, friend)
    except tabletshare.UiStepError as e:
        # The access log alone shows only the request line on a 502; this
        # line is the difference between forensics and a blind rerun.
        log.warning("tablet share failed at: %s", e)
        raise HTTPException(502, str(e))
    # targets is the raw truth from the sheet; aliases ride along so a
    # cache refresh never loses the user's renames.
    (store.dir / "tabletshare.json").write_text(json.dumps(
        {"targets": targets, "updated": int(time.time()), "aliases": aliases}))
    return {"ok": True, "targets": tabletshare.display_targets(targets, aliases)}


@app.get("/api/share/targets")
def share_targets_endpoint():
    data = _share_cache()
    return {"targets": tabletshare.display_targets(data["targets"], data["aliases"]),
            "updated": data["updated"]}


# ---- actions ---------------------------------------------------------------


def _cache_move(uri: str, from_ids: list[str] | None, to_id: str | None) -> dict | None:
    """Mirror a move in the local cache; returns the track dict if we had it.

    `from_ids` is a list because one decision can now empty several playlists
    at once (the input sweep, see `act`) — and they have to be mirrored in one
    read-modify-write, or each save would clobber the last one's removal.
    """
    cache = store.cache()
    track = None
    for pid in cache["playlists"]:
        for t in cache["playlists"][pid]["tracks"]:
            if t["uri"] == uri:
                track = t
                break
        if track:
            break
    for from_id in from_ids or []:
        if from_id in cache["playlists"]:
            entry = cache["playlists"][from_id]
            entry["tracks"] = [t for t in entry["tracks"] if t["uri"] != uri]
    if to_id and to_id in cache["playlists"]:
        if track:
            dest = cache["playlists"][to_id]["tracks"]
            if not any(t["uri"] == uri for t in dest):
                dest.append(track)
        else:
            # We no longer have the track dict; force a refetch next triage.
            cache["playlists"][to_id]["snapshot_id"] = None
    _count_move(cache, from_ids, to_id)
    store.save_cache(cache)
    return track


def _count_move(cache: dict, from_ids: list[str] | None, to_id: str | None) -> None:
    """Keep the listing's per-playlist `total` in step with a move.

    `total` is what every row in the app prints as "N tracks", and it lives
    in the cached LISTING — a different structure from the track cache this
    function's caller mirrors into. Nothing updated it, so the count was
    whatever the last Refresh read: an off-by-one on a 200-track home, and a
    conspicuous, permanent 0 on a playlist created inside sortify (reported
    2026-09-21 on a home that had just been filed into).

    Rides inside `_cache_move`'s read-modify-write so the count cannot be
    saved separately from the tracks it counts.

    One track per call, deliberately. A removal takes every copy of the uri
    with it (the API deletes by uri, not by position), so a playlist holding
    the same song twice drifts by one here — the next Refresh reconciles it
    against the real listing, which is what that button is for.
    """
    by_id = {p["id"]: p for p in (cache.get("playlist_list") or {}).get("items") or []}
    for from_id in from_ids or []:
        entry = by_id.get(from_id)
        if entry is not None and isinstance(entry.get("total"), int):
            entry["total"] = max(0, entry["total"] - 1)
    entry = by_id.get(to_id) if to_id else None
    if entry is not None and isinstance(entry.get("total"), int):
        entry["total"] += 1


def _apply_snapshot(pid: str, snapshot_id: str | None) -> None:
    if not snapshot_id:
        return
    cache = store.cache()
    if pid in cache["playlists"]:
        cache["playlists"][pid]["snapshot_id"] = snapshot_id
        store.save_cache(cache)


@app.post("/api/act")
def act(body: ActIn):
    # Filing into a subset is not filing. A song put into a best-of still
    # needs its home, so it must not leave the input it came from — and that
    # rule belongs here, where every caller passes, rather than in whichever
    # button happens to be current.
    #
    # Keyed on the same name rule as the picker (`_effective_subset_ids`), so
    # what the picker offers and what this guard covers cannot drift apart.
    #
    # Reads the cached listing directly rather than `sp.my_playlists()`,
    # which fetches (~21 paginated calls, ~60s stall) when
    # `cache["playlist_list"]` is absent — e.g. right after a cache wipe.
    # With nothing cached there is nothing to check against, so the guard
    # is skipped rather than paying that cost to enforce it. Genuinely
    # costs nothing only with a warm cache; degrades to no-guard, not a
    # fetch, when cold.
    if body.to_id and body.from_id:
        listing = (store.cache().get("playlist_list") or {}).get("items") or []
        if body.to_id in _effective_subset_ids(store.config(), listing):
            raise HTTPException(
                400,
                "that destination is a subset — adding to a subset must not "
                "remove the track from its input; send from_id: null",
            )
    # The sweep. One decision — filed, or rejected — answers the question for
    # the song, so the song leaves every inbox holding it and not merely the
    # one that happened to be playing. Without it a copy in another buffer
    # came round in a later session and had to be decided a second time.
    #
    # Rare and therefore cheap: 29 of 2244 cached input tracks sat in more
    # than one input when this was written, one of them in three. It is a
    # no-op for the other 98.7%, and there is no batch delete, so each extra
    # input costs one call.
    sweep = _sweep_targets(body.uri, exclude=body.to_id, primary=body.from_id) \
        if body.sweep_inputs else []
    note = None
    if body.action == "move":
        if not body.to_id:
            raise HTTPException(400, "move needs to_id")
        dest = store.cache()["playlists"].get(body.to_id, {}).get("tracks", [])
        added = not any(t["uri"] == body.uri for t in dest)
        if added:
            if body.to_id == LIKED_ID:
                sp.save_to_liked(body.uri)
            else:
                _apply_snapshot(body.to_id, sp.add_to_playlist(body.to_id, body.uri))
        else:
            note = "already in destination" + (" — removed from input only" if body.from_id else "")
        if body.from_id:
            _apply_snapshot(body.from_id, sp.remove_from_playlist(body.from_id, body.uri))
        _sweep_remove(body.uri, sweep)
        _cache_move(body.uri, _from_ids(body.from_id, sweep), body.to_id)
        undo_stack.append(
            {"uri": body.uri, "from_id": body.from_id, "to_id": body.to_id, "added": added,
             "also_from": [l["id"] for l in sweep]}
        )
    elif body.action == "remove":
        if not body.from_id:
            raise HTTPException(400, "remove needs from_id")
        _apply_snapshot(body.from_id, sp.remove_from_playlist(body.from_id, body.uri))
        _sweep_remove(body.uri, sweep)
        _cache_move(body.uri, _from_ids(body.from_id, sweep), None)
        undo_stack.append({"uri": body.uri, "from_id": body.from_id, "to_id": None,
                           "added": False, "also_from": [l["id"] for l in sweep]})
    else:
        raise HTTPException(400, f"unknown action {body.action!r}")
    _sync_membership(body.uri, add_to=body.to_id,
                     remove_from=_from_ids(body.from_id, sweep))
    del undo_stack[:-20]
    # `swept` names the EXTRA lists only — the one the user was looking at is
    # already in their toast. Naming them is the point: a delete from a
    # playlist that was not on screen must never be silent.
    return {"ok": True, "note": note, "can_undo": True,
            "swept": [l["name"] for l in sweep]}


def _from_ids(from_id: str | None, sweep: list[dict]) -> list[str]:
    """Every playlist one act request removed from, primary first."""
    return ([from_id] if from_id else []) + [l["id"] for l in sweep]


def _sweep_targets(uri: str, exclude: str | None, primary: str | None) -> list[dict]:
    """The inputs a sweep should empty: every one holding `uri`, minus the one
    already being removed from and minus the destination.

    Excluding the destination is not a nicety. The Homeless button files INTO
    an input, so a sweep blind to `to_id` would delete the track the same
    request had just added — and report success.

    Liked Songs is never swept even when it is configured as an input — not
    because it cannot be (`remove_from_playlist` special-cases LIKED_ID into
    a DELETE /me/library and would work fine) but because it should not be.
    Unliking is a bigger and more surprising act than emptying an inbox, and
    the user asked for inboxes.

    Reads the in-memory input membership — the same set the card's capture
    chips render from, kept current by `_sync_membership` — so consecutive
    sweeps see each other's work without a profile rebuild.
    """
    return [l for l in _profile_state.get("inputs", [])
            if l["id"] not in (exclude, primary, LIKED_ID) and uri in l["uris"]]


def _sweep_remove(uri: str, sweep: list[dict]) -> None:
    """One DELETE per swept input — there is still no batch delete."""
    for l in sweep:
        _apply_snapshot(l["id"], sp.remove_from_playlist(l["id"], uri))


def _sync_membership(uri: str, add_to: str | None, remove_from: list[str] | None) -> None:
    """Keep the in-memory membership sets current — inputs AND home profiles.

    Inputs alone used to be synced, and homes were left to the next profile
    rebuild. That is up to PROFILE_TTL (10 min) away, so for ten minutes after
    filing a song the server kept answering "no, that home doesn't hold it":
    `suggest`'s `already` flag reads `profiles[pid]["uris"]`, and that set is a
    build-time snapshot. The client hid it — the ✓ filed card is `filedUris` in
    app.js — right up until a reload wiped that, and the card came back
    offering the home the song had just been filed into as a fresh guess.

    A mirror, not an invalidation: dropping `built_at` would make the next
    poll rebuild every profile, and a rebuild re-reads any home whose snapshot
    has moved — which `_apply_snapshot` guarantees for the one just written
    to. That is a Spotify call per filed home, landing on the polling path.
    Two set operations cost nothing and say the same true thing.

    Only `uris` is mirrored. The scoring profile (artist and tag counts) stays
    at its build-time values: one track cannot meaningfully move a home's
    profile, and the next rebuild reconciles it from the real listing anyway.
    """
    gone = set(remove_from or [])
    # Out of these by our own hand: never a reason to re-check them while it
    # plays (see _recheck_context).
    with _context_recheck_lock:
        for pid in gone:
            _context_rechecked[(pid, uri)] = time.time()
    for l in _profile_state.get("inputs", []):
        if l["id"] == add_to:
            l["uris"].add(uri)
        if l["id"] in gone:
            l["uris"].discard(uri)
    profiles = _profile_state.get("profiles") or {}
    if add_to in profiles:
        profiles[add_to]["uris"].add(uri)
    for pid in gone:
        if pid in profiles:
            profiles[pid]["uris"].discard(uri)


@app.post("/api/undo")
def undo():
    if not undo_stack:
        raise HTTPException(400, "nothing to undo")
    entry = undo_stack.pop()
    if entry["to_id"] and entry["added"]:
        _apply_snapshot(entry["to_id"], sp.remove_from_playlist(entry["to_id"], entry["uri"]))
    # The sweep was ONE decision, so it costs one undo and has to be undone
    # whole: an undo that put the song back in a third of the inboxes it
    # emptied would be worse than no undo at all.
    also = entry.get("also_from") or []
    if entry["from_id"] == LIKED_ID:
        sp.save_to_liked(entry["uri"])
    elif entry["from_id"]:
        _apply_snapshot(entry["from_id"], sp.add_to_playlist(entry["from_id"], entry["uri"]))
    for pid in also:
        _apply_snapshot(pid, sp.add_to_playlist(pid, entry["uri"]))
    # If the move never added anything (track pre-existed in dest), the dest
    # cache must keep it on undo.
    _cache_move(entry["uri"], [entry["to_id"]] if entry["added"] else [], entry["from_id"])
    for pid in also:
        _cache_move(entry["uri"], [], pid)
    _sync_membership(
        entry["uri"],
        add_to=entry["from_id"],
        remove_from=[entry["to_id"]] if entry["added"] else [],
    )
    for pid in also:
        _sync_membership(entry["uri"], add_to=pid, remove_from=[])
    return {"ok": True, "restored_to": entry["from_id"]}


# ---- static ----------------------------------------------------------------

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


_ASSET_REF = re.compile(r"/static/(app\.js|style\.css)")

# The Claude remote-session button: tailnet only, never on a public page
# (standing rule; decided for sortify 2026-10-17).
_RC_BUTTON = re.compile(r'^<script src="[^"]*/rc-button\.js"[^>]*></script>\n', re.M)


def _index_response(public: bool) -> HTMLResponse:
    """index.html with each asset URL stamped by that file's mtime.

    An unversioned /static/app.js meant a tab opened before a deploy kept
    running the old script against the new server — which shows up as a
    feature simply not being there, while the server serves it happily. The
    stamp comes from the file rather than a hand-bumped constant, because the
    deploy where I forget to bump is the one that matters.

    The document itself must never be cached: versioned assets are safe to
    keep forever, but only if the page naming them is always re-fetched.
    """
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


def _bind(host: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    return s


def _sockets(host: str, port: int, public_port: int, secret: str | None) -> list[socket.socket]:
    """The tailnet socket always; the public one only with a usable secret,
    so a lost secret closes the public door instead of opening it.

    uvicorn.Server.startup() only logs its "Uvicorn running on ..." line
    when sockets is None (we always pass an explicit list), so without this
    an operator has no journal line saying which addresses actually came up.
    """
    tail = _bind(host, port)
    socks = [tail]
    tail_host, tail_port = tail.getsockname()[:2]
    log.info("sortify listening on %s:%s (tailnet)", tail_host, tail_port)
    if public_port:
        if gate.secret_ok(secret):
            try:
                pub = _bind(host, public_port)
            except OSError as e:
                # A bind failure (EADDRINUSE from a stray dev process, a
                # permissions error, ...) is unrelated to the tailnet
                # socket above, which is already open — the public route
                # failing to start is no reason to take it down too.
                log.error("public port %s NOT opened: %s", public_port, e)
            else:
                socks.append(pub)
                pub_host, pub_port = pub.getsockname()[:2]
                log.info("sortify listening on %s:%s (public, gated)", pub_host, pub_port)
        else:
            log.error("public port %s NOT opened: SORTIFY_GATE_SECRET missing or under %s bytes",
                      public_port, gate.SECRET_MIN)
    return socks


def main():
    # Config.__init__ calls configure_logging(), which is what raises the
    # "uvicorn.error" logger from its default WARNING to INFO. Build it
    # before _sockets() runs, or the "listening on ..." lines there are
    # filtered out and a successful start stays silent in the journal.
    config = uvicorn.Config(app)
    socks = _sockets(
        os.environ.get("SORTIFY_HOST", "0.0.0.0"),
        int(os.environ.get("SORTIFY_PORT", "8800")),
        PUBLIC_PORT,
        _gate_secret(),
    )
    uvicorn.Server(config).run(sockets=socks)


if __name__ == "__main__":
    main()
