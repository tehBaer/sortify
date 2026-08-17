"""Sittings: materialise one listening session from a pile at a time.

data/splits.json and data/cache.json are shared for the whole test session
(see conftest.py), and "PL1" is also used by tests/test_split_api.py — so the
`client` fixture here captures whatever was on disk before it overwrites
those two keys, and restores it after every test regardless of pass/fail or
run order. Without that, a leaked active_sitting on "PL1" would make
create_split/recluster 409 in test_split_api.py whenever that file happens to
run after this one — the exact failure mode CLAUDE.md warns has already
happened twice in this plan.
"""

import pytest
from fastapi.testclient import TestClient

import sortify.app as appmod
from sortify.spotify import SpotifyError
from sortify.split import pick_sitting
from sortify.store import Store

FIVE_MIN = 300000


def test_pick_sitting_fills_to_target_without_exceeding():
    uris = [f"u{i}" for i in range(20)]
    durations = {u: FIVE_MIN for u in uris}
    picked = pick_sitting(uris, durations, {}, target_ms=30 * 60 * 1000)
    assert len(picked) == 6


def test_pick_sitting_skips_decided_tracks():
    uris = [f"u{i}" for i in range(10)]
    durations = {u: FIVE_MIN for u in uris}
    picked = pick_sitting(uris, durations, {"u0": {}, "u1": {}}, target_ms=15 * 60 * 1000)
    assert picked == ["u2", "u3", "u4"]


def test_pick_sitting_preserves_order():
    uris = ["b", "a", "c"]
    durations = {u: FIVE_MIN for u in uris}
    assert pick_sitting(uris, durations, {}, target_ms=15 * 60 * 1000) == ["b", "a", "c"]


def test_pick_sitting_returns_at_least_one_track():
    """A single track longer than the target must still be servable."""
    picked = pick_sitting(["long"], {"long": 3 * 60 * 60 * 1000}, {}, target_ms=60 * 1000)
    assert picked == ["long"]


def test_pick_sitting_empty_when_all_decided():
    assert pick_sitting(["a"], {"a": FIVE_MIN}, {"a": {}}, target_ms=FIVE_MIN) == []


def _sitting_split(extra_piles=None):
    piles = [{"id": "p1", "name": "dream pop", "tags": ["dream pop"],
              "uris": [f"spotify:track:x{i}" for i in range(30)]}]
    if extra_piles:
        piles += extra_piles
    return {"version": 1, "splits": {"PL1": {
        "created_at": "2026-08-17T10:00:00Z", "snapshot_id": None,
        "params": {"resolution": 1.0, "min_pile": 15},
        "piles": piles, "decided": {}, "active_sitting": None}}}


@pytest.fixture
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(appmod.sp, "create_playlist",
                        lambda name, description="": calls.append(("create", name)) or "NEW1")
    monkeypatch.setattr(appmod.sp, "add_to_playlist",
                        lambda pid, uri: calls.append(("add", uri)) or "snap")
    monkeypatch.setattr(appmod.sp, "unfollow_playlist",
                        lambda pid: calls.append(("unfollow", pid)))
    s = Store()
    original_splits = s.splits()
    original_cache = s.cache()
    s.save_splits(_sitting_split())
    cache = s.cache()
    cache["playlists"]["PL1"] = {"tracks": [
        {"uri": f"spotify:track:x{i}", "duration_ms": FIVE_MIN,
         "artists": [{"id": "a", "name": "A"}]} for i in range(30)]}
    s.save_cache(cache)
    c = TestClient(appmod.app)
    c.calls = calls
    try:
        yield c
    finally:
        Store().save_splits(original_splits)
        Store().save_cache(original_cache)


def test_sitting_creates_playlist_and_adds_tracks(client):
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["sitting_id"] == "NEW1"
    assert len(body["uris"]) == 6
    assert client.calls[0][0] == "create"
    assert sum(1 for c in client.calls if c[0] == "add") == 6


def test_sitting_is_recorded_as_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active["playlist_id"] == "NEW1"
    assert active["pile_id"] == "p1"


