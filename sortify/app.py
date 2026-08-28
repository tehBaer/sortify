"""FastAPI app: serves the web UI and orchestrates triage."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import rootlist
from . import suggest as sugg
from . import tabletshare
from .deezer import Deezer
from . import inputsets
from .folders import (
    creatable_home_name_problem,
    extract_folder_map,
    home_name_excluded,
    is_subset_name,
    select_home_ids,
)
from .naming import split_output_name, violations as naming_violations
from .pacing import Governor
from .spotify import (
    BACKGROUND_DAILY_CAP,
    BULK_RESERVE,
    DAILY_CAP,
    LIKED_ID,
    REDIRECT_URI,
    AuthFlowError,
    AuthNeeded,
    Spotify,
    SpotifyError,
)
from .split import (
    SITTING_DESCRIPTION,
    SITTING_PREFIX,
    UNTAGGED,
    is_sitting_playlist,
    pick_sitting,
    select_orphans,
    split_tracks,
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


class AuthStart(BaseModel):
    client_id: str


class ConfigIn(BaseModel):
    input_ids: list[str]
    home_ids: list[str] = []
    # {playlist_id: "ambient, piano"} — the user's own matching hints per
    # home, free text split on commas at profile-build time.
    home_hints: dict[str, str] = {}
    # Subsets that may suggest themselves. Opt-in: marking one is what earns
    # it a profile, and so the read that builds it.
    subset_ids: list[str] = []


class CreatePlaylistIn(BaseModel):
    name: str
    # Only "home" exists today; explicit rather than implied because the two
    # deferred roles (inputs, subsets) differ in exactly this field. (Spec §1.)
    role: str = "home"


class PlayIn(BaseModel):
    input_id: str


class ActIn(BaseModel):
    action: str  # "move" | "remove"
    uri: str
    from_id: str | None = None  # None = just add to the home, no removal
    to_id: str | None = None


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
    subsets = _effective_subset_ids(cfg, items)
    _sets = inputsets.resolve_sets(cfg)
    # One disk read + JSON parse for the whole listing, not one per playlist.
    # Against a real ~1000-playlist account with a splits.json that has grown
    # to hundreds of KB, calling `_split_summary` inside this loop (each call
    # doing its own `store.splits()`) turned every /api/playlists response
    # into a ~1.4s stall — felt on every Playlists-view open (nav-lists,
    # btn-back, btn-split-back, post-refresh). Zero Spotify calls either way;
    # this was a purely local-disk cost the user was still paying constantly.
    splits = store.splits()["splits"]
    hints = cfg.get("home_hints") or {}
    # `subset_name_pattern` is configurable server-side; the client used to
    # test its own hardcoded `/^\{.*\}$/`, which could silently disagree with
    # this. Emitting the eligibility check here makes the client's chip
    # gate observe the same live pattern this endpoint's own role resolution
    # (and `_effective_subset_ids`) already uses.
    subset_pattern = cfg.get("subset_name_pattern") or DEFAULT_SUBSET_PATTERN
    for p in out:
        p["folder"] = (folders.get(p["id"]) or {}).get("path")
        p["role"] = (
            "input" if p["id"] in inputs
            else "home" if p["id"] in cfg.get("home_ids", [])
            else "subset" if p["id"] in subsets
            else None
        )
        p["subset_eligible"] = bool(p.get("editable")) and is_subset_name(
            p.get("name") or "", subset_pattern)
        p["input_set"] = (
            inputsets.set_of(p["name"], p.get("folder"), _sets) or inputsets.DEFAULT_KEY
        ) if p["role"] == "input" else None
        p["split"] = _split_summary(p["id"], splits)
        p["hints"] = hints.get(p["id"], "")
    entry = store.cache().get("playlist_list") or {}
    # Leftover sittings, from the same listing already in hand — zero calls.
    # Surfaced here because this is the view whose Refresh button produces
    # that listing: the orphans a refresh reveals appear right next to it.
    return {"playlists": out, "fetched_at": entry.get("fetched_at"),
            "sitting_orphans": _find_sitting_orphans(splits)}


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


@app.post("/api/config")
def set_config(body: ConfigIn):
    # Opting subsets in is unbounded — ticking all 70 {} playlists and saving
    # would land ~90 paginated calls inside the very next /api/now poll (see
    # SUBSET_WARM_BUDGET above). Refuse here, where the user can act on it —
    # mark fewer, save, let them warm, then mark more — rather than silently
    # truncating the list they asked for.
    previously_marked = set(store.config().get("subset_ids") or [])
    newly_marked = set(body.subset_ids) - previously_marked
    uncached, cost = _subset_cold_cost(newly_marked)
    if cost > SUBSET_WARM_BUDGET:
        raise HTTPException(
            400,
            f"that marks {uncached} uncached subset{'s' if uncached != 1 else ''} — "
            f"warming them would cost ~{cost} Spotify calls, over the "
            f"{SUBSET_WARM_BUDGET}-call save budget. Mark fewer, save, let those "
            "warm on the next poll, then mark more.",
        )
    store.update_config(
        input_ids=body.input_ids, home_ids=body.home_ids,
        home_hints={k: v.strip() for k, v in body.home_hints.items() if v.strip()},
        subset_ids=sorted(set(body.subset_ids)),
        # A sticky role must still be revocable: Home toggled off in the
        # Playlists view drops the id here too, or the next folder ingest
        # would resurrect it. (Spec §2.)
        sticky_home_ids=sorted(
            set(store.config().get("sticky_home_ids") or []) & set(body.home_ids)
        ),
    )
    # Hints feed the tag profiles, which otherwise sit cached for PROFILE_TTL —
    # a save should be visible on the very next suggestion, not 10 min later.
    _profile_state.clear()
    _profile_state["built_at"] = 0.0
    return {"ok": True}


@app.post("/api/playlists/create")
def create_playlist_api(body: CreatePlaylistIn):
    """Create a home playlist from inside sortify. One Spotify call.

    Everything around the call is local bookkeeping: the listing entry
    (remember_playlist), a seeded empty track cache whose snapshot_id
    matches the listing's (else every profile rebuild refetches a playlist
    we know is empty — spec §3), the home + sticky role, and a profile
    cache clear so the new home is usable now, not in PROFILE_TTL.
    """
    if body.role != "home":
        raise HTTPException(400, f"unsupported role {body.role!r} — only homes can be created yet")
    cfg = store.config()
    problem = creatable_home_name_problem(
        body.name,
        input_pattern=cfg.get("input_name_pattern"),
        exclude_patterns=cfg.get("home_name_exclude_patterns") or [],
        exclude_emoji=bool(cfg.get("home_exclude_emoji_names")),
    )
    if problem:
        raise HTTPException(400, problem)
    name = body.name.strip()

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
    store.update_config(
        home_ids=sorted(set(cfg.get("home_ids") or []) | {new_id}),
        sticky_home_ids=sorted(set(cfg.get("sticky_home_ids") or []) | {new_id}),
    )

    # Same move as set_config after a hints save, same reason: usable on the
    # next request, not up to PROFILE_TTL later.
    _profile_state.clear()
    _profile_state["built_at"] = 0.0

    return {
        "playlist": {**item, "role": "home", "folder": None, "split": None, "hints": ""},
        "note": note,
    }


def _parse_hints(cfg: dict) -> dict[str, list[str]]:
    """config's `home_hints` ({id: "a, b"}) as {id: ["a", "b"]}, lowercased."""
    out = {}
    for pid, text in (cfg.get("home_hints") or {}).items():
        tags = [t.strip().lower() for t in str(text).split(",") if t.strip()]
        if tags:
            out[pid] = tags
    return out


def _effective_input_ids(cfg: dict, playlists: list[dict]) -> set[str]:
    """Explicitly marked inputs plus everything matching any input SET.

    Set rules (name patterns like ^\\[.+\\]$, or a folder segment such as
    THE BOMB) live in config so a stale browser tab saving roles can never
    un-mark the real inputs. See sortify/inputsets.py.
    """
    ids = set(cfg.get("input_ids", []))
    return ids | inputsets.matched_ids(playlists, store.folders(), cfg)


# The convention, when config does not say otherwise. Subsets are `{like
# this}` — a shape `home_name_exclude_patterns` already refuses, so a subset
# can never also be a home (pinned by tests/test_subsets.py).
DEFAULT_SUBSET_PATTERN = r"^\{.*\}$"

# Opting a subset in is a chip tap, not a bounded action like Home marking —
# so unlike homes (guarded structurally by the ">40 candidates" check below),
# nothing stops a user from ticking all 70 {} playlists and saving at once.
# Each uncached one costs ceil(total/100) calls on the very next profile
# rebuild, which for /api/now means inside a poll — the exact shape of the
# traffic that earned this project its multi-hour quota trips (see
# CLAUDE.md). 25 calls is comfortably under WINDOW_CAP's 12/60s while still
# covering several median-sized (~22-track) subsets in one save.
SUBSET_WARM_BUDGET = 25


def _subset_cold_cost(ids: set[str]) -> tuple[int, int]:
    """(how many of `ids` have no cached tracks, total ceil(total/100) calls).

    Reads only what's already local — the cached listing (for `total`) and
    the cached track cache (for what's already warm) — so computing this
    costs nothing itself. A playlist absent from the cached listing is
    treated as needing a full paginated read (total unknown -> 0 pages
    counted, but still flagged uncached), which only undercounts a playlist
    that hasn't even been listed yet — vanishingly rare and never the bulk
    of a 70-playlist tick-all.
    """
    cache = store.cache()
    cached_pids = set(cache.get("playlists") or {})
    listing = (cache.get("playlist_list") or {}).get("items") or []
    by_id = {p["id"]: p for p in listing}
    uncached = [pid for pid in ids if pid not in cached_pids]
    cost = sum(
        math.ceil(((by_id.get(pid) or {}).get("total") or 0) / 100)
        for pid in uncached
    )
    return len(uncached), cost


def _effective_subset_ids(cfg: dict, playlists: list[dict]) -> set[str]:
    """Subsets that may suggest themselves.

    Opt-in, unlike inputs: only ids the user marked count, and marking is
    what earns a profile (and so the read that builds it). Every other {}
    playlist stays filable by hand through the picker — see the spec's
    "opting in gates suggestion, not reach".

    A mark is dropped when the playlist is gone, not ours to edit, no longer
    {}-shaped, or has since become an input or a home. Those last two make
    the role exclusive in the one direction that matters: a stale
    `subset_ids` entry can never quietly turn a home into something else.
    """
    marked = set(cfg.get("subset_ids") or [])
    if not marked:
        return set()
    pattern = cfg.get("subset_name_pattern") or DEFAULT_SUBSET_PATTERN
    taken = _effective_input_ids(cfg, playlists) | set(cfg.get("home_ids") or [])
    return {
        p["id"] for p in playlists
        if p["id"] in marked
        and p["id"] not in taken
        and p.get("editable")
        and is_subset_name(p.get("name") or "", pattern)
    }


# ---- cache helpers ---------------------------------------------------------


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
    home_ids = cfg.get("home_ids") or []
    if home_ids:
        chosen = [p for p in playlists if p["id"] in home_ids and p["id"] not in input_ids]
    else:
        chosen = [p for p in playlists if p["editable"] and p["id"] not in input_ids]
    return [p for p in chosen if p["id"] != exclude]


# ---- home profiles (shared by triage and now-playing) ----------------------

PROFILE_TTL = 600  # rebuild at most every 10 min; snapshot cache does the rest
_profile_state: dict = {"built_at": 0.0}
_profile_lock = threading.Lock()


def _ensure_profiles(force: bool = False) -> dict:
    with _profile_lock:
        return _ensure_profiles_locked(force)


def _ensure_profiles_locked(force: bool) -> dict:
    now = time.time()
    if not force and _profile_state.get("profiles") and now - _profile_state["built_at"] < PROFILE_TTL:
        return _profile_state
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
    # therefore /api/now, a polling endpoint) on every rebuild. The
    # actionable 400 belongs only to the user-initiated split flow
    # (`_tag_artists_checked`).
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

    # Subsets: the same mechanism as homes, on an opt-in set. Snapshot-keyed
    # like every other cached read, so warm cost is zero; the first build
    # after opting one in pays ceil(total/100) calls for it, which is the
    # contract homes already have.
    subset_ids = _effective_subset_ids(cfg, all_playlists)
    # Backstop, mirroring the homes guard above: /api/config already refuses
    # a save that would cross SUBSET_WARM_BUDGET, but this mirrors that shape
    # here too so no other path to this function (a stale config written
    # some other way, a future caller) can reach the same unbounded cost.
    uncached, cost = _subset_cold_cost(subset_ids)
    if cost > SUBSET_WARM_BUDGET:
        raise HTTPException(
            400,
            f"{uncached} opted-in subsets are uncached — warming them would cost "
            f"~{cost} Spotify calls, over the {SUBSET_WARM_BUDGET}-call budget. "
            "Un-mark some subsets in the Playlists view, save, let the rest warm, "
            "then mark more.",
        )
    subsets = [p for p in all_playlists if p["id"] in subset_ids]
    subset_profiles = {
        s["id"]: sugg.build_profile(
            _cached_tracks(s["id"], s["snapshot_id"]), tag_artists
        )
        for s in subsets
    }

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
            "set": (inputsets.set_of(name, (store.folders().get(iid) or {}).get("path"),
                                     inputsets.resolve_sets(cfg))
                    or inputsets.DEFAULT_KEY),
        })

    _profile_state.update(
        built_at=now, profiles=profiles, homes=homes, inputs=inputs,
        playlists=all_playlists, input_ids=input_ids,
        playlist_artists=playlist_artists,
        last_added={hid: _last_added_at(tracks) for hid, tracks in home_tracks.items()},
        subset_profiles=subset_profiles, subsets=subsets,
    )
    return _profile_state


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


# Homes show three; a subset row appears in a "done, moving on" moment, and
# three more decisions there work against it. (Spec §3.)
SUBSET_TOP_N = 2


def _subset_matches(state: dict, track: dict, tag_artists: dict,
                    track_map: dict, artist_map: dict) -> list[dict]:
    """Subsets worth offering for this track. Local arithmetic, zero calls.

    The same scorer homes use, over the subset profiles — suggest.py is not
    modified for this. `artist_map` is threaded straight through (the same
    Last.fm similar-artist map the homes call gets) so subsets keep parity
    with homes rather than silently losing that signal. Two caller-side
    rules: no weak (sub-threshold) tier, because a guess about an optional
    selection is noise rather than pressure to decide; and at most
    SUBSET_TOP_N.
    """
    if not state.get("subset_profiles"):
        return []
    names = {s["id"]: s["name"] for s in state.get("subsets", [])}
    scored = sugg.suggest(
        track, state["subset_profiles"], tag_artists, track_map,
        artist_map, state.get("playlist_artists") or {},
    )
    out = []
    for s in scored:
        if s.get("weak"):
            continue
        out.append({
            "playlist_id": s["playlist_id"],
            "name": names.get(s["playlist_id"], "subset"),
            "pct": s["pct"],
            "already": s["already"],
            "reasons": s["reasons"],
        })
    return out[:SUBSET_TOP_N]


