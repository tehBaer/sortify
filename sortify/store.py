"""JSON-file persistence: config, tokens, and the playlist/artist cache.

Everything lives as plain JSON under one data directory. This is a
single-user LAN service; readable files beat ceremony.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


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

    # cache.json: playlist snapshots and artist genres
    def cache(self) -> dict:
        return self._load("cache.json", {"playlists": {}, "artists": {}, "me": None})

    def save_cache(self, cache: dict) -> None:
        self._save("cache.json", cache)