def test_finish_unfollows_in_one_call(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    client.calls.clear()
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 200
    assert client.calls == [("unfollow", "NEW1")]
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


def test_second_sitting_refused_while_one_is_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 409


def test_sitting_on_exhausted_pile_400s(client):
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["decided"] = {
        f"spotify:track:x{i}": {"action": "reject", "to_id": None, "at": "x"} for i in range(30)}
    s.save_splits(payload)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 400


# ---- design decision 1: a re-split/recluster must not strand an active
# sitting's pile_id under a partition that no longer exists ------------------


def test_recluster_refused_while_sitting_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    r = client.post("/api/split/PL1/recluster", json={"min_pile": 5, "resolution": 1.0})
    assert r.status_code == 409
    # And it must not have spent anything getting there — same "fail for
    # free" contract as the tags-version guard in _tag_artists_checked.
    assert Store().splits()["splits"]["PL1"]["params"] == {"resolution": 1.0, "min_pile": 15}


def test_resplit_refused_while_sitting_active(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    r = client.post("/api/split/PL1")
    assert r.status_code == 409
    # Checked before the Last.fm key, before my_playlists(), before anything
    # that could spend or reach the network — so it fires on a bare split
    # record with no Last.fm client configured at all in this fixture.
    assert Store().splits()["splits"]["PL1"]["piles"][0]["id"] == "p1"


def test_recluster_and_resplit_work_again_once_the_sitting_is_finished(client):
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    client.post("/api/split/PL1/sitting/finish")
    r = client.post("/api/split/PL1/recluster", json={"min_pile": 5, "resolution": 1.0})
    assert r.status_code == 200


# ---- design decision 3: recovering from an orphaned sitting playlist -------


def test_finish_recovers_when_the_sitting_playlist_is_already_gone(client, monkeypatch):
    """The user (or something else) unfollowed the sitting playlist outside
    sortify — maybe from the Spotify app directly. Without this, finish would
    404 forever on the missing playlist, active_sitting could never clear,
    and start_sitting would refuse every future sitting with 409: a
    permanently orphaned record for a playlist that is already gone."""
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})

    def already_gone(pid):
        raise SpotifyError(404, "playlist not found")

    monkeypatch.setattr(appmod.sp, "unfollow_playlist", already_gone)
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 200
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


def test_finish_still_raises_on_a_real_spotify_error(client, monkeypatch):
    """Only 404 (already gone) is swallowed — any other failure must still
    surface, and must not clear active_sitting out from under a playlist
    that may still exist."""
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})

    def rate_limited(pid):
        raise SpotifyError(429, "cooldown")

    monkeypatch.setattr(appmod.sp, "unfollow_playlist", rate_limited)
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 502
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is not None


# ---- Fix Round 1 -------------------------------------------------------


# Critical: active_sitting must be recorded before the add loop, not after,
# so a failure partway through (a 429 landing mid-burst is the likely real
# trigger; a process restart mid-add has the same shape) still leaves a
# findable, finishable record instead of a real Spotify playlist sortify has
# never heard of.


def test_partial_add_failure_leaves_a_recoverable_record(client, monkeypatch):
    seen = []

    def flaky_add(pid, uri):
        seen.append(uri)
        if len(seen) == 4:
            raise SpotifyError(429, "cooldown landed mid-burst")
        return "snap"

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky_add)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 502
    assert len(seen) == 4  # 3 succeeded, the 4th is what failed

    # The record exists and already carries the real playlist id — recorded
    # right after create_playlist returned, before any add was attempted.
    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active is not None
    assert active["playlist_id"] == "NEW1"

    # And it is recoverable with a single finish, exactly like any other
    # active sitting — no special-case cleanup needed.
    client.calls.clear()
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 200
    assert client.calls == [("unfollow", "NEW1")]
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


def test_finish_handles_a_reservation_whose_playlist_was_never_created(client, monkeypatch):
    """create_playlist itself can fail (e.g. a cooldown on the very first
    call of the sitting) after the slot is reserved but before any playlist
    exists at all. finish must clear that reservation without trying to
    unfollow a playlist id that was never assigned."""
    def failing_create(name, description=""):
        raise SpotifyError(429, "cooldown before the first call landed")

    monkeypatch.setattr(appmod.sp, "create_playlist", failing_create)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 502

    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active is not None
    assert active["playlist_id"] is None

    def unfollow_must_not_be_called(pid):
        raise AssertionError("nothing was ever created — finish must not call unfollow")

    monkeypatch.setattr(appmod.sp, "unfollow_playlist", unfollow_must_not_be_called)
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 200
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


