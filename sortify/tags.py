"""Last.fm tag layer.

Spotify stopped returning artist genres in development mode — all 717 cached
artists come back with genres: []. Tags therefore come from Last.fm, which
covered 29 of 30 sampled artists from this library.

This module has its own HTTP client and its own rate limiter, so tag traffic
can never be routed through the Spotify budget (or Spotify traffic through
Last.fm's limiter). tests/test_tags.py asserts that this module never imports
the Spotify layer.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

# Tags that survive the count floor but say nothing about how music sounds.
# Every entry here was observed in a real probe of this user's library.
_JUNK = {
    "all", "misc", "x", "seen live", "favorites", "favourites", "my music",
    "albums i own", "under 2000 listeners", "spotify", "10s", "00s", "90s",
    "80s", "70s", "60s",
}

_DESCRIPTORS = {
    "female vocalists", "male vocalists", "female vocalist", "male vocalist",
    "female fronted", "singer-songwriter women", "oldies", "beautiful",
    "chill", "cool", "awesome", "love", "sexy", "catchy", "melancholic",
}

# Nationalities, countries and cities. Last.fm tags these heavily, and left in
# they produce piles that cut across every genre ("Norwegian", "dutch").
_PLACES = {
    "african", "american", "argentina", "australia", "australian", "austrian",
    "belgian", "brazil", "brazilian", "british", "canada", "canadian", "chile",
    "china", "chinese", "colombia", "cuba", "cuban", "czech", "danish",
    "denmark", "dutch", "egypt", "england", "english", "estonian", "ethiopia",
    "ethiopian", "finland", "finnish", "france", "french", "german", "germany",
    "greece", "greek", "hungarian", "iceland", "icelandic", "india", "indian",
    "indonesia", "iran", "iranian", "ireland", "irish", "israel", "israeli",
    "italian", "italy", "jamaica", "jamaican", "japan", "japanese", "korea",
    "korean", "latvian", "lebanon", "lithuanian", "mali", "mexican", "mexico",
    "morocco", "netherlands", "new zealand", "niger", "nigeria", "nigerian",
    "norway", "norwegian", "oslo", "peru", "poland", "polish", "portugal",
    "portuguese", "romania", "russia", "russian", "scotland", "scottish",
    "senegal", "serbia", "slovenia", "south africa", "spain", "spanish",
    "sweden", "swedish", "swiss", "switzerland", "taiwan", "thailand",
    "trondheim", "tunisia", "turkey", "turkish", "uk", "ukraine", "usa",
    "venezuela", "vietnam", "wales", "welsh",
}

_STOPLIST = _JUNK | _DESCRIPTORS | _PLACES


def _weight(item: dict) -> int:
    try:
        return int(item.get("count", 0))
    except (TypeError, ValueError):
        return 0


def clean_tags(
    raw: list[dict], artist_name: str, floor: int = 10, keep: int = 8
) -> list[tuple[str, int]]:
    """Filter Last.fm's raw top tags down to usable genre signal.

    Applied in order: drop below `floor`, drop the stoplist, drop tags that
    overlap the artist's own name (catches self-tags and label names), keep
    the top `keep` by weight.
    """
    name_l = (artist_name or "").strip().lower()
    out: list[tuple[str, int]] = []
    for item in raw:
        tag = (item.get("name") or "").strip()
        if not tag:
            continue
        w = _weight(item)
        if w < floor:
            continue
        low = tag.lower()
        if low in _STOPLIST:
            continue
        if name_l and (low == name_l or (len(name_l) >= 4 and (low in name_l or name_l in low))):
            continue
        out.append((tag, w))
    out.sort(key=lambda tw: (-tw[1], tw[0].lower()))
    return out[:keep]


API = "https://ws.audioscrobbler.com/2.0/"
KEY_PATH = Path.home() / "state" / "sortify" / "lastfm.json"

# Last.fm's stated ceiling is 5 requests/second. Sit below it: this is a
# courtesy limit on someone else's free service, not a budget to spend.
MIN_INTERVAL = 0.25


def load_key(path: Path | None = None) -> str | None:
    """Read the API key from ~/state/sortify/lastfm.json.

    Deliberately outside the repo: data/config.json sits next to
    version-controlled files.
    """
    p = Path(path or KEY_PATH)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("api_key") or None
    except (json.JSONDecodeError, OSError):
        return None


class LastFm:
    """Minimal Last.fm client with its own rate limiter.

    `sleep` and `client` are injectable so tests run without network or delay.
    """

    def __init__(self, key: str, sleep=time.sleep, client=None):
        self.key = key
        self._sleep = sleep
        self._client = client or httpx.Client(
            headers={"User-Agent": "sortify/0.1 (+https://github.com/local/sortify)"}
        )

    def top_tags(self, artist_name: str) -> list[dict] | None:
        """Raw top tags for an artist, or None if Last.fm has no such artist."""
        self._sleep(MIN_INTERVAL)
        resp = self._client.get(
            API,
            params={
                "method": "artist.getTopTags",
                "artist": artist_name,
                "api_key": self.key,
                "format": "json",
                "autocorrect": "1",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return None
        tags = data.get("toptags", {}).get("tag", [])
        # Last.fm collapses a single tag into an object rather than a list.
        if isinstance(tags, dict):
            tags = [tags]
        return tags


def enrich(artist_names: dict[str, str], cached: dict, fm: LastFm, now: str) -> dict:
    """Fetch tags for artists not already in `cached`. Returns the merged map.

    Misses are recorded as `miss: true` so they are never asked about again —
    at ~3% of artists, re-asking every split would be pure waste.
    """
    out = dict(cached)
    for aid, name in artist_names.items():
        if aid in out:
            continue
        raw = fm.top_tags(name)
        if raw is None:
            out[aid] = {"name": name, "lastfm_name": None, "tags": [],
                        "fetched_at": now, "miss": True}
        else:
            out[aid] = {"name": name, "lastfm_name": name,
                        "tags": [[t, w] for t, w in clean_tags(raw, name)],
                        "fetched_at": now, "miss": False}
    return out
