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


# ---- user hints -------------------------------------------------------------
#
# Hints are the user's own words about a home ("ambient, piano"), injected
# into that home's tag profile at the strength of its strongest organic tag.
# They deliberately ride the existing TAG_WEIGHT channel — no new weight knob
# exists to break the artist-overlap-primacy invariant.


def test_hints_lift_a_home_the_organic_tags_would_miss():
    quiet = build_profile([track("q1", ["unknown"])], ARTISTS, hints=["shoegaze"])
    plain = build_profile([track("p1", ["unknown"])], ARTISTS)
    res = suggest(track("new", ["slowdive"]), {"quiet": quiet, "plain": plain}, ARTISTS)
    assert res and res[0]["playlist_id"] == "quiet"
    assert any(r.startswith("your hint: shoegaze") for r in res[0]["reasons"])


def test_hint_match_never_outranks_a_real_artist_match():
    hinted = build_profile(
        [track("q1", ["unknown"])], ARTISTS,
        hints=["dream pop", "shoegaze"],  # a perfect hint vector for the seed
    )
    owns_artist = build_profile([track("d1", ["beach-house"])], ARTISTS)
    res = suggest(
        track("new", ["beach-house"]), {"hinted": hinted, "owns": owns_artist}, ARTISTS
    )
    assert res and res[0]["playlist_id"] == "owns"


def test_hint_tags_stay_out_of_the_organic_tag_reason():
    prof = build_profile(
        [track("d1", ["beach-house"])], ARTISTS, hints=["shoegaze"]
    )
    res = suggest(track("new", ["slowdive"]), {"H": prof}, ARTISTS)
    assert res
    hint_reasons = [r for r in res[0]["reasons"] if r.startswith("your hint:")]
    tag_reasons = [r for r in res[0]["reasons"] if r.startswith("artist tags:")]
    assert hint_reasons == ["your hint: shoegaze"]
    # dream pop is organic overlap; shoegaze must not be listed twice
    assert tag_reasons and "shoegaze" not in tag_reasons[0]


# ---- weak guesses: sub-threshold fill when nothing is confident ------------
#
# When no home clears MIN_SCORE and the track is filed nowhere, the ranking
# that was computed anyway surfaces as up-to-TOP_N entries flagged
# weak: True, each carrying at least one real reason (score > 0). The
# confident tier is untouched: a confident list never mixes with guesses,
# and its entries never carry the weak key. Ledger R2a ("a home matched only
# by neighbours can never surface a suggestion") scopes to the confident
# tier — a neighbour-only home MAY appear as a labeled guess; that mood-ish
# similarity evidence is exactly what the guess tier exists to show.


def test_weak_guesses_surface_when_nothing_clears_the_threshold():
    # Track's artist shares one tag in four with the home's profile:
    # cosine 1/(2*2) = 0.25 -> score 0.75, under MIN_SCORE but real.
    ta = {
        "h1": tag_entry("t1", name="H1"),
        "h2": tag_entry("u1", name="H2"),
        "h3": tag_entry("u2", name="H3"),
        "h4": tag_entry("u3", name="H4"),
        "seed": tag_entry("t1", "t2", "t3", "t4", name="Seed"),
    }
    prof = build_profile([track(f"u{i}", [f"h{i}"]) for i in range(1, 5)], ta)
    res = suggest(track("new", ["seed"]), {"H": prof, "heavy": profiles()["heavy"]}, ta)
    assert len(res) == 1
    r = res[0]
    assert r["playlist_id"] == "H"
    assert r["weak"] is True
    assert r["already"] is False
    assert 0 < r["score"] < suggest_mod.MIN_SCORE
    assert r["pct"] == round(r["score"] * 10)
    assert any(x.startswith("artist tags:") for x in r["reasons"])


def test_weak_guesses_ranked_and_capped_at_top_n():
    # Four neighbour-only homes at 0.27/0.21/0.15/0.09 — only TOP_N surface,
    # best first, all flagged weak.
    homes = {}
    track_map_similar = []
    for i, match in enumerate((0.9, 0.7, 0.5, 0.3)):
        homes[f"P{i}"] = build_profile(
            [track(f"p{i}", ["unknown"], title=f"Neigh {i}")], ARTISTS
        )
        track_map_similar.append(neighbour("Unknown", f"Neigh {i}", match))
    track_map = {track_key("Seedless", "New Song"): track_record(track_map_similar)}
    seed = {"uri": "new", "name": "New Song",
            "artists": [{"id": "seedless", "name": "Seedless"}]}
    res = suggest(seed, homes, ARTISTS, track_map)
    assert [r["playlist_id"] for r in res] == ["P0", "P1", "P2"]
    assert all(r["weak"] is True for r in res)
    assert res[0]["score"] > res[1]["score"] > res[2]["score"]


