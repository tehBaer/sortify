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

    # Five, not one. The extra rows cost nothing — same single request — and
    # they are what makes a rejected match recoverable: the runner-up is
    # usually the recording the user actually wanted.
    SEARCH_LIMIT = 5

    def _search_one(self, q: str, exclude: frozenset[int] = frozenset()) -> dict | None:
        """The first search hit that carries a clip and is not excluded."""
        hits = self._get("/search", {"q": q, "limit": self.SEARCH_LIMIT}).get("data") or []
        for hit in hits:
            if not hit.get("id") or not hit.get("preview"):
                continue
            if int(hit["id"]) in exclude:
                continue
            return hit
        return None

    def fetch_preview(self, artist: str, title: str, exclude=()) -> dict:
        """{"url", "deezer_id"} for a 30s preview clip, or {"miss": True}.

        Search results carry `preview` directly. The field-scoped query is
        tried first and is usually the only request; it is an EXACT match,
        though, so remixes, live takes, `feat.` suffixes and punctuation
        drift all miss it — and on this path a miss is expensive twice over,
        costing a candidate from the page's attempt budget and pushing the
        medley toward the text-only fallback. A plain free-text retry
        recovers most of them, and only runs when the strict form found
        nothing, so a clean hit still costs exactly one request.

        `exclude` is the set of Deezer ids the user has marked as the wrong
        recording for this track. It is applied HERE rather than to the
        answer, because filtering afterwards would spend the search and
        still hand back nothing — the point is to reach the next candidate,
        which is very often the right one. Everything excluded means a
        genuine miss: the text fallback below is the correct place for it to
        end up, not another search.

        The URL's CDN token EXPIRES after a while, so callers must not
        persist it — cache at most for minutes.
        """
        ex = frozenset(int(i) for i in exclude)
        hit = (self._search_one(f'artist:"{artist}" track:"{title}"', ex)
               or self._search_one(f"{artist} {title}", ex))
        if hit is None:
            return {"miss": True}
        return {"url": hit["preview"], "deezer_id": int(hit["id"])}
