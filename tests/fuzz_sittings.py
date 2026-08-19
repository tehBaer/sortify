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

  1. **Nothing stranded** — after the user does the only things they can do
     (finish, refresh, press Remove), the fake account holds NO live
     playlist. This is the invariant that matters, and the one the
     record-authoritative design could not satisfy: it measures the account,
     not `splits.json`, because the record is exactly the authority that
     cannot be trusted.
  2. **No dangling reference** — every recorded playlist id was actually
     created.
  3. *Reported, not gated:* how often a live playlist was not reachable from
     the record at quiescence. Under the old design that was the definition
     of a leak. Under reconciliation it is an ordinary intermediate state —
     a crash or a lost create response produces one, and cleanup resolves it
     — so it is printed for information and no longer fails the run.

Three failure shapes are injectable, matching the three leak classes Ruling
R17 accepted as unfixable per-slot:

  --fail-rate         429/500/502 on any call (class (c) and general races)
  --lost-create-rate  class (a): Spotify creates the playlist, the response
                      never arrives
  --crash-rate        class (b): create returns, the process dies before the
                      record is persisted
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import threading
import time

# Assigned, never setdefault. This file is not pytest-collected (no `test_`
# prefix), so tests/conftest.py never runs for it and these two lines are the
# only thing standing between the fuzzer and the live data directory. With
# SORTIFY_DATA_DIR already exported — which it is in any shell used to run the
# real app — setdefault would honour it, and `_seed_state` would overwrite the
# user's actual splits.json and cache.json: the real split, and every keep/
# reject decision recorded in it, gone. A throwaway directory is the only
# correct value here regardless of the environment, so it is not negotiable.
os.environ["SORTIFY_DATA_DIR"] = tempfile.mkdtemp(prefix="sortify-fuzz-")
os.environ["SPOTIFY_ACCOUNT_LEDGER"] = os.path.join(
    tempfile.mkdtemp(prefix="sortify-fuzz-ledger-"), "account-ledger.json"
)

from fastapi.testclient import TestClient  # noqa: E402

import sortify.app as appmod  # noqa: E402
from sortify.spotify import SpotifyError  # noqa: E402
from sortify.store import Store  # noqa: E402

FIVE_MIN = 300000
PLAYLIST = "FUZZPL"
ME = "fuzzuser"


class FakeSpotify:
    """An in-process stand-in for the Spotify mutations a sitting makes.

    `created` is the ground truth the invariants are checked against: it is
    what the user's real account would contain.
    """

    def __init__(self, rng: random.Random, fail_rate: float, latency: float,
                 lost_create_rate: float = 0.0):
        self.rng = rng
        self.fail_rate = fail_rate
        self.latency = latency
        self.lost_create_rate = lost_create_rate
        self.lock = threading.Lock()
        self.created: dict[str, bool] = {}   # playlist id -> still live?
        self.names: dict[str, tuple[str, str]] = {}   # id -> (name, description)
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

    def create_playlist(self, name: str, description: str = "", **kw) -> str:
        self._jitter()
        # Two failure shapes, and the difference between them is the whole
        # point of this fuzzer's second edition:
        #
        #   before creation — the request never landed. Nothing to clean up.
        #   AFTER creation  — Spotify made the playlist and the caller never
        #                     learned its id. This is leak class (a), and the
        #                     first edition deliberately did NOT simulate it
        #                     because no record-authoritative design could
        #                     survive it. Reconciliation can, so it is
        #                     measured now.
        self._maybe_fail((429, 500, 502))
        with self.lock:
            self.n += 1
            pid = f"P{self.n}"
            self.created[pid] = True
            self.names[pid] = (name, description)
            lost = self.rng.random() < self.lost_create_rate
        self._jitter()
        if lost:
            raise SpotifyError(502, "injected lost create response")
        return pid

    def fetch_listing(self) -> list[dict]:
        """Stands in for Spotify._fetch_my_playlists — the account as it
        really is. Patched at that seam rather than over my_playlists() so
        the genuine cache-read path (and its explicit-refresh-only rule)
        stays under test instead of being replaced by the fake."""
        self._jitter()
        with self.lock:
            return [
                {"id": pid, "name": self.names[pid][0], "owner": ME,
                 "editable": True, "total": 0, "snapshot_id": "s",
                 "image": None, "description": self.names[pid][1]}
                for pid, alive in self.created.items() if alive
            ]

    def add_to_playlist(self, playlist_id: str, uri: str, **kw) -> str:
        self._jitter()
        self._maybe_fail((429, 500, 502, 404))
        return "snap"

    def unfollow_playlist(self, playlist_id: str, **kw) -> None:
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


def _crashing_claim(real, rate: float, rng: random.Random, lock: threading.Lock):
    """Wraps app._claim_reservation to model leak class (b).

    (a) is "Spotify made it, the caller never learned the id"; (b) is "the
    caller learned the id and died before persisting it". Injected here rather
    than in the fake because that is exactly where the gap is: between
    create_playlist returning and the reservation write landing. Refusing to
    call through means the id is known to the account and to nothing else —
    a real playlist with no record anywhere, which is what a process restart
    mid-start leaves behind.
    """
    def claim(split_playlist_id: str, claim_token: str, **fields):
        if "playlist_id" in fields:
            with lock:
                crash = rng.random() < rate
            if crash:
                raise RuntimeError("injected crash before the record was persisted")
        return real(split_playlist_id, claim_token, **fields)
    return claim


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
    cache["me"] = {"id": ME}
    cache["playlist_list"] = {"fetched_at": 0, "items": []}
    cache["playlists"][PLAYLIST] = {"tracks": [
        {"uri": uri, "duration_ms": FIVE_MIN, "artists": [{"id": "a", "name": "A"}]}
        for uri in ([f"spotify:track:x{i}" for i in range(30)]
                    + [f"spotify:track:y{i}" for i in range(30)])]}
    store.save_cache(cache)


