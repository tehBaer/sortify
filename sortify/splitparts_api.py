"""Split into parts: the draft and the Done plan, kept out of app.py.

Pure over a Store and plain data; app.py owns the routes, the threads and
the Spotify client. Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md.
"""

from __future__ import annotations

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
        "done": None,
    }


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
    return draft


def add_part(draft: dict) -> dict:
    from .splitparts import MAX_PARTS
    if len(draft["parts"]) >= MAX_PARTS:
        raise ValueError(f"at most {MAX_PARTS} parts")
    nums = [int(p["key"][1:]) for p in draft["parts"] if p["key"][1:].isdigit()]
    draft["parts"].append({"key": f"p{max(nums, default=0) + 1}",
                           "name": NEW_PART_NAME, "uris": []})
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
    return draft
