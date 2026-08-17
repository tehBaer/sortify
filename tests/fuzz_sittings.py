"""Randomised concurrency fuzzer for the sitting endpoints.

NOT a pytest module (no `test_` prefix, so it is never collected — it takes
seconds to minutes and is a diagnostic, not a gate). Run it directly:

    .venv/bin/python tests/fuzz_sittings.py --rounds 300
    .venv/bin/python tests/fuzz_sittings.py --rounds 300 --fail-rate 0.15

It makes **zero real Spotify calls**: `sortify.app.sp` is monkeypatched with an
in-process fake that keeps a ledger of which playlists it created and which of
those are still live (followed). Nothing here touches the network, and the data
directory is a throwaway temp dir bound before `sortify.app` is imported, the
same way tests/conftest.py does it.

What it checks, at quiescence (all threads joined, nothing in flight):

  1. **No leak** — every live playlist is reachable from the record: the only
     playlist the fake still has live must be the one `active_sitting`
     currently points at. A live playlist nobody points at is a real playlist
     sitting in the user's Spotify account that sortify can never unfollow.
  2. **No dangling reference** — every recorded playlist id was actually
     created.

`_recover_orphan`'s "lost the race, then unfollow failed, and no free slot to
re-record in" branch logs an error and is a *known* unrecoverable case; leaks
that coincide with such a log are counted separately so they don't hide
timing leaks.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import tempfile
import threading
import time

os.environ.setdefault("SORTIFY_DATA_DIR", tempfile.mkdtemp(prefix="sortify-fuzz-"))
os.environ.setdefault(
    "SPOTIFY_ACCOUNT_LEDGER",
    os.path.join(tempfile.mkdtemp(prefix="sortify-fuzz-ledger-"), "account-ledger.json"),
)

from fastapi.testclient import TestClient  # noqa: E402

import sortify.app as appmod  # noqa: E402
from sortify.spotify import SpotifyError  # noqa: E402
from sortify.store import Store  # noqa: E402

FIVE_MIN = 300000
PLAYLIST = "FUZZPL"


class FakeSpotify:
    """An in-process stand-in for the Spotify mutations a sitting makes.

    `created` is the ground truth the invariants are checked against: it is
    what the user's real account would contain.
    """

    def __init__(self, rng: random.Random, fail_rate: float, latency: float):
        self.rng = rng
        self.fail_rate = fail_rate
        self.latency = latency
        self.lock = threading.Lock()
        self.created: dict[str, bool] = {}   # playlist id -> still live?
        self.n = 0

    def _jitter(self) -> None:
        # Network calls take time; that time is exactly the window every race
        # in this code lives in. Never held under _split_lock — if a future
        # change moved an sp.* call inside the lock, this fuzzer would grind
        # to a halt, which is itself a useful signal.
        if self.latency and self.rng.random() < 0.5:
            time.sleep(self.rng.random() * self.latency)

    def _maybe_fail(self, statuses: tuple[int, ...]) -> None:
        with self.lock:
            hit = self.rng.random() < self.fail_rate
            status = self.rng.choice(statuses)
        if hit:
            raise SpotifyError(status, f"injected {status}")

    def create_playlist(self, name: str, description: str = "") -> str:
        self._jitter()
        # Injected *before* the playlist exists. A failure after creation (a
        # lost response to POST /me/playlists) is the known-unfixable case
        # called out in the report; simulating it would only re-measure it.
        self._maybe_fail((429, 500, 502))
        with self.lock:
            self.n += 1
            pid = f"P{self.n}"
            self.created[pid] = True
        self._jitter()
        return pid

    def add_to_playlist(self, playlist_id: str, uri: str) -> str:
        self._jitter()
        self._maybe_fail((429, 500, 502, 404))
        return "snap"

    def unfollow_playlist(self, playlist_id: str) -> None:
        self._jitter()
        # No injected 404 here: a 404 from unfollow truthfully means "already
        # gone", which the fake reports on its own below when it is true.
        self._maybe_fail((429, 500, 502))
        with self.lock:
            if not self.created.get(playlist_id):
                raise SpotifyError(404, "playlist not found")
            self.created[playlist_id] = False

    def live(self) -> set[str]:
        with self.lock:
            return {pid for pid, alive in self.created.items() if alive}


class OrphanLogCounter(logging.Handler):
    """Collects the playlist ids _recover_orphan gave up on.

    Per-id, not a bare count: excluding a whole round because *one* id hit the
    known branch would hide a genuine timing leak on a different id in the
    same round.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        # NOT self.lock — logging.Handler.handle() already holds that one
        # around emit(), and it is not reentrant, so shadowing it deadlocks
        # the first thread that logs.
        self._ids_lock = threading.Lock()
        self.ids: set[str] = set()

    def emit(self, record: logging.LogRecord) -> None:
        if "orphaned beyond automatic recovery" in str(record.msg):
            with self._ids_lock:
                self.ids.add(str(record.args[0]))  # type: ignore[index]

    def reset(self) -> None:
        with self._ids_lock:
            self.ids = set()


