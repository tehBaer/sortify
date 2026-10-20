"""The split-into-parts grouping engine: pure, offline, deterministic.

Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md.
Fixtures are synthetic: artists carry one dominant Last.fm tag each, so the
"right" grouping is known by construction.
"""

import time

import pytest

from sortify import splitparts as spp
from sortify.tags import track_key


def make(groups: dict[str, int], start: int = 0):
    """Tracks + tag map: `groups` maps a tag to how many songs carry it.
    Each song has its own artist tagged only with that tag (count 100)."""
    tracks, tags = [], {}
    i = start
    for tag, count in groups.items():
        for _ in range(count):
            aid = f"a{i}"
            tracks.append({"uri": f"spotify:track:t{i}", "name": f"Song {i}",
                           "artists": [{"id": aid, "name": f"Artist {i}"}]})
            tags[aid] = {"name": f"Artist {i}", "tags": [{"name": tag, "count": 100}],
                         "miss": False}
            i += 1
    return tracks, tags


def part_tags(sugg, tracks, tags):
    by_uri = {t["uri"]: t for t in tracks}
    return [sorted({tags[by_uri[u]["artists"][0]["id"]]["tags"][0]["name"] for u in p})
            for p in sugg.parts]


def test_exactly_n_parts_from_clear_groups():
    tracks, tags = make({"rock": 10, "jazz": 10, "ambient": 10})

    s = spp.suggest(tracks, 3, tags, {}, {})

    assert len(s.parts) == 3
    assert sorted(part_tags(s, tracks, tags)) == [["ambient"], ["jazz"], ["rock"]]


