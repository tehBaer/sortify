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
- **Budget layers** (`sortify/spotify.py`): `DAILY_CAP` 1000/day and `WINDOW_CAP`
  12/60s across all sources; `BACKGROUND_DAILY_CAP` 40/day for anything proactive.
  These are set from observed damage, not from the docs — raising one needs
  evidence, not optimism. The 2026-08-13 ban came from 678 calls in ~70 minutes.
  (`DAILY_CAP` was raised 600 → 1000 on 2026-08-20 on the strength of
  autoqueuer's clean 1208-call day — the rate protections are unchanged.)
  The queued materialiser adds two more: `BULK_RESERVE` 150 means it never
  spends the day's last 150 calls and sleeps to local midnight instead — unless
  the run was enqueued with `spend_reserve: true` (per-run checkbox/API flag,
  recorded in queue.json), which moves that line to `DAILY_CAP` itself; its
  governor holds a 7.0/min ceiling and only escalates after 15 clean minutes.
  `data/pacing.json` carries the measured `max_clean_rate` — the number that
  replaces the guess band once the rate has actually been observed clean.
- **It is the rate, not the day's total.** spotify-autoqueuer ran 1208 calls on
  2026-08-14 (~1.8/min) with no penalty, against sortify's 678 at ~9.7/min that
  earned ~23h. So `WINDOW_CAP` and the pacing are the real protection; the daily
  caps are runaway backstops. Do not "fix" a 429 by lowering a daily cap.
- **One account, one ledger** (`~/kode/spotify-ledger`, symlinked in as
  `sortify/account_ledger.py`). Since Jul 2026 quota is counted per developer
  account, so sortify, spotify-autoqueuer and playlistener all spend from
  `~/state/spotify/account-ledger.json`. Do not decouple them.
- **Only quota trips propagate; rate limits stay local.** On 2026-08-14 sortify
  was in a 429 cooldown all day while spotify-autoqueuer made 1248 successful
  calls on another client ID of the same account — rate limits are enforced per
  client ID. Publishing one to the shared ledger would park every app for hours
  over a burst only sortify caused. `note_cooldown` enforces this itself.
- **The two 429s are different animals.** `classify_429` splits a rate limit
  (rolling 30s window, wait seconds, retry) from a Development Mode quota trip
  (`"reason": "QUOTA_EXCEEDED"`, stop for the allowance window, never retry).
  Retrying into a quota trip is what extends it.
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
  bound to **127.0.0.1:8800** — loopback only, NOT 0.0.0.0. The LAN address
  refuses connections; reach it from other machines over Tailscale at
  `https://mbp.tail916fc5.ts.net:9800` (tailscaled owns the :9800 listener).
  A browser check against `192.168.1.109:8800` will fail and the failure
  looks like the app being down, which it is not. Do not run ad-hoc
  `sortify` processes alongside the unit.
- Tests: `.venv/bin/pytest -q` — keep them green; they cost zero API calls.
  Frontend: `node tests/ui_harness.mjs` (no framework, no build step — a
  hand-run harness of 189 checks against a stub DOM, also zero API calls).
  Both must be green before any commit touching their surface.

## Conventions (encoded in data/config.json — don't re-derive)

- **Inputs are SETS**, not one flat list (`input_sets`, `sortify/inputsets.py`).
  Each set matches EITHER a name pattern OR a folder segment, and carries a
  label the UI groups by. Currently: `buffer` = `^\[.+\]$` (the day-to-day
  inboxes), `other` = `^<.*>$` (older lists being reworked), `the-bomb` =
  everything inside the `THE BOMB` folder. The folder form exists so a set
  can be declared without renaming playlists whose names already carry
  meaning — "Progressive rock · classic rock · Psychedelic Rock" beats any
  bracketing convention. Folder-defined sets have **no name rule**, so their
  playlists are never flagged by the naming checker.
  `input_name_pattern` remains as the fallback when `input_sets` is absent;
  ids in `input_ids` still union in and land in the first set.
  Consequence worth knowing: for a pattern set the NAME is the membership,
  so renaming is how a playlist moves between sets — and a misnamed one
  cannot be attributed to its intended set at all.
- Homes: playlists under the `ROOT` folder tree minus ARCHIVED/OLD/NEUE
  segments, emoji-prefixed names (🐾/🧸 derived super/subsets), and
  `__x__`/`{x}`/`<x>` marker names. NEUE was excluded 2026-08-24: they are
  staging buckets for new finds, not filing destinations.
- **Subsets** are non-exclusive selections: never a filing home, never an
  input, and a song in one still needs its home. `subset_ids` is the whole
  definition — **any playlist you own can be marked one**; there is no name
  convention. (There was, `{braced}`, until 2026-08-28: the chip that marks a
  subset only renders on rows the Playlists view draws, 200 of ~990, so a
  name rule left most of the library unmarkable. Don't reinstate it.)
  **Subsets are never suggested** — no profile is built for them, so marking
  one reads nothing and costs nothing. They were scored for a few hours on
  2026-08-28 and the user rejected it; `_subset_matches`, `SUBSET_TOP_N` and
  the `SUBSET_WARM_BUDGET` guard were deleted with it, and the warm budget
  only ever existed because scoring needed the tracks. Reinstating any of it
  reinstates a per-poll cost that is currently zero. The picker and the
  `/api/act` guard both key on the marked set, so their reach cannot drift
  apart. `suggest.py` is shared with homes and was never modified for
  subsets.
- The client speaks the Feb-2026 dev-mode API (`items`/`item`, `/me/library`)
  — do not "fix" it back to pre-2026 shapes. Batch ADD exists: up to 100 uris
  per playlist-items POST (probed 2026-08-23; 150 → 400). There is still no
  batch delete.

## Playlist folders (how folder paths work — costs zero API calls)

- **The Web API has no folders.** The hierarchy lives only in
  `data/folders.json`, shape `{playlist_id: {"path": "ROOT / Sub", "caps": bool}}`
  (`caps` = any ALL-CAPS segment on the path). It is never fetched from
  Spotify and never refreshed automatically — if it looks stale or empty,
  that's a data problem, not a code problem. The UI already renders paths
  everywhere (Lists sub-lines, suggestion reasons, the More… picker's grey
  `.p-sub` line, and filter matching); an empty `folders.json` is why they'd
  vanish. It was wiped to `{}` by the 2026-08-21 live-data clobber and
  restored 2026-08-23.
- **Source**: the box's own Spotify snap client (installed + logged in
  2026-08-23). The "Re-import folder tree" button (`POST /api/folders/refresh`)
  runs it headless (Xvfb `:93`, ~45s) to sync, then parses its LevelDB
  rootlist via `sortify/rootlist.py` + the vendored
  [mikez/spotify-folders](https://github.com/mikez/spotify-folders) parser.
  `POST /api/folders` still accepts a tree exported on another machine.
  Both endpoints **re-mark homes** from the tree (`home_folder_prefixes`
  minus excludes, union `sticky_home_ids`), so don't trigger them casually.
- **Known-good extract**: `~/kode/spotify-library/folders.json` (Aug 2026) is
  already in the stored mapping shape — copying it straight into
  `data/folders.json` restores paths without touching home marking or Spotify.
- **Moving playlists between folders**: `.venv/bin/spfolders move "<name>"
  ("<folder>" | --out)` drives the box's own client UI (display `:94`,
  OCR-guided, zero API calls) and verifies against the rootlist; `--dry-run`
  to preview. It shares a lock with the refresh button. After moves, re-run
  the folder re-import to update `data/folders.json`.