def test_neighbour_only_home_can_surface_as_weak_guess():
    # The R2a scoping pin: neighbour evidence alone (max 0.3 < MIN_SCORE)
    # still can't produce a CONFIDENT suggestion, but it now surfaces as a
    # labeled guess with its reason intact.
    prof = build_profile([track("h1", ["slowdive"], title="Alison")], ARTISTS)
    seed = {"uri": "new", "name": "Mystery Song",
            "artists": [{"id": "seedless", "name": "Seedless"}]}
    track_map = {
        track_key("Seedless", "Mystery Song"): track_record(
            [neighbour("Slowdive", "Alison", 0.7)]
        ),
    }
    res = suggest(seed, {"H": prof}, ARTISTS, track_map)
    assert len(res) == 1
    assert res[0]["weak"] is True
    assert res[0]["reasons"] == ["1 similar track already here"]


def test_confident_results_never_carry_weak_or_mix_with_guesses():
    # One home is confident (artist overlap); another scores sub-threshold.
    # The confident list is returned exactly as today: no weak key anywhere,
    # no guess entries appended.
    ta = {
        **ARTISTS,
        "h1": tag_entry("t1", name="H1"),
        "h2": tag_entry("u1", name="H2"),
        "h3": tag_entry("u2", name="H3"),
        "h4": tag_entry("u3", name="H4"),
        "seed2": tag_entry("t1", "t2", "t3", "t4", name="Beach House"),
    }
    weakish = build_profile([track(f"u{i}", [f"h{i}"]) for i in range(1, 5)], ta)
    owns = build_profile([track("d1", ["beach-house"])], ta)
    seed = {"uri": "new", "name": "New Song", "artists": [
        {"id": "beach-house", "name": "Beach House"},
        {"id": "seed2", "name": "Beach House"},
    ]}
    res = suggest(seed, {"weakish": weakish, "owns": owns}, ta)
    assert [r["playlist_id"] for r in res] == ["owns"]
    assert all("weak" not in r for r in res)


def test_already_filed_track_suppresses_weak_fill():
    # A track already in a home (even one scoring zero there) means the
    # list is non-empty — guesses only fill a would-be-EMPTY list.
    ta = {
        "h1": tag_entry("t1", name="H1"),
        "h2": tag_entry("u1", name="H2"),
        "h3": tag_entry("u2", name="H3"),
        "h4": tag_entry("u3", name="H4"),
        "seed": tag_entry("t1", "t2", "t3", "t4", name="Seed"),
    }
    weakish = build_profile([track(f"u{i}", [f"h{i}"]) for i in range(1, 5)], ta)
    filed = build_profile(
        [{"uri": "filed", "name": "Old", "artists": [{"id": None, "name": "Nameless"}]}],
        ta,
    )
    seed = {"uri": "filed", "name": "Old", "artists": [{"id": None, "name": "Nameless"}]}
    res = suggest(seed, {"weakish": weakish, "filed": filed}, ta)
    assert [r["playlist_id"] for r in res] == ["filed"]
    assert res[0]["already"] is True
    assert "weak" not in res[0]


def test_hints_weigh_like_the_strongest_organic_tag():
    # Two beach-house tracks -> organic "dream pop" count 2; the hint must
    # enter at 2 as well, not at 1, or a big home dilutes hints to nothing.
    prof = build_profile(
        [track("d1", ["beach-house"]), track("d2", ["beach-house"])],
        ARTISTS, hints=["ambient"],
    )
    assert prof["tag_counts"]["ambient"] == 2
    assert prof["hints"] == {"ambient"}


def artist_record(*similar, miss=False):
    return {"name": "X", "similar": [{"artist": a, "match": m} for a, m in similar],
            "fetched_at": 1.0, "miss": miss}


def test_build_profile_collects_normalized_artist_names():
    prof = build_profile([track("d1", ["beach-house"])], ARTISTS)
    assert prof["artist_names"] == {"beach house"}


def test_artist_sim_scores_home_present_similar_artists_only():
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    amap = {"seedless": artist_record(("Slowdive", 0.8), ("Nowhere Band", 0.9))}
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    total, count, names = suggest_mod._artist_sim_score(seed, prof, amap)
    assert (total, count, names) == (0.8, 1, ["Slowdive"])


def test_artist_sim_excludes_seed_artists_and_counts_collab_neighbour_once():
    # Binding, same pin as _neighbour_score: a similar artist matching ANY
    # seed artist scores nothing; a neighbour listed by BOTH credited
    # artists is counted once at its best match.
    prof = build_profile([track("h1", ["slowdive"]), track("h2", ["beach-house"])], ARTISTS)
    amap = {
        "a1": artist_record(("Beach  House", 0.9), ("Slowdive", 0.4)),  # internal double space
        "a2": artist_record(("Slowdive", 0.7)),
    }
    seed = {"uri": "n", "name": "S", "artists": [
        {"id": "a1", "name": "Beach House"}, {"id": "a2", "name": "Other"},
    ]}
    total, count, names = suggest_mod._artist_sim_score(seed, prof, amap)
    assert (total, count, names) == (0.7, 1, ["Slowdive"])


