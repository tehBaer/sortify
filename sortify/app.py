"""FastAPI app: serves the web UI and orchestrates triage."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
from .split import UNTAGGED, pick_sitting, split_tracks
from .store import TAGS_VERSION, Store
from .tags import LastFm, LastFmError, enrich, load_key

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
        },
        "cooldown_min_left": max(0, int((sp.effective_cooldown_until() - now) / 60)),
        # Cooldown is over but proactive work is still holding its breath.
        "quiet_min_left": max(0, int((sp.quiet_until() - now) / 60)),
    }


@app.post("/api/auth/start")
def auth_start(body: AuthStart):
    # Re-authorising to pick up a new scope is now the common case, and the
    # client ID is already on file — only first-time setup should have to go
    # and fetch it. Falling back also stops a blank field from overwriting the
    # stored id with "".
    client_id = body.client_id.strip() or (store.config().get("client_id") or "")
    if not client_id:
        raise HTTPException(400, "paste the Client ID from your Spotify dashboard app")
    return {"auth_url": sp.start_auth(client_id)}


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
    # One disk read + JSON parse for the whole listing, not one per playlist.
    # Against a real ~1000-playlist account with a splits.json that has grown
    # to hundreds of KB, calling `_split_summary` inside this loop (each call
    # doing its own `store.splits()`) turned every /api/playlists response
    # into a ~1.4s stall — felt on every Playlists-view open (nav-lists,
    # btn-back, btn-split-back, post-refresh). Zero Spotify calls either way;
    # this was a purely local-disk cost the user was still paying constantly.
    splits = store.splits()["splits"]
    for p in out:
        p["folder"] = (folders.get(p["id"]) or {}).get("path")
        p["role"] = (
            "input" if p["id"] in inputs
            else "home" if p["id"] in cfg.get("home_ids", [])
            else None
        )
        p["split"] = _split_summary(p["id"], splits)
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
    """Whatever artist data is already cached — never fetches.

    Every entry has empty genres and always will: the Feb-2026 dev-mode API
    dropped the `genres` field from /artists/{id}. Scoring is therefore pure
    artist overlap, and suggest.py's genre cosine is a dead branch.
    """
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


def _pile_progress(split: dict) -> list[dict]:
    decided = split.get("decided", {})
    out = []
    for p in split["piles"]:
        out.append({**p, "decided": sum(1 for u in p["uris"] if u in decided),
                    "total": len(p["uris"])})
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


@app.post("/api/split/{playlist_id}")
def create_split(playlist_id: str, params: SplitParams = SplitParams()):
    """Read a playlist, tag its artists via Last.fm, cluster into piles.

    The only Spotify spend is the track read (~15 calls for 1372, and zero if
    the snapshot hasn't moved since the last read — see `_cached_tracks`, the
    same cache `triage` uses). Tagging is Last.fm, one request per
    not-yet-cached artist; clustering is local and free. A Last.fm failure
    partway through does not lose what was already verified — see
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

    snapshot_id = by_id.get(playlist_id, {}).get("snapshot_id")
    tracks = _cached_tracks(playlist_id, snapshot_id)
    if not tracks:
        raise HTTPException(400, "playlist has no tracks")

    names = {}
    for t in tracks:
        for a in t.get("artists", []):
            if a.get("id"):
                names.setdefault(a["id"], a.get("name") or "")

    try:
        artists = enrich(names, cached_artists, fm, _now_iso())
    except LastFmError as exc:
        saved = exc.partial if exc.partial is not None else cached_artists
        store.save_tag_artists(saved)
        # How many of *this playlist's* artists made it, not the size of the
        # whole cross-playlist cache `saved` carries forward.
        tagged_here = len(set(names) & set(saved))
        raise HTTPException(
            502,
            f"Last.fm tagging stopped after {tagged_here} of {len(names)} "
            f"artists in this playlist ({exc}); progress was saved — "
            "re-running the split will resume instead of starting over.",
        ) from exc
    store.save_tag_artists(artists)

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
        store.save_splits(payload)
    untagged = sum(len(p["uris"]) for p in piles if p["id"] == UNTAGGED)
    return {"piles": piles, "tagged": len(tracks) - untagged, "untagged": untagged}


@app.get("/api/split/{playlist_id}")
def get_split(playlist_id: str):
    split = store.splits()["splits"].get(playlist_id)
    if not split:
        raise HTTPException(404, "no split for that playlist")
    return {**split, "piles": _pile_progress(split)}


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
    return {"piles": _pile_progress(split)}


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
    """
    try:
        sp.unfollow_playlist(new_id)
    except SpotifyError as e:
        if e.status != 404:
            _recover_orphan(split_playlist_id, pile_id, new_id, e)
    raise HTTPException(409, detail)


def _recover_orphan(split_playlist_id: str, pile_id: str, new_id: str, cause: SpotifyError) -> None:
    """The unfollow itself failed (not 404 — a real error, e.g. a 429 or a
    5xx). Rather than let the playlist drop out of sight entirely, re-stamp
    a fresh reservation for it so a later `finish` can still clean it up —
    but only into a genuinely empty slot, never clobbering a different
    sitting that has since legitimately claimed it. Always raises: the
    caller's own attempt to materialise a sitting still failed either way.
    """
    with _split_lock:
        payload = store.splits()
        split = payload["splits"].get(split_playlist_id)
        if split is not None and not split.get("active_sitting"):
            split["active_sitting"] = {
                "playlist_id": new_id, "pile_id": pile_id, "uris": [],
                "started_at": _now_iso(), "claim": uuid.uuid4().hex,
            }
            store.save_splits(payload)
        else:
            # No free slot to re-record it in — a different sitting won it
            # in the meantime. Nothing in splits.json can point at new_id
            # right now; surfaced in the logs since the API response alone
            # would otherwise be the only trace of it.
            log.error(
                "sitting playlist %s orphaned beyond automatic recovery: lost a "
                "start/finish race, then unfollow failed (%s), and no free slot "
                "to re-record it in — needs manual cleanup in the Spotify app",
                new_id, cause,
            )
    raise SpotifyError(cause.status, str(cause)) from cause


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

    new_id = sp.create_playlist(f"▶ {pile['name']}", "sortify sitting — safe to delete")

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
    again. It also erased _recover_orphan's re-stamp the same way.

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
        if not split or not split.get("active_sitting"):
            raise HTTPException(404, "no active sitting")
        sitting = dict(split["active_sitting"])

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
    return {"ok": True, "cleared": cleared}


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
    # There used to be a targeted artist fetch here to sharpen the card's genre
    # reasons. It cost a call per unseen artist to learn nothing: the dev-mode
    # API no longer returns genres at all.
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
        "sitting": _sitting_for_context(ctx_id),
        "suggestions": sugg.suggest(track, state["profiles"], state["artist_info"]) if sortable else [],
        "homes": _homes_payload(state),
        "inputs": [
            {"id": l["id"], "name": l["name"], "has_track": track["uri"] in l["uris"]}
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
    return _playback_call(sp.skip_next)


@app.post("/api/player/play")
def player_play(body: PlayIn):
    if body.input_id == LIKED_ID:
        # Liked Songs has no playlist id, so there is no context_uri for it.
        raise HTTPException(400, "Liked Songs can't be started as a playlist — play it from Spotify.")
    return _playback_call(sp.play_context, body.input_id)


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