# Important 1: create_split/recluster must not silently wipe or race an
# active_sitting that starts during their (Last.fm-enrichment-shaped, or —
# for recluster — merely non-zero) window between the entry guard and the
# final write. Covered indirectly by the entry-guard tests above; the
# atomic recheck at the write itself has no network-timing hook to attach a
# test to without mocking internals, so it is verified by inspection here:
# both create_split and recluster re-read store.splits() and re-check
# active_sitting under _split_lock immediately before writing, matching the
# reviewer's suggested fix. The concurrency test below exercises the same
# lock via the sitting endpoints, where a real race is easy to construct.


# Important 2: an active_sitting request against a playlist with no cached
# tracks must 400, not silently treat every duration as 0.


def test_sitting_400s_with_no_cached_tracks(client):
    s = Store()
    cache = s.cache()
    cache["playlists"].pop("PL1", None)
    s.save_cache(cache)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 400


# Important 3: target_minutes has an upper bound (422), and the track count
# is hard-capped regardless of computed target — chosen values and rationale
# are in app.py next to SITTING_MAX_MINUTES / SITTING_MAX_TRACKS.


def test_target_minutes_above_the_cap_is_rejected(client):
    r = client.post(
        "/api/split/PL1/sitting",
        json={"pile_id": "p1", "target_minutes": appmod.SITTING_MAX_MINUTES + 1},
    )
    assert r.status_code == 422


def test_the_sitting_ceilings_are_pinned_to_their_literals():
    """These two numbers ARE the structural bound on one sitting's burst:
    1 create + SITTING_MAX_TRACKS adds + 1 finish = 42 calls, worst case,
    whatever the caller asks for and whatever the cached durations look like.

    Pinned to literals rather than only compared against themselves. The
    truncation test below asserts `len(uris) == SITTING_MAX_TRACKS` against a
    pile of `SITTING_MAX_TRACKS + 10`, which stays true for any value of the
    constant — raising it 40 -> 400 leaves that test green while turning one
    sitting into a 400-call burst against a 12-per-60s window cap. Same
    treatment BACKGROUND_DAILY_CAP == 40 gets in test_no_proactive_work.py,
    and for the same reason: CLAUDE.md sets these numbers from three
    multi-hour lockouts, so moving one has to be a deliberate act with
    evidence behind it rather than something a refactor can do quietly."""
    assert appmod.SITTING_MAX_TRACKS == 40
    assert appmod.SITTING_MAX_MINUTES == 360


def test_sitting_hard_caps_track_count_even_when_durations_never_trip_the_target(client):
    """A pile whose tracks have no duration data at all (e.g. a partially
    populated cache) would otherwise never trip pick_sitting's running-total
    check — every remaining track would come back as "the sitting". The
    ceiling here is what keeps that from becoming a 300+ call burst."""
    s = Store()
    payload = s.splits()
    payload["splits"]["PL1"]["piles"].append({
        "id": "p2", "name": "no duration data", "tags": [],
        "uris": [f"spotify:track:z{i}" for i in range(appmod.SITTING_MAX_TRACKS + 10)],
    })
    s.save_splits(payload)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p2", "target_minutes": 30})
    assert r.status_code == 200
    assert len(r.json()["uris"]) == appmod.SITTING_MAX_TRACKS

    # What the truncation is for in the first place: the Spotify spend. The
    # ceiling here is a literal (1 create + at most 40 adds), so unlike the
    # count above it does not follow SITTING_MAX_TRACKS upward.
    assert sum(1 for c in client.calls if c[0] == "add") == len(r.json()["uris"])
    assert len(client.calls) <= 41


# Important 4: two concurrent start_sitting calls (two tabs, a double-click)
# must not both win — exactly one playlist gets created regardless of how
# the threads interleave, because the check-and-reserve is atomic under
# _split_lock.


