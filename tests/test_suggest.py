from collections import Counter

import sortify.suggest as suggest_mod
from sortify.suggest import (
    ARTIST_BASE,
    ARTIST_PER_TRACK,
    NEIGHBOUR_WEIGHT,
    TAG_WEIGHT,
    build_profile,
    suggest,
)
from sortify.tags import track_key


def tag_entry(*tags, name="X", miss=False):
    return {"name": name, "tags": [{"name": t, "count": 100} for t in tags], "miss": miss}


ARTISTS = {
    "beach-house": tag_entry("dream pop", "shoegaze", name="Beach House"),
    "slowdive": tag_entry("shoegaze", "dream pop", name="Slowdive"),
    "kvelertak": tag_entry("black'n'roll", "hardcore punk", name="Kvelertak"),
    "unknown": tag_entry(name="Unknown"),
}


def track(uri, artists, title="Some Song"):
    names = {
        "beach-house": "Beach House", "slowdive": "Slowdive", "kvelertak": "Kvelertak",
        "unknown": "Unknown", "same-artist": "Same", "other-artist": "Other",
        "new-artist": "New",
    }
    return {"uri": uri, "name": title, "artists": [{"id": a, "name": names.get(a, a)} for a in artists]}


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


def test_tag_only_match_still_suggests():
    slow = {"slowdive-b": tag_entry("shoegaze", name="B-side")}
    info = {**ARTISTS, **slow}
    res = suggest(track("spotify:track:new", ["slowdive-b"]), profiles(), info)
    assert res and res[0]["playlist_id"] == "dreamy"
    assert res[0]["score"] < 3.0  # tags alone can't outrank a real artist match


def test_no_signal_gives_no_suggestions():
    res = suggest(track("spotify:track:new", ["unknown"]), profiles(), ARTISTS)
    assert res == []


def test_already_in_playlist_flagged_and_first():
    res = suggest(track("spotify:track:d1", ["beach-house"]), profiles(), ARTISTS)
    assert res[0]["playlist_id"] == "dreamy"
    assert res[0]["already"] is True


def test_artist_match_beats_pure_tag_match():
    both = profiles()
    res = suggest(track("spotify:track:new", ["kvelertak"]), both, ARTISTS)
    assert res[0]["playlist_id"] == "heavy"


def test_profile_counts_cleaned_artist_tags():
    tracks = [{"uri": "u1", "artists": [{"id": "a1", "name": "A"}]}]
    ta = {"a1": tag_entry("cumbia", "seen live")}  # "seen live" is _JUNK
    prof = build_profile(tracks, ta)
    assert prof["tag_counts"] == Counter({"cumbia": 1})
    assert "genre_counts" not in prof


def test_tag_match_scores_and_says_artist_level():
    # home full of cumbia; playing track's artist tagged cumbia but NOT in the home
    home_tracks = [
        {"uri": f"u{i}", "artists": [{"id": f"h{i}", "name": f"H{i}"}]}
        for i in range(3)
    ]
    home_tags = {f"h{i}": tag_entry("cumbia", name=f"H{i}") for i in range(3)}
    prof = build_profile(home_tracks, home_tags)
    profs = {"H": prof}

    new_track = {"uri": "new", "artists": [{"id": "newartist", "name": "New"}]}
    new_tags = {**home_tags, "newartist": tag_entry("cumbia", name="New")}

    results = suggest(new_track, profs, new_tags)
    assert results
    r = results[0]
    assert any(x.startswith("artist tags:") for x in r["reasons"])
    assert r["score"] > 0


def test_artist_overlap_still_outranks_a_perfect_tag_match():
    # H1 contains the artist directly (no tag overlap needed).
    # H2 has a different artist but a perfect tag cosine with the new track.
    h1_track = {"uri": "h1t", "artists": [{"id": "same-artist", "name": "Same"}]}
    h2_track = {"uri": "h2t", "artists": [{"id": "other-artist", "name": "Other"}]}
    ta = {
        "same-artist": tag_entry("polka", name="Same"),
        "other-artist": tag_entry("cumbia", name="Other"),
        "new-artist": tag_entry("cumbia", name="New"),
    }
    profs = {
        "H1": build_profile([h1_track], ta),
        "H2": build_profile([h2_track], ta),
    }
    new_track = {"uri": "new", "artists": [{"id": "same-artist", "name": "Same"}]}
    # give new_track the same artist as H1 (overlap) — but check score comparison
    # against a track that only matches H2 on tags, using the *other* artist id
    # for a clean single-signal comparison.
    tag_only_track = {"uri": "tagonly", "artists": [{"id": "new-artist", "name": "New"}]}

    scores = {}
    for pid, prof in profs.items():
        res = suggest(new_track if pid == "H1" else tag_only_track, {pid: prof}, ta)
        scores[pid] = res[0]["score"] if res else 0.0

    assert scores["H1"] > scores["H2"]


