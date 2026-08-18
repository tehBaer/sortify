from collections import Counter

import sortify.suggest as suggest_mod
from sortify.suggest import build_profile, suggest


def tag_entry(*tags, name="X", miss=False):
    return {"name": name, "tags": [{"name": t, "count": 100} for t in tags], "miss": miss}


ARTISTS = {
    "beach-house": tag_entry("dream pop", "shoegaze", name="Beach House"),
    "slowdive": tag_entry("shoegaze", "dream pop", name="Slowdive"),
    "kvelertak": tag_entry("black'n'roll", "hardcore punk", name="Kvelertak"),
    "unknown": tag_entry(name="Unknown"),
}


def track(uri, artists):
    names = {"beach-house": "Beach House", "slowdive": "Slowdive", "kvelertak": "Kvelertak", "unknown": "Unknown"}
    return {"uri": uri, "artists": [{"id": a, "name": names.get(a, a)} for a in artists]}


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
