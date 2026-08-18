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


def test_drops_multi_word_places_and_cities():
    """The gap that named a real pile "trinidad and tobago · Bamako ·
    bollywood": the stoplist had single-word countries but not multi-word
    ones, and no cities beyond the two Norwegian ones."""
    out = clean_tags(
        raw(("afrobeat", 100), ("trinidad and tobago", 90), ("Bamako", 88),
            ("new york", 80), ("New Orleans", 70), ("Rio de Janeiro", 60),
            ("guinea-bissau", 55), ("united kingdom", 50), ("west african", 45),
            ("DR Congo", 40), ("san francisco", 35), ("east coast", 30),
            ("Ivory Coast", 25), ("puerto rico", 20), ("sierra leone", 15)),
        "Various",
        keep=20,
    )
    assert [t for t, _ in out] == ["afrobeat"]


def test_drops_demonyms_ethnonyms_and_language_words():
    """Same claim as a country name, and just as silent about how the music
    sounds. `svenskt`/`norsk`/`dansk`/`francais` are the local-language form
    Last.fm's Nordic and French taggers actually use."""
    out = clean_tags(
        raw(("psychedelic", 100), ("sudanese", 90), ("trinidadian", 85),
            ("khmer", 80), ("chicano", 75), ("tuvan", 70), ("tuareg", 65),
            ("svenskt", 60), ("norsk", 55), ("dansk", 50), ("francais", 45),
            ("Lebanese", 40), ("Colombian", 35), ("Ghanaian", 30),
            ("Uruguayan", 25), ("Peruvian", 20), ("asian", 15)),
        "Various",
        keep=20,
    )
    assert [t for t, _ in out] == ["psychedelic"]


def test_keeps_genres_that_are_named_after_places():
    """The conservative half of the rule. A tag goes only when everything it
    says is where the music came from — not when it names a way of sounding
    that happens to carry a place in its name. Bollywood is the sharp case:
    place-derived, and still a genre (Indian film music)."""
    place_derived_genres = [
        "bollywood", "highlife", "cumbia", "bossa nova", "chicha", "fado",
        "calypso", "soca", "zouk", "morna", "samba", "candombe", "tango",
        "flamenco", "klezmer", "krautrock", "mpb", "j-pop", "city pop",
        "enka", "molam", "ethio-jazz", "afrobeat", "soukous", "tishoumaren",
        "anatolian rock", "chanson francaise", "desert blues", "delta blues",
        "chicago blues", "texas blues", "british folk", "french house",
        "new orleans rhythm and blues", "east coast rap", "west coast rap",
        "latin jazz", "afro-cuban jazz", "japanese city pop",
    ]
    for genre in place_derived_genres:
        assert clean_tags(raw((genre, 100)), "Various") == [(genre, 100)], genre


def test_keeps_the_world_family_on_purpose():
    """`world`/`world music`/`ethnic` are marketing categories rather than
    sounds, and were tried in the stoplist. They are load-bearing on the real
    library — the only surviving tag for Orchestra Baobab, Salif Keita,
    Miriam Makeba and seven more, whose every other tag is a country — so
    they stay. Pinned so removing them is a deliberate act with a
    measurement behind it, not a tidy-up."""
    out = clean_tags(raw(("world", 100), ("world music", 90), ("ethnic", 80)),
                     "Orchestra Baobab")
    assert [t for t, _ in out] == ["world", "world music", "ethnic"]


def test_drops_bare_numbers():
    """`11` and `13` are in the real data (someone's private numbering) and
    survive the count floor easily. A rule, not two stoplist entries, because
    `12` would otherwise walk straight through."""
    out = clean_tags(raw(("dub", 100), ("11", 79), ("13", 60), ("50s", 40)), "Various")
    assert [t for t, _ in out] == ["dub"]


def test_drops_library_bookkeeping_and_crew_names():
    """Non-genre strings that a single artist can carry to a pile NAME: the
    namer scores by distinctiveness, so a tag on one artist is the most
    likely thing to win."""
    out = clean_tags(
        raw(("boom bap", 100), ("funk_add_to_lidarr_batch_8", 95),
            ("need to rate", 90), ("posted", 85), ("wu-tang", 80),
            ("black hippy", 75), ("ofwgkta", 70), ("solo album", 65),
            ("if this band doesnt get huge i will buy a hat and eat it", 60),
            ("lesser known yet streamable artists", 55), ("groovy", 50),
            ("vintage", 45)),
        "Various",
        keep=20,
    )
    assert [t for t, _ in out] == ["boom bap"]


def test_the_pile_that_started_this_has_no_name_left():
    """The three tags that actually named pile p1 of the 1231-track split.
    All three artists' remaining signal is elsewhere (or nowhere) — none of
    these may ever name a pile again."""
    assert clean_tags(raw(("trinidad and tobago", 100)), "Lord Kitchener") == []
    assert clean_tags(raw(("Bamako", 100)), "Rail Band") == []
    # ...except bollywood, which is the one of the three that is a genre.
    assert clean_tags(raw(("bollywood", 100)), "R. D. Burman") == [("bollywood", 100)]


def test_caps_at_keep_limit():
    out = clean_tags(raw(*[(f"genre{i}", 100 - i) for i in range(20)]), "X", keep=8)
    assert len(out) == 8


def test_handles_string_counts_and_missing_counts():
    out = clean_tags([{"name": "techno", "count": "47"}, {"name": "house"}], "X")
    assert out == [("techno", 47)]


def test_empty_input_gives_empty_output():
    assert clean_tags([], "X") == []