def test_missing_or_miss_artists_contribute_nothing():
    tracks = [{"uri": "u1", "artists": [{"id": "missing", "name": "M"}, {"id": "missed", "name": "N"}]}]
    ta = {"missed": tag_entry("cumbia", name="N", miss=True)}  # "missing" absent entirely
    prof = build_profile(tracks, ta)
    assert prof["tag_counts"] == Counter()

    tt = suggest_mod._track_tags(tracks[0], ta)
    assert tt == Counter()


def test_removing_track_from_profile_changes_its_score():
    # Regression pin for the eval harness (Task 3): if hold-one-out did
    # nothing, a track's score against its own home would be identical
    # whether or not it was left in the profile used to rank it, and the
    # harness would silently report a trivially-perfect accuracy.
    dreamy_tracks = [
        track("spotify:track:d1", ["beach-house"]),
        track("spotify:track:d2", ["beach-house"]),
        track("spotify:track:d3", ["slowdive"]),
    ]
    held = track("spotify:track:d1", ["beach-house"])

    with_held = build_profile(dreamy_tracks, ARTISTS)
    without_held = build_profile([t for t in dreamy_tracks if t["uri"] != held["uri"]], ARTISTS)

    score_with = suggest(held, {"dreamy": with_held}, ARTISTS)[0]["score"]
    without_results = suggest(held, {"dreamy": without_held}, ARTISTS)
    score_without = without_results[0]["score"] if without_results else 0.0

    assert score_with != score_without
    # And "already" must not fire once the held-out track is actually removed.
    assert without_results and without_results[0]["already"] is False


def test_tag_weight_read_at_call_time(monkeypatch):
    # ARTIST_TAG_DILUTION was removed (fix round 1, ruling R3a): it and
    # TAG_WEIGHT only ever appeared as a product, so TAG_WEIGHT alone is now
    # the single knob. This pins that `suggest()` reads TAG_WEIGHT as a
    # module-level lookup at call time (not a bound default) — the eval
    # harness's `weights()` context manager depends on that to vary it
    # between runs without re-importing the module.
    home_track = {"uri": "h1", "artists": [{"id": "h", "name": "H"}]}
    ta = {
        "h": tag_entry("cumbia", name="H"),
        "n": tag_entry("cumbia", name="N"),
    }
    prof = build_profile([home_track], ta)
    new_track = {"uri": "new", "artists": [{"id": "n", "name": "N"}]}

    monkeypatch.setattr(suggest_mod, "TAG_WEIGHT", 1.0)
    single = suggest(new_track, {"H": prof}, ta)[0]["score"]

    monkeypatch.setattr(suggest_mod, "TAG_WEIGHT", 2.0)
    doubled = suggest(new_track, {"H": prof}, ta)[0]["score"]

    assert doubled == round(single * 2, 2)