def test_concurrent_sitting_starts_yield_exactly_one_winner(client):
    import threading

    results = []

    def worker():
        r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert sorted(results) == [200, 409]
    assert sum(1 for c in client.calls if c[0] == "create") == 1


# ---- Fix Round 2 -------------------------------------------------------
#
# New Important, found while re-reviewing the Critical fix above: the
# reservation is now written in three separate lock-protected steps spread
# across the create_playlist call and the whole add loop, and a concurrent
# `finish` on the SAME sitting is legal at any point in that span — it just
# clears active_sitting to None. Before fix round 1, nothing was recorded
# until the very end, so a concurrent finish during start_sitting just
# 404'd (nothing to finish yet) and start completed cleanly; the
# recording-before-adds fix reopened a window for exactly the kind of
# unfindable litter it was meant to close, just shaped differently — a
# concurrent finish, not a mid-add failure.
#
# _claim_reservation/_reservation_alive close it: started_at doubles as a
# claim token, checked at each write and before each add, so a vanished (or
# superseded) reservation is detected and the just-created playlist is
# unfollowed before start_sitting reports a clean 409 instead of crashing
# on a None.


def test_finish_racing_the_in_flight_create_playlist_leaves_no_litter(client, monkeypatch):
    """A concurrent finish clears active_sitting while create_playlist is
    still in flight for this call — simulated here as a side effect inside
    the fake create_playlist itself, standing in for a second request that
    would otherwise interleave at exactly that point."""
    def create_then_finish_races_in(name, description=""):
        s = Store()
        payload = s.splits()
        payload["splits"]["PL1"]["active_sitting"] = None
        s.save_splits(payload)
        return "NEW1"

    monkeypatch.setattr(appmod.sp, "create_playlist", create_then_finish_races_in)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 409

    # The playlist that DID get created is unfollowed rather than left
    # behind, and no stray record survives for it.
    assert ("unfollow", "NEW1") in client.calls
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


def test_finish_racing_the_add_loop_stops_early_and_unfollows(client, monkeypatch):
    """A concurrent finish clears active_sitting partway through the add
    loop. The remaining adds must not run — burning ~24 calls into a
    playlist nobody can reach anymore — and the playlist that was already
    created must be unfollowed before the 409 goes out.

    Fix round 3 asserted the opposite here (no unfollow), on the premise that
    a reservation carrying a playlist_id can only be cleared by a finish that
    already unfollowed that id. finish_sitting is not atomic, so that premise
    is false: it reads the reservation under one acquisition of _split_lock
    and clears it under a later one, and a finish that read it while
    playlist_id was still None spends no unfollow call at all. That is the
    interleaving simulated here — the reservation vanishes mid-add with the
    playlist still live — and the only thing standing between it and a real
    playlist stranded in the user's account is this unfollow. Being wrong the
    other way costs one call that comes back 404 in a rare race."""
    added = []

    def flaky_add(pid, uri):
        added.append(uri)
        if len(added) == 3:
            s = Store()
            payload = s.splits()
            payload["splits"]["PL1"]["active_sitting"] = None
            s.save_splits(payload)
        return "snap"

    monkeypatch.setattr(appmod.sp, "add_to_playlist", flaky_add)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 409

    # 6 tracks total for this pile/target; the loop must have stopped well
    # short of all of them once the reservation vanished after the 3rd.
    assert len(added) < 6
    assert ("unfollow", "NEW1") in client.calls
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


