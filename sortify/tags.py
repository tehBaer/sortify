"""Last.fm tag layer.

Spotify stopped returning artist genres in development mode — all 717 cached
artists come back with genres: []. Tags therefore come from Last.fm, which
covered 29 of 30 sampled artists from this library.

This module has its own HTTP client and its own rate limiter, so tag traffic
can never be routed through the Spotify budget (or Spotify traffic through
Last.fm's limiter). tests/test_tags.py asserts that this module never imports
the Spotify layer, not even transitively.

`enrich` stores tags exactly as Last.fm returned them. Hygiene (`clean_tags`)
runs at split time instead, because `data/tags.json` is a permanent cache that
is never re-fetched: anything filtered out here would be unrecoverable without
~700 fresh requests, and the stoplist and thresholds are certain to need
tuning once the whole library is visible.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# Tags that survive the count floor but say nothing about how music sounds.
# Every entry here was observed in a real probe of this user's library.
_JUNK = {
    "all", "misc", "x", "seen live", "favorites", "favourites", "my music",
    "albums i own", "under 2000 listeners", "spotify", "10s", "00s", "90s",
    "80s", "70s", "60s", "50s", "40s",
    # Personal library bookkeeping that leaked into public tags. The two
    # lidarr strings are verbatim from data/tags.json; they carry the word
    # "funk", so left in they would quietly bind two unrelated artists
    # together and can win a pile name outright (a tag on a single artist
    # creates no clustering edge but is maximally distinctive to the TF-IDF
    # namer — which is exactly how a city ended up naming a pile).
    "funk_add_to_lidarr_batch_8", "funk_add_to_lidarr_batch_11",
    "need to rate", "posted", "lesser known yet streamable artists",
    "if this band doesnt get huge i will buy a hat and eat it",
    "solo album", "new music", "favorit", "blandband",
    # Artist, crew and collective names used as tags. The existing name
    # filter below already drops a tag that is the artist's own name; these
    # are the same thing one step removed (a Wu-Tang member tagged
    # "wu-tang"), which that filter cannot see.
    "wu-tang", "wu-fam", "boot camp clik", "black hippy", "ofwgkta",
    "buena vista social club", "the black angels", "the black asap rocky",
    "blank and the blanks", "real recognize real and this nigga the realest",
    "lamarr sessions", "robertitus global", "rocky ram", "zomeki", "evislwa",
    "ellias", "jecks", "dubabub", "withhouse", "mj-house bounce",
}

_DESCRIPTORS = {
    "female vocalists", "male vocalists", "female vocalist", "male vocalist",
    "female fronted", "singer-songwriter women", "oldies", "beautiful",
    "chill", "cool", "awesome", "love", "sexy", "catchy", "melancholic",
    "groovy", "sad", "swag", "based", "vintage", "retro", "relaxing cafe",
    # Note what is deliberately NOT here: "funky". It sits one letter from a
    # genre this library is full of, and the risk of shaving real funk signal
    # off an artist outweighs the little it adds as a mood word.
}

# Nationalities, countries, regions and cities. Last.fm tags these heavily,
# and left in they produce piles that cut across every genre ("Norwegian",
# "dutch"). Everything below was read out of the real `data/tags.json` (891
# artists) unless it is the other half of an observed pair — a country whose
# demonym is in the data, or vice versa — because Last.fm's vocabulary uses
# both forms interchangeably and half a pair is a hole waiting to be found by
# the next artist.
#
# The line drawn here, and the reason "bollywood" is NOT below: drop a tag
# when everything it tells you is where the music (or the listener) is from;
# keep it when it names a way of sounding, even one named after a place.
# Bollywood is Indian film music — an arrangement style, an instrumentation, a
# vocal tradition — so it stays, and so do highlife, cumbia, chicha, bossa
# nova, fado, morna, calypso, soca, zouk, samba, candombe, tango, flamenco,
# klezmer, krautrock, mpb, j-pop, city pop, enka, molam, ethio-jazz, anatolian
# rock, chanson francaise, desert blues, delta/chicago/texas blues and
# east/west coast rap. Only the bare place words go.
#
# `world`, `world music` and `ethnic` were tried here and deliberately left
# out, against first instinct — they are marketing categories, not sounds,
# and `world` alone sits on 75 artists spanning afrobeat, cumbia, bossa nova
# and Tuvan throat singing. Measured on this library they are load-bearing
# anyway: they are the ONLY surviving tag for Orchestra Baobab, Salif Keita,
# Miriam Makeba, Oumou Sangaré, Cheikh Lô, Amadou & Mariam and four more,
# whose every other tag is a country. Dropping them moved 21 tracks into
# `untagged` and cut the cumbia/latin/salsa pile from 59 to 31, while fixing
# nothing: the place-named pile is already gone without them. Re-tuning is
# free (hygiene runs at split time), so this is a measurement, not a
# principle — remeasure before changing it.
_PLACES = {
    # Continents and supra-national regions.
    "africa", "afrika", "african", "east africa", "east african",
    "north africa", "north african", "west africa", "west african",
    "south africa", "south african", "asia", "asian", "europe", "european",
    "scandinavia", "scandinavian", "balkan", "balkans",
    # Countries, and the demonyms/language words that stand in for them.
    "algeria", "algerian", "american", "argentina", "australia", "australian",
    "austrian", "belgian", "benin", "brasil", "brazil", "brazilian",
    "brazilian music", "british", "burkina faso", "burkinabe", "cabo verde",
    "cambodia", "cambodian", "cameroon", "canada", "canadian", "cape verde",
    "chile", "china", "chinese", "colombia", "colombian", "congo", "cuba",
    "cuban", "czech", "danish", "dansk", "denmark", "dr congo", "dutch",
    "egypt", "england", "english", "estonian", "ethiopia", "ethiopian",
    "finland", "finnish", "france", "francais", "français", "french",
    "gambia", "gambian", "german", "germany", "ghana", "ghanaian", "greece",
    "greek", "guadeloupe", "guinea", "guinea bissau", "guinea-bissau",
    "guinean", "haiti", "hebrew", "hungarian", "iceland", "icelandic",
    "india", "indian", "indonesia", "iran", "iranian", "ireland", "irish",
    "israel", "israeli", "italian", "italy", "ivory coast", "jamaica",
    "jamaican", "japan", "japanese", "korea", "korean", "latvian", "lebanese",
    "lebanon", "lithuanian", "mali", "martinique", "mexican", "mexico",
    "morocco", "netherlands", "new zealand", "niger", "nigeria", "nigerian",
    "norsk", "norway", "norwegian", "persian", "peru", "peruvian", "poland",
    "polish", "portugal", "portuguese", "puerto rico", "romania", "russia",
    "russian", "scotland", "scottish", "senegal", "senegalese", "serbia",
    "sierra leone", "slovenia", "spain", "spanish", "sudan", "sudanese",
    "svensk", "svenskt", "sweden", "swedish", "swiss", "switzerland", "syria",
    "syrian", "taiwan", "thai", "thailand", "togo", "trinidad",
    "trinidad and tobago", "trinidadian", "trini", "tunisia", "turkey",
    "turkish", "uk", "ukraine", "united kingdom", "united states", "uruguay",
    "uruguayan", "us", "usa", "venezuela", "vietnam", "vietnamese", "wales",
    "welsh", "yemen", "zambia",
    # Peoples and ethnonyms — the same claim as a demonym, and just as silent
    # about how anything sounds. The music these sit on keeps its real style
    # tags: tishoumaren for the Tuareg artists, throat singing for the Tuvan
    # ones, cambodian rocks / psychedelic for the Khmer ones.
    "chicano", "khmer", "tuareg", "tuva", "tuvan",
    # Cities and city-regions. `chicago` alone binds Curtis Mayfield, Kanye
    # West and Rotary Connection into one pile across soul and hip-hop;
    # `new york` does the same for 24 artists. The compounds that name an
    # actual sound (chicago blues, new orleans rhythm and blues) survive,
    # since only exact matches are dropped.
    "amsterdam", "atlanta", "austin", "bamako", "berlin", "brooklyn",
    "chicago", "detroit", "east coast", "kingston", "lausanne", "lauzanne",
    "london", "memphis", "new orleans", "new york", "oslo",
    "rio de janeiro", "san francisco", "trondheim", "west coast",
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

    Applied in order: drop below `floor`, drop bare numbers, drop the
    stoplist, drop tags that overlap the artist's own name (catches self-tags
    and label names), keep the top `keep` by weight.

    The bare-number rule is a rule rather than two more stoplist entries
    because the data holds "11" and "13" — someone's private numbering — and
    "12" would sail straight through a list. No genre in Last.fm's vocabulary
    is a bare number; decade tags ("90s", "10s") keep their suffix and are
    handled by `_JUNK`.

    `raw` is Last.fm's own shape — `[{"name": ..., "count": ...}, ...]` — which
    is what `enrich` stores verbatim. This runs at split time, not fetch time,
    so `floor`, `keep` and the stoplist can all be retuned for free.

    The name filter only drops a tag that *is* the artist name, or that
    *contains* it ("Shimshai Live"). The reverse direction — tag contained in
    the artist name — was measured against the 720 real artists in
    `data/cache.json` and cost 20 of them their primary genre: Jaga Jazzist
    lost `jazz`, Funkadelic lost `funk`, The Moody Blues lost `blues`.
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
        if low.isdigit():
            continue
        if low in _STOPLIST:
            continue
        if name_l and (low == name_l or (len(name_l) >= 4 and name_l in low)):
            continue
        out.append((tag, w))
    out.sort(key=lambda tw: (-tw[1], tw[0].lower()))
    return out[:keep]


class LastFmError(Exception):
    """Raised when Last.fm returns an error (other than artist not found).

    `partial` carries whatever `enrich` had already verified when it aborted,
    so a transient failure 600 artists into a 700-artist run does not throw
    those 600 answers away. It is None for errors raised outside `enrich`.
    """

    def __init__(self, message: str, partial: dict | None = None):
        super().__init__(message)
        self.partial = partial


API = "https://ws.audioscrobbler.com/2.0/"
KEY_PATH = Path.home() / "state" / "sortify" / "lastfm.json"

# Last.fm's stated ceiling is 5 requests/second. Sit below it: this is a
# courtesy limit on someone else's free service, not a budget to spend.
MIN_INTERVAL = 0.25

# Last.fm code 6 is documented as "Invalid parameters - your request is
# missing a required parameter"; artist.getTopTags also reuses it for an
# unknown artist, and the two are told apart only by the message text. Treat
# it as a miss ONLY when the message says so — a malformed request (e.g. an
# empty api_key) otherwise writes `miss: true` for every artist in the
# library, permanently. Every other code (10: invalid key, 26: suspended,
# 29: rate limit, 8/11/16: service) raises for the same reason.
NOT_FOUND_CODE = 6
_NOT_FOUND_PHRASES = ("not be found", "not found")


def _looks_like_not_found(message: str) -> bool:
    low = (message or "").lower()
    return any(p in low for p in _NOT_FOUND_PHRASES)


# Unit separator (0x1F) rather than a printable character: track titles
# routinely contain dashes and slashes ("Free - Bird", "AC/DC"), so a plain
# join could collide two distinct (artist, title) pairs onto the same key.
TRACK_KEY_SEP = "\x1f"


def _norm_name(s: str) -> str:
    """Lowercase, whitespace-collapsed normalisation of one name.

    The name-half of `track_key`, factored out and exported so any other
    code that needs to decide "is this the same artist name" (e.g.
    `suggest._neighbour_score`'s same-artist exclusion) uses the exact same
    rule `track_key` uses — a second, independently-written `.strip().lower()`
    nearby is exactly how "Beach  House" (internal double space) stops
    matching "Beach House" in one of the two call sites and not the other.
    """
    return " ".join((s or "").split()).lower()


def track_key(artist: str, title: str) -> str:
    """Normalise (artist, title) into `lastfm_tracks.json`'s lookup key.

    Lowercase and whitespace-collapsed so "Aerosmith" / " aerosmith " and
    "Dream On" / "Dream  On" land on the same record.
    """
    return f"{_norm_name(artist)}{TRACK_KEY_SEP}{_norm_name(title)}"


def load_key(path: Path | None = None) -> str | None:
    """Read the API key from ~/state/sortify/lastfm.json.

    Deliberately outside the repo: data/config.json sits next to
    version-controlled files.
    """
    p = Path(path or KEY_PATH)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    # Only accept a dict with a non-empty string api_key.
    if not isinstance(data, dict):
        return None
    key = data.get("api_key")
    if isinstance(key, str) and key:
        return key
    return None


@dataclass(frozen=True)
class ArtistTags:
    """One artist's answer from Last.fm.

    `matched_name` is what Last.fm says it actually matched
    (`toptags.@attr.artist`), which `autocorrect=1` may quietly change — this
    library has 13 artists of three characters or fewer (`Air`, `C2C`, `OM`),
    prime collision targets. None when the response did not say.
    """

    matched_name: str | None
    tags: list[dict] = field(default_factory=list)


class LastFm:
    """Minimal Last.fm client with its own rate limiter.

    `sleep` and `client` are injectable so tests run without network or delay.
    """

    def __init__(self, key: str, sleep=time.sleep, client=None, timeout: float = 15.0):
        # A blank key is not a runtime hiccup, it is a misconfiguration:
        # load_key() returns None when the state file is missing or renamed,
        # and httpx renders that as an empty api_key, which Last.fm rejects
        # with code 6 — the same code as "artist not found".
        if not isinstance(key, str) or not key.strip():
            raise LastFmError(
                "Last.fm API key is missing or blank "
                f"(expected a non-empty string, got {key!r}); see {KEY_PATH}"
            )
        self.key = key
        self._sleep = sleep
        self._client = client or httpx.Client(
            headers={"User-Agent": "sortify/0.1 (+https://github.com/local/sortify)"}
        )
        # Per-request timeout `top_tags` passes to `self._client.get(...)`.
        # Defaults to the splitter's long-standing 15s; `/api/now`'s
        # force-path fetch (app.py's `_fetch_missing_now_tags`) overrides
        # this on its own instance to 5s so a slow Last.fm can't turn a
        # user-triggered request into a ~45s hang — see NOW_FETCH_TIMEOUT.
        self._timeout = timeout

    def top_tags(self, artist_name: str) -> ArtistTags | None:
        """Raw top tags for an artist, or None if Last.fm has no such artist.

        Tags come back exactly as Last.fm sent them, unfiltered — see the
        module docstring. Raises on anything other than a genuine "artist not
        found", so that service errors, rate limits, malformed requests or
        auth failures abort the batch loudly rather than writing false
        permanent misses to a cache that is never re-fetched.
        """
        if not isinstance(artist_name, str) or not artist_name.strip():
            raise LastFmError(f"artist name must be a non-empty string, got {artist_name!r}")
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
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise LastFmError(f"Last.fm returned a non-object body: {type(data).__name__}")
        if "error" in data:
            error_code = data.get("error")
            error_msg = data.get("message", "")
            try:
                code = int(error_code)  # Last.fm has been seen stringifying it
            except (TypeError, ValueError):
                code = None
            if code == NOT_FOUND_CODE and _looks_like_not_found(error_msg):
                return None
            raise LastFmError(f"Last.fm error {error_code}: {error_msg}")
        # A 200 with no `toptags` at all is a broken response (a CDN
        # maintenance page, say), not an artist without tags. Treating the two
        # alike would record every artist after that point as tagless forever.
        toptags = data.get("toptags")
        if not isinstance(toptags, dict):
            raise LastFmError(
                f"Last.fm response for {artist_name!r} has no usable 'toptags' object"
            )
        tags = toptags.get("tag", [])
        # Last.fm collapses a single tag into an object rather than a list.
        if isinstance(tags, dict):
            tags = [tags]
        if not isinstance(tags, list):
            raise LastFmError(f"Last.fm returned a non-list 'tag' for {artist_name!r}")
        attr = toptags.get("@attr")
        matched = attr.get("artist") if isinstance(attr, dict) else None
        return ArtistTags(matched_name=matched if isinstance(matched, str) else None,
                          tags=[t for t in tags if isinstance(t, dict)])

    def _validate_track_args(self, artist_name: str, track_name: str) -> None:
        if not isinstance(artist_name, str) or not artist_name.strip():
            raise LastFmError(f"artist name must be a non-empty string, got {artist_name!r}")
        if not isinstance(track_name, str) or not track_name.strip():
            raise LastFmError(f"track title must be a non-empty string, got {track_name!r}")

    def track_similar(self, artist_name: str, track_name: str) -> list[dict] | None:
        """Similar tracks for (artist, title), or None if Last.fm has no such track.

        Mirrors `top_tags`' request/error/pacing structure exactly. Entries are
        slimmed to `{"artist", "track", "match"}` — Last.fm's own shape nests
        the artist name and carries fields this design has no use for.
        """
        self._validate_track_args(artist_name, track_name)
        self._sleep(MIN_INTERVAL)
        resp = self._client.get(
            API,
            params={
                "method": "track.getSimilar",
                "artist": artist_name,
                "track": track_name,
                "api_key": self.key,
                "format": "json",
                "limit": 20,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise LastFmError(f"Last.fm returned a non-object body: {type(data).__name__}")
        if "error" in data:
            error_code = data.get("error")
            error_msg = data.get("message", "")
            try:
                code = int(error_code)
            except (TypeError, ValueError):
                code = None
            if code == NOT_FOUND_CODE and _looks_like_not_found(error_msg):
                return None
            raise LastFmError(f"Last.fm error {error_code}: {error_msg}")
        similartracks = data.get("similartracks")
        if not isinstance(similartracks, dict):
            raise LastFmError(
                f"Last.fm response for {artist_name!r}/{track_name!r} has no usable "
                "'similartracks' object"
            )
        tracks = similartracks.get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        if not isinstance(tracks, list):
            raise LastFmError(
                f"Last.fm returned a non-list 'track' for {artist_name!r}/{track_name!r}"
            )
        out = []
        for t in tracks:
            if not isinstance(t, dict):
                continue
            name = t.get("name")
            artist_field = t.get("artist")
            similar_artist = artist_field.get("name") if isinstance(artist_field, dict) else None
            try:
                match = float(t.get("match", 0))
            except (TypeError, ValueError):
                match = 0.0
            out.append({"artist": similar_artist, "track": name, "match": match})
        return out

    def artist_similar(self, artist_name: str) -> list[dict] | None:
        """Similar artists for an artist, or None if Last.fm has no such artist.

        Mirrors `track_similar`' request/error/pacing structure exactly. Entries are
        slimmed to `{"artist", "match"}` — Last.fm's own shape carries fields this
        design has no use for.
        """
        if not isinstance(artist_name, str) or not artist_name.strip():
            raise LastFmError(f"artist name must be a non-empty string, got {artist_name!r}")
        self._sleep(MIN_INTERVAL)
        resp = self._client.get(
            API,
            params={
                "method": "artist.getSimilar",
                "artist": artist_name,
                "api_key": self.key,
                "format": "json",
                "limit": 20,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise LastFmError(f"Last.fm returned a non-object body: {type(data).__name__}")
        if "error" in data:
            error_code = data.get("error")
            error_msg = data.get("message", "")
            try:
                code = int(error_code)
            except (TypeError, ValueError):
                code = None
            if code == NOT_FOUND_CODE and _looks_like_not_found(error_msg):
                return None
            raise LastFmError(f"Last.fm error {error_code}: {error_msg}")
        similarartists = data.get("similarartists")
        if not isinstance(similarartists, dict):
            raise LastFmError(
                f"Last.fm response for {artist_name!r} has no usable "
                "'similarartists' object"
            )
        artists = similarartists.get("artist", [])
        if isinstance(artists, dict):
            artists = [artists]
        if not isinstance(artists, list):
            raise LastFmError(
                f"Last.fm returned a non-list 'artist' for {artist_name!r}"
            )
        out = []
        for a in artists:
            if not isinstance(a, dict):
                continue
            name = a.get("name")
            try:
                match = float(a.get("match", 0))
            except (TypeError, ValueError):
                match = 0.0
            out.append({"artist": name, "match": match})
        return out

    def track_top_tags(self, artist_name: str, track_name: str) -> list[str] | None:
        """Top tags for (artist, title), or None if Last.fm has no such track.

        Mirrors `top_tags`' request/error/pacing structure exactly. Unlike
        `top_tags`, this returns plain tag names — the scoring layer only
        needs the vocabulary, not the per-tag counts.
        """
        self._validate_track_args(artist_name, track_name)
        self._sleep(MIN_INTERVAL)
        resp = self._client.get(
            API,
            params={
                "method": "track.getTopTags",
                "artist": artist_name,
                "track": track_name,
                "api_key": self.key,
                "format": "json",
                "limit": 20,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise LastFmError(f"Last.fm returned a non-object body: {type(data).__name__}")
        if "error" in data:
            error_code = data.get("error")
            error_msg = data.get("message", "")
            try:
                code = int(error_code)
            except (TypeError, ValueError):
                code = None
            if code == NOT_FOUND_CODE and _looks_like_not_found(error_msg):
                return None
            raise LastFmError(f"Last.fm error {error_code}: {error_msg}")
        toptags = data.get("toptags")
        if not isinstance(toptags, dict):
            raise LastFmError(
                f"Last.fm response for {artist_name!r}/{track_name!r} has no usable "
                "'toptags' object"
            )
        tags = toptags.get("tag", [])
        if isinstance(tags, dict):
            tags = [tags]
        if not isinstance(tags, list):
            raise LastFmError(
                f"Last.fm returned a non-list 'tag' for {artist_name!r}/{track_name!r}"
            )
        return [name for name in (
            (t.get("name") or "").strip() for t in tags if isinstance(t, dict)
        ) if name]


def _store_tags(raw: list[dict]) -> list[dict]:
    """Normalise Last.fm's tag list to name+count. Nothing is dropped."""
    return [{"name": (item.get("name") or "").strip(), "count": _weight(item)}
            for item in raw]


def _blank(name) -> bool:
    return not isinstance(name, str) or not name.strip()


def enrich(artist_names: dict[str, str], cached: dict, fm: LastFm, now: str,
           on_progress=None) -> dict:
    """Fetch tags for artists not already in `cached`. Returns the merged map.

    `cached` is the **inner** artists map from `data/tags.json`
    (`Store.tag_artists()`), not the versioned envelope — passing the envelope
    would match no artist ids and silently re-fetch the whole library.

    Tags are stored raw and unfiltered; `split.split_tracks` applies
    `clean_tags`. Misses are recorded as `miss: true` so they are never asked
    about again — at ~3% of artists, re-asking every split would be pure waste.

    On failure the exception carries `.partial`: the map as it stood, holding
    only verified-good entries plus whatever was already cached. Callers should
    persist it rather than discard several hundred answered requests.

    `on_progress(done, total)`, if given, is called after each artist that
    actually cost a request. This is the slow phase of a split — ~700 artists
    paced at MIN_INTERVAL — so it is the one worth reporting; the caller turns
    it into a progress bar. It stays a plain callable rather than anything
    richer so this module keeps knowing nothing about HTTP servers or the
    Spotify layer (test_module_never_imports_spotify_source).
    """
    out = dict(cached)
    # Counts only artists that will actually go over the wire. The caller
    # turns `total - done` into a time estimate (remaining x MIN_INTERVAL), so
    # an artist answered without a round trip — already cached, or blank —
    # must not inflate it. On a re-run after a failure, tags.json may already
    # hold nearly all of them, and counting those would promise minutes of
    # work that finishes in seconds.
    total = sum(1 for aid, name in artist_names.items()
                if aid not in out and not _blank(name))
    done = 0
    for aid, name in artist_names.items():
        if aid in out:
            continue
        if _blank(name):
            # A blank artist name is a data condition, not a service failure —
            # Spotify's own placeholder for a removed/unavailable track has no
            # artist at all, and no request could ever produce a different
            # answer for it. That makes it exactly analogous to a genuine
            # Last.fm "not found": permanently and knowably nothing. Recording
            # it as a miss (instead of calling top_tags, which rightly refuses
            # to send a blank name over the wire — kept as defence in depth)
            # means one dead track can no longer abort tagging for every
            # artist still waiting behind it in the batch.
            out[aid] = {"name": name if isinstance(name, str) else "", "lastfm_name": None,
                        "tags": [], "fetched_at": now, "miss": True}
            continue
        try:
            got = fm.top_tags(name)
        except LastFmError as exc:
            exc.partial = out
            raise
        except Exception as exc:  # transport errors, bad JSON, anything
            raise LastFmError(f"tag fetch failed for {name!r}: {exc}", partial=out) from exc
        if got is None:
            out[aid] = {"name": name, "lastfm_name": None, "tags": [],
                        "fetched_at": now, "miss": True}
        else:
            out[aid] = {"name": name, "lastfm_name": got.matched_name,
                        "tags": _store_tags(got.tags),
                        "fetched_at": now, "miss": False}
        # After the entry is stored, so a caller that reads `.partial` on the
        # next artist's failure sees exactly the count it was last told.
        done += 1
        if on_progress is not None:
            on_progress(done, total)
    return out


def fetch_track(fm: LastFm, artist: str, title: str, now: float) -> dict:
    """One `data/lastfm_tracks.json` record for (artist, title): getSimilar
    plus track top tags.

    A code-6 "not found" on one call does not make the whole record a miss —
    it just leaves that half empty. `miss` is True only when BOTH calls come
    back not-found, matching `Store.lastfm_track_map()`'s never-re-fetch
    convention for genuine misses. Any raised `LastFmError` (or other
    exception from the transport) propagates untouched; the caller leaves the
    key absent from the map so the next run retries it, exactly as `enrich`'s
    callers do for artist tags on a real failure.
    """
    similar = fm.track_similar(artist, title)
    tags = fm.track_top_tags(artist, title)
    return {
        "similar": similar if similar is not None else [],
        "tags": tags if tags is not None else [],
        "fetched_at": now,
        "miss": similar is None and tags is None,
    }
