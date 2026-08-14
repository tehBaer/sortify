# sortify — rules for Claude

## Spotify API budget (non-negotiable)

Three multi-hour API lockouts (18h45m and ~24h Aug 2026; ~23h on 2026-08-13) were
caused by our own traffic — probes, warmups, refresh storms, and a background job —
not by real usage. Dev-mode quota is a small, undocumented daily allowance with
escalating penalties. Therefore:

- **Never call api.spotify.com directly** (no curl, no httpx scripts, no raw tokens).
  Every manual call goes through `.venv/bin/spx GET <path>` — it shares the app's
  ledger (`data/usage.json`), throttle, and cooldown guard, and refuses beyond
  **150 dev calls/day**. The remaining budget belongs to the user's real usage.
- **Budget layers** (`sortify/spotify.py`): `DAILY_CAP` 600/day and `WINDOW_CAP`
  12/60s across all sources; `BACKGROUND_DAILY_CAP` 40/day for anything proactive.
  These are set from observed damage, not from the docs — raising one needs
  evidence, not optimism. The 2026-08-13 ban came from 678 calls in ~70 minutes.
- **Polling pace is the server's to decide.** `/api/now` caches for exactly the
  playing track's remaining runtime and returns `poll_after_ms`; the client just
  obeys it. Never give the frontend its own interval — a 6s poll against a 5s
  cache meant every poll missed, ~600 calls/hour from one open tab. Explicit
  user action (opening the view, refocusing the tab) sends `?force=1`, which
  skips the TTL but not `NOW_FORCE_MIN_INTERVAL`.
- **Check `.venv/bin/spx budget` before and after any Spotify-touching work** and
  state the numbers to the user.
- **No bulk operations on your own initiative** (warmups, mass fetches, full
  rebuilds). Estimate the call cost, ask, and wait for an explicit go.
- **After a rate-limit cooldown: zero proactive calls.** The user's own usage is
  the first traffic. The app enforces this too — `QUIET_AFTER_COOLDOWN` keeps
  background work silent for 6h past a cooldown's end, because resuming the
  instant one lifted is what earned the 2026-08-13 ban 70 minutes later.
- **Verify locally first**: `data/cache.json`, server logs, the test suite. A live
  probe is last-resort, single-shot, never in a loop.
- **Prior art**: `~/kode/spotify-autoqueuer/src/spotify/` (token bucket, 429
  circuit breaker + persisted FileBudgetGuard ledger, paced playlist walks) is
  the house pattern library for Spotify API handling — read it before changing
  this project's client. Same discipline rules in its CLAUDE.md.

## Run

- Server: systemd user unit `sortify.service` (`systemctl --user restart sortify`),
  serves 0.0.0.0:8800. Do not run ad-hoc `sortify` processes alongside it.
- Tests: `.venv/bin/pytest -q` — keep them green; they cost zero API calls.

## Conventions (encoded in data/config.json — don't re-derive)

- Inputs: bracketed `[Name]` playlists (`input_name_pattern`).
- Homes: playlists under the `ROOT` folder tree minus ARCHIVED/OLD segments,
  emoji-prefixed names (🐾/🧸 derived super/subsets), and `__x__`/`{x}`/`<x>`
  marker names.
- The client speaks the Feb-2026 dev-mode API (`items`/`item`, `/me/library`,
  no batch endpoints) — do not "fix" it back to pre-2026 shapes.
