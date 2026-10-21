"""Split into parts: the draft and the Done plan, kept out of app.py.

Pure over a Store and plain data; app.py owns the routes, the threads and
the Spotify client. Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md.
"""

from __future__ import annotations

import math
import threading

from .splitparts import Suggestion
from .tags import track_key

# One lock for every read-modify-write of splitparts.json: a move and a
# Done step from two requests must not lose each other's write.
_lock = threading.RLock()


def _songs(tracks: list[dict]) -> dict[str, dict]:
    out = {}
    for t in tracks:
        u = t.get("uri")
        if u and u not in out:
            out[u] = {"name": t.get("name") or "",
                      "artists": ", ".join(a.get("name") or "" for a in t.get("artists") or []),
                      "local": bool(t.get("is_local"))}
    return out


def slim_tracks(tracks: list[dict]) -> list[dict]:
    """What the engine needs per song, deduped, playlist order kept."""
    out, seen = [], set()
    for t in tracks:
        u = t.get("uri")
        if u and u not in seen:
            seen.add(u)
            out.append({"uri": u, "name": t.get("name") or "",
                        "artists": [{"id": a.get("id"), "name": a.get("name") or ""}
                                    for a in t.get("artists") or []],
                        "is_local": bool(t.get("is_local"))})
    return out


def draft_from(playlist: dict, snapshot_id: str | None, tracks: list[dict],
               sugg: Suggestion, n: int) -> dict:
    return {
        "playlist_id": playlist["id"],
        "name": playlist.get("name") or "",
        "snapshot_id": snapshot_id,
        "n": n,
        "parts": [{"key": f"p{i + 1}", "name": name, "uris": list(uris)}
                  for i, (uris, name) in enumerate(zip(sugg.parts, sugg.names))],
        "unknown": list(sugg.unknown),
        "hint": sugg.hint,
        "scores": {str(k): v for k, v in sugg.scores.items()},
        "songs": _songs(tracks),
        "tracks": slim_tracks(tracks),
        # Manual edits (a move, an added or removed part) since the
        # suggestion: what the UI asks about before replacing the draft.
        "edited": False,
        "done": None,
    }


def without_tracks(draft: dict) -> dict:
    """The draft as the client gets it: `tracks` is the engine's input
    (~250 KB on a 1000-song home) and the client never reads it."""
    return {k: v for k, v in draft.items() if k != "tracks"}


def get_draft(store, pid: str) -> dict | None:
    with _lock:
        d = store.splitparts()["drafts"].get(pid)
        return d if isinstance(d, dict) else None


def put_draft(store, draft: dict) -> None:
    with _lock:
        env = store.splitparts()
        env["drafts"][draft["playlist_id"]] = draft
        store.save_splitparts(env)


def drop_draft(store, pid: str) -> None:
    with _lock:
        env = store.splitparts()
        env["drafts"].pop(pid, None)
        store.save_splitparts(env)


def missing_lastfm(tracks: list[dict], tag_artists: dict, track_map: dict,
                   artist_map: dict) -> tuple[dict[str, str], list[tuple[str, str]], list[tuple[str, str]]]:
    """What Last.fm has never been asked about for these songs. A recorded
    miss counts as asked — never re-fetched (the house write-once rule)."""
    need_tags: dict[str, str] = {}
    need_tracks: list[tuple[str, str]] = []
    need_similar: list[tuple[str, str]] = []
    seen_similar: set[str] = set()
    for t in tracks:
        artists = [a for a in t.get("artists") or [] if a.get("name")]
        if not artists or t.get("is_local"):
            continue
        for a in artists:
            aid = a.get("id")
            if aid and aid not in tag_artists and aid not in need_tags:
                need_tags[aid] = a["name"]
        title = t.get("name") or ""
        if title and not any(isinstance(track_map.get(track_key(a["name"], title)), dict)
                             for a in artists):
            need_tracks.append((artists[0]["name"], title))
        first = artists[0]
        aid = first.get("id")
        if aid and aid not in seen_similar and not isinstance(artist_map.get(aid), dict):
            seen_similar.add(aid)
            need_similar.append((aid, first["name"]))
    return need_tags, need_tracks, need_similar


NEW_PART_NAME = "New part"


def _part(draft: dict, key: str) -> dict:
    for p in draft["parts"]:
        if p["key"] == key:
            return p
    raise KeyError(f"no part {key!r}")


def move(draft: dict, uri: str, to_key: str) -> dict:
    target = _part(draft, to_key)
    source = next((p for p in draft["parts"] if uri in p["uris"]), None)
    if source is None:
        raise KeyError(f"song {uri!r} is not in this split")
    if source is not target:
        source["uris"].remove(uri)
        target["uris"].append(uri)
        draft["edited"] = True
    return draft


def has_song(draft: dict, uri: str) -> bool:
    return any(uri in p["uris"] for p in draft["parts"])