def test_a_freshly_fetched_artists_tags_lag_in_the_home_profile():
    """Pins a deliberate freshness asymmetry in how `/api/now` calls this
    module, not a bug: `suggest()` takes `tag_artists` fresh on every call
    (app.py re-reads tags.json guard-on-read on every request) but `profiles`
    is `_ensure_profiles`'s cached build, refreshed at most every
    PROFILE_TTL (10 min). So a Last.fm fetch that lands mid-poll (see
    `_fetch_missing_now_tags`) is visible in `_track_tags` — the currently
    playing track's own tags — on the very next request, but a *home*
    playlist containing that same artist keeps scoring it as tagless in
    `prof["tag_counts"]` until the profile is next rebuilt. Accepted trade:
    rebuilding profiles on every poll would cost a full re-scan of every home
    playlist's cached tracks for a benefit that only matters while an artist
    is mid-fetch, a narrow and self-correcting window (PROFILE_TTL is 10
    minutes, not indefinite).
    """
    # "shoegaze" not yet known when the home profile was last built...
    stale_tag_artists = {"beach-house": tag_entry(name="Beach House")}  # no tags yet
    home_tracks = [track("spotify:track:d1", ["beach-house"])]
    stale_profile = build_profile(home_tracks, stale_tag_artists)
    assert stale_profile["tag_counts"] == Counter()  # nothing to match against, yet

    # ...but a fetch lands between then and this request: tags.json now has
    # them, and `/api/now` reads that fresh copy for `tag_artists` while
    # still handing `suggest()` the profile built before the fetch.
    fresh_tag_artists = {"beach-house": tag_entry("dream pop", "shoegaze", name="Beach House")}

    # Immediate: the currently playing track's own tags see the fetch right away.
    playing = track("spotify:track:new", ["beach-house"])
    track_tags = suggest_mod._track_tags(playing, fresh_tag_artists)
    assert track_tags == Counter({"dream pop": 1, "shoegaze": 1})

    # Lagging: the home's tag-similarity signal doesn't, because `suggest()`
    # only ever reads whatever `prof["tag_counts"]` already holds — nothing
    # about calling it with fresher `tag_artists` reaches back into a profile
    # dict that was already computed.
    res = suggest(playing, {"dreamy": stale_profile}, fresh_tag_artists)
    assert res and res[0]["playlist_id"] == "dreamy"
    assert "artist tags" not in " ".join(res[0]["reasons"])  # no tag-similarity reason yet

    # Only a rebuilt profile (what actually happens after PROFILE_TTL) picks it up.
    rebuilt_profile = build_profile(home_tracks, fresh_tag_artists)
    res_after_rebuild = suggest(playing, {"dreamy": rebuilt_profile}, fresh_tag_artists)
    assert any("artist tags" in r for r in res_after_rebuild[0]["reasons"])


# ---- Task 2: neighbour scoring + track-tag resolution ---------------------


def track_record(similar, tags=None, miss=False):
    return {"similar": similar, "tags": tags or [], "fetched_at": "t", "miss": miss}


def neighbour(artist, track_name, match):
    return {"artist": artist, "track": track_name, "match": match}


def test_build_profile_captures_every_artist_track_key():
    home_tracks = [
        track("d1", ["beach-house"], title="Space Song"),
        track("d2", ["slowdive"], title="Alison"),
    ]
    prof = build_profile(home_tracks, ARTISTS)
    assert prof["track_keys"] == {
        track_key("Beach House", "Space Song"),
        track_key("Slowdive", "Alison"),
    }


def test_neighbour_same_artist_only_scores_zero():
    # BINDING regression pin: a neighbour whose artist matches the seed
    # track's own artist (case-insensitively) must be excluded before
    # scoring and before the count — even though the home actually holds
    # that exact (artist, title) pair.
    seed = track("new", ["beach-house"], title="Space Song")
    home_tracks = [track("h1", ["beach-house"], title="Other Song")]
    prof = build_profile(home_tracks, ARTISTS)
    track_map = {
        track_key("Beach House", "Space Song"): track_record([
            neighbour("Beach House", "Other Song", 0.9),
            neighbour("beach house", "Other Song", 0.5),  # case-insensitive match too
        ]),
    }
    assert suggest_mod._neighbour_score(seed, prof, track_map) == (0.0, 0)

    # And through the full pipeline: still suggested (artist overlap), but
    # with no neighbour score contribution and no neighbour reason.
    res = suggest(seed, {"dreamy": prof}, ARTISTS, track_map)
    assert res and res[0]["playlist_id"] == "dreamy"
    assert not any("similar track" in r for r in res[0]["reasons"])