def test_artist_sim_miss_or_absent_records_contribute_nothing():
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    assert suggest_mod._artist_sim_score(seed, prof, {}) == (0.0, 0, [])
    amap = {"seedless": artist_record(("Slowdive", 0.8), miss=True)}
    assert suggest_mod._artist_sim_score(seed, prof, amap) == (0.0, 0, [])


def test_artist_sim_creates_a_guess_from_a_zero_score_home():
    # The coverage win: no tags, no track record, artist unknown to every
    # home — but Last.fm knows the artist's neighbours live in H.
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    amap = {"seedless": artist_record(("Slowdive", 0.8))}
    res = suggest(seed, {"H": prof}, ARTISTS, {}, amap)
    assert len(res) == 1 and res[0]["weak"] is True
    assert res[0]["reasons"] == ["similar artists: Slowdive"]
    assert res[0]["score"] == round(suggest_mod.ARTIST_SIM_WEIGHT * 0.8, 2)


def test_artist_sim_never_reaches_the_confident_tier():
    # Containment pin (spec §Scoring): even a maxed signal must neither
    # produce an unflagged entry nor appear beside a confident one.
    ten = [(f"N{i}", 1.0) for i in range(10)]
    sim_home = build_profile(
        [track(f"h{i}", ["unknown"], title=f"T{i}") for i in range(10)], ARTISTS)
    sim_home["artist_names"] = {f"n{i}" for i in range(10)}  # force 10 hits
    owns = build_profile([track("d1", ["beach-house"])], ARTISTS)
    amap = {"seedless": artist_record(*ten)}
    lone = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    res = suggest(lone, {"SIM": sim_home}, ARTISTS, {}, amap)
    assert res and res[0]["weak"] is True  # capped, flagged, never confident
    both = {"uri": "n", "name": "S", "artists": [
        {"id": "beach-house", "name": "Beach House"}, {"id": "seedless", "name": "Seedless"}]}
    res2 = suggest(both, {"SIM": sim_home, "owns": owns}, ARTISTS, {}, amap)
    assert [r["playlist_id"] for r in res2] == ["owns"]
    assert all("weak" not in r for r in res2)


def test_artist_sim_weight_read_at_call_time(monkeypatch):
    # Same contract TAG_WEIGHT pins — the eval harness varies it per run.
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    amap = {"seedless": artist_record(("Slowdive", 0.5))}
    monkeypatch.setattr(suggest_mod, "ARTIST_SIM_WEIGHT", 1.0)
    single = suggest(seed, {"H": prof}, ARTISTS, {}, amap)[0]["score"]
    monkeypatch.setattr(suggest_mod, "ARTIST_SIM_WEIGHT", 2.0)
    assert suggest(seed, {"H": prof}, ARTISTS, {}, amap)[0]["score"] == round(single * 2, 2)


# ---- co-occurrence: "you already file this artist next to these" -----------
#
# Corpus is {playlist_id: {artist_id: name}} over ALL cached playlists —
# inputs included, because an input playlist is exactly where "added A
# alongside B" evidence lives. Guess-tier only, like artist_sim: the
# confident tier's primacy headroom (3.4 - 3.3 = 0.1) has no room for a
# fourth signal, and the artist-sim precedent shows a guess-tier signal
# still lifts the artist-absent subset.


def cooc_corpus(**playlists):
    return {pid: dict(artists) for pid, artists in playlists.items()}


def test_cooc_counts_home_artist_shared_via_another_playlist():
    # Seed artist "seedless" sits next to Slowdive in input playlist "inp";
    # the home "H" contains Slowdive -> one partner, named.
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    corpus = cooc_corpus(
        inp={"seedless": "Seedless", "slowdive": "Slowdive"},
        H={"slowdive": "Slowdive"},
    )
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    count, names = suggest_mod._cooc_score(seed, "H", prof, corpus)
    assert (count, names) == (1, ["Slowdive"])


def test_cooc_excludes_the_scored_home_itself():
    # The ONLY shared playlist is the scored home itself — no evidence.
    # Without this exclusion the signal re-derives artist overlap.
    prof = build_profile(
        [track("h1", ["slowdive"]), track("h2", ["seedless"])], ARTISTS
    )
    corpus = cooc_corpus(H={"slowdive": "Slowdive", "seedless": "Seedless"})
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    assert suggest_mod._cooc_score(seed, "H", prof, corpus) == (0, [])


