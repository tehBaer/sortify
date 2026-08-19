"""Track BPM from Deezer's public API.

Spotify's audio-features endpoint no longer exists for dev-mode apps, so BPM
comes from Deezer instead: keyless, matched by artist+title search, two GET
requests per track (search for the id, then the track detail that carries
`bpm`). Results are cached write-once in `data/deezer.json` by the caller —
this module only talks to the network.

Deezer is not Spotify: none of the budget ledger applies. Its own limit is
50 requests per 5 s per IP, which the one-track-per-minute now-playing path
cannot approach. Deezer reports quota/API errors as HTTP 200 with an
`{"error": ...}` body — those raise `DeezerError` (retryable later) rather
than being recorded as a miss, which would be permanent.
"""

from __future__ import annotations

import time

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

    def fetch_track(self, artist: str, title: str) -> dict:
        """A `deezer.json` record for one track.

        {"bpm", "deezer_id", "fetched_at"} on success; {"miss": True} when
        Deezer has no matching track or knows the track but carries no BPM
        (it reports "unknown" as 0) — both are permanent answers worth
        remembering. Transport failures and Deezer error payloads raise
        instead: they are retryable, and recording them would make a
        temporary outage permanent.
        """
        hits = self._get(
            "/search", {"q": f'artist:"{artist}" track:"{title}"', "limit": 1}
        ).get("data") or []
        if not hits or not hits[0].get("id"):
            return {"miss": True}
        track_id = int(hits[0]["id"])
        bpm = self._get(f"/track/{track_id}").get("bpm") or 0
        try:
            bpm = float(bpm)
        except (TypeError, ValueError):
            bpm = 0.0
        if bpm <= 0:
            return {"miss": True}
        return {"bpm": round(bpm, 1), "deezer_id": track_id, "fetched_at": time.time()}
