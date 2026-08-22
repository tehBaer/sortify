"""Tests for scripts/probe_deezer_bpm.py — the coverage probe that decides
whether Deezer BPM is worth a full backfill (option-2 gate)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import probe_deezer_bpm as probe  # noqa: E402

from sortify.deezer import DeezerError  # noqa: E402
from sortify.tags import track_key  # noqa: E402


def track(uri, artist, title):
    return {"uri": uri, "name": title, "artists": [{"id": artist.lower(), "name": artist}]}


def test_collect_targets_keys_by_first_artist_and_skips_blank():
    lists = {
        "H": [
            track("u1", "Slowdive", "Alison"),
            {"uri": "u2", "name": "", "artists": [{"name": "X"}]},   # blank title
            {"uri": "u3", "name": "T", "artists": []},                # no artists
        ],
    }
    targets = probe.collect_target_tracks(lists)
    assert list(targets) == [track_key("Slowdive", "Alison")]
    assert targets[track_key("Slowdive", "Alison")] == {
        "artist": "Slowdive", "title": "Alison", "keys": [track_key("Slowdive", "Alison")],
    }


def test_sample_targets_is_seeded_skips_known_and_caps():
    targets = {f"k{i}": {"artist": f"A{i}", "title": "T", "keys": [f"k{i}"]} for i in range(20)}
    known = {"k0": {"bpm": 120.0}, "k1": {"miss": True}}  # both count as known
    a = probe.sample_targets(targets, known, n=5, seed=7)
    b = probe.sample_targets(targets, known, n=5, seed=7)
    assert a == b
    assert len(a) == 5
    assert all(k not in known for k in a)


def test_run_probe_records_hits_and_misses_but_not_errors():
    class FakeDeezer:
        def fetch_track(self, artist, title):
            if artist == "Hit":
                return {"bpm": 128.0, "deezer_id": 1, "fetched_at": 0.0}
            if artist == "Miss":
                return {"miss": True}
            raise DeezerError("quota")

    targets = {
        "kh": {"artist": "Hit", "title": "T", "keys": ["kh"]},
        "km": {"artist": "Miss", "title": "T", "keys": ["km"]},
        "ke": {"artist": "Err", "title": "T", "keys": ["ke"]},
    }
    recorded = {}
    stats = probe.run_probe(
        FakeDeezer(), ["kh", "km", "ke"], targets,
        record=recorded.__setitem__, sleep=lambda s: None,
    )
    assert stats == {"attempted": 3, "hits": 1, "misses": 1, "errors": 1}
    assert recorded["kh"]["bpm"] == 128.0
    assert recorded["km"] == {"miss": True}
    assert "ke" not in recorded  # errors are retryable, never recorded


def test_run_probe_aborts_after_consecutive_failures():
    class DeadDeezer:
        def fetch_track(self, artist, title):
            raise DeezerError("down")

    targets = {f"k{i}": {"artist": "A", "title": "T", "keys": [f"k{i}"]} for i in range(30)}
    stats = probe.run_probe(
        DeadDeezer(), list(targets), targets,
        record=lambda k, v: None, sleep=lambda s: None,
    )
    assert stats["attempted"] == probe.CONSECUTIVE_FAILURE_LIMIT
    assert stats["errors"] == probe.CONSECUTIVE_FAILURE_LIMIT