def test_sizes_follow_the_music():
    tracks, tags = make({"rock": 30, "jazz": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert [len(p) for p in s.parts] == [30, 10]


def test_no_sliver_parts():
    tracks, tags = make({"rock": 20, "jazz": 20, "polka": 2})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert len(s.parts) == 2
    assert min(len(p) for p in s.parts) >= 3
    assert sum(len(p) for p in s.parts) == 42


def test_same_input_same_output_regardless_of_order():
    tracks, tags = make({"rock": 12, "jazz": 9, "ambient": 7})

    a = spp.suggest(tracks, 3, tags, {}, {})
    b = spp.suggest(list(reversed(tracks)), 3, tags, {}, {})

    assert [sorted(p) for p in a.parts] == [sorted(p) for p in b.parts]
    assert a.names == b.names


def test_each_part_keeps_playlist_order():
    tracks, tags = make({"rock": 10, "jazz": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    order = {t["uri"]: i for i, t in enumerate(tracks)}
    for p in s.parts:
        assert [order[u] for u in p] == sorted(order[u] for u in p)


def test_duplicate_uris_appear_once():
    tracks, tags = make({"rock": 10, "jazz": 10})

    s = spp.suggest(tracks + [dict(tracks[0])], 2, tags, {}, {})

    every = [u for p in s.parts for u in p]
    assert len(every) == len(set(every)) == 20


def test_songs_without_data_follow_their_artist_and_are_marked():
    tracks, tags = make({"rock": 10, "jazz": 10})
    # Two songs by an artist with no tags: one has a getSimilar record that
    # points at the jazz songs, so it has data; the other has nothing.
    tracks.append({"uri": "spotify:track:x1", "name": "With record",
                   "artists": [{"id": "ax", "name": "Ghost"}]})
    tracks.append({"uri": "spotify:track:x2", "name": "No record",
                   "artists": [{"id": "ax", "name": "Ghost"}]})
    jazz = [t for t in tracks if tags.get(t["artists"][0]["id"], {}).get("tags", [{}])[0].get("name") == "jazz"]
    track_map = {track_key("Ghost", "With record"): {
        "similar": [{"artist": t["artists"][0]["name"], "track": t["name"], "match": 1.0}
                    for t in jazz], "tags": [], "miss": False}}

    s = spp.suggest(tracks, 2, tags, track_map, {})

    part_of = {u: i for i, p in enumerate(s.parts) for u in p}
    jazz_part = part_of[jazz[0]["uri"]]
    assert part_of["spotify:track:x1"] == jazz_part
    assert part_of["spotify:track:x2"] == jazz_part
    assert s.unknown == ["spotify:track:x2"]


def test_a_song_with_no_data_and_no_artist_mate_goes_to_the_largest_part():
    tracks, tags = make({"rock": 14, "jazz": 10})
    tracks.append({"uri": "spotify:track:lonely", "name": "Lonely",
                   "artists": [{"id": "az", "name": "Nobody"}]})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert "spotify:track:lonely" in s.parts[0]
    assert s.unknown == ["spotify:track:lonely"]


def test_same_artist_songs_split_when_their_similar_lists_disagree():
    tracks, tags = make({"rock": 8, "ballad": 8})
    tags["aA"] = {"name": "Both", "tags": [{"name": "rock", "count": 100},
                                          {"name": "ballad", "count": 100}], "miss": False}
    tracks += [{"uri": "spotify:track:A1", "name": "Loud", "artists": [{"id": "aA", "name": "Both"}]},
               {"uri": "spotify:track:A2", "name": "Soft", "artists": [{"id": "aA", "name": "Both"}]}]
    by_tag = {"rock": [], "ballad": []}
    for t in tracks[:16]:
        by_tag[tags[t["artists"][0]["id"]]["tags"][0]["name"]].append(t)

    def sims(group):
        return [{"artist": t["artists"][0]["name"], "track": t["name"], "match": 1.0} for t in group]

    track_map = {track_key("Both", "Loud"): {"similar": sims(by_tag["rock"]), "tags": [], "miss": False},
                 track_key("Both", "Soft"): {"similar": sims(by_tag["ballad"]), "tags": [], "miss": False}}

    s = spp.suggest(tracks, 2, tags, track_map, {})

    part_of = {u: i for i, p in enumerate(s.parts) for u in p}
    assert part_of["spotify:track:A1"] == part_of[by_tag["rock"][0]["uri"]]
    assert part_of["spotify:track:A2"] == part_of[by_tag["ballad"][0]["uri"]]
    assert part_of["spotify:track:A1"] != part_of["spotify:track:A2"]


def test_the_hint_names_a_clearly_cleaner_count():
    tracks, tags = make({"rock": 10, "jazz": 10, "ambient": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert s.hint == 3
    assert set(s.scores) == {2, 3}


def test_no_hint_when_the_chosen_count_is_right():
    tracks, tags = make({"rock": 10, "jazz": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert s.hint is None


def test_names_come_from_distinctive_tags():
    tracks, tags = make({"rock": 10, "jazz": 10})

    s = spp.suggest(tracks, 2, tags, {}, {})

    assert sorted(s.names) == ["jazz", "rock"]


def test_out_of_range_counts_and_sizes_are_refused():
    tracks, tags = make({"rock": 10, "jazz": 10})
    with pytest.raises(spp.SplitError):
        spp.suggest(tracks, 1, tags, {}, {})
    with pytest.raises(spp.SplitError):
        spp.suggest(tracks, 7, tags, {}, {})
    small, small_tags = make({"rock": 9})
    with pytest.raises(spp.SplitError):
        spp.suggest(small, 2, small_tags, {}, {})
    big = [{"uri": f"spotify:track:b{i}", "name": f"B{i}", "artists": []} for i in range(2001)]
    with pytest.raises(spp.SplitError):
        spp.suggest(big, 2, {}, {}, {})


def test_not_enough_data_is_refused_with_a_reason():
    tracks = [{"uri": f"spotify:track:n{i}", "name": f"N{i}",
               "artists": [{"id": f"n{i}", "name": f"N{i}"}]} for i in range(12)]
    with pytest.raises(spp.SplitError, match="Last.fm"):
        spp.suggest(tracks, 2, {}, {}, {})


def test_place_puts_a_song_where_it_fits_best():
    tracks, tags = make({"rock": 10, "jazz": 10})
    rock = [t["uri"] for t in tracks[:10]]
    jazz = [t["uri"] for t in tracks[10:]]

    got = spp.place(tracks, [rock[1:], jazz[1:], []], [rock[0], jazz[0]], tags, {}, {})

    assert got == {rock[0]: 0, jazz[0]: 1}


def test_artist_similarity_counts():
    tracks, tags = make({"rock": 10, "jazz": 10})
    # One more song by an untagged artist that Last.fm calls similar to the
    # jazz artists: it has data (artist-similar) and lands with jazz.
    tracks.append({"uri": "spotify:track:s1", "name": "S1",
                   "artists": [{"id": "as", "name": "Sideman"}]})
    artist_map = {"as": {"name": "Sideman", "miss": False,
                         "similar": [{"artist": f"Artist {i}", "match": 1.0} for i in range(10, 20)]}}

    s = spp.suggest(tracks, 2, tags, {}, artist_map)

    part_of = {u: i for i, p in enumerate(s.parts) for u in p}
    assert part_of["spotify:track:s1"] == part_of[tracks[10]["uri"]]
    assert "spotify:track:s1" not in s.unknown


def test_a_thousand_songs_in_reasonable_time():
    groups = {f"g{i}": 50 for i in range(20)}
    tracks, tags = make(groups)

    t0 = time.monotonic()
    s = spp.suggest(tracks, 4, tags, {}, {})
    elapsed = time.monotonic() - t0

    assert len(s.parts) == 4
    assert elapsed < 15, f"{elapsed:.1f}s"


def test_duplicate_recordings_both_get_similar_credit():
    """Two different uris with the same artist name and title should both
    receive getSimilar credits, and results should be order-independent."""
    # Create a base set: rock artist and jazz artist
    tracks, tags = make({"rock": 10, "jazz": 10})

    # Add two duplicate recordings: different uris but same artist name and title
    tracks.append({"uri": "spotify:track:dup1", "name": "Dup",
                   "artists": [{"id": "ad", "name": "Reissued"}]})
    tracks.append({"uri": "spotify:track:dup2", "name": "Dup",
                   "artists": [{"id": "ad", "name": "Reissued"}]})
    tags["ad"] = {"name": "Reissued", "tags": [{"name": "rock", "count": 100}],
                  "miss": False}

    # Add a song whose getSimilar mentions the duplicates
    tracks.append({"uri": "spotify:track:ref", "name": "Ref",
                   "artists": [{"id": "aref", "name": "Referrer"}]})
    track_map = {track_key("Referrer", "Ref"): {
        "similar": [
            {"artist": "Reissued", "track": "Dup", "match": 0.8}
        ],
        "tags": [], "miss": False
    }}

    # Build signals forward
    sig_fwd = spp.build_signals(tracks, tags, track_map, {})
    ref_idx = sig_fwd.index.get("spotify:track:ref")
    dup1_idx = sig_fwd.index.get("spotify:track:dup1")
    dup2_idx = sig_fwd.index.get("spotify:track:dup2")

    # Build signals reversed
    sig_rev = spp.build_signals(list(reversed(tracks)), tags, track_map, {})
    # Map back to uris
    uri_to_tsim_rev = {}
    for u, idx in sig_rev.index.items():
        uri_to_tsim_rev[u] = sig_rev.tsim[idx]

    # Both duplicates should have the getSimilar match with the referrer
    assert dup1_idx in sig_fwd.tsim[ref_idx] or ref_idx in sig_fwd.tsim[dup1_idx]
    assert dup2_idx in sig_fwd.tsim[ref_idx] or ref_idx in sig_fwd.tsim[dup2_idx]

    # Check both have the match value
    ref_dup1_match = sig_fwd.tsim[ref_idx].get(dup1_idx, 0.0) or sig_fwd.tsim[dup1_idx].get(ref_idx, 0.0)
    ref_dup2_match = sig_fwd.tsim[ref_idx].get(dup2_idx, 0.0) or sig_fwd.tsim[dup2_idx].get(ref_idx, 0.0)
    assert ref_dup1_match == 0.8
    assert ref_dup2_match == 0.8

    # Check order-independence: reverse should give same matches
    ref_uri_matches_rev = uri_to_tsim_rev.get("spotify:track:ref", {})
    assert uri_to_tsim_rev.get("spotify:track:dup1", {}).get(sig_rev.index["spotify:track:ref"], 0.0) == 0.8 or \
           ref_uri_matches_rev.get(sig_rev.index.get("spotify:track:dup1"), 0.0) == 0.8
    assert uri_to_tsim_rev.get("spotify:track:dup2", {}).get(sig_rev.index["spotify:track:ref"], 0.0) == 0.8 or \
           ref_uri_matches_rev.get(sig_rev.index.get("spotify:track:dup2"), 0.0) == 0.8


def test_artists_sharing_a_name_both_get_artist_similarity():
    """Two artist ids with the same normalized name should both receive
    artist similarity credits."""
    tracks, tags = make({"rock": 5, "jazz": 5})

    # Add two artists with the same name but different ids
    tracks.append({"uri": "spotify:track:twin1", "name": "Twin1",
                   "artists": [{"id": "atwin1", "name": "Twin"}]})
    tracks.append({"uri": "spotify:track:twin2", "name": "Twin2",
                   "artists": [{"id": "atwin2", "name": "Twin"}]})
    tags["atwin1"] = {"name": "Twin", "tags": [{"name": "rock", "count": 100}],
                      "miss": False}
    tags["atwin2"] = {"name": "Twin", "tags": [{"name": "jazz", "count": 100}],
                      "miss": False}

    # Add a third artist whose artist_map similar list names "Twin"
    tracks.append({"uri": "spotify:track:ref_artist", "name": "RefArtist",
                   "artists": [{"id": "aref", "name": "Referrer"}]})
    tags["aref"] = {"name": "Referrer", "tags": [{"name": "indie", "count": 100}],
                    "miss": False}
    artist_map = {"aref": {"name": "Referrer", "miss": False,
                           "similar": [{"artist": "Twin", "match": 0.9}]}}

    sig = spp.build_signals(tracks, tags, {}, artist_map)

    # Both (aref, atwin1) and (aref, atwin2) pairs should be in asim
    assert sig.asim.get(("aref", "atwin1"), 0.0) == 0.9 or sig.asim.get(("atwin1", "aref"), 0.0) == 0.9
    assert sig.asim.get(("aref", "atwin2"), 0.0) == 0.9 or sig.asim.get(("atwin2", "aref"), 0.0) == 0.9