def _seed_state(store: Store) -> None:
    store.save_splits({"version": 1, "splits": {PLAYLIST: {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None,
        "params": {"resolution": 1.0, "min_pile": 15},
        "piles": [
            {"id": "p1", "name": "one", "tags": ["a"],
             "uris": [f"spotify:track:x{i}" for i in range(30)]},
            {"id": "p2", "name": "two", "tags": ["b"],
             "uris": [f"spotify:track:y{i}" for i in range(30)]},
        ],
        "decided": {}, "active_sitting": None}}})
    cache = store.cache()
    cache["playlists"][PLAYLIST] = {"tracks": [
        {"uri": uri, "duration_ms": FIVE_MIN, "artists": [{"id": "a", "name": "A"}]}
        for uri in ([f"spotify:track:x{i}" for i in range(30)]
                    + [f"spotify:track:y{i}" for i in range(30)])]}
    store.save_cache(cache)


def run_round(client: TestClient, rng: random.Random, threads: int, ops: int) -> None:
    def worker(seed: int) -> None:
        r = random.Random(seed)
        for _ in range(ops):
            op = r.choices(["start", "finish", "get"], weights=[4, 4, 2])[0]
            try:
                if op == "start":
                    client.post(f"/api/split/{PLAYLIST}/sitting",
                                json={"pile_id": r.choice(["p1", "p2"]),
                                      "target_minutes": r.choice([15, 30, 45])})
                elif op == "finish":
                    client.post(f"/api/split/{PLAYLIST}/sitting/finish")
                else:
                    client.get(f"/api/split/{PLAYLIST}")
            except Exception:
                # A 502/409 comes back as a response, not an exception; an
                # actual exception escaping the endpoint is a bug in its own
                # right, but the invariant check below is what this fuzzer
                # exists to measure, so keep going and let it speak.
                pass

    ts = [threading.Thread(target=worker, args=(rng.randrange(1 << 30),))
          for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    # Quiescence is the precondition for the invariants: a thread still in
    # flight would make the check meaningless (and corrupt the next round's
    # seeded state), so refuse to report rather than report noise.
    if any(t.is_alive() for t in ts):
        raise SystemExit("a worker thread never finished — the round is not quiescent")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=300)
    ap.add_argument("--threads", type=int, default=0, help="0 = random 4-6 per round")
    ap.add_argument("--ops", type=int, default=6, help="operations per thread")
    ap.add_argument("--fail-rate", type=float, default=0.0)
    ap.add_argument("--latency", type=float, default=0.002)
    ap.add_argument("--seed", type=int, default=20260817)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    store = Store()
    orphan_log = OrphanLogCounter()
    logging.getLogger("uvicorn.error").addHandler(orphan_log)

    leaks = dangling = known_orphan_leaks = 0
    for i in range(args.rounds):
        fake = FakeSpotify(random.Random(rng.randrange(1 << 30)), args.fail_rate, args.latency)
        appmod.sp.create_playlist = fake.create_playlist          # type: ignore[method-assign]
        appmod.sp.add_to_playlist = fake.add_to_playlist          # type: ignore[method-assign]
        appmod.sp.unfollow_playlist = fake.unfollow_playlist      # type: ignore[method-assign]
        _seed_state(store)
        orphan_log.reset()

        client = TestClient(appmod.app)
        threads = args.threads or rng.choice([4, 5, 6])
        run_round(client, rng, threads, args.ops)

        active = store.splits()["splits"][PLAYLIST].get("active_sitting")
        recorded = active.get("playlist_id") if active else None
        live = fake.live()
        unreachable = live - ({recorded} if recorded else set())
        if unreachable & orphan_log.ids:
            known_orphan_leaks += 1
        unreachable -= orphan_log.ids
        if unreachable:
            leaks += 1
            print(f"  round {i}: LEAK {sorted(unreachable)} "
                  f"(recorded={recorded!r}, live={sorted(live)})")
        if recorded is not None and recorded not in fake.created:
            dangling += 1
            print(f"  round {i}: DANGLING recorded={recorded!r} was never created")

    print(f"rounds={args.rounds} threads={args.threads or '4-6'} ops={args.ops} "
          f"fail_rate={args.fail_rate} seed={args.seed}")
    print(f"  timing leaks:                 {leaks}/{args.rounds}")
    print(f"  dangling records:             {dangling}/{args.rounds}")
    print(f"  known unrecoverable orphans:  {known_orphan_leaks}/{args.rounds} "
          f"(_recover_orphan had no free slot; logged)")
    return 1 if (leaks or dangling) else 0


if __name__ == "__main__":
    sys.exit(main())
