"""Store's parsed-JSON read cache.

The cost this kills: tags.json (1.9MB), lastfm_tracks.json (5.8MB) and
lastfm_artists.json (2.8MB) were reparsed on every read — and /api/now/suggest
reads all three per request BY DESIGN (guard-on-read freshness), so every
suggestion computation carried ~180ms of parsing that almost never changed.
The cache keys on the file's (inode, mtime_ns, size), so freshness still
comes from the filesystem, not a TTL: any writer — this process, a sibling
Store instance, a backfill script, a hand edit — lands a new stat identity
and the next read reparses.

Deliberately NOT cached (see Store.CACHED_READS):
- cache.json: its in-process writers (_cache_move, _apply_snapshot, the
  spotify client) mutate the parsed object in place before saving; a shared
  object would let one thread mutate what another is iterating.
- usage.json / the ledger paths: the Spotify budget must never act on a
  stale read (CLAUDE.md's budget section) — and they're tiny anyway.
- config.json, queue.json, pacing.json, splits.json, …: sub-millisecond
  parses; caching buys nothing and widens the sharing contract for free.

The sharing contract for the cached files: callers get THE SAME parsed
object until the file changes, so they must never mutate it — which the
in-process writers already honour (_merge_save_* build new dicts, enrich
copies, suggest/split only build local structures).
"""

import json

import pytest

from sortify.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def write_raw(store, name, text):
    """A writer that is NOT Store._save — a sibling process, a hand edit."""
    (store.dir / name).write_text(text)


def test_a_repeated_read_serves_the_same_parsed_object(store):
    store.save_tag_artists({"a1": {"name": "A", "tags": []}})
    assert store.tags() is store.tags()
    store.save_folders({"p1": {"path": "ROOT / Sub", "caps": False}})
    assert store.folders() is store.folders()
    assert store.lastfm_tracks() is not None  # missing file: exercised below


def test_the_cached_file_whitelist_is_exactly_these(store):
    assert Store.CACHED_READS == frozenset(
        {"tags.json", "lastfm_tracks.json", "lastfm_artists.json", "folders.json"})
    store.save_cache({"playlists": {}, "artists": {}, "me": None, "playlist_list": None})
    store.save_config({"client_id": None})
    # Uncached files keep today's fresh-copy-per-read behaviour: their
    # callers mutate the returned dict in place before saving.
    assert store.cache() is not store.cache()
    assert store.config() is not store.config()


def test_a_save_through_the_same_store_is_visible_immediately(store):
    store.save_tag_artists({"a1": {"name": "A"}})
    assert set(store.tag_artists()) == {"a1"}
    store.save_tag_artists({"a1": {"name": "A"}, "a2": {"name": "B"}})
    assert set(store.tag_artists()) == {"a1", "a2"}


def test_a_foreign_write_is_picked_up_by_stat_identity(store):
    """The classic mtime-cache failure is a same-second overwrite going
    unseen. The key includes inode and mtime_ns; atomic writes replace the
    inode, and even a raw in-place rewrite moves mtime_ns — either way the
    next read must reparse."""
    store.save_lastfm_artists({"version": 1, "artists": {"x": {}}})
    assert set(store.lastfm_artist_map()) == {"x"}
    write_raw(store, "lastfm_artists.json",
              json.dumps({"version": 1, "artists": {"y": {}}}))
    assert set(store.lastfm_artist_map()) == {"y"}


def test_a_sibling_store_instance_sees_the_write(tmp_path):
    """Tests (and scripts) construct fresh Store()s against the same dir all
    the time; a per-instance cache must not let instance A serve a parse from
    before instance B's save."""
    a, b = Store(tmp_path), Store(tmp_path)
    a.save_tag_artists({"a1": {}})
    assert set(a.tag_artists()) == {"a1"}
    b.save_tag_artists({"b1": {}})
    assert set(a.tag_artists()) == {"b1"}


def test_a_missing_file_returns_a_fresh_default_every_time(store):
    """The default must never be cached: callers may mutate the dict they
    got (fill it, then save it), and a shared default would leak one
    caller's fill into another's 'empty' read."""
    first = store.tags()
    second = store.tags()
    assert first == {"version": 2, "artists": {}}
    assert first is not second
    first["artists"]["poison"] = {}
    assert "poison" not in store.tags()["artists"]


def test_a_corrupt_file_raises_and_is_not_cached_as_anything(store):
    """tags.json's bare read raises on truly corrupt JSON today (the
    _versioned envelopes degrade only malformed-but-valid-JSON); the cache
    must preserve that, and a later good write must serve normally."""
    write_raw(store, "tags.json", "{ not json")
    with pytest.raises(json.JSONDecodeError):
        store.tags()
    store.save_tag_artists({"ok": {}})
    assert set(store.tag_artists()) == {"ok"}


def test_a_deleted_file_falls_back_to_the_default(store):
    store.save_tag_artists({"a1": {}})
    assert set(store.tag_artists()) == {"a1"}
    (store.dir / "tags.json").unlink()
    assert store.tag_artists() == {}


def test_the_cache_actually_avoids_reparsing(store, monkeypatch):
    """The point of the exercise: the second read must not call json.load."""
    store.save_tag_artists({"a1": {}})
    store.tags()  # warm
    def boom(*a, **k):
        raise AssertionError("reparsed a file whose stat identity is unchanged")
    monkeypatch.setattr(json, "load", boom)
    assert set(store.tag_artists()) == {"a1"}