def test_neighbour_same_artist_exclusion_survives_internal_whitespace_drift():
    # Fix round 1 regression: the seed-artist exclusion must use the SAME
    # normalizer track_key uses (tags._norm_name), not an independently
    # written .strip().lower() — otherwise an artist name with an internal
    # double space ("Beach  House") stops matching "Beach House" and the
    # same-artist exclusion silently fails to fire.
    seed = {
        "uri": "new", "name": "Space Song",
        "artists": [{"id": "beach-house", "name": "Beach  House"}],  # internal double space
    }
    home_tracks = [track("h1", ["beach-house"], title="Other Song")]
    prof = build_profile(home_tracks, ARTISTS)
    track_map = {
        track_key("Beach  House", "Space Song"): track_record([
            neighbour("Beach House", "Other Song", 0.9),  # single space — must still match
        ]),
    }
    assert suggest_mod._neighbour_score(seed, prof, track_map) == (0.0, 0)


def test_neighbour_cross_artist_present_scores_and_reasons():
    seed = track("new", ["unknown"], title="Mystery Song")
    prof = build_profile([track("h1", ["slowdive"], title="Alison")], ARTISTS)
    track_map = {
        track_key("Unknown", "Mystery Song"): track_record([
            neighbour("Slowdive", "Alison", 0.7),
        ]),
    }
    score, count = suggest_mod._neighbour_score(seed, prof, track_map)
    assert (score, count) == (0.7, 1)


def test_neighbour_match_weighting_favors_higher_match():
    prof = build_profile([track("h1", ["slowdive"], title="Alison")], ARTISTS)
    track_map = {
        track_key("Unknown", "SongA"): track_record([neighbour("Slowdive", "Alison", 0.9)]),
        track_key("Unknown", "SongB"): track_record([neighbour("Slowdive", "Alison", 0.2)]),
    }
    score_a, _ = suggest_mod._neighbour_score(track("a", ["unknown"], title="SongA"), prof, track_map)
    score_b, _ = suggest_mod._neighbour_score(track("b", ["unknown"], title="SongB"), prof, track_map)
    assert score_a == 0.9
    assert score_b == 0.2
    assert score_a > score_b


def test_neighbour_missing_or_miss_records_contribute_nothing():
    seed = track("new", ["unknown"], title="Nope")
    prof = build_profile([track("h1", ["slowdive"], title="Alison")], ARTISTS)
    assert suggest_mod._neighbour_score(seed, prof, {}) == (0.0, 0)

    track_map = {
        track_key("Unknown", "Nope"): track_record(
            [neighbour("Slowdive", "Alison", 0.9)], miss=True
        ),
    }
    assert suggest_mod._neighbour_score(seed, prof, track_map) == (0.0, 0)


def test_neighbour_sum_uncapped_in_return_value_but_capped_in_scoring():
    # The 10-neighbour-all-match-1.0 fixture: _neighbour_score itself returns
    # the raw (uncapped) sum per its documented interface; the cap that keeps
    # the artist-overlap-primacy pin intact lives in suggest()'s scoring.
    h2_tracks = [track(f"h2t{i}", ["other-artist"], title=f"H2 Song {i}") for i in range(10)]
    ta = {"other-artist": tag_entry("cumbia", name="Other"), "new-artist": tag_entry(name="New")}
    prof = build_profile(h2_tracks, ta)
    seed = track("new", ["new-artist"], title="New Song")
    similar = [neighbour("Other", f"H2 Song {i}", 1.0) for i in range(10)]
    track_map = {track_key("New", "New Song"): track_record(similar)}

    raw_sum, count = suggest_mod._neighbour_score(seed, prof, track_map)
    assert count == 10
    assert raw_sum == 10.0


