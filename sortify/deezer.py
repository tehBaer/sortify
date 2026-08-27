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

    def _search_one(self, q: str) -> dict | None:
        """The first search hit that actually carries a clip, else None."""
        hits = self._get("/search", {"q": q, "limit": 1}).get("data") or []
        if not hits or not hits[0].get("id") or not hits[0].get("preview"):
            return None
        return hits[0]

    def fetch_preview(self, artist: str, title: str) -> dict:
        """{"url", "deezer_id"} for a 30s preview clip, or {"miss": True}.

        Search results carry `preview` directly. The field-scoped query is
        tried first and is usually the only request; it is an EXACT match,
        though, so remixes, live takes, `feat.` suffixes and punctuation
        drift all miss it — and on this path a miss is expensive twice over,
        costing a candidate from the page's attempt budget and pushing the
        medley toward the text-only fallback. A plain free-text retry
        recovers most of them, and only runs when the strict form found
        nothing, so a clean hit still costs exactly one request.

        The URL's CDN token EXPIRES after a while, so callers must not
        persist it — cache at most for minutes.
        """
        hit = (self._search_one(f'artist:"{artist}" track:"{title}"')
               or self._search_one(f"{artist} {title}"))
        if hit is None:
            return {"miss": True}
        return {"url": hit["preview"], "deezer_id": int(hit["id"])}
