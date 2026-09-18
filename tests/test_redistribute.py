"""Tag-driven redistribution of buffer playlists.

Zero API calls: every test runs against a hand-built tag map.
"""

import pytest

from sortify import redistribute as R


def _artist(name, *tags):
    """A tags.json artist entry. Tags are (name, count) pairs."""
    return {"name": name, "lastfm_name": name,
            "tags": [{"name": t, "count": c} for t, c in tags], "miss": False}


def _track(tid, *artist_ids):
    return {"id": tid, "uri": f"spotify:track:{tid}",
            "name": tid, "artists": [{"id": a} for a in artist_ids]}


@pytest.fixture
def tagmap():
    return {
        "a_mantra": _artist("Deva", ("mantra", 100), ("new age", 80),
                            ("meditative", 60), ("electronic", 20)),
        "a_techno": _artist("Surgeon", ("techno", 100), ("minimal", 70),
                            ("electronic", 60)),
        "a_disco":  _artist("Chic", ("disco", 100), ("funk", 70),
                            ("pop", 50), ("electronic", 15)),
        "a_folk":   _artist("Nick", ("folk", 100), ("singer-songwriter", 80),
                            ("acoustic", 70)),
        "a_blank":  {"name": "Nobody", "lastfm_name": None, "tags": [],
                     "miss": True},
    }


# ---- vectors ----------------------------------------------------------

def test_track_vector_reads_every_artist_on_the_track(tagmap):
    v = R.track_vector(_track("t1", "a_mantra", "a_techno"), tagmap)
    assert "mantra" in v and "techno" in v


def test_track_vector_of_an_untagged_artist_is_empty(tagmap):
    assert R.track_vector(_track("t1", "a_blank"), tagmap) == {}


def test_unknown_artist_id_does_not_raise(tagmap):
    assert R.track_vector(_track("t1", "nope"), tagmap) == {}


def test_playlist_vector_sums_its_tracks(tagmap):
    v = R.playlist_vector([_track("t1", "a_techno"), _track("t2", "a_techno")],
                          tagmap)
    assert v["techno"] > 0


# ---- idf --------------------------------------------------------------

def test_idf_discounts_a_tag_present_in_every_profile():
    profiles = {"x": {"electronic": 1.0, "techno": 1.0},
                "y": {"electronic": 1.0, "disco": 1.0},
                "z": {"electronic": 1.0, "folk": 1.0}}
    idf = R.build_idf(profiles)
    assert idf["electronic"] < idf["techno"]
    assert idf["techno"] == idf["disco"] == idf["folk"]


def test_idf_of_an_unseen_tag_is_the_maximum():
    idf = R.build_idf({"x": {"a": 1.0}, "y": {"b": 1.0}})
    assert R.idf_of(idf, "never-seen") >= max(idf.values())


# ---- assignment -------------------------------------------------------

def test_a_mantra_track_beats_the_generic_electronic_pull(tagmap):
    """The whole point of idf: 'electronic' is everywhere, 'mantra' is not."""
    targets = {
        "mantric": R.playlist_vector([_track("m", "a_mantra")], tagmap),
        "electronique": R.playlist_vector(
            [_track("e1", "a_techno"), _track("e2", "a_techno")], tagmap),
        "discopop": R.playlist_vector([_track("d", "a_disco")], tagmap),
    }
    idf = R.build_idf(targets)
    got = R.assign(_track("new", "a_mantra"), targets, idf, tagmap)
    assert got.target == "mantric"
    assert got.score > 0


def test_a_techno_track_lands_in_the_electronic_buffer(tagmap):
    targets = {
        "mantric": R.playlist_vector([_track("m", "a_mantra")], tagmap),
        "electronique": R.playlist_vector([_track("e", "a_techno")], tagmap),
    }
    idf = R.build_idf(targets)
    assert R.assign(_track("new", "a_techno"), targets, idf, tagmap).target == "electronique"


def test_assignment_reports_the_tags_that_drove_it(tagmap):
    targets = {"mantric": R.playlist_vector([_track("m", "a_mantra")], tagmap),
               "electronique": R.playlist_vector([_track("e", "a_techno")], tagmap)}
    idf = R.build_idf(targets)
    got = R.assign(_track("new", "a_mantra"), targets, idf, tagmap)
    assert "mantra" in got.matched


def test_an_untagged_track_still_gets_a_target_but_scores_zero(tagmap):
    """'Force a best guess' — nothing is left behind, but the score is honest."""
    targets = {"mantric": R.playlist_vector([_track("m", "a_mantra")], tagmap),
               "electronique": R.playlist_vector([_track("e", "a_techno")], tagmap)}
    idf = R.build_idf(targets)
    got = R.assign(_track("new", "a_blank"), targets, idf, tagmap)
    assert got.target in targets
    assert got.score == 0.0
    assert got.blind is True


