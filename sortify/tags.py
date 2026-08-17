"""Last.fm tag layer.

Spotify stopped returning artist genres in development mode — all 717 cached
artists come back with genres: []. Tags therefore come from Last.fm, which
covered 29 of 30 sampled artists from this library.

This module must never import sortify.spotify. It has its own HTTP client and
its own rate limiter, so tag traffic can never be routed through the Spotify
budget (or Spotify traffic through Last.fm's limiter). tests/test_tags.py
asserts the absence of that import.
"""

from __future__ import annotations

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