def test_artist_overlap_still_outranks_max_combined_tag_and_neighbour():
    # Extends the existing tag-only primacy pin: even the worst case, a
    # perfect tag cosine PLUS a fully-capped neighbour score (the
    # 10-neighbour-all-match-1.0 fixture), must not outrank a single,
    # minimal (n=1) artist match. "same-artist" is deliberately untagged so
    # H1's score is the true floor (ARTIST_BASE + ARTIST_PER_TRACK, no tag
    # bonus of its own) rather than getting an accidental boost.
    ta = {
        "same-artist": tag_entry(name="Same"),
        "other-artist": tag_entry("cumbia", name="Other"),
        "new-artist": tag_entry("cumbia", name="New"),
    }
    h1_track = track("h1t", ["same-artist"], title="H1 Song")
    h2_tracks = [track(f"h2t{i}", ["other-artist"], title=f"H2 Song {i}") for i in range(10)]
    profs = {"H1": build_profile([h1_track], ta), "H2": build_profile(h2_tracks, ta)}

    new_track = track("new", ["same-artist"], title="New Song")
    tag_only_track = track("tagonly", ["new-artist"], title="New Song2")
    similar = [neighbour("Other", f"H2 Song {i}", 1.0) for i in range(10)]
    track_map = {track_key("New", "New Song2"): track_record(similar)}

    scores = {}
    for pid, prof in profs.items():
        seed = new_track if pid == "H1" else tag_only_track
        res = suggest(seed, {pid: prof}, ta, track_map)
        scores[pid] = res[0]["score"] if res else 0.0

    assert scores["H1"] == ARTIST_BASE + ARTIST_PER_TRACK  # 3.4: the true floor, no tag bonus
    assert scores["H2"] == round(TAG_WEIGHT + NEIGHBOUR_WEIGHT, 2)  # 3.3: both signals maxed
    assert scores["H1"] > scores["H2"]


def test_resolve_tags_track_level_replaces_artist_level():
    t = track("new", ["beach-house"], title="Space Song")
    track_map = {
        track_key("Beach House", "Space Song"): track_record([], tags=["ambient", "psychedelic"]),
    }
    tags, level = suggest_mod._resolve_tags(t, ARTISTS, track_map)
    assert level == "track"
    assert set(tags) == {"ambient", "psychedelic"}


def test_resolve_tags_falls_back_to_artist_when_no_track_record():
    t = track("new", ["beach-house"], title="Space Song")
    tags, level = suggest_mod._resolve_tags(t, ARTISTS, {})
    assert level == "artist"
    assert set(tags) == {"dream pop", "shoegaze"}


def test_resolve_tags_falls_back_when_track_record_has_no_usable_tags():
    t = track("new", ["beach-house"], title="Space Song")
    track_map = {track_key("Beach House", "Space Song"): track_record([], tags=[])}
    tags, level = suggest_mod._resolve_tags(t, ARTISTS, track_map)
    assert level == "artist"
    assert set(tags) == {"dream pop", "shoegaze"}


def test_resolve_tags_ignores_a_miss_record():
    t = track("new", ["beach-house"], title="Space Song")
    track_map = {
        track_key("Beach House", "Space Song"): track_record([], tags=["ambient"], miss=True),
    }
    tags, level = suggest_mod._resolve_tags(t, ARTISTS, track_map)
    assert level == "artist"


def test_suggest_track_level_tags_replace_artist_and_switch_reason_wording():
    ta = {**ARTISTS, "h-artist": tag_entry("ambient", name="H Artist")}
    home_track = track("h1", ["h-artist"], title="Home Song")
    prof = build_profile([home_track], ta)

    # "unknown" has no artist-level tags at all in ARTISTS, so any tag
    # signal here can only have come from the track-level record.
    seed = track("new", ["unknown"], title="New Song")
    track_map = {track_key("Unknown", "New Song"): track_record([], tags=["ambient"])}

    res = suggest(seed, {"H": prof}, ta, track_map)
    assert res
    assert any(r.startswith("tags:") for r in res[0]["reasons"])
    assert not any(r.startswith("artist tags:") for r in res[0]["reasons"])


def test_neighbour_score_finds_record_under_any_credited_artist_key():
    # A collab track's lastfm_tracks record may have been fetched under
    # either credited artist's name — _neighbour_score must not require it
    # to be the first one.
    def multi_track(uri, artist_ids, title):
        names = {"beach-house": "Beach House", "slowdive": "Slowdive"}
        return {
            "uri": uri, "name": title,
            "artists": [{"id": a, "name": names[a]} for a in artist_ids],
        }

    seed = multi_track("new", ["beach-house", "slowdive"], "Collab Song")
    prof = build_profile([track("h1", ["unknown"], title="Other Song")], ARTISTS)
    # Record only exists under the SECOND credited artist's key.
    track_map = {
        track_key("Slowdive", "Collab Song"): track_record(
            [neighbour("Unknown", "Other Song", 0.6)]
        ),
    }
    score, count = suggest_mod._neighbour_score(seed, prof, track_map)
    assert (score, count) == (0.6, 1)
