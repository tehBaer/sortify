from sortify.split import split_tracks


def tags(*pairs):
    """The stored shape: Last.fm's raw tags, hygiene applied at split time."""
    return [{"name": n, "count": c} for n, c in pairs]


TAGS = {
    "bh": {"name": "Beach House", "tags": tags(("dream pop", 100), ("shoegaze", 80)), "miss": False},
    "sd": {"name": "Slowdive", "tags": tags(("shoegaze", 100), ("dream pop", 90)), "miss": False},
    "kv": {"name": "Kvelertak", "tags": tags(("black metal", 100), ("hardcore punk", 70)), "miss": False},
    "mayhem": {"name": "Mayhem", "tags": tags(("black metal", 100), ("norwegian black metal", 90)), "miss": False},
    "gone": {"name": "Gone", "tags": [], "miss": True},
}


def track(uri, artist, table=None):
    table = TAGS if table is None else table
    return {"uri": uri, "duration_ms": 300000,
            "artists": [{"id": artist, "name": table.get(artist, {}).get("name", artist)}]}


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


# Three genuine communities. The undersized one (d1/d2) shares one low-weight
# tag with the house pair and nothing at all with the metal pair, so its
# nearest neighbour is unambiguous — and it is *not* the first pile, so
# picking a merge target by index instead of by similarity fails the test.
MERGE_TAGS = {
    "b1": {"name": "Metal One", "tags": tags(("black metal", 100), ("death metal", 90))},
    "b2": {"name": "Metal Two", "tags": tags(("black metal", 95), ("death metal", 85))},
    "d1": {"name": "Quiet One", "tags": tags(("ambient", 100), ("drone", 90), ("techno", 12))},
    "d2": {"name": "Quiet Two", "tags": tags(("ambient", 95), ("drone", 85), ("techno", 11))},
    "h1": {"name": "House One", "tags": tags(("deep house", 100), ("techno", 90))},
    "h2": {"name": "House Two", "tags": tags(("deep house", 95), ("techno", 85))},
}


def merge_tracks():
    out = []
    for aid, n in (("b1", 10), ("b2", 10), ("h1", 10), ("h2", 10), ("d1", 1), ("d2", 1)):
        out += [track(f"spotify:track:{aid}{i}", aid, MERGE_TAGS) for i in range(n)]
    return out


def test_small_piles_merge_into_nearest_neighbour_by_tags():
    """The undersized pile must land with the pile it shares a tag with."""
    piles = split_tracks(merge_tracks(), MERGE_TAGS, {"min_pile": 15})
    assert len(piles) == 2
    by_uri = {u: p["id"] for p in piles for u in p["uris"]}
    assert by_uri["spotify:track:d10"] == by_uri["spotify:track:h10"]
    assert by_uri["spotify:track:d10"] != by_uri["spotify:track:b10"]
    assert by_uri["spotify:track:d20"] == by_uri["spotify:track:h10"]


def test_no_merging_when_every_pile_is_big_enough():
    """Guards the other direction: min_pile 2 leaves all three piles alone."""
    piles = split_tracks(merge_tracks(), MERGE_TAGS, {"min_pile": 2})
    assert len(piles) == 3
    assert [p["id"] for p in piles] == ["p1", "p2", "p3"]


def test_preserves_playlist_order_within_a_pile():
    tracks = many("bh", 5)
    piles = split_tracks(tracks, TAGS, {"min_pile": 1})
    assert piles[0]["uris"] == [t["uri"] for t in tracks]


def test_hygiene_is_applied_at_split_time():
    """Stored tags are raw, so the stoplist has to run here. An artist whose
    only tags are junk has no usable signal and belongs in untagged."""
    raw_tags = {
        "bh": TAGS["bh"],
        "junk": {"name": "Junk Band", "tags": tags(("norwegian", 100), ("seen live", 90))},
    }
    piles = split_tracks(many("bh", 20) + [track("spotify:track:j0", "junk", raw_tags)],
                         raw_tags, {"min_pile": 5})
    assert [p["id"] for p in piles] == ["p1", "untagged"]
    assert piles[1]["uris"] == ["spotify:track:j0"]
    assert "norwegian" not in piles[0]["tags"]


def test_tag_floor_is_a_split_parameter():
    """Retuning the floor must not need a re-fetch: same cache, both answers."""
    faint = {"faint": {"name": "Faint", "tags": tags(("ambient", 12))}}
    tracks = [track("spotify:track:f0", "faint", faint)]
    assert split_tracks(tracks, faint, {"min_pile": 1})[0]["tags"] == ["ambient"]
    assert split_tracks(tracks, faint, {"min_pile": 1, "tag_floor": 20})[0]["id"] == "untagged"


def test_max_tags_per_artist_is_a_split_parameter():
    """Keeping only the top tag breaks the pair's shared second tag, so they
    stop clustering together — again with no re-fetch."""
    pair = {
        "x": {"name": "X", "tags": tags(("techno", 100), ("dub", 40))},
        "y": {"name": "Y", "tags": tags(("ambient", 100), ("dub", 40))},
    }
    tracks = [track("spotify:track:x0", "x", pair), track("spotify:track:y0", "y", pair)]
    assert len(split_tracks(tracks, pair, {"min_pile": 1})) == 1
    assert len(split_tracks(tracks, pair, {"min_pile": 1, "max_tags_per_artist": 1})) == 2


def test_deterministic():
    tracks = many("bh", 10) + many("kv", 10)
    assert split_tracks(tracks, TAGS, {"min_pile": 5}) == split_tracks(tracks, TAGS, {"min_pile": 5})