def test_the_add_loop_stops_when_a_DIFFERENT_sitting_owns_the_slot(client, monkeypatch):
    """The other half of the mid-add check, and the half nothing pinned: the
    question is not "is some sitting active" but "is MY reservation still the
    one on disk".

    The interleaving: a finish clears this sitting's slot while its add loop
    is still running, and a fresh start_sitting immediately claims the empty
    slot — entirely legal, and it takes no more than a double-click to
    produce. A check that only asked whether *a* sitting exists would see the
    newcomer's record, conclude everything is fine, and keep adding to a
    playlist nothing points at any more: up to SITTING_MAX_TRACKS Spotify
    calls into an orphan, at WINDOW_CAP pacing, while the user waits. The
    claim token is what tells the two reservations apart, exactly as it does
    in `_claim_reservation`.

    The existing race tests all clear the slot to None, which `bool(active)`
    alone already catches — this one leaves a live reservation behind, so it
    fails if and only if the token is actually compared."""
    added = []

    def add_then_a_new_sitting_claims_the_slot(pid, uri):
        added.append(uri)
        if len(added) == 2:
            s = Store()
            payload = s.splits()
            payload["splits"]["PL1"]["active_sitting"] = {
                "playlist_id": "NEW2", "pile_id": "p1", "uris": [],
                "started_at": "2026-08-17T12:00:00Z", "claim": "a-different-claim"}
            s.save_splits(payload)
        return "snap"

    monkeypatch.setattr(appmod.sp, "add_to_playlist", add_then_a_new_sitting_claims_the_slot)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})

    assert r.status_code == 409
    # 6 tracks were picked for this sitting; the loop must have stopped at the
    # first checkpoint after the slot changed hands, not run to the end.
    assert len(added) == 2
    assert ("unfollow", "NEW1") in client.calls  # its own playlist cleaned up

    # And the loser must leave the winner's record exactly as it found it.
    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active["playlist_id"] == "NEW2"
    assert active["claim"] == "a-different-claim"
    assert active["uris"] == []


def test_no_spotify_call_is_made_while_the_split_lock_is_held(client, monkeypatch):
    """`_split_lock` guards short local read-modify-writes of splits.json and
    nothing else. Holding it across a network call would serialise every
    decide, start, finish and recluster in the app behind one Spotify request
    — and a 429 cooldown inside that span parks the lot for as long as the
    retry takes.

    Worth an explicit test because the failure is invisible from the outside:
    the mutation that motivated it (holding `_split_lock` across
    `sp.create_playlist`) deadlocks rather than fails, so the suite hangs and
    reports nothing at all. `_split_lock` is a plain, non-reentrant Lock, so
    a same-thread `acquire(blocking=False)` from inside the fake network call
    answers the question directly — no timeout, no second thread, no hang.
    """
    observed = {"create": None, "adds": []}

    def create_checking_the_lock(name, description=""):
        observed["create"] = appmod._split_lock.acquire(blocking=False)
        if observed["create"]:
            appmod._split_lock.release()
        client.calls.append(("create", name))
        return "NEW1"

    def add_checking_the_lock(pid, uri):
        got = appmod._split_lock.acquire(blocking=False)
        observed["adds"].append(got)
        if got:
            appmod._split_lock.release()
        return "snap"

    def unfollow_checking_the_lock(pid):
        observed["unfollow"] = appmod._split_lock.acquire(blocking=False)
        if observed["unfollow"]:
            appmod._split_lock.release()

    monkeypatch.setattr(appmod.sp, "create_playlist", create_checking_the_lock)
    monkeypatch.setattr(appmod.sp, "add_to_playlist", add_checking_the_lock)
    monkeypatch.setattr(appmod.sp, "unfollow_playlist", unfollow_checking_the_lock)

    assert client.post(
        "/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30}
    ).status_code == 200
    assert client.post("/api/split/PL1/sitting/finish").status_code == 200

    assert observed["create"] is True, "create_playlist ran with _split_lock held"
    assert observed["adds"] and all(observed["adds"]), "an add ran with _split_lock held"
    assert observed["unfollow"] is True, "unfollow_playlist ran with _split_lock held"


def test_uninterrupted_sitting_still_completes_normally(client):
    """The normal, non-racing path: unchanged behaviour after adding the
    claim-token checks — same assertions as
    test_sitting_creates_playlist_and_adds_tracks, kept here as an explicit
    "nothing regressed" companion to the two race tests above."""
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["sitting_id"] == "NEW1"
    assert len(body["uris"]) == 6
    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active == {"playlist_id": "NEW1", "pile_id": "p1", "uris": body["uris"],
                      "started_at": active["started_at"], "claim": active["claim"]}


