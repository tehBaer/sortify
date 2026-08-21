"""JSON-file persistence: config, tokens, and the playlist/artist cache.

Everything lives as plain JSON under one data directory. This is a
single-user LAN service; readable files beat ceremony.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# tags.json shape version. 2: raw Last.fm tags, hygiene applied at split time.
TAGS_VERSION = 2


def _atomic_write(path: Path, payload: Any) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class Store:
    def __init__(self, data_dir: Path | str | None = None):
        self.dir = Path(data_dir or os.environ.get("SORTIFY_DATA_DIR") or DEFAULT_DATA_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _load(self, name: str, default: Any) -> Any:
        path = self.dir / name
        if not path.exists():
            return default
        with open(path) as f:
            return json.load(f)

    def _save(self, name: str, payload: Any) -> None:
        _atomic_write(self.dir / name, payload)

    # config.json: client_id + which playlists play which role
    def config(self) -> dict:
        return self._load("config.json", {"client_id": None, "input_ids": [], "home_ids": []})

    def save_config(self, cfg: dict) -> None:
        self._save("config.json", cfg)

    def update_config(self, **fields: Any) -> dict:
        cfg = self.config()
        cfg.update(fields)
        self.save_config(cfg)
        return cfg

    # tokens.json: OAuth tokens plus the in-flight PKCE verifier during auth
    def tokens(self) -> dict:
        return self._load("tokens.json", {})

    def save_tokens(self, tokens: dict) -> None:
        self._save("tokens.json", tokens)

    # folders.json: {playlist_id: {path, caps}} from the desktop-client extract
    def folders(self) -> dict:
        return self._load("folders.json", {})

    def save_folders(self, mapping: dict) -> None:
        self._save("folders.json", mapping)

    # usage.json: {day, count} — local daily API-call budget accounting
    def usage(self) -> dict:
        return self._load("usage.json", {"day": "", "count": 0})

    def save_usage(self, usage: dict) -> None:
        self._save("usage.json", usage)

    # cache.json: playlist snapshots and artist genres.
    # "playlists" is per-playlist track data keyed by id; "playlist_list" is the
    # user's playlist *listing* — different things, hence the separate key.
    def cache(self) -> dict:
        return self._load(
            "cache.json",
            {"playlists": {}, "artists": {}, "me": None, "playlist_list": None},
        )

    def save_cache(self, cache: dict) -> None:
        self._save("cache.json", cache)

    # tags.json is an *envelope*:
    #   {"version": 2, "artists": {artist_id: {name, lastfm_name, tags,
    #                                          fetched_at, miss}}}
    # Last.fm data, not Spotify's — kept separate from cache.json on purpose.
    # Version 2 stores Last.fm's raw tags; version 1 stored them pre-filtered.
    def tags(self) -> dict:
        """The whole envelope. Consumers almost always want `tag_artists()`."""
        return self._load("tags.json", {"version": TAGS_VERSION, "artists": {}})

    def tag_artists(self) -> dict:
        """The inner `{artist_id: record}` map.

        This is what `tags.enrich(cached=...)` and `split.split_tracks(tags=...)`
        take. Handing either of them the envelope fails silently: enrich finds
        no artist ids and re-fetches the whole library, split finds no tags and
        calls every track untagged.
        """
        artists = self.tags().get("artists")
        return artists if isinstance(artists, dict) else {}

    def save_tags(self, payload: dict) -> None:
        """Write the envelope. To write just the artists map use
        `save_tag_artists()`, which wraps it for you."""
        self._save("tags.json", payload)

    def save_tag_artists(self, artists: dict) -> None:
        """Wrap an inner artists map in the current envelope and store it."""
        self._save("tags.json", {"version": TAGS_VERSION, "artists": artists})

    # splits.json: {playlist_id: {piles, decided, params, ...}}
    # Piles are virtual — materialising them all would cost ~1384 calls.
    def splits(self) -> dict:
        return self._load("splits.json", {"version": 1, "splits": {}})

    def save_splits(self, payload: dict) -> None:
        self._save("splits.json", payload)

    # queue.json / pacing.json: the queued materialiser's persisted state.
    # boxdash reads BOTH files directly (its house pattern), so their shape is
    # a published contract: versioned envelopes, guard-on-read like tags.json,
    # written atomically (0600 via mkstemp) so a half-written file is never
    # visible and the card keeps working while sortify is down.
    QUEUE_DEFAULT = {"version": 1, "playlist_id": None, "pending": [],
                     "current": None, "state": "stopped", "stop_reason": None,
                     "progress": {}, "enqueued_at": None, "updated_at": None}
    PACING_DEFAULT = {"version": 1, "rate_per_min": 1.8, "ceiling": 7.0,
                      "clean_since": None, "max_clean_rate": None,
                      "history_429": [], "updated_at": None}

    def _versioned(self, name: str, default: dict) -> dict:
        # dict(default) is a shallow copy: nested mutables like "pending": []
        # would still be the SAME list object as QUEUE_DEFAULT's, so mutating
        # a freshly-defaulted read (e.g. q["pending"].append(...)) silently
        # poisons every later default read (ruling R-T8e) — deepcopy instead.
        data = self._load(name, copy.deepcopy(default))
        return data if isinstance(data, dict) and data.get("version") == default["version"] else copy.deepcopy(default)

    def queue(self) -> dict:
        return self._versioned("queue.json", self.QUEUE_DEFAULT)

    def save_queue(self, payload: dict) -> None:
        self._save("queue.json", payload)

    def pacing(self) -> dict:
        return self._versioned("pacing.json", self.PACING_DEFAULT)

    def save_pacing(self, payload: dict) -> None:
        self._save("pacing.json", payload)

    # lastfm_tracks.json: rebuildable Last.fm track-level cache (getSimilar +
    # track top tags), keyed by tags.track_key(artist, title). Deliberately
    # separate from tags.json (the artist-level cache, owned by the splitting
    # workstream) and from descriptions.json/user_tags.json (precious,
    # user-typed data) — see the design doc's Storage section. Safe to delete
    # at any time; a missing or malformed file is just an empty cache.
    LASTFM_TRACKS_DEFAULT = {"version": 1, "tracks": {}}

    def lastfm_tracks(self) -> dict:
        """The whole envelope. Consumers almost always want `lastfm_track_map()`."""
        return self._versioned("lastfm_tracks.json", self.LASTFM_TRACKS_DEFAULT)

    def save_lastfm_tracks(self, payload: dict) -> None:
        self._save("lastfm_tracks.json", payload)

    def lastfm_track_map(self) -> dict:
        """The inner `{track_key: record}` map — the analog of `tag_artists()`.

        {} on anything malformed, so a corrupt or hand-edited file degrades to
        "nothing cached yet" instead of raising mid-fetch.
        """
        tracks = self.lastfm_tracks().get("tracks")
        return tracks if isinstance(tracks, dict) else {}

    # deezer.json: {"version": 1, "tracks": {track_key: {"bpm", "deezer_id",
    # "fetched_at"} | {"miss": true}}} — BPM per track from Deezer's public
    # API (Spotify's audio-features endpoint is gone for dev-mode apps).
    # Same write-once discipline and key convention as lastfm_tracks.json.
    DEEZER_DEFAULT = {"version": 1, "tracks": {}}

    def deezer_tracks(self) -> dict:
        """The whole envelope. Consumers almost always want `deezer_map()`."""
        return self._versioned("deezer.json", self.DEEZER_DEFAULT)

    def save_deezer_tracks(self, payload: dict) -> None:
        self._save("deezer.json", payload)

    def deezer_map(self) -> dict:
        tracks = self.deezer_tracks().get("tracks")
        return tracks if isinstance(tracks, dict) else {}

    # lastfm_artists.json: rebuildable Last.fm artist-level cache (getSimilar),
    # keyed by Spotify artist ID. Deliberately separate from tags.json (the
    # artist-level cache, owned by the splitting workstream) and from
    # descriptions.json/user_tags.json (precious, user-typed data) — see the
    # design doc's Storage section. Safe to delete at any time; a missing or
    # malformed file is just an empty cache.
    LASTFM_ARTISTS_DEFAULT = {"version": 1, "artists": {}}

    def lastfm_artists(self) -> dict:
        """The whole envelope. Consumers almost always want `lastfm_artist_map()`."""
        return self._versioned("lastfm_artists.json", self.LASTFM_ARTISTS_DEFAULT)

    def save_lastfm_artists(self, payload: dict) -> None:
        self._save("lastfm_artists.json", payload)

    def lastfm_artist_map(self) -> dict:
        """The inner `{artist_id: record}` map — the analog of `lastfm_track_map()`.

        {} on anything malformed, so a corrupt or hand-edited file degrades to
        "nothing cached yet" instead of raising mid-fetch.
        """
        artists = self.lastfm_artists().get("artists")
        return artists if isinstance(artists, dict) else {}
