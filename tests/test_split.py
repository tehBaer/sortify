from sortify.split import split_tracks

TAGS = {
    "bh": {"name": "Beach House", "tags": [["dream pop", 100], ["shoegaze", 80]], "miss": False},
    "sd": {"name": "Slowdive", "tags": [["shoegaze", 100], ["dream pop", 90]], "miss": False},
    "kv": {"name": "Kvelertak", "tags": [["black metal", 100], ["hardcore punk", 70]], "miss": False},
    "mayhem": {"name": "Mayhem", "tags": [["black metal", 100], ["norwegian black metal", 90]], "miss": False},
    "gone": {"name": "Gone", "tags": [], "miss": True},
}


def track(uri, artist):
    return {"uri": uri, "duration_ms": 300000,
            "artists": [{"id": artist, "name": TAGS.get(artist, {}).get("name", artist)}]}


def many(artist, n, start=0):
    return [track(f"spotify:track:{artist}{i}", artist) for i in range(start, start + n)]


def test_separates_two_genre_families():
    tracks = many("bh", 10) + many("sd", 10) + many("kv", 10) + many("mayhem", 10)
    piles = split_tracks(tracks, TAGS, {"min_pile": 5})
    assert len(piles) == 2
    by_uri = {u: p["id"] for p in piles for u in p["uris"]}
    assert by_uri["spotify:track:bh0"] == by_uri["spotify:track:sd0"]
    assert by_uri["spotify:track:kv0"] == by_uri["spotify:track:mayhem0"]
    assert by_uri["spotify:track:bh0"] != by_uri["spotify:track:kv0"]


def test_pile_names_use_distinctive_tags():
    tracks = many("bh", 10) + many("sd", 10) + many("kv", 10) + many("mayhem", 10)
    piles = split_tracks(tracks, TAGS, {"min_pile": 5})
    names = " | ".join(p["name"] for p in piles)
    assert "black metal" in names
    assert "dream pop" in names or "shoegaze" in names


def test_untagged_tracks_get_their_own_pile():
    tracks = many("bh", 10) + many("gone", 4)
    piles = split_tracks(tracks, TAGS, {"min_pile": 5})
    untagged = [p for p in piles if p["id"] == "untagged"]
    assert len(untagged) == 1
    assert len(untagged[0]["uris"]) == 4


def test_untagged_pile_survives_below_min_pile():
    """Untagged is exempt from merging — it must never be folded into a genre."""
    tracks = many("bh", 30) + many("gone", 1)
    piles = split_tracks(tracks, TAGS, {"min_pile": 15})
    assert any(p["id"] == "untagged" and len(p["uris"]) == 1 for p in piles)


def test_every_track_lands_in_exactly_one_pile():
    tracks = many("bh", 10) + many("sd", 5) + many("kv", 7) + many("gone", 3)
    piles = split_tracks(tracks, TAGS, {"min_pile": 5})
    placed = [u for p in piles for u in p["uris"]]
    assert sorted(placed) == sorted(t["uri"] for t in tracks)
    assert len(placed) == len(set(placed))


def test_all_untagged_gives_one_pile():
    piles = split_tracks(many("gone", 20), TAGS)
    assert len(piles) == 1
    assert piles[0]["id"] == "untagged"


def test_empty_input_gives_no_piles():
    assert split_tracks([], TAGS) == []


def test_tracks_follow_first_listed_artist():
    """A featured guest must not drag a track out of its pile."""
    feat = {"uri": "spotify:track:feat", "duration_ms": 300000,
            "artists": [{"id": "kv", "name": "Kvelertak"}, {"id": "bh", "name": "Beach House"}]}
    piles = split_tracks(many("bh", 10) + many("kv", 10) + [feat], TAGS, {"min_pile": 5})
    by_uri = {u: p["id"] for p in piles for u in p["uris"]}
    assert by_uri["spotify:track:feat"] == by_uri["spotify:track:kv0"]


def test_small_piles_merge_into_nearest_neighbour():
    tracks = many("bh", 30) + many("sd", 2)
    piles = split_tracks(tracks, TAGS, {"min_pile": 15})
    assert len([p for p in piles if p["id"] != "untagged"]) == 1


def test_preserves_playlist_order_within_a_pile():
    tracks = many("bh", 5)
    piles = split_tracks(tracks, TAGS, {"min_pile": 1})
    assert piles[0]["uris"] == [t["uri"] for t in tracks]


def test_deterministic():
    tracks = many("bh", 10) + many("kv", 10)
    assert split_tracks(tracks, TAGS, {"min_pile": 5}) == split_tracks(tracks, TAGS, {"min_pile": 5})