# ---- Fix Round 3 -------------------------------------------------------
#
# New Important, found by re-review of round 2: the claim token was
# `_now_iso()`, whole-second resolution. Two reservations for the SAME
# playlist minted within one wall-clock second (a zero-cost finish — no
# unfollow call when playlist_id is still None — immediately followed by a
# fresh start) compared equal, so a losing reservation's post-network write
# could land on a winning one's record instead of being refused: exactly the
# unfindable-litter outcome the round-1/round-2 fixes exist to close, coming
# back through the one gap a second-resolution token leaves open. Fixed by
# minting `claim` as a uuid4 instead of reusing the human-readable
# `started_at` timestamp for identity.


def test_reservations_in_the_same_wall_clock_second_do_not_collide(client, monkeypatch):
    """Forces exactly the scenario a whole-second token cannot distinguish:
    A reserves, its create_playlist is in flight, a finish clears A's
    reservation (free — playlist_id is still None, so finish doesn't even
    call unfollow), and B starts and fully completes a fresh sitting — all
    inside one _now_iso() tick. Freezes _now_iso so the two reservations'
    `started_at` really would be byte-identical if that were still the
    claim; only `claim` (a uuid4, unaffected by freezing the clock) can
    tell them apart.

    B's own start_sitting call re-enters this same fake create_playlist
    (it's the only one installed), so the fake distinguishes A's call
    (the first, outer one, which triggers the race) from B's (the second,
    nested one, which must behave like an ordinary create_playlist so B's
    sitting completes normally) with a simple counter.
    """
    monkeypatch.setattr(appmod, "_now_iso", lambda: "2026-08-17T12:00:00Z")
    calls_made = []

    def create_then_race(name, description=""):
        calls_made.append(name)
        if len(calls_made) > 1:
            return "NEW-B"  # B's own (nested) create_playlist call
        # A's call: while it's "in flight", simulate a concurrent finish
        # (free — A's playlist_id is still None, so finish doesn't even
        # call unfollow) followed by a concurrent fresh start_sitting, all
        # inside the same _now_iso() second.
        s = Store()
        payload = s.splits()
        payload["splits"]["PL1"]["active_sitting"] = None
        s.save_splits(payload)
        inner = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
        assert inner.status_code == 200  # B must complete cleanly
        return "NEW-A"

    monkeypatch.setattr(appmod.sp, "create_playlist", create_then_race)
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})

    # A must lose cleanly (409, its own playlist unfollowed) rather than
    # silently overwrite B's now-active, genuinely-in-progress reservation.
    assert r.status_code == 409
    assert ("unfollow", "NEW-A") in client.calls

    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active is not None
    assert active["playlist_id"] == "NEW-B"  # B's record survives untouched
    assert "NEW-A" not in str(active)  # A never landed in the record at all


# Residual flagged alongside the token fix: _abandon_orphaned_playlist's own
# unfollow call can itself fail (429, 5xx — anything but "already gone").
# Previously that just propagated as a 502 and dropped the playlist with no
# record anywhere. _recover_orphan now re-stamps a fresh reservation for it
# instead, so a later finish can still find and clean it up.


def test_abandon_unfollow_failure_restamps_a_findable_reservation(client, monkeypatch):
    def create_then_lose_race(name, description=""):
        s = Store()
        payload = s.splits()
        payload["splits"]["PL1"]["active_sitting"] = None  # the "finish"
        s.save_splits(payload)
        return "NEW1"

    def failing_unfollow(pid):
        raise SpotifyError(500, "upstream hiccup")

    monkeypatch.setattr(appmod.sp, "create_playlist", create_then_lose_race)
    monkeypatch.setattr(appmod.sp, "unfollow_playlist", failing_unfollow)

    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 502  # the caller's own attempt still failed

    # But the playlist was not simply dropped: a fresh reservation exists
    # for it, findable by a later finish.
    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active is not None
    assert active["playlist_id"] == "NEW1"

    # And finish can actually clean it up once unfollow works again.
    monkeypatch.setattr(appmod.sp, "unfollow_playlist",
                        lambda pid: client.calls.append(("unfollow", pid)))
    client.calls.clear()
    r2 = client.post("/api/split/PL1/sitting/finish")
    assert r2.status_code == 200
    assert client.calls == [("unfollow", "NEW1")]
    assert Store().splits()["splits"]["PL1"]["active_sitting"] is None


