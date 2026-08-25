"""30s preview clips from Deezer's public API.

Spotify's dev-mode API lost `preview_url`, so the hold-to-preview player's
audio bites come from Deezer instead: keyless, matched by artist+title
search, one GET per track. This module only talks to the network — nothing
is persisted (the clip URLs carry expiring CDN tokens).

Deezer is not Spotify: none of the budget ledger applies. Its own limit is
50 requests per 5 s per IP, which the preview path cannot approach. Deezer
reports quota/API errors as HTTP 200 with an `{"error": ...}` body — those
raise `DeezerError` (retryable later).
"""

from __future__ import annotations

import httpx

BASE = "https://api.deezer.com"


class DeezerError(Exception):
    pass


class Deezer:
    def __init__(self, timeout: float = 5.0):
        self._client = httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._client.get(f"{BASE}{path}", params=params)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise DeezerError(str(data["error"]))
        return data if isinstance(data, dict) else {}

    def fetch_preview(self, artist: str, title: str) -> dict:
        """{"url", "deezer_id"} for a 30s preview clip, or {"miss": True}.

        One request: search results carry `preview` directly. The URL's CDN
        token EXPIRES after a while, so callers must not persist it — cache
        at most for minutes.
        """
        hits = self._get(
            "/search", {"q": f'artist:"{artist}" track:"{title}"', "limit": 1}
        ).get("data") or []
        if not hits or not hits[0].get("id") or not hits[0].get("preview"):
            return {"miss": True}
        return {"url": hits[0]["preview"], "deezer_id": int(hits[0]["id"])}