def _subset_targets_payload(state: dict) -> list[dict]:
    """Every editable {}-named playlist, opted in or not — the picker's list.

    Opting in gates whether a subset SUGGESTS itself; filing by hand reaches
    all of them, so this reads the listing rather than the opt-in set.
    """
    cfg = store.config()
    pattern = cfg.get("subset_name_pattern") or DEFAULT_SUBSET_PATTERN
    folders = store.folders()
    return [
        {"id": p["id"], "name": p["name"], "total": p.get("total"),
         "folder": (folders.get(p["id"]) or {}).get("path")}
        for p in state.get("playlists", [])
        if p.get("editable") and is_subset_name(p.get("name") or "", pattern)
    ]


@app.post("/api/refresh")
def refresh_profiles():
    """The Refresh button: the one place the playlist listing is re-read.

    Everything else runs off the cached list, so this has to invalidate it
    before rebuilding — otherwise the button would spin and change nothing.
    """
    before = sp.budget_spent()
    sp.my_playlists(refresh=True)
    state = _ensure_profiles(force=True)
    # This is the one user action that can burst: the listing itself, plus a
    # re-read of every home whose snapshot_id moved while the list sat frozen.
    # Report the cost rather than let it disappear into the ledger.
    return {
        "ok": True,
        "homes": len(state["homes"]),
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
    # Guard-on-read (see _ensure_profiles_locked) — triage must stay usable
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


# ---- splitting --------------------------------------------------------------


class SplitParams(BaseModel):
    resolution: float = Field(1.0, gt=0)
    min_pile: int = Field(15, ge=1)
    tag_floor: int = Field(10, ge=0)
    max_tags_per_artist: int = Field(8, ge=1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lastfm_client() -> LastFm | None:
    key = load_key()
    return LastFm(key) if key else None


def _foreign_playlist_error(name: str, owner: str | None) -> HTTPException:
    """403: raised both when the cached listing already shows a playlist
    isn't ours (before any Spotify call — see `create_split`'s pre-flight
    guard) and when a live read discovers the same thing (ownership changed,
    sharing revoked, or the cache is simply stale). Same actionable message
    either way, and — deliberately — a status distinct from the 502 a
    Spotify failure gets and the 502 a Last.fm failure gets, so this class of
    failure (nothing to retry, nothing transient, the fix is "make a copy")
    is never mistaken for either.
    """
    who = owner or "another Spotify user"
    return HTTPException(
        403,
        f'"{name}" belongs to {who}, not you. The Feb-2026 dev-mode API won\'t let '
        "sortify read another user's playlist tracks at all, so splitting it can never "
        'succeed here. Make your own copy first — in the Spotify app, right-click or '
        'long-press the playlist and choose "Add to your Library" (this duplicates it '
        "into a playlist you own) — then split that copy instead.",
    )


def _tag_artists_checked() -> dict:
    """`store.tag_artists()`, refusing a tags.json shape the splitter can't read.

    Version 1 stored tags pre-filtered, in a different shape than the raw
    Last.fm tags version 2 expects; feeding a v1 file to `split_tracks` fails
    deep inside as an unhelpful AttributeError. Catch the mismatch here, with
    an actionable message, before it gets that far.
    """
    envelope = store.tags()
    version = envelope.get("version")
    if version != TAGS_VERSION:
        raise HTTPException(
            400,
            f"data/tags.json is version {version!r}, but this build expects "
            f"version {TAGS_VERSION} (raw Last.fm tags, hygiene applied at "
            "split time). Move the old file aside and re-run the split.",
        )
    return store.tag_artists()


# Guards every save of tags.json against the lost-update race between two
# writers that both start from a read taken before their (slow) network work:
# the split flow's `enrich()` walk and the now-playing on-demand fetch below
# can each hold a snapshot that predates the other's save, and a bare
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


def _split_summary(playlist_id: str, splits: dict | None = None) -> dict | None:
    """Local read only, for the Playlists picker: pile count and how much of
    a previous split is still undecided, so a playlist someone already split
    doesn't look untouched. Zero Spotify calls — the picker is not the place
    to spend any.

    `splits` is `store.splits()["splits"]`, pre-fetched by the caller. Every
    caller iterating a whole playlist listing MUST pass it — calling
    `store.splits()` fresh per playlist re-reads and re-parses the same
    (potentially hundreds-of-KB) file once per playlist, which is exactly the
    /api/playlists regression a review round caught (~1.4s added on a real
    ~1000-playlist account). Defaults to a fresh read only for the rare
    single-playlist caller.
    """
    if splits is None:
        splits = store.splits()["splits"]
    split = splits.get(playlist_id)
    if not split:
        return None
    return {"piles": len(split["piles"]), "remaining": _remaining(split)}


def _pile_progress(split: dict, playlist_id: str) -> list[dict]:
    decided = split.get("decided", {})
    out = []
    for p in split["piles"]:
        plan = _materialise_plan(
            split, p, reconciled=(playlist_id, p["id"]) in _reconciled)
        out.append({**p, "decided": sum(1 for u in p["uris"] if u in decided),
                    "total": len(p["uris"]),
                    # What materialising this pile would spend RIGHT NOW, and
                    # what (if anything) already exists for it. The client
                    # displays this number; `_materialise_tick` is what
                    # actually spends it, one call at a time.
                    "materialise_calls": plan["calls"],
                    "materialised": plan["record_view"]})
    return out


def _remaining(split: dict) -> int:
    """How many track *occurrences* across every pile are still undecided.

    Deliberately per-pile-occurrence, like `_pile_progress` above, rather
    than `sum(len(p["uris"])) - len(decided)`: `decided` is keyed by uri, so
    a uri that occurs twice in the source (the same track added to the
    playlist twice — `split_tracks` does not deduplicate) would inflate the
    naive total by one occurrence that no single `decided` entry can ever
    account for, and a stale `decided` entry left behind by a re-split that
    dropped its uri from every current pile would deflate it — either way
    driving the count negative and never reaching 0. Counting per pile, per
    occurrence, self-corrects both: a decided uri counts as decided exactly
    as many times as it actually appears in the current piles, no more, no
    less.
    """
    decided = split.get("decided", {})
    return sum(
        len(p["uris"]) - sum(1 for u in p["uris"] if u in decided)
        for p in split["piles"]
    )


# Guards every read-modify-write of splits.json for a single playlist. Two
# concurrent requests (two tabs, a double-click) can otherwise both read the
# same "no active sitting" state and both pass a check-then-act guard before
# either has written anything — the lock makes each check-and-write atomic
# instead. Held only around local dict/disk work, never across a Spotify or
# Last.fm network call, so a slow Last.fm enrichment run cannot block a
# concurrent finish. One process, in-memory lock: sortify is a single-user
# LAN service with one server process, so this is sufficient without a
# file-level lock.
_split_lock = threading.Lock()

# uris (as (playlist_id, uri) pairs) with a keep currently being spent by
# THIS process — i.e. between the reservation write in `decide` and the
# Spotify call it's waiting on returning. Only ever read/mutated while
# holding `_split_lock`, so it needs no lock of its own. Its purpose is
# narrow: telling a genuinely-concurrent request in this same process
# ("still in flight, don't touch it") apart from a `"pending": True` entry
# left behind by a process that died mid-call ("nothing here anymore, this
# is retryable") — see `decide`'s docstring. A fresh process starts with
# this empty, which is exactly what makes every pre-existing pending entry
# correctly read as abandoned after a restart.
_pending_keeps: set[tuple[str, str]] = set()


# How long a split has got to, keyed by playlist id. Module state on purpose:
# the alternative — writing progress into splits.json — would be ~700 writes
# of a file that has reached 269 KB, per split. This is one small dict per
# playlist ever split in this process's lifetime, and sortify is a single
# process (one systemd unit, no uvicorn workers), so there is nothing to
# share it with. `create_split` is a sync `def`, so FastAPI runs it in a
# worker thread and the poll endpoint below is served concurrently with it.
_split_progress: dict[str, dict] = {}
_split_progress_lock = threading.Lock()

_IDLE_PROGRESS = {"state": "idle", "phase": None, "done": 0, "total": 0, "detail": None}

# Only ever read by the client while a split is actually running. 1s is far
# below the ~0.25s-per-artist the tagging phase moves at, and the endpoint it
# paces is a dict lookup under a lock — no disk, no network, nothing that can
# reach Spotify. That last part is the whole reason a poll is allowed here at
# all; see test_split_progress_spends_no_api_calls.
SPLIT_PROGRESS_POLL_MS = 1000


def _progress_begin(playlist_id: str) -> None:
    with _split_progress_lock:
        _split_progress[playlist_id] = dict(_IDLE_PROGRESS, state="running", phase="starting")


def _progress_set(playlist_id: str, **fields) -> None:
    """Merge into the live entry, leaving untouched fields alone — the failure
    path relies on this to keep the `done` count the last `on_progress` call
    published, which is what the resume message is built from."""
    with _split_progress_lock:
        entry = _split_progress.get(playlist_id)
        if entry is not None:
            entry.update(fields)


@app.post("/api/split/{playlist_id}")
def create_split(playlist_id: str, params: SplitParams = SplitParams()):
    """Read a playlist, tag its artists via Last.fm, cluster into piles.

    Progress reporting lives here rather than inside `_run_split` so that
    every exit — including the ones that refuse before any work starts, like
    the ownership pre-flight and the active-sitting guard — lands in a
    terminal state. A refusal has to be distinguishable from a run stuck at
    zero, or the progress bar answers "is it working?" with a shrug.
    """
    _progress_begin(playlist_id)
    try:
        result = _run_split(playlist_id, params)
    except HTTPException as e:
        # `detail` is the message the UI already shows for this failure, so
        # a poll that lands after the POST has rejected tells the same story
        # rather than a vaguer one.
        _progress_set(playlist_id, state="failed", detail=str(e.detail))
        raise
    except Exception as e:
        _progress_set(playlist_id, state="failed", detail=f"{type(e).__name__}: {e}")
        raise
    _progress_set(playlist_id, state="done", phase="complete")
    return result


@app.get("/api/split/{playlist_id}/progress")
def split_progress(playlist_id: str):
    """How far the split for this playlist has got. Costs nothing at all.

    Reads one module-level dict and nothing else — no `store` read (which
    could trigger a fetch), no Spotify call, no Last.fm call. That is the
    condition on which this endpoint is allowed to be polled on a timer:
    CLAUDE.md's rule is that a 6s client poll against a 5s cache once cost
    ~600 Spotify calls an hour from a single open tab, and this feature adds
    a poll running once a second for three minutes.

    A playlist with no run on record answers "idle" rather than 404, the same
    choice `queue_status` makes: the split view polls this before anything
    has ever been split, and that is the most frequent call of all.
    """
    with _split_progress_lock:
        entry = dict(_split_progress.get(playlist_id) or _IDLE_PROGRESS)
    # The client obeys this instead of choosing an interval of its own — the
    # same contract /api/now has. Zero means stop: a terminal run cannot
    # change again, and a client still polling one would be the orphaned
    # interval this feature is under orders not to create.
    entry["poll_after_ms"] = SPLIT_PROGRESS_POLL_MS if entry["state"] == "running" else 0
    return entry


def _run_split(playlist_id: str, params: SplitParams) -> dict:
    """The split itself. Only the Spotify spend is the track read (~15 calls
    for 1372, and zero if the snapshot hasn't moved since the last read — see
    `_cached_tracks`, the same cache `triage` uses). Tagging is Last.fm, one
    request per not-yet-cached artist; clustering is local and free. A Last.fm
    failure partway through does not lose what was already verified — see
    `_tag_artists_checked` and the `LastFmError` handling below.
    """
    # Checked before anything else, including the Last.fm key: replacing the
    # piles out from under an active sitting would strand its playlist_id —
    # the pile the user is mid-listening to may not exist under that id (or
    # at all) once new piles are written. Refusing costs nothing, same as the
    # tags-version guard below.
    existing = store.splits()["splits"].get(playlist_id)
    if existing and existing.get("active_sitting"):
        raise HTTPException(409, "a sitting is active — finish it before re-splitting")

    fm = _lastfm_client()
    if fm is None:
        raise HTTPException(400, "No Last.fm API key — expected ~/state/sortify/lastfm.json")

    # Checked before any Spotify read: a stale tags.json is a local problem
    # that should fail for free, not after spending a track fetch.
    cached_artists = _tag_artists_checked()

    # Validated against the cached listing, like triage — an unknown id must
    # not cost a call just to find that out.
    by_id = {p["id"]: p for p in sp.my_playlists()}
    if playlist_id != LIKED_ID and playlist_id not in by_id:
        raise HTTPException(404, "unknown playlist")

    # Refused before any Spotify call: the Feb-2026 dev-mode API 403s on
    # /playlists/{id}/items for anything the account doesn't own, and the
    # cached listing already knows which playlists those are (`editable` is
    # false for them). Without this, a non-owned playlist still pays one
    # wasted call — the paginated read 403s on its first page, not partway
    # through — but that call is entirely avoidable, and it's what made the
    # "the bomb" incident's failure opaque (a bare 502 with no clue why).
    # Liked Songs is the one id never in `by_id` (see the check above) and
    # is always readable, so `p is None` exempts it rather than tripping the
    # guard.
    p = by_id.get(playlist_id)
    if p is not None and not p["editable"]:
        raise _foreign_playlist_error(p["name"], p.get("owner"))

    snapshot_id = by_id.get(playlist_id, {}).get("snapshot_id")
    _progress_set(playlist_id, phase="reading")
    try:
        tracks = _cached_tracks(playlist_id, snapshot_id)
    except SpotifyError as e:
        if e.status == 403:
            # The cached listing said this was ours, but the live read just
            # found otherwise — ownership changed, sharing was revoked, or
            # the entry was simply stale since the last Refresh. Same
            # actionable message as the pre-flight guard above, not a bare
            # 502: the fix is "make a copy", not "try again later".
            raise _foreign_playlist_error(
                p["name"] if p else playlist_id, p.get("owner") if p else None
            ) from e
        raise  # e.g. a 429 cooldown must still surface as itself, not this
    if not tracks:
        raise HTTPException(400, "playlist has no tracks")

    names = {}
    for t in tracks:
        for a in t.get("artists", []):
            aid = a.get("id")
            if not aid:
                continue
            name = a.get("name") or ""
            # A non-blank name always wins over a blank one, regardless of
            # which occurrence comes first in the playlist. The same artist
            # id can appear on a real track and on a blank-artist Spotify
            # placeholder (a removed/unavailable track) — first-occurrence-
            # wins would let the placeholder's blank name reach `enrich`
            # even when a later track in the same playlist names the artist
            # correctly, permanently poisoning data/tags.json (never
            # re-fetched) with a false miss for an artist Last.fm actually
            # knows. An id with only blank occurrences still ends up with
            # "" here, which is correct: that one really is unknowable.
            if name or aid not in names:
                names[aid] = name

    # Set before the first artist, so a poll landing in the gap between the
    # track read and the first Last.fm answer already reads "tagging" rather
    # than showing the reading phase for longer than it actually ran.
    _progress_set(playlist_id, phase="tagging", done=0, total=0)
    try:
        artists = enrich(
            names, cached_artists, fm, _now_iso(),
            on_progress=lambda done, total: _progress_set(
                playlist_id, done=done, total=total),
        )
    except LastFmError as exc:
        saved = exc.partial if exc.partial is not None else cached_artists
        _merge_save_tag_artists(saved)
        # How many of *this playlist's* artists made it, not the size of the
        # whole cross-playlist cache `saved` carries forward.
        tagged_here = len(set(names) & set(saved))
        raise HTTPException(
            502,
            f"Last.fm tagging stopped after {tagged_here} of {len(names)} "
            f"artists in this playlist ({exc}); progress was saved — "
            "re-running the split will resume instead of starting over.",
        ) from exc
    _merge_save_tag_artists(artists)

    _progress_set(playlist_id, phase="clustering")
    piles = split_tracks(tracks, artists, params.model_dump())
    with _split_lock:
        payload = store.splits()
        prev = payload["splits"].get(playlist_id, {})
        if prev.get("active_sitting"):
            # A sitting can only have started here if the entry guard's
            # negative answer went stale during the Last.fm walk above — the
            # tags this run fetched are already persisted, so a retry
            # resumes rather than re-tagging from scratch.
            raise HTTPException(
                409, "a sitting became active while splitting — finish it, then re-run the split")
        payload["splits"][playlist_id] = {
            "created_at": _now_iso(),
            "snapshot_id": snapshot_id,
            "params": params.model_dump(),
            "piles": piles,
            "decided": prev.get("decided", {}),
            "active_sitting": None,
        }
        # Carried forward for the same reason `decided` is (see above): a
        # re-run usually produces the very same piles, and dropping the
        # materialisation records would make a pile that already has a
        # permanent playlist look untouched — offering to spend 310 calls on
        # a second copy of it. Piles that really did change under the same
        # id are caught by the fingerprint in `_materialise_plan`, not by
        # throwing the records away here.
        #
        # But a record whose pile id no longer exists after this re-cluster
        # would never be shown or priced again — swept to history instead,
        # so a playlist sortify made never stops being traceable to the pile
        # it came from (review finding I3).
        new_ids = {p["id"] for p in piles}
        carried, history = {}, list(prev.get("materialised_history", []))
        for pid, rec in (prev.get("materialised") or {}).items():
            if pid in new_ids:
                carried[pid] = rec
            else:
                history.append({**rec, "swept": "recluster"})
        payload["splits"][playlist_id]["materialised"] = carried
        payload["splits"][playlist_id]["materialised_history"] = history
        store.save_splits(payload)
    untagged = sum(len(p["uris"]) for p in piles if p["id"] == UNTAGGED)
    return {"piles": piles, "tagged": len(tracks) - untagged, "untagged": untagged}


@app.get("/api/split/{playlist_id}")
def get_split(playlist_id: str):
    split = store.splits()["splits"].get(playlist_id)
    if not split:
        raise HTTPException(404, "no split for that playlist")
    return {**split, "piles": _pile_progress(split, playlist_id)}


@app.post("/api/split/{playlist_id}/recluster")
def recluster(playlist_id: str, params: SplitParams = SplitParams()):
    """Re-cluster from cached tracks and tags. Costs nothing at all."""
    payload = store.splits()
    split = payload["splits"].get(playlist_id)
    if not split:
        raise HTTPException(404, "no split for that playlist")
    if split.get("active_sitting"):
        # Same reasoning as create_split: new piles would leave the active
        # sitting's pile_id pointing at a partition that no longer exists.
        # Cheap fail-fast for the common case; the real guarantee is the
        # atomic recheck at the write below.
        raise HTTPException(409, "a sitting is active — finish it before reclustering")
    tracks = store.cache()["playlists"].get(playlist_id, {}).get("tracks", [])
    if not tracks:
        raise HTTPException(400, "no cached tracks — run the split again")
    artists = _tag_artists_checked()
    new_piles = split_tracks(tracks, artists, params.model_dump())

    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        if not split:
            raise HTTPException(404, "no split for that playlist")
        if split.get("active_sitting"):
            # A sitting started in the window between the guard above and
            # this write (narrow — no network call sits in between it, only
            # local clustering — but not zero).
            raise HTTPException(
                409, "a sitting became active while reclustering — finish it, then try again")
        split["piles"] = new_piles
        split["params"] = params.model_dump()
        store.save_splits(payload)
    return {"piles": _pile_progress(split, playlist_id)}


# Structural ceilings on a sitting, independent of each other:
#   - SITTING_MAX_MINUTES bounds the *requested* duration, so a caller can't
#     ask for an absurd target (100000 minutes) and get an absurd burst back.
#   - SITTING_MAX_TRACKS bounds the *actual* track count no matter what
#     target_minutes says or what the cached durations look like. A pile
#     whose tracks are all missing duration_ms (no cache entry ever
#     populates a genuine 0, but a stale/partial one could) would otherwise
#     never trip pick_sitting's duration check at all — every remaining
#     track in a 300-track pile would come back as "the sitting", i.e. 301
#     calls in one burst. 40 is roughly double the ~22-track 2h default, so
#     it does not constrain a normal sitting; it exists purely as the floor
#     under which "per sitting" stops being a documented property of this
#     code and starts being a property of nothing.
SITTING_MAX_MINUTES = 360  # 6 h
SITTING_MAX_TRACKS = 40    # calls per sitting <= 1 create + 40 add + 1 finish = 42


class SittingIn(BaseModel):
    pile_id: str
    target_minutes: int = Field(120, gt=0, le=SITTING_MAX_MINUTES)


def _claim_reservation(split_playlist_id: str, claim: str, **fields: Any) -> bool:
    """Write `fields` into the active_sitting reservation stamped `claim`,
    iff that exact reservation is still the one on disk.

    `finish_sitting` can legally clear active_sitting to None (or a later
    start_sitting can replace it) at any point during create_playlist or the
    add loop, since neither of those is inside this lock — they're network
    calls. `claim` is a uuid4 minted fresh per reservation, checked here so a
    write from THIS materialisation attempt never lands on a reservation
    that isn't it: the lock only makes each individual read-modify-write
    atomic, not the whole span between them. It must be a value nothing else
    could plausibly generate at the same moment — `started_at` (whole-second
    resolution) failed exactly that: two reservations for the same playlist
    within one wall-clock second compared equal, so a losing reservation's
    write could land on a winning one's record instead of being refused.
    (Parameter named split_playlist_id, not playlist_id, so **fields can
    carry a "playlist_id" key — the sitting playlist's id — without
    colliding with this function's own argument.)
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(split_playlist_id)
        active = split.get("active_sitting") if split else None
        if not active or active.get("claim") != claim:
            return False
        active.update(fields)
        store.save_splits(payload)
        return True


def _reservation_alive(split_playlist_id: str, claim: str) -> bool:
    with _split_lock:
        split = store.splits()["splits"].get(split_playlist_id)
        active = split.get("active_sitting") if split else None
        return bool(active and active.get("claim") == claim)


# One user action never unfollows more than this. The rule that decides what
# a sitting is (split.is_sitting_playlist) is conservative, but it is still a
# rule about the user's real account: if it is ever wrong, being wrong should
# cost ten calls and be visible, not one call per playlist in the library.
# The remainder is reported and the button can simply be pressed again.
SITTING_SWEEP_CAP = 10

# Sitting playlists this process is materialising right now, and a count of
# the materialisations that have not yet learned their playlist's id.
#
# `_inflight` is the ordinary case: between create_playlist returning and the
# add loop finishing, a playlist exists that a refreshed listing can already
# show, and no sweep may touch it. `_materialising` covers the window BEFORE
# that, where the playlist may exist in the account while nothing — not even
# this process — knows its id yet. Nothing can be excluded by id in that
# window, so the sweep declines to run at all rather than delete a sitting
# somebody is starting. Both are read and mutated only under `_split_lock`,
# the same convention `_materialising_piles` follows.
_inflight: set[str] = set()
_materialising = 0


def _cached_listing() -> list[dict]:
    """The playlist listing as of the user's last Refresh, or nothing.

    Deliberately NOT `sp.my_playlists()`: that falls back to fetching when the
    cache is cold, which would turn a finish or a cleanup into ~21 paginated
    calls the user never asked for — the exact shape of proactive traffic
    CLAUDE.md forbids. No cache means no orphans are known, which is the
    correct answer here rather than a reason to spend.
    """
    entry = store.cache().get("playlist_list") or {}
    return entry.get("items") or []


def _sweep_protection(splits: dict | None = None) -> tuple[set[str], bool]:
    """Ids no sweep may touch, and whether it must decline entirely.

    `splits` may be passed in by a caller that has already read splits.json,
    because /api/playlists reads it exactly once for the whole listing — on a
    real account that file is hundreds of KB and a second read per request is
    a measurable stall (see the comment in `playlists`).
    """
    with _split_lock:
        protected = set(_inflight)
        blind = _materialising > 0
        if splits is None:
            splits = store.splits()["splits"]
        for split in splits.values():
            active = split.get("active_sitting") or {}
            if active.get("playlist_id"):
                protected.add(active["playlist_id"])
    return protected, blind


def _sweep_sitting_orphans(cap: int | None = None) -> dict:
    """Unfollow every leftover sitting the cached listing knows about.

    This is the whole point of the redesign. `splits.json` cannot be the
    authority on what exists in the account, because creating a playlist and
    recording it are not atomic: a lost create response, a crash before the
    record saves, or a failed unfollow all leave a real playlist that no
    record names (Ruling R17). The account itself is authoritative instead,
    read through `my_playlists()` — which serves from cache.json at **zero
    Spotify calls**, so finding orphans is free and only removing them costs
    anything, one call each.

    Reading the listing is deliberately NOT a refresh. The listing is re-read
    only when the user asks for it, so this sees the account as of their last
    Refresh — which is why orphans surface on the Playlists view, next to the
    button that updates it, rather than appearing by magic.
    """
    protected, blind = _sweep_protection()
    if blind:
        # A start is between its create call and its record. Anything marked
        # in the listing might be that playlist, and it would be unfollowed
        # out from under a sitting the user just asked for.
        return {"removed": [], "remaining": None, "deferred": True}
    me = (store.cache().get("me") or {}).get("id")
    found, remaining = select_orphans(
        _cached_listing(), me, protected,
        SITTING_SWEEP_CAP if cap is None else cap)
    removed: list[str] = []
    for entry in found:
        try:
            sp.unfollow_playlist(entry["id"])
        except SpotifyError as e:
            if e.status != 404:
                # Leave it in the cached listing. It is still in the account,
                # so it must stay visible as an orphan — pruning it here is
                # exactly the "sortify can no longer see it" failure this
                # change exists to end. No retry: the user presses the button
                # again, and one 429 must not become a burst of them.
                log.warning("could not unfollow leftover sitting %s (%s) — "
                            "it stays listed for the next cleanup", entry["id"], e)
                continue
            # 404 means already gone: nothing to unfollow, and the listing
            # entry is simply stale. Treated as removed.
        removed.append(entry["id"])
    sp.forget_playlists(set(removed))
    return {"removed": removed, "remaining": remaining, "deferred": False}


def _find_sitting_orphans(splits: dict | None = None) -> list[dict]:
    """The same selection, without spending anything. Zero calls."""
    protected, blind = _sweep_protection(splits)
    if blind:
        return []
    me = (store.cache().get("me") or {}).get("id")
    found, _ = select_orphans(_cached_listing(), me, protected, cap=1000)
    return [{"id": p["id"], "name": p.get("name") or ""} for p in found]


def _abandon_orphaned_playlist(
    split_playlist_id: str, pile_id: str, new_id: str, detail: str
) -> None:
    """A concurrent finish discarded the reservation for this sitting while
    it was still being materialised. The playlist itself is real, though —
    unfollow it (the one thing that would otherwise leave the exact litter
    the recording-before-adds fix exists to prevent) and report a clean
    error instead of crashing on a vanished record.

    Every checkpoint unfollows, unconditionally. A previous round skipped it
    at the two later ones, reasoning that once playlist_id is recorded, the
    only thing that can clear the reservation is a `finish` that saw that id
    and already unfollowed it. That premise was false: `finish_sitting` reads
    the reservation under one acquisition of _split_lock and clears it under
    a later one, so a finish that read it while playlist_id was still None
    makes no unfollow call at all and then clears a record that has since
    gained an id it never saw — leaving a live playlist in the user's account
    that nothing points at. `finish_sitting`'s clear is now a compare-and-swap
    on exactly that pair, which closes it from the other side too, but the
    unfollow here stays: being wrong this way costs one call that comes back
    404 in a rare race, and being wrong the other way costs a playlist the
    user has to find and delete by hand. Unfollowing `new_id` can never harm
    another sitting either — it is this request's own playlist and no other
    reservation can name it.

    When the unfollow itself fails, this used to re-stamp a fresh reservation
    for `new_id` so a later finish could still reach it — and when no free
    slot existed it gave up and logged, which was leak class (c), ~10/300 at
    2% injection. Both halves are gone. The playlist marks itself, so the
    next cleanup finds it in the listing whether or not splits.json has room
    for it; re-stamping now would only invent a second, competing authority
    over the same playlist. Logged at warning, not error: this is a known,
    self-healing state, not an unrecoverable one.
    """
    try:
        sp.unfollow_playlist(new_id)
    except SpotifyError as e:
        if e.status != 404:
            log.warning(
                "sitting playlist %s was left in the account (unfollow failed: %s) — "
                "it is marked, so the next Playlists cleanup will remove it", new_id, e)
    raise HTTPException(409, detail)


@app.post("/api/split/{playlist_id}/sitting")
def start_sitting(playlist_id: str, body: SittingIn):
    """Materialise one sitting as a disposable playlist. ~24 calls at 2 h,
    structurally capped at 1 + SITTING_MAX_TRACKS + 1 regardless of input.

    The sitting is recorded (with the playlist id, as soon as it exists) in
    short, lock-protected writes around the actual API calls rather than one
    write at the very end: a failure partway through the add loop — a 429
    cooldown landing on track 5 of 22 is the likely real case, but a process
    restart mid-add has the same shape — must still leave a findable,
    finishable record. Recording only at the end would instead leave a real
    playlist in the account that sortify has never heard of and can never
    unfollow.

    That same span (create_playlist, then up to SITTING_MAX_TRACKS adds) is
    also long enough — at WINDOW_CAP pacing, over a minute for a full
    sitting — for a concurrent `finish` to land on this exact sitting before
    it's done starting. `_claim_reservation`/`_reservation_alive` detect
    that (the reservation this call made is gone or superseded) at each
    checkpoint and unfollow-and-error rather than either crashing on a
    vanished record or quietly burning the rest of the add budget on a
    playlist nobody can reach anymore.
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        if not split:
            raise HTTPException(404, "no split for that playlist")
        if split.get("active_sitting"):
            raise HTTPException(409, "a sitting is already active — finish it first")

        pile = next((p for p in split["piles"] if p["id"] == body.pile_id), None)
        if not pile:
            raise HTTPException(404, "no such pile")

        tracks = store.cache()["playlists"].get(playlist_id, {}).get("tracks", [])
        if not tracks:
            raise HTTPException(400, "no cached tracks — run the split again")
        durations = {t["uri"]: t.get("duration_ms") or 0 for t in tracks}
        uris = pick_sitting(pile["uris"], durations, split.get("decided", {}),
                            body.target_minutes * 60 * 1000)
        if not uris:
            raise HTTPException(400, "that pile is finished")
        uris = uris[:SITTING_MAX_TRACKS]

        # Reserve the slot before spending anything, atomically with the
        # checks above: a second request that reaches this lock next sees a
        # sitting already claimed, even though no Spotify call has happened
        # yet. playlist_id is filled in below once create_playlist returns.
        # claim is this reservation's identity — see _claim_reservation. A
        # fresh uuid4, not a timestamp: two reservations for this same
        # playlist can be minted within the same wall-clock second (a
        # zero-cost finish followed immediately by a new start), and a
        # whole-second `started_at` would compare equal between them.
        # started_at is kept purely as the human-readable value shown
        # alongside it; nothing checks it for identity.
        claim = uuid.uuid4().hex
        split["active_sitting"] = {"playlist_id": None, "pile_id": pile["id"],
                                   "uris": [], "started_at": _now_iso(), "claim": claim}
        store.save_splits(payload)

    # The name and description are the marker the account is read back by —
    # see split.is_sitting_playlist. They are not decoration: they are the
    # only thing that makes a stray playlist recognisable after a crash.
    global _materialising
    with _split_lock:
        _materialising += 1
    try:
        new_id = sp.create_playlist(f"{SITTING_PREFIX}{pile['name']}", SITTING_DESCRIPTION)
        with _split_lock:
            _inflight.add(new_id)
    finally:
        # Dropped only once the id is claimed (or known never to exist), so
        # there is no instant where a sweep can neither see the id nor know
        # that a start is in progress.
        with _split_lock:
            _materialising -= 1

    try:
        return _materialise_sitting(playlist_id, claim, pile, new_id, uris, durations)
    finally:
        with _split_lock:
            _inflight.discard(new_id)


def _materialise_sitting(playlist_id, claim, pile, new_id, uris, durations):
    """The add loop, split out so `start_sitting` can hold the in-flight claim
    across every exit path — including the HTTPExceptions raised from the
    abandon checkpoints."""
    if not _claim_reservation(playlist_id, claim, playlist_id=new_id):
        _abandon_orphaned_playlist(
            playlist_id, pile["id"], new_id,
            "the sitting was finished by another request while it was starting")

    for uri in uris:
        if not _reservation_alive(playlist_id, claim):
            _abandon_orphaned_playlist(
                playlist_id, pile["id"], new_id,
                "the sitting was finished by another request while tracks were still being added")
        sp.add_to_playlist(new_id, uri)

    if not _claim_reservation(playlist_id, claim, uris=uris):
        _abandon_orphaned_playlist(
            playlist_id, pile["id"], new_id,
            "the sitting was finished by another request just as it finished starting")

    minutes = sum(durations.get(u, 0) for u in uris) // 60000
    return {"sitting_id": new_id, "uris": uris, "minutes": minutes}


@app.post("/api/split/{playlist_id}/sitting/finish")
def finish_sitting(playlist_id: str):
    """Unfollow the sitting playlist in one call and clear the reservation —
    but only the exact reservation this call observed.

    The read below and the clear at the end are two separate acquisitions of
    _split_lock with a network call between them, so the slot can change
    underneath: a start still in flight can fill in a playlist_id we read as
    None, another finish can clear it, and a whole new sitting can then claim
    it. Clearing unconditionally is what made that a data-loss bug — a
    double-clicked finish is enough. The slower of the two reads sitting A,
    its unfollow goes out, the faster one completes and clears, sitting B
    starts and populates a real playlist, and then the slower one's clear
    lands and wipes B's record: B's playlist is live, full, playing, and
    nothing in splits.json points at it, so `finish` can never reach it
    again. (It erased the abandon path's re-stamp the same way, back when
    that path had one.)

    So the clear is a compare-and-swap on what we actually observed — same
    claim, same playlist_id — and otherwise the slot is left exactly as
    found. Leaving it alone is always safe: whatever occupies it now is
    either a sitting still starting (its own checkpoints resolve it) or one
    newer than ours (its own finish resolves it). The response says which
    happened, so a caller that meant to end the *current* sitting can just
    ask again.
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        sitting = dict(split["active_sitting"]) if split and split.get("active_sitting") else None
    if sitting is None:
        # No record — but "no record" is precisely the state a lost create
        # response or a crash mid-start leaves behind, and the account may
        # still hold the playlist. Sweeping first means finish cleans up the
        # case the old 404 used to strand. Only a genuinely empty account
        # still 404s.
        swept = _sweep_sitting_orphans()
        if not swept["removed"] and not swept["remaining"]:
            raise HTTPException(404, "no active sitting")
        return {"ok": True, "cleared": False, "swept": swept["removed"],
                "remaining": swept["remaining"]}

    claim = sitting.get("claim")
    playlist_ref = sitting.get("playlist_id")
    if playlist_ref is not None:
        try:
            sp.unfollow_playlist(playlist_ref)
        except SpotifyError as e:
            # Already gone — deleted or unfollowed from the Spotify app
            # directly, bypassing sortify — is not a failure worth blocking
            # on. Re-raising here would leave active_sitting stuck forever:
            # finish would always 404 on the same missing playlist, and
            # start_sitting would always 409 on a record finish could never
            # clear. Clearing our own bookkeeping is correct either way; the
            # playlist itself is already gone from the account.
            if e.status != 404:
                raise
    # playlist_ref is None when create_playlist itself never returned (e.g.
    # a cooldown hit before the sitting's single call landed) — the
    # reservation exists, but nothing was ever created in the account, so
    # there is nothing to unfollow. Clearing the record is still correct.

    cleared = False
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        active = split.get("active_sitting") if split else None
        if (active is not None
                and active.get("claim") == claim
                and active.get("playlist_id") == playlist_ref):
            split["active_sitting"] = None
            store.save_splits(payload)
            cleared = True

    # Clear first, then sweep: the record is gone by now, so this call's own
    # playlist is no longer protected and a leftover from an EARLIER sitting
    # (one whose record never existed) is removed in the same click. The
    # unfollow above already dealt with this sitting; if it is still listed,
    # the 404 path below treats it as gone and prunes the stale entry.
    swept = _sweep_sitting_orphans()
    if playlist_ref:
        sp.forget_playlists({playlist_ref})
    return {"ok": True, "cleared": cleared, "swept": swept["removed"],
            "remaining": swept["remaining"]}


@app.post("/api/sittings/cleanup")
def cleanup_sittings():
    """Remove leftover sitting playlists the account still holds.

    User-initiated, one call per playlist, capped at SITTING_SWEEP_CAP, and
    never proactive — it runs when the button on the Playlists view is
    pressed and at no other time. Finding the orphans costs nothing; the
    listing it reads is the one the user last refreshed.
    """
    return {"ok": True, **_sweep_sitting_orphans()}


# ---- materialising a pile as a permanent playlist ---------------------------
#
# A sitting is disposable: capped at SITTING_MAX_TRACKS, unfollowed on finish,
# and reserved through `active_sitting` so only one exists per split at a
# time. Materialising is the opposite on every axis — a real playlist the user
# keeps, browses and plays with sortify closed, holding the WHOLE pile (309
# tracks for the biggest one here), with no reservation, no cap and no
# auto-unfollow. sortify never deletes one; the only place it can be removed
# is Spotify itself.
#
# What the two share is the hazard, and there the sitting path's four rounds
# of fixes are copied deliberately rather than re-derived (see
# `.superpowers/sdd/2026-08-17-playlist-splitting/progress.md`): creating a
# playlist and recording that it exists are two operations that cannot be made
# atomic, so the record is written BEFORE the create call, carries a uuid4
# claim token, and every later write is a compare-and-swap against that exact
# record (`_claim_materialisation`). Recording only after the fact is what
# left real playlists in the account that sortify could neither see nor
# remove.
#
# Two things are deliberately NOT shared. There is no reservation to lose, so
# nothing here can strand a slot; and a half-finished materialisation is a
# perfectly good state to be in — the record says which uris landed, so a
# retry adds only the rest. A 429 cooldown 40 tracks into a 309-track pile is
# the realistic failure, and it must cost 40 calls, not 349.

MATERIALISE_DESCRIPTION = (
    "sortify pile — a permanent copy of one pile from a split. "
    "sortify will never delete this; remove it yourself if you don't want it."
)

# (split playlist id, pile id) pairs currently being materialised by THIS
# process — i.e. between the record write below and the last add returning.
# Read and mutated only under `_split_lock`, exactly like `_pending_keeps`,
# and for the same reason: it tells a genuinely-concurrent request in this
# process ("still running, don't start a second playlist") apart from a
# half-finished record left by a process that died mid-run ("nothing is
# running, this is retryable"). A fresh process starts empty, which is what
# makes every pre-existing partial record correctly resumable after a restart.
_pending_materialise: set[tuple[str, str]] = set()

# (split playlist id, pile id) pairs whose record THIS process has verified
# against the account — either by creating/adding under its own CAS, or by
# the reconcile read below. Batching is what makes this necessary: a crash
# between a 100-track POST landing and the CAS recording it leaves up to 100
# landed tracks unrecorded, and Spotify permits duplicates, so a blind retry
# would double them (design §1). A pair NOT in this set whose record has a
# playlist_id is treated as untrustworthy and re-read before anything is
# added — an EMPTY `added` included, because "created, then one batch landed
# unrecorded" is precisely the record that looks empty and is not.
# Mutated only under `_split_lock`; a fresh process starts empty,
# which is exactly the "never trust the record across an interruption" rule.
_reconciled: set[tuple[str, str]] = set()


def _batches(n: int) -> int:
    """Calls needed to move n tracks at the 100-per-call batch limit."""
    return -(-n // 100)


def _source_playlist_name(playlist_id: str) -> str | None:
    """The source playlist's display name, from the cached listing only —
    zero Spotify calls; None when the cache doesn't know it. The output
    title is fixed at create time (design §3): a later rename of the
    source does not ripple into existing outputs."""
    items = (store.cache().get("playlist_list") or {}).get("items") or []
    return next((p.get("name") for p in items if p.get("id") == playlist_id), None)


def _unique(uris: list[str]) -> list[str]:
    """Order-preserving dedupe.

    `split_tracks` does not deduplicate — a track added to the source playlist
    twice appears twice in its pile — but a permanent playlist should hold it
    once. Deduping here (rather than in the pile itself) keeps `decided`
    accounting per-occurrence, which `_remaining` depends on.
    """
    seen: set[str] = set()
    out = []
    for u in uris:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _pile_fingerprint(pile: dict) -> str:
    """Identity of a pile's *contents*, for spotting a re-cluster.

    Pile ids are positional (`p1`, `p2`, …), so after a re-cluster `p3` is a
    different pile of music under the same id. A fingerprint rather than a
    stored copy of the uri list: `added` already has to hold every uri that
    landed, and a second full copy per materialised pile would roughly double
    splits.json — which `_sitting_for_context` re-reads on every /api/now
    poll. sha1 of the uris in order; not security, just a stable equality
    check that survives a restart (Python's own `hash` does not).
    """
    h = hashlib.sha1()
    for u in pile["uris"]:
        h.update(u.encode())
        h.update(b"\n")
    return h.hexdigest()


def _materialise_plan(split: dict, pile: dict, reconciled: bool = False) -> dict:
    """What materialising `pile` would do right now. Pure; no I/O, no calls.

    The single place the call cost is computed, shared by the GET (which shows
    the number) and the POST (which refuses unless the caller echoes that same
    number back). Two implementations of "how many calls is this" would be two
    chances to display one figure and spend another.

    `stale` means a record exists but was built from a different set of uris —
    the split was re-clustered since, so `p3` is no longer the music that
    playlist was named after. Resuming into it would pour the new pile's
    tracks into the old pile's playlist, so it counts as no record at all: a
    fresh playlist, at full price, which the caller sees before clicking.

    `reconciled` is whether THIS process has already verified the record
    against the account (`_reconciled`). ANY resumable record (playlist_id
    set, something still missing) that hasn't been verified prices in a read
    of the destination first — ceil(len(added)/100) calls, floored at 1, and
    a floor in the other sense too since the real playlist can hold more than
    the record claims (that is the whole reason for the read). Reconciliation
    is what makes a 100-track batch safe to retry.

    An EMPTY `added` earns the read just as much as a partial one, and is in
    fact the likeliest case to need it: the create CAS records `playlist_id`
    with `added: []`, and a death between the next tick's 100-track POST
    returning and its CAS leaves exactly that record with up to 100 tracks
    already on Spotify. Exempting it — which an earlier draft of this
    function did — re-posts the whole batch and doubles them, since Spotify
    permits duplicates. The rule is the design's, without an exception: the
    record is never trusted across an interruption.
    """
    record = (split.get("materialised") or {}).get(pile["id"])
    stale = bool(record) and record.get("fingerprint") != _pile_fingerprint(pile)
    usable = record if (record and not stale) else None
    added = set(usable.get("added", [])) if usable else set()
    missing = [u for u in _unique(pile["uris"]) if u not in added]
    need_create = not (usable and usable.get("playlist_id"))
    added_list = usable.get("added", []) if usable else []
    reconcile_calls = (
        _batches(max(len(added_list), 1))
        if (usable and usable.get("playlist_id") and missing and not reconciled)
        else 0
    )
    return {
        "record": usable,
        "stale": stale,
        "missing": missing,
        "need_create": need_create,
        "reconcile_calls": reconcile_calls,
        "calls": reconcile_calls + _batches(len(missing)) + (1 if need_create else 0),
        "record_view": (
            {"playlist_id": record.get("playlist_id"),
             "added": len(record.get("added", [])),
             "name": record.get("name"),
             "stale": stale}
            if record else None
        ),
    }


def _claim_materialisation(
    split_playlist_id: str, pile_id: str, claim: str, added_uri: str | None = None,
    added_uris: list[str] | None = None, **fields: Any
) -> bool:
    """Update this pile's materialisation record, iff it is still ours.

    The materialise counterpart of `_claim_reservation`, and the same
    argument: `_split_lock` makes each read-modify-write atomic, but not the
    whole span between them, and the Spotify calls in between are exactly
    where something else can replace what we observed. `claim` is a uuid4
    (never a timestamp — see `_claim_reservation` for the whole-second
    collision that cost a round).

    Under `_materialise_tick` (R-T7b), the claim is minted per RECORD, not
    per attempt: a fresh record gets a fresh claim, but every tick that
    resumes it — one Spotify call at a time, possibly hours apart — reuses
    that same claim rather than re-minting one per tick. This is safe
    because correctness here does not rest on the claim alone: it rests on
    single-writer. The queue worker is the only caller of `_materialise_tick`
    for a given pile at a time, and `_pending_materialise` (guarded by
    `_split_lock`) is what enforces that within one process — a concurrent
    tick for the same pile is refused outright rather than racing to CAS.
    The claim's job, then, is narrower than "identify this attempt": it
    identifies this RECORD, so a genuine replacement (a re-cluster's fresh
    record, or another process's) is detected and this tick's write is
    refused instead of landing on the wrong slot.

    `added_uri` appends to the confirmed-added list, which is what makes a
    retry resume. It is written only after `add_to_playlist` returned, so the
    failure mode is "a landed add wasn't recorded" (the retry re-adds it, and
    Spotify shows the track twice) rather than "an add that never happened was
    recorded" (the track is silently missing from a playlist the user thinks
    is complete, with nothing left to retry). `decide` makes the same trade in
    the same direction.
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(split_playlist_id)
        record = (split.get("materialised") or {}).get(pile_id) if split else None
        if not record or record.get("claim") != claim:
            return False
        if added_uri is not None and added_uri not in record["added"]:
            record["added"].append(added_uri)
        for u in added_uris or []:
            if u not in record["added"]:
                record["added"].append(u)
        record.update(fields)
        record["updated_at"] = _now_iso()
        store.save_splits(payload)
        return True


def _rerecord_materialisation(split_playlist_id: str, pile_id: str, record: dict) -> bool:
    """Put a record back for a playlist that really exists, if and only if the
    slot is genuinely empty. False means something else legitimately owns it.

    The materialise counterpart of the sitting path's old re-stamp, minus its
    unfollow: the playlist here is one the user asked to keep and (past the
    first add) already holds their tracks, so deleting it to tidy up the
    bookkeeping would be the worse error by far. Re-recording keeps it
    findable — which is the whole point of recording before creating.
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(split_playlist_id)
        if split is None:
            return False
        mats = split.setdefault("materialised", {})
        if mats.get(pile_id):
            return False
        mats[pile_id] = record
        store.save_splits(payload)
        return True


def _materialise_tick(playlist_id: str, pile_id: str, spend_reserve: bool = False) -> dict:
    """Advance one pile's materialisation by ONE Spotify call — a create, a
    reconcile read (one call per 100 tracks in the destination, *approximated*
    in `spent`), or a batch add of up to 100 tracks.

    "Approximated" is deliberate and load-bearing: `spent` for a reconcile is
    `_batches(len(actual))` over the tracks `playlist_tracks` handed back
    after slimming, so local/null items the read paid for are already gone,
    and `_paginate` costs one extra call on an exact multiple of 100. The
    number is only ever consumed as `>= 1` by the pacing governor's
    clean-credit and never reaches the dashboard, so the arithmetic is left
    alone rather than made exact at the cost of threading a count back out of
    the client.

    The one-shot endpoint this replaces spent a whole pile in a blocking
    loop — measured at 12.4 calls/min for 25 minutes, above the rate that
    earned the 2026-08-13 ban. Same machinery, same records, same claims;
    the only change is that the loop now lives in the queue worker, which
    owns the pacing between calls (delivery is the queue's job now, not a
    request handler's). Returns {"spent", "done", "gone"}; SpotifyError
    propagates to the caller, which classifies it — including the two hazard
    paths below, which raise HTTPException(409) internally (they are shared
    with nothing else that needs a different shape) but are translated to
    SpotifyError(409, ...) here, at the one seam where the tick's contract
    with its caller is fixed (controller ruling R-T7a).

    One subtlety: a fresh claim is minted only when a record must be
    (re)stamped from scratch — a brand-new pile or one whose fingerprint went
    stale. A resumed pile reuses its persisted claim, which is fine because
    the worker is the only writer and `_claim_materialisation` still CASes
    on it.
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        pile = next((p for p in (split or {}).get("piles", []) if p["id"] == pile_id), None)
        if not split or not pile:
            return {"spent": 0, "done": True, "gone": True}
        if (playlist_id, pile_id) in _pending_materialise:
            return {"spent": 0, "done": False, "gone": False}
        plan = _materialise_plan(split, pile,
                                 reconciled=(playlist_id, pile_id) in _reconciled)
        if plan["calls"] == 0:
            return {"spent": 0, "done": True, "gone": False}
        if plan["stale"]:
            old = split["materialised"].pop(pile_id, None)
            if old:
                split.setdefault("materialised_history", []).append(old)
        record = (split.get("materialised") or {}).get(pile_id)
        if not record or plan["stale"] or not record.get("claim"):
            existing = plan["record"] if not plan["stale"] else None
            record = {
                "playlist_id": existing.get("playlist_id") if existing else None,
                "pile_id": pile_id, "name": pile["name"],
                "fingerprint": _pile_fingerprint(pile),
                "track_count": len(_unique(pile["uris"])),
                "added": list(existing.get("added", [])) if existing else [],
                "claim": uuid.uuid4().hex,
                "created_at": existing.get("created_at") if existing else _now_iso(),
                "updated_at": _now_iso(),
            }
            # Written BEFORE the create call — the create/record gap is where
            # this project's stray playlists have come from.
            split.setdefault("materialised", {})[pile_id] = record
            store.save_splits(payload)
        claim = record["claim"]
        need_create = not record.get("playlist_id")
        need_reconcile = not need_create and plan["reconcile_calls"] > 0
        batch = [] if (need_create or need_reconcile) else plan["missing"][:100]
        _pending_materialise.add((playlist_id, pile_id))

    def _mark_reconciled():
        # Every successful CAS proves this process owns the record and its
        # `added` list is truth — creates and adds included, so a pile this
        # process started never pays for a read it doesn't need.
        with _split_lock:
            _reconciled.add((playlist_id, pile_id))

    def _unverify():
        # The gap between "the write left this process" and "the CAS recorded
        # it" is an interruption in exactly the sense design §1 means, and it
        # does not need the process to die: an httpx read timeout or a
        # connection reset mid-response raises after the POST has already
        # landed. Leaving the pair in `_reconciled` across that would let a
        # resume IN THIS PROCESS re-POST up to 100 uris that are already on
        # the playlist — and Spotify stores duplicates without complaint.
        # So the rule is applied as stated: any record with a playlist_id
        # this process has not itself verified gets reconciled. A spurious
        # discard costs one read; a missed one costs up to 100 duplicated
        # tracks.
        with _split_lock:
            _reconciled.discard((playlist_id, pile_id))

    try:
        if need_create:
            _unverify()
            new_name = split_output_name(_source_playlist_name(playlist_id),
                                         pile["name"])
            new_id = sp.create_playlist(new_name, MATERIALISE_DESCRIPTION, bulk=True,
                                        spend_reserve=spend_reserve)
            if _claim_materialisation(playlist_id, pile_id, claim,
                                      playlist_id=new_id, name=new_name):
                _mark_reconciled()
            else:
                try:
                    _abandon_unrecorded_playlist(playlist_id, pile_id, new_id, record)
                except HTTPException as exc:
                    raise SpotifyError(exc.status_code, exc.detail) from exc
            # A create call never finishes a pile on its own — every pile
            # has at least one track still to add afterwards.
            return {"spent": 1, "done": False, "gone": False}
        if need_reconcile:
            # First act on picking up a resumable pile: one read of the
            # destination, and `added` becomes what is ACTUALLY there. A
            # crash mid-batch can then neither duplicate nor skip (design
            # §1) — the record is never trusted across an interruption.
            actual = sp.playlist_tracks(record["playlist_id"], bulk=True,
                                        spend_reserve=spend_reserve)
            actual_uris = {t["uri"] for t in actual}
            landed = [u for u in _unique(pile["uris"]) if u in actual_uris]
            if _claim_materialisation(playlist_id, pile_id, claim, added=landed):
                _mark_reconciled()
            return {"spent": max(1, _batches(len(actual))), "done": False,
                    "gone": False}
        _unverify()
        sp.add_to_playlist(record["playlist_id"], batch, bulk=True,
                           spend_reserve=spend_reserve)
        if _claim_materialisation(playlist_id, pile_id, claim, added_uris=batch):
            _mark_reconciled()
        else:
            try:
                _readopt_materialisation(playlist_id, pile_id, record,
                                         record["playlist_id"], batch)
            except HTTPException as exc:
                raise SpotifyError(exc.status_code, exc.detail) from exc
    finally:
        with _split_lock:
            _pending_materialise.discard((playlist_id, pile_id))
    remaining = len(plan["missing"]) - len(batch)
    return {"spent": 1, "done": remaining == 0, "gone": False}


def _abandon_unrecorded_playlist(
    split_playlist_id: str, pile_id: str, new_id: str, record: dict
) -> None:
    """The record for a just-created (still empty) playlist is gone. Unfollow
    it and fail cleanly — mirrors `_abandon_orphaned_playlist`. Always raises
    HTTPException(409); `_materialise_tick`, its only caller, catches that
    and re-raises as SpotifyError(409, ...) to keep its own contract
    (SpotifyError propagates, nothing else does) — see R-T7a.
    """
    try:
        sp.unfollow_playlist(new_id)
    except SpotifyError as e:
        if e.status != 404:
            # The unfollow failed for real. Rather than lose sight of the
            # playlist entirely, re-record it if the slot is free.
            if not _rerecord_materialisation(
                split_playlist_id, pile_id,
                {**record, "playlist_id": new_id, "added": [], "claim": uuid.uuid4().hex},
            ):
                log.error(
                    "materialised playlist %s for pile %s lost its record, could not be "
                    "unfollowed (%s) and its slot is taken — needs manual cleanup in the "
                    "Spotify app", new_id, pile_id, e,
                )
            raise
    raise HTTPException(
        409, "this pile's saved-playlist record was replaced while it was being created — "
             "the empty playlist was removed again; nothing else changed")


def _readopt_materialisation(
    split_playlist_id: str, pile_id: str, record: dict, new_id: str, added_here: list[str]
) -> None:
    """The record vanished while tracks were being added. Always raises
    HTTPException(409); `_materialise_tick`, its only caller, catches that
    and re-raises as SpotifyError(409, ...) — see R-T7a and the note on
    `_abandon_unrecorded_playlist`.
    """
    restored = {**record, "playlist_id": new_id,
                "added": list(record["added"]) + list(added_here),
                "claim": uuid.uuid4().hex, "updated_at": _now_iso()}
    if not _rerecord_materialisation(split_playlist_id, pile_id, restored):
        log.error(
            "materialised playlist %s for pile %s lost its record mid-run and its slot is "
            "taken by another record — the playlist is real and holds %d tracks; it needs "
            "to be re-made or removed by hand in the Spotify app",
            new_id, pile_id, len(restored["added"]),
        )
    raise HTTPException(
        409, "this pile's saved-playlist record was replaced while tracks were being added — "
             "the playlist itself is fine and was re-recorded; open the split again to see it")


# ---- the queued materialiser: one paced call per tick ----------------------
#
# The worker thread is created ONLY by the enqueue/resume endpoints (Task 9);
# there is no code path from boot to Spotify traffic, and
# tests/test_no_proactive_work.py pins that. Pacing belongs to the Governor
# (sortify/pacing.py); stopping belongs to _queue_next_action; the single
# Spotify call per tick belongs to _materialise_tick. queue.json and
# pacing.json are read directly by boxdash, so both are rewritten (atomic,
# versioned) after every state change, not just at exit.

_queue_lock = threading.Lock()
_queue_wake = threading.Event()          # set() = "wake now and re-decide"
_queue_worker: threading.Thread | None = None

# A pause/cancel/done already recorded in queue.json is never allowed to be
# clobbered back to something actionable by a worker (or a stray pause) that
# hasn't noticed it yet (ruling R-T8a, extended by I3 review round 1): once a
# queue is terminal, the ONLY way out is "stopped" (cancel always remains
# reachable, or the resume endpoint's `force=True` escape hatch below).
_QUEUE_TERMINAL = ("paused", "stopped", "done")
_QUEUE_RESUMABLE = ("running", "sleeping", "quiet")


def _apply_queue_state(
    q: dict, state: str, stop_reason: str | None, force: bool = False,
    keep_stop_reason: bool = False,
) -> bool:
    """The guarded write itself, given a queue dict already read under
    `_queue_lock`. Split out from `_set_queue_state` (review round 1, M1) so
    the resume endpoint can check `pending`/`current` and apply the state
    change in the SAME lock acquisition, instead of racing a separate
    `_set_queue_state` call against a concurrent cancel that empties them.

    Returns False (refusing the write) when the queue is already terminal
    (paused/stopped/done) and this call isn't targeting "stopped" — the
    worker racing its own "resume" write against a pause it hasn't read yet
    (C2/R-T8a), a stray pause/resume landing on an already-finished run
    (I3), or a non-429 error noticed after a user already cancelled (fix
    round 2, minor 1). Cancel ("stopped") is always reachable, matching the
    Task 9 binding note. Every caller MUST treat a False return as its own
    stop signal; the worker exits either way, so tolerating a refusal here
    needs no extra branching at the call site.

    `force=True` is the one deliberate override, for the human-only resume
    endpoint: a "paused" or "stopped" queue is exactly the state resume is
    meant to leave, so the guard would otherwise refuse the very call whose
    job is crossing it (Task 9 binding note — an explicit escape hatch
    rather than weakening the check-and-set itself, which everything else
    still relies on to keep a pause/cancel from being clobbered).

    `keep_stop_reason=True` (review round 1, M1) leaves the stored
    `stop_reason` untouched instead of overwriting it with the `stop_reason`
    argument — resume uses this so a quota/error reason a user is still
    looking at doesn't flicker to None the instant the click lands; the
    worker's own unconditional "running" transition at the top of
    `_drain_queue_body` is what actually clears it, once a run is genuinely
    under way again rather than just requested.
    """
    if not force and q["state"] in _QUEUE_TERMINAL and state != "stopped":
        return False
    q["state"] = state
    if not keep_stop_reason:
        q["stop_reason"] = stop_reason
    q["updated_at"] = _now_iso()
    store.save_queue(q)
    return True


def _set_queue_state(state: str, stop_reason: str | None = None, force: bool = False) -> bool:
    """Check-and-set queue.json's state under one lock acquisition. See
    `_apply_queue_state` for the guard's rules; this is the plain entry
    point used everywhere except resume (which needs the lock held across
    its own extra check — see `_apply_queue_state`'s docstring)."""
    with _queue_lock:
        q = store.queue()
        return _apply_queue_state(q, state, stop_reason, force=force)


def _queue_progress(q: dict) -> dict:
    """The boxdash snapshot: everything the card shows, in one read."""
    split = store.splits()["splits"].get(q.get("playlist_id") or "", {})
    piles = {p["id"]: p for p in split.get("piles", [])}
    cur = piles.get(q.get("current") or "")
    rec = (split.get("materialised") or {}).get(q.get("current") or "", {})
    total = len(q.get("pending", [])) + (1 if q.get("current") else 0)
    done = (q.get("pile_count_at_enqueue") or total) - total
    return {"pile_id": q.get("current"), "pile_index": done + (1 if cur else 0),
            "pile_count": q.get("pile_count_at_enqueue") or total,
            "track": len(rec.get("added", [])),
            "track_total": len(_unique(cur["uris"])) if cur else 0,
            "spent_today": sp.budget_spent(), "bulk_today": sp.bulk_spent(),
            "daily_cap": DAILY_CAP, "reserve": BULK_RESERVE,
            "spend_reserve": bool(q.get("spend_reserve"))}


def _queue_next_action(now: float) -> tuple:
    """Decide, without doing: ("stop", reason) | ("sleep", secs, state) |
    ("tick", playlist_id, pile_id, spend_reserve). Mutates queue.json only to
    advance current/pending as piles finish (free, local)."""
    with _queue_lock:
        q = store.queue()
        if q["state"] in _QUEUE_TERMINAL:
            return ("stop", q["state"])
        block = sp.bulk_block_reason(spend_reserve=bool(q.get("spend_reserve")))
        if block:
            reason, until = block
            state = "quiet" if reason == "quiet" else "sleeping"
            return ("sleep", max(until - now, 1.0), state)
        while True:
            pid = q.get("current")
            if not pid:
                if not q["pending"]:
                    q.update(state="done", stop_reason=None, updated_at=_now_iso())
                    q["progress"] = _queue_progress(q)
                    store.save_queue(q)
                    return ("stop", "done")
                q["current"] = q["pending"].pop(0)
                store.save_queue(q)
                continue
            split = store.splits()["splits"].get(q["playlist_id"], {})
            pile = next((p for p in split.get("piles", []) if p["id"] == pid), None)
            if pile is None or _materialise_plan(
                    split, pile,
                    reconciled=(q["playlist_id"], pid) in _reconciled)["calls"] == 0:
                q["current"] = None          # vanished or finished: next pile
                store.save_queue(q)
                continue
            return ("tick", q["playlist_id"], pid, bool(q.get("spend_reserve")))


def _worker_may_stop() -> bool:
    """The atomic version of "am I still supposed to exit?", checked right
    before every planned exit from `_drain_queue_body`'s loop (I4 fix round
    2 — the round-1 fix of clearing `_queue_worker` in a `finally` closed
    the case where resume runs AFTER a worker has fully exited, but not
    this one: a worker can READ a terminal state, decide to stop, and then
    a resume's forced write can land in the gap before that decision's
    `return` actually executes. `_start_queue_worker`'s `is_alive()` check
    then sees the (still technically alive) old worker and skips spawning a
    new one, trusting it to notice the resume — but it already committed to
    stopping based on stale data, so nothing ever picks the run back up.
    Reproduced directly: a plain pause-then-immediate-resume round trip
    over HTTP hit this roughly 1 time in 5 in a tight loop, so this is not
    the vanishingly rare window the round-1 fix left as accepted risk — it
    needed closing, not just documenting.

    Re-reads queue.json fresh, under `_queue_lock`. If the state has
    already flipped back to something resumable (a resume raced in first),
    returns False WITHOUT touching `_queue_worker` — the caller must
    `continue` its loop rather than exit, since it is now the (only, still
    current) worker for this resumed run. Otherwise it is safe to commit:
    clears `_queue_worker` (this thread's own bookkeeping, matching the
    `finally` wrapper's own guard against clobbering a newer thread) and
    returns True so the caller can `return`.
    """
    global _queue_worker
    with _queue_lock:
        if store.queue()["state"] in _QUEUE_RESUMABLE:
            return False
        if _queue_worker is threading.current_thread():
            _queue_worker = None
        return True


def _drain_queue() -> None:
    """Thread entry point. Delegates the actual walk to `_drain_queue_body`;
    this wrapper's only job is guaranteeing `_queue_worker` is cleared under
    `_queue_lock` as the worker's LAST act (review round 1, I4) — so
    `_start_queue_worker`'s "is a worker already running" check has an
    explicit, lock-synchronized signal to consult instead of leaning only on
    `Thread.is_alive()`, which can still read True for a thread that has
    already decided to return but hasn't finished unwinding. `_worker_may_stop`
    (fix round 2) closes the more common half of this same race — a resume
    landing between a stop DECISION and this cleanup, which turned out to be
    reproducible about 1 time in 5 rather than a negligible edge case — so
    this `finally` now mainly guards paths `_worker_may_stop` doesn't cover
    (e.g. an unhandled exception) and remains defense in depth either way.
    """
    global _queue_worker
    try:
        _drain_queue_body()
    finally:
        with _queue_lock:
            if _queue_worker is threading.current_thread():
                _queue_worker = None


def _drain_queue_body() -> None:
    gov = Governor(store.pacing())
    gov.note_interruption()                  # every (re)start floors the rate at 1.8 (never raises it)
    store.save_pacing(gov.to_state())
    with _queue_lock:
        q = store.queue()
        if not q.get("pile_count_at_enqueue"):
            # Fixed at the run's start so `_queue_progress`'s pile_count is a
            # stable denominator for boxdash, not one that drops to 0 as
            # `pending` and `current` drain to empty at the very end.
            q["pile_count_at_enqueue"] = len(q.get("pending", [])) + (1 if q.get("current") else 0)
            store.save_queue(q)
    if not _set_queue_state("running"):
        if _worker_may_stop():
            return                            # paused/cancelled before this thread even ran
        # Minor 1 (review round 2): a resume raced in first (I4) — state is
        # running again, but our OWN guarded write above never landed, so a
        # stop_reason `keep_stop_reason=True` preserved (e.g. "quota") would
        # otherwise linger under state "running" forever. The M1 docstring
        # promise is that the worker's own unconditional "running"
        # transition clears it — this IS that transition, it just needed a
        # second attempt now that the guard is known to pass (state is
        # already resumable, so this plain call cannot be refused by
        # anything except a fresh pause/cancel landing in the next instant,
        # which is exactly the state that call would then correctly record).
        _set_queue_state("running")
    while True:
        action = _queue_next_action(time.time())
        if action[0] == "stop":
            if _worker_may_stop():
                return
            continue                          # a resume raced in first (I4) — re-decide, don't exit
        if action[0] == "sleep":
            if not _set_queue_state(action[2]):
                if _worker_may_stop():
                    return                     # a pause/cancel beat us to the write (R-T8a)
                continue                       # ...or a resume beat THAT (I4) — re-decide
            # note_interruption() only ever pulls the rate DOWN to at most
            # START_RATE (ruling R-T8f, sortify/pacing.py) — so calling it
            # unconditionally here is safe even when this sleep is the
            # direct continuation of a rate 429 whose halving `note_429`
            # just applied one iteration ago: a halved rate is already at or
            # below START_RATE, so the reset is a no-op for it, and every
            # other sleep (reserve cap, quiet period, an externally-sourced
            # cooldown) still gets its `_clean_since` correctly cleared.
            gov.note_interruption()
            store.save_pacing(gov.to_state())
            # M-4: poll in ≤60s chunks WITHOUT bouncing queue.json through
            # "running" and straight back to the same sleep label every
            # chunk — a multi-hour bulk-reserve wait used to rewrite
            # queue.json (and bump updated_at) twice a minute for the whole
            # span with no actual transition behind either write. Instead,
            # re-check the same block sp.bulk_block_reason() would recompute
            # at the top of the outer loop; if it's still the SAME reason
            # (same displayed state), just keep waiting in place — only a
            # genuine change (block clears, or clears to a different
            # labelled state) breaks out to do the real write.
            while True:
                _queue_wake.wait(min(action[1], 60))
                # Cleared unconditionally right after waiting — whether it
                # fired or the wait simply timed out — so a set() landing
                # mid-sleep is consumed exactly once and can never latch
                # into a file-churning hot loop on the next iteration
                # (C1/I1).
                _queue_wake.clear()
                with _queue_lock:
                    sleeping_q = store.queue()
                if sleeping_q["state"] not in _QUEUE_RESUMABLE:
                    break                      # paused/cancelled — let the write below report it
                still_blocked = sp.bulk_block_reason(
                    spend_reserve=bool(sleeping_q.get("spend_reserve")))
                if still_blocked:
                    reason, until = still_blocked
                    state_label = "quiet" if reason == "quiet" else "sleeping"
                    if state_label == action[2]:
                        action = (action[0], max(until - time.time(), 1.0), action[2])
                        continue               # unchanged — no write, keep waiting
                break                          # cleared, or changed to a different label
            if not _set_queue_state("running"):
                if _worker_may_stop():
                    return                     # paused/cancelled during the sleep
                # else: a resume raced in first (I4) — keep going
            continue
        _, playlist_id, pile_id, spend_reserve = action
        tick_started = time.monotonic()
        try:
            result = _materialise_tick(playlist_id, pile_id, spend_reserve=spend_reserve)
        except SpotifyError as e:
            info = sp.last_429 or {}
            if e.status == 429 and info.get("ts", 0) >= tick_started:
                gov.note_429(info["kind"], info.get("retry_after", 60), time.time())
                store.save_pacing(gov.to_state())
                if info["kind"] == "quota":
                    # Permanent: request() already published note_cooldown to
                    # the account ledger; resuming is a human's call (spec §2).
                    _set_queue_state("stopped", stop_reason="quota")
                    if _worker_may_stop():
                        return
                    # A resume raced in first (I4/round 2) — that's a
                    # legitimate human action even right after a quota trip
                    # (binding note: resume IS the human-only unblock here).
                    # Keep going: the very next _queue_next_action() call
                    # hits bulk_block_reason() and sleeps through the still-
                    # active cooldown, so nothing actually spends early.
                    continue
                continue                     # rate: cooldown sleep happens above
            # No fresh 429 to pin this on. Some raises never touch last_429
            # at all — the pre-flight cooldown check and the bulk-reserve
            # guard in spotify.py both raise SpotifyError(429, ...) without
            # stamping it. Consult the real block reason before treating
            # this as a hard stop (ruling R-T8b): a reserve-crossing race or
            # a cooldown that started between calls must ride out as a
            # sleep, not park a multi-day run on a transient race.
            if sp.bulk_block_reason(spend_reserve=spend_reserve):
                continue                     # next loop's sleep branch labels it correctly
            # Auth failures, 5xx, and anything else unexplained: stop
            # spending and surface the reason rather than grind a broken loop.
            log.error("queue worker paused by error: %s", e)
            _set_queue_state("paused", stop_reason=str(e))
            if _worker_may_stop():
                return
            continue                          # a resume raced in first (I4/round 2) — retry the tick
        info = sp.last_429 or {}
        fresh = info.get("ts", 0) >= tick_started
        if fresh:
            gov.note_429(info["kind"], info.get("retry_after", 60), time.time())
        elif result.get("spent", 0) >= 1:
            # Only a tick that actually spent a call earns clean-time
            # credit (I3) — a vanished-split or in-flight-backoff tick
            # spends nothing and must not count toward escalation.
            gov.note_success(time.time())
        store.save_pacing(gov.to_state())
        if fresh and info["kind"] == "quota":
            # A quota trip can surface on the success path too: `sp` is a
            # shared client, so another thread's concurrent call (e.g.
            # /api/now) can trip quota while THIS tick's own call succeeds.
            # Mirror the except-branch handling exactly (C3) — a quota trip
            # must stop the worker no matter which path noticed it.
            _set_queue_state("stopped", stop_reason="quota")
            if _worker_may_stop():
                return
            continue                          # a resume raced in first (I4/round 2) — see the except branch above
        with _queue_lock:
            q = store.queue()
            q["progress"] = _queue_progress(q)
            q["updated_at"] = _now_iso()
            store.save_queue(q)
        # Governor-paced wait before the next tick; consumed exactly once,
        # same reasoning as the sleep branch above (C1/I1) — the top of the
        # loop re-decides from queue.json regardless of why we woke.
        _queue_wake.wait(gov.interval())
        _queue_wake.clear()


class QueueIn(BaseModel):
    pile_ids: list[str] | None = None    # None = every pile in the split
    # The echo, not a flag — same argument as the endpoint this replaces:
    # the caller must state the price it was shown (finding I1).
    expected_calls: int = Field(..., ge=0)
    # Per-run opt-out of BULK_RESERVE: this run may spend the day's last
    # 150 calls instead of sleeping at DAILY_CAP - BULK_RESERVE. Recorded in
    # queue.json so a resume keeps the choice made at enqueue time.
    spend_reserve: bool = False


def _start_queue_worker() -> None:
    """The ONLY function that creates the worker thread — called from exactly
    two places (enqueue and resume; pinned by test_the_queue_worker_cannot_self_start).
    No other code path may bring Spotify traffic into existence on its own."""
    global _queue_worker
    with _queue_lock:
        if _queue_worker and _queue_worker.is_alive():
            return
        _queue_wake.clear()
        _queue_worker = threading.Thread(
            target=_drain_queue, name="queue-materialiser", daemon=True)
        _queue_worker.start()


def _effective_queue() -> dict:
    """queue.json with the state a reader should act on: a file that says
    "running" while no worker thread is alive is a restart's leftover —
    paused, resumable, and starting nothing by itself."""
    q = store.queue()
    if q["state"] in _QUEUE_RESUMABLE and not (
            _queue_worker and _queue_worker.is_alive()):
        q["state"] = "paused"
    return q


@app.post("/api/split/{playlist_id}/queue")
def enqueue_piles(playlist_id: str, body: QueueIn):
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        if not split:
            raise HTTPException(404, "no split for that playlist")
        # M3 (review round 1): `[]` is an explicit "nothing selected", not
        # the same thing as `None`'s "everything" — `body.pile_ids or [...]`
        # used to treat them the same because `[]` is falsy.
        if body.pile_ids is not None and not body.pile_ids:
            raise HTTPException(409, "no piles selected")
        wanted = body.pile_ids if body.pile_ids is not None else [p["id"] for p in split["piles"]]
        piles = [p for p in split["piles"] if p["id"] in wanted]
        if len(piles) != len(set(wanted)):
            raise HTTPException(404, "unknown pile id in the request")
        plans = {p["id"]: _materialise_plan(
            split, p, reconciled=(playlist_id, p["id"]) in _reconciled)
            for p in piles}
        total = sum(pl["calls"] for pl in plans.values())
        if body.expected_calls != total:
            raise HTTPException(
                409,
                f"cost has changed: saving these piles now spends {total} "
                f"Spotify calls, not the {body.expected_calls} you confirmed. "
                "Nothing was spent. Re-open the pile list and confirm the new "
                "number.")
        missing_count = {p["id"]: len(plans[p["id"]]["missing"]) for p in piles}
        # Batching means every pile under 100 tracks now costs the same 2
        # calls (1 create/lookup + 1 add), so `calls` alone no longer orders
        # small piles ahead of large ones within that tie. Break ties by how
        # many tracks are actually missing, so the 2026-08-18 intent —
        # smallest piles first, so finished playlists appear early — survives
        # batching instead of degrading into enqueue order.
        order = sorted((pid for pid in plans if plans[pid]["calls"] > 0),
                       key=lambda pid: (plans[pid]["calls"], missing_count[pid]))
    if total == 0:
        return {"ok": True, "queued": [], "total_calls": 0, "complete": True}
    with _queue_lock:
        q = store.queue()
        # R-T9a (review round 1, I2): refuse whenever anything is pending or
        # in progress, regardless of `state` — a PAUSED, resumable run is
        # still user work sitting there; replacing it needs an explicit
        # cancel first, not a second enqueue that overwrites its plan. The
        # old check only looked at "running"/"sleeping"/"quiet" and let a
        # second enqueue silently clobber a paused run's `pending`.
        if q.get("pending") or q.get("current"):
            # Minor 2 (review round 2): name the OWNING playlist — a user on
            # playlist B's page hitting this needs to know where to go to
            # cancel, not just that "a queue" exists somewhere.
            raise HTTPException(
                409, f"a queue for {q.get('playlist_id')!r} already has "
                     "piles pending or in progress — pause or cancel it "
                     "first")
        store.save_queue({"version": 1, "playlist_id": playlist_id,
                          "pending": order, "current": None, "state": "running",
                          "stop_reason": None, "progress": {},
                          "spend_reserve": body.spend_reserve,
                          "pile_count_at_enqueue": len(order),
                          "enqueued_at": _now_iso(), "updated_at": _now_iso()})
    _start_queue_worker()
    return {"ok": True, "queued": order, "total_calls": total, "complete": False}


def _strip_source_prefix(current: str, pile_name: str) -> str:
    """The bare pile half of an output's current title.

    Design §3 fixes an output's name at create time: a later rename of the
    source does not ripple. This action deliberately does ripple — it is the
    user asking for it — but must not COMPOUND. Composing straight from the
    record's current name turns `oldsrc · pile` into `newsrc · oldsrc · pile`
    (and `newer · newsrc · oldsrc · pile` the time after that), because the
    skip test only recognises the source's *present* name.

    The prefix is stripped only when what remains is exactly this pile's own
    name. Pile names are themselves ` · `-joined tag lists
    ("cumbia · latin · salsa"), so an unconditional split on the first
    separator would eat a real tag off an output that never carried a source
    prefix at all. A title truncated at 100 chars won't match and keeps its
    old behaviour — one compounded rename, not a growing one.
    """
    head, sep, tail = current.partition(" · ")
    return tail if sep and tail == pile_name else current


# One rename per output, all inside a single request, all on the interactive
# budget. Past WINDOW_CAP (12/60s) `_spend_budget` sleeps *while holding
# `_budget_lock`* (sortify/spotify.py) — which stalls every other Spotify call
# in the process behind it, the now-playing poll included, for the best part
# of a minute. The realistic split has 8 outputs, so a cap here costs nothing
# real and keeps one click from parking the whole app.
RENAME_OUTPUTS_MAX = 12


class RenameOutputsIn(BaseModel):
    # The echo, not a flag — the caller states the price it was shown (I1).
    expected_calls: int = Field(..., ge=0)


@app.post("/api/split/{playlist_id}/rename_outputs")
def rename_outputs(playlist_id: str, body: RenameOutputsIn):
    """Rename this split's materialised playlists to `{source} · pile` form.

    Explicit and priced, never automatic (design §3): outputs created before
    the naming rule keep their bare pile names until the user asks. One
    rename call per playlist, interactive budget — this is a user click.
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        if not split:
            raise HTTPException(404, "no split for that playlist")
        source = _source_playlist_name(playlist_id)
        if not source:
            raise HTTPException(
                409, "the source playlist's name isn't in the cached listing — "
                     "Refresh the Playlists view first (free if unchanged)")
        mats = split.get("materialised") or {}
        todo = []
        # Walk the CURRENT piles, not every materialisation record: /recluster
        # carries records forward without sweeping orphans (only create_split
        # does), so a record whose pile id is gone is invisible to the client
        # — counting it here would quote N in the button and validate against
        # N+k, 409ing the rename permanently with no route out of the UI.
        # Price and display have to be computed from one collection, and the
        # client's is `data.piles`. The falsy-name skip matches it too.
        for pile in split.get("piles") or []:
            rec = mats.get(pile["id"])
            if not rec or not rec.get("playlist_id"):
                continue
            cur = rec.get("name") or ""
            if not cur or cur.startswith(f"{source} · "):
                continue
            todo.append((pile["id"], rec["playlist_id"], rec.get("claim"),
                         split_output_name(source, _strip_source_prefix(cur, pile["name"]))))
    if len(todo) > RENAME_OUTPUTS_MAX:
        raise HTTPException(
            409, f"renaming {len(todo)} saved playlists in one request would "
                 f"spend {len(todo)} interactive Spotify calls back to back; "
                 f"past {RENAME_OUTPUTS_MAX} the client sleeps holding the "
                 "budget lock and stalls everything else, the now-playing poll "
                 "included. Nothing was spent — re-run once fewer than "
                 f"{RENAME_OUTPUTS_MAX + 1} outputs still need it (piles "
                 "materialised from here on are already named correctly).")
    if body.expected_calls != len(todo):
        raise HTTPException(
            409, f"cost has changed: renaming now spends {len(todo)} Spotify "
                 f"calls, not the {body.expected_calls} you confirmed. "
                 "Nothing was spent.")
    renamed = []
    for pid, spotify_id, claim, target in todo:
        sp.rename_playlist(spotify_id, target)
        # CAS so a concurrent re-cluster's fresh record isn't stamped with a
        # stale name; a refused write just means the record moved on — the
        # playlist itself is renamed either way, which is what was asked.
        _claim_materialisation(playlist_id, pid, claim, name=target)
        renamed.append({"pile_id": pid, "name": target})
    return {"ok": True, "renamed": renamed}


@app.get("/api/split/{playlist_id}/queue")
def queue_status(playlist_id: str):
    # M4 (review round 1): GET stays global on purpose — it's the read path
    # boxdash and every split page poll, and a playlist_id mismatch here
    # should show "nothing queued for you", not 409. pause/resume/cancel are
    # the ones that mutate, so those are the ones that check ownership.
    return {"queue": _effective_queue(), "pacing": store.pacing()}


def _require_owned_queue(playlist_id: str, q: dict) -> None:
    """R-T9c (review round 1, M4): pause/resume/cancel act on the one
    queue.json in the whole app, so a stale tab still pointed at a playlist
    whose run already finished (or that never owned the current run) must
    not be able to touch someone else's queue."""
    if q.get("playlist_id") != playlist_id:
        raise HTTPException(409, "no active queue for this playlist")


@app.post("/api/split/{playlist_id}/queue/pause")
def pause_queue(playlist_id: str):
    with _queue_lock:
        q = store.queue()
        _require_owned_queue(playlist_id, q)
        # I3 (review round 1): honour the guard's refusal instead of always
        # answering {"ok": true} — pausing a queue that already finished or
        # was already stopped must not resurrect it into "paused", and the
        # caller needs to know the click didn't do what it asked.
        if not _apply_queue_state(q, "paused", None):
            raise HTTPException(
                409, f"queue is {q['state']}, not running — nothing to pause")
    _queue_wake.set()
    return {"ok": True}


@app.post("/api/split/{playlist_id}/queue/resume")
def resume_queue(playlist_id: str):
    # M1 (review round 1): the emptiness check and the state write happen
    # under the SAME _queue_lock acquisition now, so a concurrent cancel
    # can't empty `pending`/`current` between this check and the write.
    with _queue_lock:
        q = store.queue()
        _require_owned_queue(playlist_id, q)
        if not q.get("pending") and not q.get("current"):
            raise HTTPException(409, "nothing queued")
        # force=True: resume's whole job is leaving "paused"/"stopped" — the
        # check-and-set guard exists to stop everyone ELSE from doing that.
        # keep_stop_reason=True: a quota/error reason the user is still
        # looking at should not flicker to None the instant this click
        # lands — the worker's own unconditional "running" transition (top
        # of `_drain_queue_body`) is what actually clears it once the run
        # is genuinely under way again.
        _apply_queue_state(q, "running", None, force=True, keep_stop_reason=True)
    _start_queue_worker()
    return {"ok": True}


@app.delete("/api/split/{playlist_id}/queue")
def cancel_queue(playlist_id: str):
    with _queue_lock:
        q = store.queue()
        _require_owned_queue(playlist_id, q)
        q.update(pending=[], current=None, state="stopped",
                 stop_reason="cancelled", updated_at=_now_iso())
        store.save_queue(q)
    _queue_wake.set()
    return {"ok": True}


class DecideIn(BaseModel):
    uri: str
    action: str  # "keep" | "reject" | "undecide"
    to_id: str | None = None


@app.post("/api/split/{playlist_id}/decide")
def decide(playlist_id: str, body: DecideIn):
    """Record a decision on one track from a split.

    Keep costs one Spotify call (added to a home playlist, or Liked Songs);
    reject costs none — it is recorded locally and the source playlist is
    never touched. That asymmetry is the whole saving over the input-playlist
    flow, which drains its source at 2 calls per decision: over 1372 tracks
    that is ~1372 calls saved, and the original playlist survives intact as
    an archive.

    A decision is final through this endpoint once it is a *keep* —
    correcting one means moving or removing an ordinary playlist track,
    which is exactly what `/api/act` + `/api/undo` already do; duplicating
    that here would be a second, hidden way to spend a call. A *reject* is
    different: since it never touched Spotify, changing your mind about one
    costs nothing more than deciding fresh, so a later "keep" on a
    previously-rejected uri is honoured as a correction rather than a no-op —
    and `action="undecide"` on a *rejected* uri (only) clears it back to
    undecided for free, so an accidental reject doesn't permanently drop a
    track out of every future sitting (`pick_sitting` skips anything in
    `decided`). `undecide` on anything else — a keep, or a uri with no
    decision at all — is a no-op for the same immutable-keep reason above.
    Every no-op response says so explicitly via `"changed": false` and
    `"decision"`, rather than looking identical to a real change.

    A keep is recorded as `"pending": true` at the same moment it is
    reserved, *before* the Spotify call — the call itself cannot happen
    under `_split_lock` (nothing blocking may run while it's held), so the
    reservation has to exist first or two concurrent requests for the same
    uri could both pass the "not yet decided" check and both spend a call.
    That ordering has its own gap: if this process dies between writing the
    reservation and the call finishing (a systemd restart mid-cooldown is
    this app's own history), the entry is left permanently `"pending": true`
    with nothing to ever clear it — a `pending` marker is only trustworthy
    to a request running in the SAME process, tracked by `_pending_keeps` in
    memory. A later request in the same process sees the uri is genuinely
    still in flight and correctly no-ops; a request after a restart sees an
    empty `_pending_keeps` (this process never started that call) and
    correctly treats the leftover `pending` entry as abandoned and retries
    it. The tradeoff this accepts: a retry after a genuine crash may
    double-add if the original call had actually landed just before the
    process died — unprovable without spending a call to check, and strictly
    better than the alternative (a keep silently recorded forever for a
    track that was never added, un-retryable, because a plain `"action":
    "keep"` entry is otherwise final).

    An abandoned pending keep may only be settled by ANOTHER keep — a retry
    of the same operation — never by a different action. Without that
    restriction, `reject` could settle over an abandoned pending keep too
    (its outcome is just as "unknown, retry" as a keep's own retry), and a
    later `undecide` on that reject would erase it completely: keep (crash)
    -> reject (settles the abandoned pending entry) -> undecide (clears the
    reject) leaves the uri fully undecided again with no memory that an add
    might already have landed, and a later keep can then genuinely double-
    add. Restricting the retry to the same action closes that chain at its
    first step, and keeps a pending keep resolving the same way an ordinary
    keep does: only by completing (or re-failing) itself.
    """
    if body.action not in ("keep", "reject", "undecide"):
        raise HTTPException(400, f"unknown action {body.action!r}")
    if body.action == "keep" and not body.to_id:
        raise HTTPException(400, "keep needs to_id")

    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(playlist_id)
        if not split:
            raise HTTPException(404, "no split for that playlist")
        if not any(body.uri in p["uris"] for p in split["piles"]):
            # Guards `_remaining` as much as the request itself: an
            # out-of-pile uri would add a `decided` entry no pile's
            # occurrence count could ever account for.
            raise HTTPException(404, "that track is not in this split")

        decided = split["decided"]
        previous = decided.get(body.uri)

        if body.action == "undecide":
            changed = previous is not None and previous["action"] == "reject"
            if changed:
                del decided[body.uri]
                store.save_splits(payload)
            # What actually stands now: nothing, if this cleared a reject
            # (changed=True) or there was never anything here — otherwise
            # whatever this no-op left untouched, most commonly a keep,
            # which undecide can never touch (see the docstring). Reporting
            # unconditional None here was a real regression: a client that
            # trusts "the server tells you what actually stands" (the whole
            # point of `decision`) would render a settled keep as cleared,
            # even though disk still holds it and no Spotify call happened.
            decision = (
                None if changed or previous is None
                else {"action": previous["action"], "to_id": previous.get("to_id")}
            )
            return {
                "ok": True, "remaining": _remaining(split), "changed": changed,
                "decision": decision,
            }

        in_flight = (playlist_id, body.uri) in _pending_keeps
        settle = (
            previous is None
            or (previous["action"] == "reject" and body.action == "keep")
            # An abandoned pending keep (crash-recovery — see the docstring)
            # may only be settled by ANOTHER keep, i.e. a retry of the same
            # operation, never by a different action. Without `body.action
            # == "keep"` here, a reject could settle over it — turning
            # "outcome unknown, retry as keep" into a confident, ordinary
            # reject that remembers nothing about the unresolved add — and a
            # later `undecide` on that reject would erase it entirely,
            # opening the uri back up to a fresh keep with no record that an
            # add might already have landed. Restricting the retry to the
            # same action closes that whole chain at its first step: a
            # pending keep now resolves only by completing (or re-failing)
            # itself, matching the immutable-keep rule everywhere else in
            # this endpoint.
            or (previous.get("pending") and body.action == "keep" and not in_flight)
        )
        if not settle:
            # A snapshot, not a guarantee: if `previous` is still `pending`
            # (the winner is genuinely in flight right now), that winner's
            # own call can still fail and roll back after this response is
            # sent, making the `remaining`/`decision` reported here stale.
            # Accepted rather than fixed — closing it would mean blocking
            # this request on the winner's outcome, which is exactly the
            # coupling `_split_lock` exists to avoid. It is safe in the
            # direction that matters: nothing here mis-reports a call as
            # spent when it was not, only occasionally the reverse.
            return {
                "ok": True, "remaining": _remaining(split), "changed": False,
                "decision": {"action": previous["action"], "to_id": previous.get("to_id")},
            }

        entry = {"action": body.action, "to_id": body.to_id, "at": _now_iso()}
        if body.action == "keep":
            entry["pending"] = True
            _pending_keeps.add((playlist_id, body.uri))
        decided[body.uri] = entry
        store.save_splits(payload)
        remaining = _remaining(split)

    # The Spotify call (if any) happens outside the lock — see the module
    # note on `_split_lock`: nothing blocking may run while it is held.
    if body.action == "keep":
        try:
            if body.to_id == LIKED_ID:
                sp.save_to_liked(body.uri)
            else:
                snapshot_id = sp.add_to_playlist(body.to_id, body.uri)
        except Exception:
            # The call never landed, so the track was never actually kept.
            # Roll the reservation back to whatever it was before (nothing,
            # or the reject we just tried to override) so a retry finds it
            # undecided instead of permanently, wrongly, "kept". Nothing else
            # in this process could have settled this uri in the meantime
            # (`_pending_keeps` made every concurrent request in this process
            # see it as in-flight and no-op), so restoring exactly `previous`
            # is safe.
            with _split_lock:
                _pending_keeps.discard((playlist_id, body.uri))
                payload = store.splits()
                split = payload["splits"].get(playlist_id)
                if split is not None:
                    if previous is None:
                        split["decided"].pop(body.uri, None)
                    else:
                        split["decided"][body.uri] = previous
                    store.save_splits(payload)
            raise

        # The add itself is done — never roll the decided entry back again
        # past this point, even if the local bookkeeping below fails; that
        # would undo a Spotify call that already succeeded, and a retry
        # would double-add. The `finally` still has to clear `_pending_keeps`
        # and the on-disk "pending" flag no matter what, though — otherwise
        # a bug in `_apply_snapshot`/`_cache_move` would leave this uri
        # permanently "in flight" in this process (every future decide()
        # would see `in_flight=True` from `_pending_keeps` forever) and
        # permanently `"pending": true` on disk, neither of which is true
        # any more: the add landed.
        try:
            if body.to_id != LIKED_ID:
                _apply_snapshot(body.to_id, snapshot_id)
            # Mirrors the destination's cache the same way `act`'s move
            # branch does: without this, `_cached_tracks` still has the
            # pre-keep snapshot_id stamped fresh but missing the track, so
            # it looks up-to-date forever, and both a later `/api/act`
            # re-add and the suggestion engine's home profiles never learn
            # the track landed.
            _cache_move(body.uri, None, body.to_id)
        finally:
            # Disk first, THEN memory — not the other order. If the save
            # here fails (a disk hiccup, no crash needed), clearing
            # `_pending_keeps` first would leave the in-memory state saying
            # "not in flight" while the on-disk entry still says `"pending":
            # true`. The very next plain retry in this same process would
            # then read that combination as an abandoned crash and spend a
            # second, duplicate add for a track that already landed. Saving
            # first means a failure here instead leaves both disk AND memory
            # agreeing the uri is still in flight — indistinguishable from a
            # genuinely slow call, so every retry in this process correctly
            # no-ops — and only a real process restart (empty
            # `_pending_keeps`) will ever retry it, exactly the case the
            # crash-recovery path above already handles safely.
            with _split_lock:
                payload = store.splits()
                split = payload["splits"].get(playlist_id)
                if split is not None and body.uri in split["decided"]:
                    split["decided"][body.uri].pop("pending", None)
                    store.save_splits(payload)
                _pending_keeps.discard((playlist_id, body.uri))

    return {
        "ok": True, "remaining": remaining, "changed": True,
        "decision": {"action": body.action, "to_id": body.to_id},
    }


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
        value = sp.currently_playing()
        ttl = _now_ttl(value)
        if _skip_settle["uri"] and time.time() < _skip_settle["until"]:
            if ((value or {}).get("track") or {}).get("uri") == _skip_settle["uri"]:
                ttl = min(ttl, NOW_SKIP_SETTLE_TTL)
            else:
                _skip_settle.update(uri=None, until=0.0)  # settled: back to normal
        _now_cache.update(at=time.time(), value=value, ttl=ttl)
        return value, ttl


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


def _sitting_for_context(ctx_id: str | None) -> dict | None:
    """If the currently-playing context is a sitting's disposable playlist,
    say so — and hand back that split's `decided` map for it, restricted to
    this sitting's own uris.

    This is the fix for a real gap: without it, a reloaded tab has no way to
    know a sitting is active (the client-side pointer to it lives only in
    memory), so filing a track goes through the ordinary /api/act path
    instead of /api/split/.../decide — 1 Spotify call spent, nothing written
    to `decided`, and `pick_sitting` serves the exact same track again in a
    later sitting. A local `store.splits()` read on every /api/now poll (the
    poll that already exists) closes it for free and survives reload by
    construction: there's no client state to lose.

    Local read only, no Spotify call — matches every other helper in this
    section. Scans every split's `active_sitting`, since nothing here is
    keyed by split id the way the /api/split/{id} routes are; in practice
    there are at most a couple of splits on disk at once.
    """
    if not ctx_id:
        return None
    for split_id, split in store.splits()["splits"].items():
        active = split.get("active_sitting")
        if active and active.get("playlist_id") == ctx_id:
            pile = next((p for p in split["piles"] if p["id"] == active["pile_id"]), None)
            uris = active.get("uris") or []
            decided = split.get("decided", {})
            return {
                "split_id": split_id,
                "pile_id": active["pile_id"],
                "pile_name": pile["name"] if pile else active["pile_id"],
                "uris": uris,
                "decided": {u: decided[u] for u in uris if u in decided},
            }
    return None


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
    which must never reach Last.fm at all. Reuses `_lastfm_client()`/`enrich`,
    the same client, `MIN_INTERVAL` pacing and tags.json envelope shape the
    split flow uses, rather than a second one; no key configured means
    `_lastfm_client()` returns None and this does nothing, silently.

    Two bounds on top of that reuse: the `LastFm` instance this path gets
    back from `_lastfm_client()` has its transport swapped for one built with
    `timeout=NOW_FETCH_TIMEOUT` — a fresh `httpx.Client`, only on this
    instance, never touching the splitter's own — *and* `fm._timeout` is set
    to the same value, since `LastFm.top_tags` passes `timeout=self._timeout`
    on every request, which in httpx overrides whatever the client itself was
    constructed with. Setting only the client would leave every actual
    request still bounded by the splitter's 15s default; both need setting
    for this path to actually be bounded at 5s — and the SAME swapped
    instance is reused for the track-record step below, so that step is
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
    from the split flow can't lose (or be lost by) this write.

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
            (t for t in entry.get("tracks") or []
             if t.get("name") and (t.get("artists") or [{}])[0].get("name")),
            key=lambda t: t.get("added_at") or "", reverse=True,
        )
        clips = []
        consumed = 0
        for t in candidates[offset:offset + PREVIEW_MAX_ATTEMPTS]:
            if len(clips) >= PREVIEW_CLIPS:
                break
            consumed += 1
            artist = t["artists"][0]["name"]
            try:
                rec = _deezer_client().fetch_preview(artist, t["name"])
            except Exception:
                log.warning("deezer preview failed for %r — skipping", t["name"])
                continue
            if not rec.get("miss"):
                clips.append({"name": t["name"], "artist": artist, "url": rec["url"]})
        nxt = offset + consumed
        payload = {
            "clips": clips,
            "next_offset": nxt if nxt < len(candidates) else None,
            "tracks": [{"name": t["name"], "artist": t["artists"][0]["name"]}
                       for t in candidates[:PREVIEW_LIST_N]],
            "total": len(entry.get("tracks") or []),
        }
        _preview_cache[(playlist_id, offset)] = (time.monotonic(), payload)
        return payload


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
    items = (store.cache().get("playlist_list") or {}).get("items") or []
    by_id = {p["id"]: p for p in items}
    out = []
    for iid in sorted(_effective_input_ids(store.config(), items)):
        if iid == LIKED_ID:
            out.append({"id": iid, "name": "Liked Songs", "has_track": False})
        elif iid in by_id:
            out.append({"id": iid, "name": by_id[iid]["name"], "has_track": False})
    return out


@app.get("/api/now")
def now_playing(force: bool = False):
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

    state = _ensure_profiles()
    track = np["track"]
    # A targeted artist fetch used to sit here to sharpen the card's genre
    # reasons; it cost a call per unseen artist to learn nothing, since the
    # dev-mode API stopped returning Spotify genres at all. `_fetch_missing_now_tags`
    # is that fetch's replacement — Last.fm instead of Spotify, bounded, and
    # gated on `force` so the passive poll still spends nothing.
    sortable = track["type"] == "track" and not track["is_local"] and track.get("id")
    if force and sortable:
        try:
            _fetch_missing_now_tags(track)
        except Exception:
            # A fetch failure must never break the now response — this is a
            # bonus enrichment, not the payload the client actually needs.
            log.exception("now-playing tag fetch failed")
    # Guard-on-read, fresh on every request — same reasoning as `triage`'s own
    # re-read below `_ensure_profiles`: `_profile_state`'s cached profiles are
    # a profile-build-time snapshot that can sit stale for up to PROFILE_TTL,
    # so a fetch just above (or any other write to tags.json) would otherwise
    # be invisible for up to 10 minutes. Deliberate freshness/cost trade-off: a
    # local JSON read on every poll (zero API cost) buys always-current tags
    # instead of leaning on the profile cache. lastfm_track_map() gets the
    # same treatment, and for the same reason — the force-path fetch just
    # above can land a brand-new neighbour/track-tag record mid-poll.
    tag_artists = store.tag_artists()
    track_map = store.lastfm_track_map()
    artist_map = store.lastfm_artist_map()
    ctx_id = np["context_playlist_id"]
    ctx = next((p for p in state["playlists"] if p["id"] == ctx_id), None)
    return {
        "playing": True,
        "poll_after_ms": _poll_after_ms(stale_in),
        "fetched_ago_ms": _now_fetched_ago_ms(),
        "is_playing": np["is_playing"],
        "progress_ms": np["progress_ms"],
        "track": {**track, "sortable": bool(sortable)},
        "context": (
            {"id": ctx_id, "name": ctx["name"] if ctx else None,
             "is_input": ctx_id in state["input_ids"]}
            if ctx_id else None
        ),
        "sitting": _sitting_for_context(ctx_id),
        "suggestions": sugg.suggest(
            track, state["profiles"], tag_artists, track_map, artist_map,
            state.get("playlist_artists"),
        ) if sortable else [],
        "subsets": _subset_matches(state, track, tag_artists, track_map, artist_map) if sortable else [],
        "subset_targets": _subset_targets_payload(state),
        "homes": _homes_payload(state),
        "inputs": [
            {"id": l["id"], "name": l["name"], "has_track": track["uri"] in l["uris"],
             "set": l.get("set", inputsets.DEFAULT_KEY)}
            for l in state["inputs"]
        ],
    }


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
    out = _playback_call(sp.skip_next)
    if prev_uri:
        with _now_lock:
            _skip_settle.update(uri=prev_uri, until=time.time() + NOW_SKIP_SETTLE_WINDOW)
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


def _cache_move(uri: str, from_id: str | None, to_id: str | None) -> dict | None:
    """Mirror a move in the local cache; returns the track dict if we had it."""
    cache = store.cache()
    track = None
    for pid in cache["playlists"]:
        for t in cache["playlists"][pid]["tracks"]:
            if t["uri"] == uri:
                track = t
                break
        if track:
            break
    if from_id and from_id in cache["playlists"]:
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
    store.save_cache(cache)
    return track


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
    # Keyed on the destination's NAME, not `_effective_subset_ids` — the
    # picker in §5 reaches every {}-named playlist, opted in or not, and the
    # guard has to cover the same reach or ~69 of 70 subsets go unguarded.
    #
    # Reads the cached listing directly rather than `sp.my_playlists()`,
    # which fetches (~21 paginated calls, ~60s stall) when
    # `cache["playlist_list"]` is absent — e.g. right after a cache wipe.
    # With nothing cached there is nothing to check against, so the guard
    # is skipped rather than paying that cost to enforce it. Genuinely
    # costs nothing only with a warm cache; degrades to no-guard, not a
    # fetch, when cold.
    if body.to_id and body.from_id:
        cfg = store.config()
        listing = (store.cache().get("playlist_list") or {}).get("items") or []
        name_by_id = {p["id"]: p.get("name") or "" for p in listing}
        pattern = cfg.get("subset_name_pattern") or DEFAULT_SUBSET_PATTERN
        if body.to_id in name_by_id and is_subset_name(name_by_id[body.to_id], pattern):
            raise HTTPException(
                400,
                "that destination is a subset — adding to a subset must not "
                "remove the track from its input; send from_id: null",
            )
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
        _cache_move(body.uri, body.from_id, body.to_id)
        undo_stack.append(
            {"uri": body.uri, "from_id": body.from_id, "to_id": body.to_id, "added": added}
        )
    elif body.action == "remove":
        if not body.from_id:
            raise HTTPException(400, "remove needs from_id")
        _apply_snapshot(body.from_id, sp.remove_from_playlist(body.from_id, body.uri))
        _cache_move(body.uri, body.from_id, None)
        undo_stack.append({"uri": body.uri, "from_id": body.from_id, "to_id": None, "added": False})
    else:
        raise HTTPException(400, f"unknown action {body.action!r}")
    _sync_input_membership(body.uri, add_to=body.to_id, remove_from=body.from_id)
    del undo_stack[:-20]
    return {"ok": True, "note": note, "can_undo": True}


def _sync_input_membership(uri: str, add_to: str | None, remove_from: str | None) -> None:
    """Keep the in-memory input membership (capture chips) current."""
    for l in _profile_state.get("inputs", []):
        if l["id"] == add_to:
            l["uris"].add(uri)
        if l["id"] == remove_from:
            l["uris"].discard(uri)


@app.post("/api/undo")
def undo():
    if not undo_stack:
        raise HTTPException(400, "nothing to undo")
    entry = undo_stack.pop()
    if entry["to_id"] and entry["added"]:
        _apply_snapshot(entry["to_id"], sp.remove_from_playlist(entry["to_id"], entry["uri"]))
    if entry["from_id"] == LIKED_ID:
        sp.save_to_liked(entry["uri"])
    elif entry["from_id"]:
        _apply_snapshot(entry["from_id"], sp.add_to_playlist(entry["from_id"], entry["uri"]))
    # If the move never added anything (track pre-existed in dest), the dest
    # cache must keep it on undo.
    _cache_move(entry["uri"], entry["to_id"] if entry["added"] else None, entry["from_id"])
    _sync_input_membership(
        entry["uri"],
        add_to=entry["from_id"],
        remove_from=entry["to_id"] if entry["added"] else None,
    )
    return {"ok": True, "restored_to": entry["from_id"]}


# ---- static ----------------------------------------------------------------

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


_ASSET_REF = re.compile(r"/static/(app\.js|style\.css)")


@app.get("/")
def index():
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
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def main():
    uvicorn.run(
        app,
        host=os.environ.get("SORTIFY_HOST", "0.0.0.0"),
        port=int(os.environ.get("SORTIFY_PORT", "8800")),
    )


if __name__ == "__main__":
    main()
