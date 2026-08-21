import json

from sortify.spotify import code_challenge
from sortify.store import Store


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path)
    assert s.config()["client_id"] is None
    s.update_config(client_id="abc", input_ids=["x"])
    s2 = Store(tmp_path)
    assert s2.config()["client_id"] == "abc"
    assert s2.config()["input_ids"] == ["x"]
    # file on disk is plain readable JSON
    raw = json.loads((tmp_path / "config.json").read_text())
    assert raw["client_id"] == "abc"


def test_pkce_challenge_rfc7636_vector():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_queue_and_pacing_default_when_missing(tmp_path):
    s = Store(tmp_path)
    assert s.queue()["state"] == "stopped" and s.queue()["version"] == 1
    assert s.pacing()["rate_per_min"] == 1.8 and s.pacing()["ceiling"] == 7.0


def test_queue_and_pacing_round_trip_and_are_private(tmp_path):
    s = Store(tmp_path)
    q = s.queue(); q.update(playlist_id="PL", pending=["p2"], state="running")
    s.save_queue(q)
    assert s.queue()["pending"] == ["p2"]
    import os, stat
    mode = stat.S_IMODE(os.stat(tmp_path / "queue.json").st_mode)
    assert mode == 0o600  # boxdash reads these; nobody else should


def test_default_queue_pending_list_is_not_shared_across_reads(tmp_path):
    s = Store(tmp_path)
    q1 = s.queue()
    q1["pending"].append("poison")
    q2 = s.queue()  # a fresh default read must not see the first read's mutation
    assert q2["pending"] == []


def test_wrong_version_reads_as_default(tmp_path):
    s = Store(tmp_path)
    (tmp_path / "pacing.json").write_text('{"version": 99, "rate_per_min": 40}')
    assert s.pacing()["rate_per_min"] == 1.8  # a v99 file must not set our pace


def test_lastfm_tracks_default_when_missing(tmp_path):
    s = Store(tmp_path)
    assert s.lastfm_tracks() == {"version": 1, "tracks": {}}
    assert s.lastfm_track_map() == {}


def test_lastfm_tracks_round_trip(tmp_path):
    s = Store(tmp_path)
    record = {"similar": [{"artist": "Nazareth", "track": "Dream On", "match": 0.21}],
              "tags": ["ballad"], "fetched_at": 123.0, "miss": False}
    s.save_lastfm_tracks({"version": 1, "tracks": {"aerosmith\x1fdream on": record}})
    assert s.lastfm_tracks()["tracks"]["aerosmith\x1fdream on"] == record
    assert s.lastfm_track_map()["aerosmith\x1fdream on"] == record


def test_lastfm_tracks_wrong_version_reads_as_default(tmp_path):
    s = Store(tmp_path)
    (tmp_path / "lastfm_tracks.json").write_text('{"version": 99, "tracks": {"x": {}}}')
    assert s.lastfm_tracks() == {"version": 1, "tracks": {}}
    assert s.lastfm_track_map() == {}


def test_lastfm_track_map_survives_a_malformed_file(tmp_path):
    s = Store(tmp_path)
    (tmp_path / "lastfm_tracks.json").write_text('{"version": 1}')
    assert s.lastfm_track_map() == {}


def test_lastfm_tracks_default_map_is_not_shared_across_reads(tmp_path):
    s = Store(tmp_path)
    t1 = s.lastfm_tracks()
    t1["tracks"]["poison"] = {}
    t2 = s.lastfm_tracks()  # a fresh default read must not see the first read's mutation
    assert t2["tracks"] == {}


def test_lastfm_artists_default_to_empty(tmp_path):
    assert Store(tmp_path).lastfm_artists() == {"version": 1, "artists": {}}


def test_lastfm_artists_round_trip_and_map(tmp_path):
    s = Store(tmp_path)
    s.save_lastfm_artists({"version": 1, "artists": {
        "id1": {"name": "Slowdive", "similar": [{"artist": "Ride", "match": 0.9}],
                "fetched_at": 1.0, "miss": False},
    }})
    assert s.lastfm_artist_map()["id1"]["similar"][0]["artist"] == "Ride"


def test_lastfm_artist_map_guards_malformed_payloads(tmp_path):
    s = Store(tmp_path)
    (tmp_path / "lastfm_artists.json").write_text('{"version": 99, "artists": []}')
    assert s.lastfm_artist_map() == {}
