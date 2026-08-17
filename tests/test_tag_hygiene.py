from sortify.tags import clean_tags


def raw(*pairs):
    return [{"name": n, "count": c} for n, c in pairs]


def test_drops_tags_below_floor():
    out = clean_tags(raw(("shoegaze", 100), ("obscure", 3)), "Slowdive")
    assert out == [("shoegaze", 100)]


def test_drops_geography_and_nationality():
    out = clean_tags(
        raw(("psychedelic rock", 100), ("turkish", 51), ("netherlands", 27),
            ("Trondheim", 66), ("icelandic", 95), ("Norway", 100)),
        "Altin Gun",
    )
    assert out == [("psychedelic rock", 100)]


def test_drops_junk_and_descriptors():
    out = clean_tags(
        raw(("spiritual", 100), ("All", 100), ("misc", 66), ("x", 66),
            ("seen live", 90), ("female vocalists", 80)),
        "Shimshai",
    )
    assert out == [("spiritual", 100)]


def test_drops_self_tags_and_substring_matches():
    out = clean_tags(
        raw(("trip-hop", 100), ("shimshai", 33), ("Shimshai Live", 40)),
        "Shimshai",
    )
    assert out == [("trip-hop", 100)]


def test_keeps_a_genre_that_is_part_of_the_artist_name():
    """Measured on the live library: dropping tags *contained in* the artist
    name cost 20 of 720 artists their primary genre."""
    assert clean_tags(raw(("jazz", 100), ("nu jazz", 80)), "Jaga Jazzist") == [
        ("jazz", 100), ("nu jazz", 80)]
    assert clean_tags(raw(("funk", 100)), "Funkadelic") == [("funk", 100)]
    assert clean_tags(raw(("blues", 100)), "The Moody Blues") == [("blues", 100)]
    assert clean_tags(raw(("house", 100)), "Green-House") == [("house", 100)]


def test_short_artist_names_do_not_strip_everything():
    """An artist named "A" must not lose every tag containing the letter a."""
    out = clean_tags(raw(("ambient", 100), ("trance", 80), ("a", 50)), "A")
    assert [t for t, _ in out] == ["ambient", "trance"]


def test_keeps_compound_genres():
    out = clean_tags(
        raw(("anatolian rock", 85), ("desert blues", 100), ("instrumental hip-hop", 71)),
        "Various",
    )
    assert [t for t, _ in out] == ["desert blues", "anatolian rock", "instrumental hip-hop"]


def test_caps_at_keep_limit():
    out = clean_tags(raw(*[(f"genre{i}", 100 - i) for i in range(20)]), "X", keep=8)
    assert len(out) == 8


def test_handles_string_counts_and_missing_counts():
    out = clean_tags([{"name": "techno", "count": "47"}, {"name": "house"}], "X")
    assert out == [("techno", 47)]


def test_empty_input_gives_empty_output():
    assert clean_tags([], "X") == []