def drain(client: TestClient) -> None:
    """What the user can actually do: finish the sitting, refresh the
    listing, press Remove — until the account is clean.

    Two exit conditions, and needing both is the point. "No orphans left" is
    not enough on its own: a sitting whose unfollow hit a 429 keeps its
    record, and a recorded sitting is deliberately NOT an orphan — sweeping
    one would delete a playlist somebody may still be listening to. It is
    finish's job, and finish is retryable. So the account is clean only when
    nothing is recorded AND nothing is stray.

    Bounded at 20 rounds so a genuinely stuck state fails the round loudly
    instead of hanging it. A 404 from cleanup means the endpoint does not
    exist — the state the "before" measurement runs in.
    """
    for _ in range(20):
        client.post(f"/api/split/{PLAYLIST}/sitting/finish")
        appmod.sp.my_playlists(refresh=True)
        if client.post("/api/sittings/cleanup").status_code == 404:
            return                      # pre-reconciliation code
        recorded = client.get(f"/api/split/{PLAYLIST}").json().get("active_sitting")
        if not recorded and not appmod._find_sitting_orphans():
            return


def run_round(client: TestClient, rng: random.Random, threads: int, ops: int) -> None:
    def worker(seed: int) -> None:
        r = random.Random(seed)
        for _ in range(ops):
            op = r.choices(["start", "finish", "get", "refresh", "cleanup"],
                           weights=[4, 4, 2, 1, 1])[0]
            try:
                if op == "start":
                    client.post(f"/api/split/{PLAYLIST}/sitting",
                                json={"pile_id": r.choice(["p1", "p2"]),
                                      "target_minutes": r.choice([15, 30, 45])})
                elif op == "finish":
                    client.post(f"/api/split/{PLAYLIST}/sitting/finish")
                elif op == "refresh":
                    # The listing is only ever re-read on an explicit user
                    # action, so a sweep racing a start is only possible once
                    # a refresh has actually happened. Doing it mid-round is
                    # what makes that race reachable at all.
                    appmod.sp.my_playlists(refresh=True)
                elif op == "cleanup":
                    client.post("/api/sittings/cleanup")
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
    ap.add_argument("--lost-create-rate", type=float, default=0.0,
                    help="leak class (a): Spotify creates the playlist, the response is lost")
    ap.add_argument("--crash-rate", type=float, default=0.0,
                    help="leak class (b): create returns, the process dies before the record saves")
    ap.add_argument("--latency", type=float, default=0.002)
    ap.add_argument("--seed", type=int, default=20260817)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    store = Store()

    real_claim = appmod._claim_reservation
    inject_lock = threading.Lock()
    unrecorded = dangling = unreconciled = 0
    for i in range(args.rounds):
        fake = FakeSpotify(random.Random(rng.randrange(1 << 30)), args.fail_rate,
                           args.latency, args.lost_create_rate)
        appmod.sp.create_playlist = fake.create_playlist          # type: ignore[method-assign]
        appmod.sp.add_to_playlist = fake.add_to_playlist          # type: ignore[method-assign]
        appmod.sp.unfollow_playlist = fake.unfollow_playlist      # type: ignore[method-assign]
        appmod.sp._fetch_my_playlists = fake.fetch_listing        # type: ignore[method-assign]
        appmod._claim_reservation = _crashing_claim(              # type: ignore[assignment]
            real_claim, args.crash_rate, random.Random(rng.randrange(1 << 30)), inject_lock)
        _seed_state(store)

        client = TestClient(appmod.app)
        threads = args.threads or rng.choice([4, 5, 6])
        run_round(client, rng, threads, args.ops)

        active = store.splits()["splits"][PLAYLIST].get("active_sitting")
        recorded = active.get("playlist_id") if active else None
        live = fake.live()
        unreachable = live - ({recorded} if recorded else set())
        if unreachable:
            unrecorded += 1
        if recorded is not None and recorded not in fake.created:
            dangling += 1
            print(f"  round {i}: DANGLING recorded={recorded!r} was never created")

        # The invariant this edition exists for. Everything above measures
        # leaks against the RECORD, which is precisely the authority that
        # cannot be trusted; this measures against the ACCOUNT, after the
        # user has done the only things they can do. A playlist still live
        # here is one they would have to find and delete by hand.
        drain(client)
        stranded = fake.live()
        if stranded:
            unreconciled += 1
            print(f"  round {i}: UNRECONCILED {sorted(stranded)} still in the account")

    appmod._claim_reservation = real_claim                       # type: ignore[assignment]
    print(f"rounds={args.rounds} threads={args.threads or '4-6'} ops={args.ops} "
          f"fail_rate={args.fail_rate} lost_create={args.lost_create_rate} "
          f"crash={args.crash_rate} seed={args.seed}")
    print(f"  unrecorded at quiescence:     {unrecorded}/{args.rounds} "
          f"(pending reconciliation, NOT a leak)")
    print(f"  dangling records:             {dangling}/{args.rounds}")
    print(f"  UNRECONCILED after cleanup:   {unreconciled}/{args.rounds} "
          f"(live playlists the user must delete by hand)")
    # `unrecorded` is reported, not gated: a playlist the record cannot name
    # is now an expected intermediate state that cleanup resolves. The gate
    # is whether the ACCOUNT ends clean.
    return 1 if (dangling or unreconciled) else 0


if __name__ == "__main__":
    sys.exit(main())