def test_cooc_excludes_seed_artists_from_partners():
    # The seed artist co-occurring with ITSELF in another playlist must not
    # count — same binding same-artist pin as _neighbour_score.
    prof = build_profile([track("h1", ["seedless"])], {**ARTISTS, "seedless": tag_entry(name="Seedless")})
    corpus = cooc_corpus(inp={"seedless": "Seedless"})
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    assert suggest_mod._cooc_score(seed, "H", prof, corpus) == (0, [])


def test_cooc_partner_must_be_in_the_home_profile():
    # Seed shares a playlist with Kvelertak, but the home holds only
    # Slowdive — co-occurrence elsewhere with a non-home artist is nothing.
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    corpus = cooc_corpus(inp={"seedless": "Seedless", "kvelertak": "Kvelertak"})
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    assert suggest_mod._cooc_score(seed, "H", prof, corpus) == (0, [])


def test_cooc_partner_counted_once_across_many_shared_playlists():
    # Slowdive shares TWO playlists with the seed artist; one partner, once.
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    corpus = cooc_corpus(
        inp1={"seedless": "Seedless", "slowdive": "Slowdive"},
        inp2={"seedless": "Seedless", "slowdive": "Slowdive"},
    )
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    count, names = suggest_mod._cooc_score(seed, "H", prof, corpus)
    assert (count, names) == (1, ["Slowdive"])


def test_cooc_creates_a_guess_from_a_zero_score_home():
    # No tags, no Last.fm records at all — but the user already files this
    # artist next to Slowdive in an input playlist, and H holds Slowdive.
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    corpus = cooc_corpus(
        inp={"seedless": "Seedless", "slowdive": "Slowdive"},
        H={"slowdive": "Slowdive"},
    )
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    res = suggest(seed, {"H": prof}, ARTISTS, {}, {}, corpus)
    assert len(res) == 1 and res[0]["weak"] is True
    assert res[0]["reasons"] == ["filed alongside: Slowdive"]
    assert res[0]["score"] == round(
        suggest_mod.COOC_WEIGHT * min(1, suggest_mod.COOC_CAP), 2
    )


def test_cooc_never_reaches_the_confident_tier():
    # Containment, same pin as artist_sim: a maxed co-occurrence signal must
    # neither produce an unflagged entry nor appear beside a confident one.
    prof = build_profile(
        [track(f"h{i}", [f"m{i}"]) for i in range(10)],
        {f"m{i}": tag_entry(name=f"M{i}") for i in range(10)},
    )
    corpus = {
        "inp": {"seedless": "Seedless", **{f"m{i}": f"M{i}" for i in range(10)}},
    }
    lone = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    res = suggest(lone, {"COOC": prof}, ARTISTS, {}, {}, corpus)
    assert res and res[0]["weak"] is True
    owns = build_profile([track("d1", ["beach-house"])], ARTISTS)
    both = {"uri": "n", "name": "S", "artists": [
        {"id": "beach-house", "name": "Beach House"}, {"id": "seedless", "name": "Seedless"}]}
    res2 = suggest(both, {"COOC": prof, "owns": owns}, ARTISTS, {}, {}, corpus)
    assert [r["playlist_id"] for r in res2] == ["owns"]
    assert all("weak" not in r for r in res2)


def test_cooc_weight_read_at_call_time(monkeypatch):
    # Same contract TAG_WEIGHT pins — the eval harness varies it per run.
    prof = build_profile([track("h1", ["slowdive"])], ARTISTS)
    corpus = cooc_corpus(inp={"seedless": "Seedless", "slowdive": "Slowdive"})
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    monkeypatch.setattr(suggest_mod, "COOC_WEIGHT", 0.5)
    single = suggest(seed, {"H": prof}, ARTISTS, {}, {}, corpus)[0]["score"]
    monkeypatch.setattr(suggest_mod, "COOC_WEIGHT", 1.0)
    assert suggest(seed, {"H": prof}, ARTISTS, {}, {}, corpus)[0]["score"] == round(single * 2, 2)


def test_cooc_reason_caps_listed_names_at_two():
    prof = build_profile(
        [track("h1", ["slowdive"]), track("h2", ["beach-house"]), track("h3", ["kvelertak"])],
        ARTISTS,
    )
    corpus = cooc_corpus(inp={
        "seedless": "Seedless", "slowdive": "Slowdive",
        "beach-house": "Beach House", "kvelertak": "Kvelertak",
    })
    seed = {"uri": "n", "name": "S", "artists": [{"id": "seedless", "name": "Seedless"}]}
    res = suggest(seed, {"H": prof}, ARTISTS, {}, {}, corpus)
    cooc_reasons = [r for r in res[0]["reasons"] if r.startswith("filed alongside:")]
    assert cooc_reasons == ["filed alongside: Beach House, Kvelertak"]
