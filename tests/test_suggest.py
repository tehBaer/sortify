from sortify.suggest import build_profile, suggest


ARTISTS = {
    "beach-house": {"name": "Beach House", "genres": ["dream pop", "shoegaze"]},
    "slowdive": {"name": "Slowdive", "genres": ["shoegaze", "dream pop"]},
    "kvelertak": {"name": "Kvelertak", "genres": ["black'n'roll", "hardcore punk"]},
    "unknown": {"name": "Unknown", "genres": []},
}


def track(uri, artists):
    return {"uri": uri, "artists": [{"id": a, "name": ARTISTS.get(a, {}).get("name", a)} for a in artists]}


def profiles():
    dreamy = build_profile(
        [track("spotify:track:d1", ["beach-house"]), track("spotify:track:d2", ["beach-house"]),
         track("spotify:track:d3", ["slowdive"])],
        ARTISTS,
    )
    heavy = build_profile([track("spotify:track:h1", ["kvelertak"])], ARTISTS)
    return {"dreamy": dreamy, "heavy": heavy}


def test_artist_match_ranks_first():
    res = suggest(track("spotify:track:new", ["beach-house"]), profiles(), ARTISTS)
    assert res and res[0]["playlist_id"] == "dreamy"
    assert any("Beach House" in r for r in res[0]["reasons"])


def test_genre_only_match_still_suggests():
    slow = {"slowdive-b": {"name": "B-side", "genres": ["shoegaze"]}}
    info = {**ARTISTS, **slow}
    res = suggest(track("spotify:track:new", ["slowdive-b"]), profiles(), info)
    assert res and res[0]["playlist_id"] == "dreamy"
    assert res[0]["score"] < 3.0  # genre alone can't outrank a real artist match


def test_no_signal_gives_no_suggestions():
    res = suggest(track("spotify:track:new", ["unknown"]), profiles(), ARTISTS)
    assert res == []


def test_already_in_playlist_flagged_and_first():
    res = suggest(track("spotify:track:d1", ["beach-house"]), profiles(), ARTISTS)
    assert res[0]["playlist_id"] == "dreamy"
    assert res[0]["already"] is True


def test_artist_match_beats_pure_genre_match():
    both = profiles()
    res = suggest(track("spotify:track:new", ["kvelertak"]), both, ARTISTS)
    assert res[0]["playlist_id"] == "heavy"
