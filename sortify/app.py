"""FastAPI app: serves the web UI and orchestrates triage."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import suggest as sugg
from .folders import extract_folder_map, home_name_excluded, select_home_ids
from .spotify import (
    BACKGROUND_DAILY_CAP,
    DAILY_CAP,
    LIKED_ID,
    AuthNeeded,
    Spotify,
    SpotifyError,
)
from .store import Store

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


class AuthFinish(BaseModel):
    redirect_url: str


class ConfigIn(BaseModel):
    input_ids: list[str]
    home_ids: list[str] = []


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
        },
        "cooldown_min_left": max(0, int((sp.effective_cooldown_until() - now) / 60)),
        # Cooldown is over but proactive work is still holding its breath.
        "quiet_min_left": max(0, int((sp.quiet_until() - now) / 60)),
    }


@app.post("/api/auth/start")
def auth_start(body: AuthStart):
    return {"auth_url": sp.start_auth(body.client_id.strip())}


@app.post("/api/auth/finish")
def auth_finish(body: AuthFinish):
    return {"me": sp.finish_auth(body.redirect_url)}


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
    for p in out:
        p["folder"] = (folders.get(p["id"]) or {}).get("path")
        p["role"] = (
            "input" if p["id"] in inputs
            else "home" if p["id"] in cfg.get("home_ids", [])
            else None
        )
    entry = store.cache().get("playlist_list") or {}
    return {"playlists": out, "fetched_at": entry.get("fetched_at")}


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
    store.update_config(home_ids=home_ids)
    return {
        "playlists_in_folders": len(mapping),
        "homes_marked": len(home_ids),
        "rule": rule,
        "home_folders": sorted({mapping[pid]["path"] for pid in home_ids}),
    }


@app.post("/api/config")
def set_config(body: ConfigIn):
    store.update_config(input_ids=body.input_ids, home_ids=body.home_ids)
    return {"ok": True}


def _effective_input_ids(cfg: dict, playlists: list[dict]) -> set[str]:
    """Explicitly marked inputs plus everything matching the name convention.

    The pattern (e.g. ^\\[.+\\]$ for bracketed names) lives in config so a
    stale browser tab saving roles can never un-mark the real inputs.
    """
    ids = set(cfg.get("input_ids", []))
    pat = cfg.get("input_name_pattern")
    if pat:
        rx = re.compile(pat)
        ids |= {p["id"] for p in playlists if rx.fullmatch(p["name"].strip())}
    return ids


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


def _known_artists() -> dict[str, dict]:
    """Cached artist genres only — never fetches. Artist-overlap scoring works
    without genres; the background enricher fills them in within budget."""
    return store.cache()["artists"]


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
    artist_info = _known_artists()
    profiles = {h["id"]: sugg.build_profile(home_tracks[h["id"]], artist_info) for h in homes}

    # Input contents too, so the capture chips can show membership.
    by_id = {p["id"]: p for p in all_playlists}
    inputs = []
    for iid in sorted(input_ids):
        if iid != LIKED_ID and iid not in by_id:
            continue
        name = "Liked Songs" if iid == LIKED_ID else by_id[iid]["name"]
        tracks = _cached_tracks(iid, by_id.get(iid, {}).get("snapshot_id"))
        inputs.append({"id": iid, "name": name, "uris": {t["uri"] for t in tracks}})

    _profile_state.update(
        built_at=now, profiles=profiles, homes=homes, inputs=inputs,
        artist_info=artist_info, playlists=all_playlists, input_ids=input_ids,
    )
    return _profile_state


def _homes_payload(state: dict, exclude: str = "") -> list[dict]:
    folders = store.folders()
    return [
        {"id": h["id"], "name": h["name"], "image": h["image"], "total": h["total"],
         "folder": (folders.get(h["id"]) or {}).get("path")}
        for h in state["homes"] if h["id"] != exclude
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
    artist_info = _known_artists()
    state["artist_info"] = artist_info

    tracks_out = []
    for t in input_tracks:
        sortable = t["type"] == "track" and not t["is_local"] and t.get("id")
        tracks_out.append(
            {
                **t,
                "sortable": bool(sortable),
                "suggestions": sugg.suggest(t, state["profiles"], artist_info) if sortable else [],
            }
        )

    name = "Liked Songs" if playlist_id == LIKED_ID else by_id[playlist_id]["name"]
    return {
        "playlist": {"id": playlist_id, "name": name},
        "homes": _homes_payload(state, exclude=playlist_id),
        "tracks": tracks_out,
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
        _now_cache.update(at=time.time(), value=value, ttl=ttl)
        return value, ttl


def _poll_after_ms(stale_in: float) -> int:
    """When the client should come back — just after the cache goes stale, so
    its next poll is a fetch rather than a wasted round trip."""
    return max(1000, int(stale_in * 1000) + 500)


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
        return {"playing": False, "poll_after_ms": _poll_after_ms(stale_in)}

    state = _ensure_profiles()
    track = np["track"]
    # Targeted fetch for just the playing track's artists (≤ a handful of
    # calls, keeps the current card's genre reasons sharp).
    missing = [a["id"] for a in track["artists"] if a.get("id") and a["id"] not in state["artist_info"]]
    if missing:
        try:
            fetched = sp.artists_genres(missing)
        except SpotifyError:
            fetched = {}
        if fetched:
            state["artist_info"].update(fetched)
            cache = store.cache()
            cache["artists"].update(fetched)
            store.save_cache(cache)

    sortable = track["type"] == "track" and not track["is_local"] and track.get("id")
    ctx_id = np["context_playlist_id"]
    ctx = next((p for p in state["playlists"] if p["id"] == ctx_id), None)
    return {
        "playing": True,
        "poll_after_ms": _poll_after_ms(stale_in),
        "is_playing": np["is_playing"],
        "progress_ms": np["progress_ms"],
        "track": {**track, "sortable": bool(sortable)},
        "context": (
            {"id": ctx_id, "name": ctx["name"] if ctx else None,
             "is_input": ctx_id in state["input_ids"]}
            if ctx_id else None
        ),
        "suggestions": sugg.suggest(track, state["profiles"], state["artist_info"]) if sortable else [],
        "homes": _homes_payload(state),
        "inputs": [
            {"id": l["id"], "name": l["name"], "has_track": track["uri"] in l["uris"]}
            for l in state["inputs"]
        ],
    }


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


# ---- background genre enricher ----------------------------------------------


# One artist at a time, minutes apart. Proactive traffic is the only thing
# that can earn a multi-hour 429 while nobody is even using the app, so it
# gets the slowest pace that still makes progress.
ENRICH_INTERVAL = 300     # seconds between proactive artist fetches
ENRICH_IDLE_SLEEP = 1800  # recheck interval while blocked or out of work


def _next_missing_artist() -> str | None:
    """One artist id from the cached playlists that has no genres yet."""
    cache = store.cache()
    known = cache["artists"]
    for pl in cache["playlists"].values():
        for t in pl["tracks"]:
            for a in t["artists"]:
                aid = a.get("id")
                if aid and aid not in known:
                    return aid
    return None


def _genre_enricher():
    """Slowly backfill artist genres from cached playlists.

    Deliberately glacial: one artist every few minutes, a few dozen a day,
    silent for hours after any cooldown, and yielding entirely once the user's
    own traffic is underway. Suggestions already score on artist overlap
    without genres — this only sharpens the stated reasons, so it is never
    worth a single minute of rate-limit penalty.

    It also narrates itself into the server log: a background job that spends
    the user's quota invisibly is exactly how a multi-hour ban becomes a
    mystery to the person who did nothing but open the app.
    """
    while True:
        try:
            reason = sp.background_block_reason()
            if reason:
                log.info("genre enricher idle — %s", reason)
                time.sleep(ENRICH_IDLE_SLEEP)
                continue
            aid = _next_missing_artist()
            if aid is None:
                log.info("genre enricher idle — every cached artist has genres")
                time.sleep(ENRICH_IDLE_SLEEP)
                continue
            fetched = sp.artists_genres([aid], background=True)
            if fetched:
                cache = store.cache()
                cache["artists"].update(fetched)
                store.save_cache(cache)
                if _profile_state.get("artist_info") is not None:
                    _profile_state["artist_info"].update(fetched)
                log.info(
                    "genre enricher: %s done (%d/%d background calls today)",
                    aid, sp.background_spent(), BACKGROUND_DAILY_CAP,
                )
            time.sleep(ENRICH_INTERVAL)
        except SpotifyError as e:
            # Covers hitting a cap and walking into a fresh cooldown alike.
            # Back off hard; background_block_reason decides when to resume.
            log.warning("genre enricher backing off — %s", e)
            time.sleep(ENRICH_IDLE_SLEEP)
        except Exception:
            log.exception("genre enricher error")
            time.sleep(ENRICH_IDLE_SLEEP)


@app.on_event("startup")
def _start_enricher():
    threading.Thread(target=_genre_enricher, daemon=True, name="genre-enricher").start()


# ---- static ----------------------------------------------------------------

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def main():
    uvicorn.run(
        app,
        host=os.environ.get("SORTIFY_HOST", "0.0.0.0"),
        port=int(os.environ.get("SORTIFY_PORT", "8800")),
    )


if __name__ == "__main__":
    main()