# ---- Fix Round 4 -------------------------------------------------------
#
# finish_sitting was the one writer of active_sitting that did not tie its
# write to the state it had observed: it read the reservation under one
# acquisition of _split_lock and then cleared the slot under a later one,
# unconditionally, with a network call in between. Anything that changed the
# slot in that window was silently destroyed — including a fully materialised
# sitting whose playlist was live and playing, and _recover_orphan's
# re-stamped reservation. The clear is now a compare-and-swap on the
# (claim, playlist_id) pair the call actually saw.


def _incrementing_creates(client, monkeypatch):
    """create_playlist returning a distinct id per call, so two sittings in
    one test are actually distinguishable (the fixture's fake always returns
    "NEW1")."""
    made = []

    def create(name, description=""):
        made.append(name)
        pid = f"P{len(made)}"
        client.calls.append(("create", pid))
        return pid

    monkeypatch.setattr(appmod.sp, "create_playlist", create)
    return made


def test_a_slow_finish_does_not_clear_a_reservation_it_never_saw(client, monkeypatch):
    """The single-threaded twin of the double-click race below: while THIS
    finish's unfollow is in flight, the reservation it read is replaced by a
    different one (a second finish cleared it, then a new sitting claimed the
    slot). Clearing at that point would wipe a live sitting's only record."""
    _incrementing_creates(client, monkeypatch)
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})

    def unfollow_then_someone_else_starts(pid):
        client.calls.append(("unfollow", pid))
        s = Store()
        payload = s.splits()
        payload["splits"]["PL1"]["active_sitting"] = {
            "playlist_id": "P2", "pile_id": "p1", "uris": [],
            "started_at": "2026-08-17T12:00:00Z", "claim": "a-different-claim"}
        s.save_splits(payload)

    monkeypatch.setattr(appmod.sp, "unfollow_playlist", unfollow_then_someone_else_starts)
    r = client.post("/api/split/PL1/sitting/finish")
    assert r.status_code == 200
    assert r.json()["cleared"] is False

    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active is not None and active["playlist_id"] == "P2"


def test_double_clicked_finish_does_not_wipe_the_next_sitting(client, monkeypatch):
    """Two real threads, the worst case no earlier round closed: finish #1
    reads sitting A and its unfollow goes out; finish #2 completes and clears;
    sitting B then starts, creates a playlist, adds every track and returns
    200; finish #1's unfollow finally lands (404, tolerated) and it clears the
    slot. Every request returned 200 and B's playlist was live, fully
    populated, being listened to — with nothing in splits.json pointing at
    it."""
    import threading

    _incrementing_creates(client, monkeypatch)
    lock = threading.Lock()
    entered, release = threading.Event(), threading.Event()
    seen = []

    def blocking_unfollow(pid):
        with lock:
            seen.append(pid)
            first = len(seen) == 1
        client.calls.append(("unfollow", pid))
        if first:
            entered.set()
            assert release.wait(timeout=10)
        else:
            raise SpotifyError(404, "already gone")

    monkeypatch.setattr(appmod.sp, "unfollow_playlist", blocking_unfollow)
    client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})

    slow = {}

    def slow_finish():
        slow["r"] = client.post("/api/split/PL1/sitting/finish")

    t = threading.Thread(target=slow_finish)
    t.start()
    assert entered.wait(timeout=10)  # finish #1 is now inside its unfollow

    assert client.post("/api/split/PL1/sitting/finish").status_code == 200
    started = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert started.status_code == 200
    release.set()
    t.join(timeout=10)

    assert slow["r"].status_code == 200
    assert slow["r"].json()["cleared"] is False
    active = Store().splits()["splits"]["PL1"]["active_sitting"]
    assert active is not None
    assert active["playlist_id"] == started.json()["sitting_id"]


def test_finish_reports_whether_it_cleared(client):
    r = client.post("/api/split/PL1/sitting", json={"pile_id": "p1", "target_minutes": 30})
    assert r.status_code == 200
    assert client.post("/api/split/PL1/sitting/finish").json() == {"ok": True, "cleared": True}