def forget_song(draft: dict, uri: str) -> dict:
    """The song left the split's playlist (moved to another one): it goes
    from every part and from what the draft knows about its songs."""
    for p in draft["parts"]:
        p["uris"] = [u for u in p["uris"] if u != uri]
    draft["unknown"] = [u for u in draft.get("unknown") or [] if u != uri]
    (draft.get("songs") or {}).pop(uri, None)
    draft["tracks"] = [t for t in draft.get("tracks") or [] if t.get("uri") != uri]
    draft["edited"] = True
    return draft


def move_targets(listing: list[dict], pid: str, folders: dict, liked_id: str,
                 home_ids) -> list[dict]:
    """Where a song of this split can go instead: the other homes in the
    cached listing that can take an add — never the split one itself,
    archived (🗄️) ones or Liked Songs. Homes only: a song leaving a home is
    being re-filed, and inputs and subsets buried the homes in the search
    (2026-10-21). Sorted by name."""
    from .playlist_roles import is_archived
    homes = set(home_ids)
    out = [{"id": p["id"], "name": p.get("name") or "",
            "folder": (folders.get(p["id"]) or {}).get("path")}
           for p in listing
           if p.get("id") in homes and p["id"] not in (pid, liked_id) and p.get("editable")
           and not is_archived(p.get("name") or "")]
    return sorted(out, key=lambda t: (t["name"].casefold(), t["id"]))


def buffer_targets(listing: list[dict], pid: str, input_ids, set_keys: dict[str, str],
                   liked_id: str) -> list[dict]:
    """Where a song of this split can go back to an inbox: the input
    playlists in the cached listing that can take an add, each with the key
    of its input set (the picker's grey line). Never the split one itself,
    archived ones (already left out of `input_ids`) or Liked Songs. Kept
    apart from `move_targets`, whose list stays homes only. Sorted by name."""
    from .playlist_roles import is_archived
    inputs = set(input_ids)
    out = [{"id": p["id"], "name": p.get("name") or "", "set": set_keys.get(p["id"])}
           for p in listing
           if p.get("id") in inputs and p["id"] not in (pid, liked_id) and p.get("editable")
           and not is_archived(p.get("name") or "")]
    return sorted(out, key=lambda t: (t["name"].casefold(), t["id"]))


def rename_part(draft: dict, key: str, name: str) -> dict:
    """A name drafted for a part while editing. Trimmed; an empty one is
    refused (ValueError), as a part with no name could never be created."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a part needs a name")
    part = _part(draft, key)
    if part["name"] != name:
        part["name"] = name
        draft["edited"] = True
    return draft


def add_part(draft: dict) -> dict:
    from .splitparts import MAX_PARTS
    if len(draft["parts"]) >= MAX_PARTS:
        raise ValueError(f"at most {MAX_PARTS} parts")
    nums = [int(p["key"][1:]) for p in draft["parts"] if p["key"][1:].isdigit()]
    draft["parts"].append({"key": f"p{max(nums, default=0) + 1}",
                           "name": NEW_PART_NAME, "uris": []})
    draft["edited"] = True
    return draft


def removal_moves(draft: dict, key: str, tag_artists: dict, track_map: dict,
                  artist_map: dict) -> dict[str, str]:
    from .splitparts import place
    leaving = _part(draft, key)["uris"]
    others = [p for p in draft["parts"] if p["key"] != key]
    picks = place(draft["tracks"], [p["uris"] for p in others], leaving,
                  tag_artists, track_map, artist_map)
    return {u: others[picks.get(u, 0)]["key"] for u in leaving}


def remove_part(draft: dict, key: str, moves: dict[str, str]) -> dict:
    from .splitparts import MIN_PARTS
    if len(draft["parts"]) <= MIN_PARTS:
        raise ValueError(f"at least {MIN_PARTS} parts must remain")
    leaving = _part(draft, key)
    draft["parts"] = [p for p in draft["parts"] if p["key"] != key]
    for u in leaving["uris"]:
        _part(draft, moves[u])["uris"].append(u)
    draft["edited"] = True
    return draft


ORIGINAL_CHOICES = ("subset", "part")


def done_plan(draft: dict, original: str, part_key: str | None,
              names: dict[str, str] | None = None, remove_batch: int = 100,
              into: dict[str, str] | None = None, homes: dict[str, dict] | None = None) -> dict:
    """What Done will do, and what it costs, without doing any of it.

    `into` maps a part key to an existing home its songs are added to
    instead of a new playlist. `homes` describes those homes from the cache
    (zero calls): `{id: {"name", "uris": set, "cached": bool, "total"}}`.
    The price assumes a cached track list is current (Done proves it with
    a snapshot read and re-reads it when it is not); with nothing cached at
    all the read is certain, so it is counted. Whether a target may be used
    is the caller's check — this only refuses what no target could fix."""
    if original not in ORIGINAL_CHOICES:
        raise ValueError("choose what happens to the original")
    keys = [p["key"] for p in draft["parts"]]
    keep_key = part_key if original == "part" else None
    if original == "part" and keep_key not in keys:
        raise ValueError("pick the part the original becomes")
    songs = draft.get("songs") or {}
    names = names or {}
    into = into or {}
    homes = homes or {}
    for key, to_id in into.items():
        if key not in keys:
            raise ValueError(f"no part {key!r}")
        if key == keep_key:
            raise ValueError("the part the original becomes stays in it — it cannot also go into another home")
        if not to_id:
            raise ValueError(f"pick the home part {key!r} goes into")

    def local(u: str) -> bool:
        return bool((songs.get(u) or {}).get("local"))

    new_parts, into_parts, empty = [], [], []
    for p in draft["parts"]:
        name = (names.get(p["key"]) or p["name"]).strip() or p["name"]
        if p["key"] == keep_key:
            continue
        uris = list(dict.fromkeys(u for u in p["uris"] if not local(u)))
        if not uris:
            empty.append(name)
        elif p["key"] in into:
            to_id = into[p["key"]]
            home = homes.get(to_id) or {}
            held = home.get("uris") or set()
            into_parts.append({"key": p["key"], "to_id": to_id,
                               "to_name": home.get("name") or to_id, "uris": uris,
                               "count": sum(1 for u in uris if u not in held),
                               "read": 0 if home.get("cached") else
                               max(1, math.ceil((home.get("total") or 0) / 100))})
        else:
            new_parts.append({"key": p["key"], "name": name, "uris": uris})
    # Destinations, not created playlists: a part going into a home is
    # as much a place for songs as a new one, and so is the kept part.
    if len(new_parts) + len(into_parts) + (1 if keep_key else 0) < 2:
        raise ValueError("at least 2 parts need songs in them")

    removals: list[str] = []
    if keep_key:
        keep = set(_part(draft, keep_key)["uris"])
        removals = list(dict.fromkeys(
            u for p in draft["parts"] if p["key"] != keep_key
            for u in p["uris"] if u not in keep and not local(u)))
    calls = 1 + sum(1 + math.ceil(len(p["uris"]) / 100) for p in new_parts)
    # Per into-part: the home's snapshot, its track read when nothing is
    # cached, and the adds for the songs it does not already hold.
    calls += sum(1 + p["read"] + math.ceil(p["count"] / 100) for p in into_parts)
    calls += 1 if original == "subset" else math.ceil(len(removals) / remove_batch)
    return {"new_parts": new_parts, "into_parts": into_parts, "empty": empty,
            "local": [songs[u]["name"] for u in songs if local(u)],
            "removals": removals, "calls": calls, "creates": len(new_parts)}