def test_a_scored_assignment_is_not_blind(tagmap):
    targets = {"mantric": R.playlist_vector([_track("m", "a_mantra")], tagmap),
               "electronique": R.playlist_vector([_track("e", "a_techno")], tagmap)}
    idf = R.build_idf(targets)
    assert R.assign(_track("new", "a_mantra"), targets, idf, tagmap).blind is False


def test_blind_tracks_spread_instead_of_all_piling_into_one_buffer(tagmap):
    """A no-signal track picks a target by rotation, not by argmax of zeros.

    Without this every untagged track lands in whichever profile happens to
    sort first, which would dump ~half of [Ursus] into one playlist.
    """
    targets = {"a": {"x": 1.0}, "b": {"y": 1.0}, "c": {"z": 1.0}}
    idf = R.build_idf(targets)
    picks = {R.assign(_track(f"t{i}", "a_blank"), targets, idf, tagmap, blind_seq=i).target
             for i in range(3)}
    assert len(picks) == 3


# ---- anchors ----------------------------------------------------------

def test_anchors_give_an_empty_playlist_a_usable_profile(tagmap):
    """[Gammalakustisk] has one untagged track; its name is the only signal."""
    targets = {
        "gammalakustisk": R.with_anchors({}, {"folk": 100, "acoustic": 100,
                                              "singer-songwriter": 80}),
        "electronique": R.playlist_vector([_track("e", "a_techno")], tagmap),
    }
    idf = R.build_idf(targets)
    assert R.assign(_track("new", "a_folk"), targets, idf, tagmap).target == "gammalakustisk"


def test_anchors_do_not_erase_real_track_signal(tagmap):
    base = R.playlist_vector([_track("e", "a_techno")], tagmap)
    merged = R.with_anchors(base, {"folk": 50})
    assert merged["techno"] > 0 and merged["folk"] > 0


# ---- planning ---------------------------------------------------------

def test_plan_assigns_every_source_track(tagmap):
    sources = {"ursus": [_track("t1", "a_mantra"), _track("t2", "a_techno"),
                         _track("t3", "a_blank")]}
    targets = {"mantric": R.playlist_vector([_track("m", "a_mantra")], tagmap),
               "electronique": R.playlist_vector([_track("e", "a_techno")], tagmap)}
    plan = R.plan(sources, targets, tagmap)
    assert len(plan) == 3
    assert {p.uri for p in plan} == {f"spotify:track:t{i}" for i in (1, 2, 3)}


def test_plan_records_where_each_track_came_from(tagmap):
    sources = {"ursus": [_track("t1", "a_mantra")],
               "neue": [_track("t2", "a_techno")]}
    targets = {"mantric": R.playlist_vector([_track("m", "a_mantra")], tagmap),
               "electronique": R.playlist_vector([_track("e", "a_techno")], tagmap)}
    by_uri = {p.uri: p for p in R.plan(sources, targets, tagmap)}
    assert by_uri["spotify:track:t1"].source == "ursus"
    assert by_uri["spotify:track:t2"].source == "neue"


def test_plan_never_targets_a_source(tagmap):
    """[Ursus] and [Neue] are being emptied — neither may receive tracks."""
    sources = {"ursus": [_track("t1", "a_mantra")]}
    targets = {"mantric": R.playlist_vector([_track("m", "a_mantra")], tagmap)}
    assert all(p.target != "ursus" for p in R.plan(sources, targets, tagmap))


def test_plan_drops_a_track_already_present_in_its_chosen_target(tagmap):
    """Re-adding a track the target already holds makes a duplicate."""
    dup = _track("t1", "a_mantra")
    targets_tracks = {"mantric": [dup]}
    targets = {"mantric": R.playlist_vector([dup], tagmap)}
    plan = R.plan({"ursus": [dup]}, targets, tagmap, target_tracks=targets_tracks)
    assert plan[0].duplicate is True


def test_grouping_batches_by_target_within_the_add_limit(tagmap):
    class P:
        def __init__(self, t, u):
            self.target, self.uri, self.duplicate = t, u, False
    plan = [P("mantric", f"spotify:track:{i}") for i in range(250)]
    batches = list(R.add_batches(plan, limit=100))
    assert [len(uris) for _, uris in batches] == [100, 100, 50]
    assert all(t == "mantric" for t, _ in batches)


def test_grouping_skips_duplicates(tagmap):
    class P:
        def __init__(self, t, u, d=False):
            self.target, self.uri, self.duplicate = t, u, d
    plan = [P("mantric", "spotify:track:a"), P("mantric", "spotify:track:b", d=True)]
    batches = list(R.add_batches(plan, limit=100))
    assert batches == [("mantric", ["spotify:track:a"])]
