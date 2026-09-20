"""The Now card's one-tap destinations.

A quick add is an ordinary subset add with the picker skipped: the same
`/api/act` call, the same undo, and the same rule that putting a song in a
selection is not filing it. What lives here is only the LIST — which
buttons exist, what they are called, and what each one already knows about
the playing track.

Configured in data/config.json as `quick_adds`:

    [{"key": "star", "label": "Star", "playlist_id": "1ytl…"},
     {"key": "explore", "label": "Explore artist", "playlist_id": null,
      "create_name": "Utforsk"}]

`playlist_id: null` with a `create_name` is a button whose playlist does
not exist yet; `POST /api/explore` creates it on first press and writes the
id back here. An entry with neither is dropped — it could only ever fail.
"""

from __future__ import annotations


def payload(cfg: dict, playlists: list[dict], tracks_of, uri: str | None) -> list[dict]:
    """The buttons to draw, in configured order.

    `tracks_of(playlist_id)` returns that playlist's cached tracks, or None
    when it has never been read. The distinction matters: False means "not
    in it yet", None means nobody knows, and drawing the first for the
    second would invite a duplicate the badge promised wasn't there.
    """
    by_id = {p["id"]: p for p in playlists}
    out = []
    for entry in cfg.get("quick_adds") or []:
        pid = entry.get("playlist_id")
        if not pid and not entry.get("create_name"):
            continue
        p = by_id.get(pid) or {}
        tracks = tracks_of(pid) if pid else None
        has = None if tracks is None else any(t.get("uri") == uri for t in tracks)
        out.append({
            "key": entry.get("key"),
            "label": entry.get("label") or entry.get("key"),
            "playlist_id": pid,
            "name": p.get("name"),
            "total": p.get("total"),
            "has_track": has,
        })
    return out


def entry_for(cfg: dict, key: str) -> dict | None:
    """The configured entry with this key, or None."""
    for entry in cfg.get("quick_adds") or []:
        if entry.get("key") == key:
            return entry
    return None


def with_target(cfg: dict, key: str, playlist_id: str) -> list[dict]:
    """`quick_adds` with `key`'s target filled in — what a create writes back."""
    return [{**e, "playlist_id": playlist_id} if e.get("key") == key else dict(e)
            for e in (cfg.get("quick_adds") or [])]


def note_artist(log: dict, artist_id: str | None, name: str | None, uri: str) -> dict:
    """Record one press of Explore artist. Zero calls; pure bookkeeping.

    Keyed by artist id, falling back to the name when the payload carries
    no id — an artist without an id is still worth exploring, and dropping
    it would lose the press entirely. The same song twice is one press: the
    button is a mark, not a counter of taps.
    """
    key = artist_id or name
    if not key:
        return log
    artists = dict(log.get("artists") or {})
    now = _now_iso()
    was = artists.get(key)
    if was is None:
        artists[key] = {"name": name, "id": artist_id, "count": 1,
                        "first_at": now, "last_at": now, "tracks": [uri]}
    elif uri in (was.get("tracks") or []):
        artists[key] = was
    else:
        artists[key] = {**was, "name": name or was.get("name"),
                        "count": (was.get("count") or 0) + 1, "last_at": now,
                        "tracks": (was.get("tracks") or []) + [uri]}
    return {**log, "version": 1, "artists": artists}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