def twin_name_problem(names: list[str], listing: list[dict],
                      skip_id: str | None = None) -> str | None:
    """Filing finds a playlist's row in the client by its NAME and refuses
    when two share one, so a part named like another part, or like a
    playlist already in the listing, could never be filed. Case-insensitive
    and trimmed, the same comparison `filing.file_playlist` makes."""
    seen: set[str] = set()
    for name in names:
        k = name.strip().lower()
        if k in seen:
            return f"two parts are called {name.strip()!r} — give each part its own name"
        seen.add(k)
    taken = {(p.get("name") or "").strip().lower() for p in listing if p.get("id") != skip_id}
    for name in names:
        if name.strip().lower() in taken:
            return (f"a playlist called {name.strip()!r} already exists — filing finds "
                    "playlists by name, so give this part another one")
    return None


def abandon_report(draft: dict) -> dict:
    """What a started Done already made, for the user to know what exists."""
    done = draft.get("done") or {}
    names = (done.get("opts") or {}).get("names") or {}
    by_key = {p["key"]: p for p in draft["parts"]}
    created = []
    for key, pid in (done.get("created") or {}).items():
        part = by_key.get(key) or {"name": key}
        name = (names.get(key) or part["name"]).strip() or part["name"]
        created.append({"id": pid, "name": name})
    return {"created": created, "original_done": bool(done.get("original_done"))}


def reconcile(draft: dict, tracks: list[dict], snapshot_id: str | None,
              tag_artists: dict, track_map: dict, artist_map: dict) -> dict:
    """The playlist changed in Spotify: keep the user's moves for songs
    still there, drop songs that left, place new songs where they fit best
    and mark them "?"."""
    from .splitparts import place
    slim = slim_tracks(tracks)
    present = {t["uri"] for t in slim}
    for p in draft["parts"]:
        p["uris"] = [u for u in p["uris"] if u in present]
    placed = {u for p in draft["parts"] for u in p["uris"]}
    new = [t["uri"] for t in slim if t["uri"] not in placed]
    picks = place(slim, [p["uris"] for p in draft["parts"]], new,
                  tag_artists, track_map, artist_map)
    for u in new:
        draft["parts"][picks.get(u, 0)]["uris"].append(u)
    draft["unknown"] = [u for u in draft["unknown"] if u in present] + \
        [u for u in new if u not in draft["unknown"]]
    draft["tracks"] = slim
    draft["songs"] = _songs(tracks)
    draft["snapshot_id"] = snapshot_id
    return draft
