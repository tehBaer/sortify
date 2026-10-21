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
  `BULK_RESERVE` 150 is a fourth layer: a bulk call never spends the day's
  last 150 calls, sleeping to local midnight instead.
- **It is the rate, not the day's total.** spotify-autoqueuer ran 1208 calls on
  2026-08-14 (~1.8/min) with no penalty, against sortify's 678 at ~9.7/min that
  earned ~23h. So `WINDOW_CAP` and the pacing are the real protection; the daily
  caps are runaway backstops. Do not "fix" a 429 by lowering a daily cap.
- **One account, one ledger** (`~/kode/spotify/spotify-ledger`, symlinked in as
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
- **Prior art**: `~/kode/spotify/spotify-autoqueuer/src/spotify/` (token bucket, 429
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
- **Public route (since 2026-10):** Tailscale Funnel `https://mbp.tail916fc5.ts.net:10000`
  → `127.0.0.1:8801`, a second socket of the same process
  (`SORTIFY_PUBLIC_PORT`). Everything on it is behind Sign in with Spotify,
  owner only (`gate_owner_id` in config; `sortify/gate.py`). The port a request
  arrived on (`scope["server"]`) is what separates the routes — never headers.
  The Claude button is stripped from public pages and stays on the tailnet
  port. Each sign-in costs one `/me` call. Secret: `SORTIFY_GATE_SECRET` in
  the git-ignored `.env`; **without it `_sockets()` logs an error and opens
  only the tailnet socket — that stops sortify's own gate from running, it
  does not close port 8801 to the world.** Funnel points at 127.0.0.1:8801
  regardless of what sortify does, so **port 8801 is reserved**: whatever
  listens there is on the internet with no gate of its own. Never run a dev
  or prototype server on it. Spec:
  `docs/superpowers/specs/2026-10-16-spotify-login-gate-design.md`.
  Two separate daily caps protect sign-in: `DAILY_EXCHANGE_CAP` (100/day)
  is taken before the code exchange, so a stranger throwing garbage codes
  at /gate/callback only spends that counter; `DAILY_VERIFY_CAP` (20/day,
  `_gate_counter`) is taken only after a successful exchange, right before
  the account check, so bogus codes can never spend the owner's 20
  sign-ins. A stranger can still spend the 100 exchanges and so block fresh
  public sign-ins until midnight (or a restart); existing cookies and the
  tailnet route keep working. Accepted: the token endpoint stays protected.
  On the public port, `/static/index.html` (any spelling StaticFiles would
  resolve to it) never serves the raw file, which carries the Claude button
  line: signed in it redirects to `/`, signed out to `/gate/start`.
- Tests: `.venv/bin/pytest -q` — keep them green; they cost zero API calls.
  Frontend: `node tests/ui_harness.mjs` (no framework, no build step — a
  hand-run harness against a stub DOM, also zero API calls).
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
  segments, emoji-led names (those are subsets, or archived under 🗄️ — see
  **Subsets** below), and `__x__`/`{x}`/`<x>` marker names. NEUE was excluded 2026-08-24: they are
  staging buckets for new finds, not filing destinations.
- **Subsets** are non-exclusive selections: never a filing home, never an
  input, and a song in one still needs its home. **A subset is its NAME**
  since 2026-10-01: our playlist whose name starts with an emoji, except 🗄️,
  which means **archived** — no role at all, not even input. The rule lives
  in `~/kode/spotify/spotify-ledger/playlist_roles.py` (symlinked in as
  `sortify/playlist_roles.py`; spotify-autoqueuer copies its JS port), with
  precedence archived > input > home > subset. Marking is a rename (the
  Subset chip, `POST /api/playlists/{id}/subset`, 1 call): 🐾 on, the leading
  emoji off. `subset_ids` is gone. It was an id list from 2026-08-28 because
  the chip could only reach the ~200 rows the view draws and a name rule
  left most of the library unmarkable; a rename from the chip removes that
  objection, and only a name lets other tools see the role. **Subsets are
  never suggested** — no profile is built for them, so a subset costs
  nothing per poll. They were scored for a few hours on 2026-08-28 and the
  user rejected it; `_subset_matches`, `SUBSET_TOP_N` and the
  `SUBSET_WARM_BUDGET` guard were deleted with it. The picker and the
  `/api/act` guard both key on the same rule, so their reach cannot drift
  apart. `suggest.py` is shared with homes and was never modified for subsets.
- **Quick adds** (`quick_adds` in config, `sortify/quickadds.py`) are the Now
  card's one-tap destinations — Star, Explore artist, Add to rotation. Each is
  an ordinary subset add with the picker skipped: same `/api/act` shape
  (`from_id: null`), same undo, same "a selection is not a filing" rule, so
  the targets must be subsets (emoji-named). A button whose target already holds the
  song spends nothing — membership comes off the cached track list, free.
  The explore entry starts with `playlist_id: null` + `create_name`;
  `POST /api/explore` creates it once (1 call), names it with 🐾, writes the
  id back into config, and records the artist in `data/explore.json` — that
  log is the seed for exploring those artists later, and nothing else writes it.
- The client speaks the Feb-2026 dev-mode API (`items`/`item`, `/me/library`)
  — do not "fix" it back to pre-2026 shapes. Batch ADD exists: up to 100 uris
  per playlist-items POST (probed 2026-08-23; 150 → 400). Batch DELETE
  exists too: `DELETE /playlists/{id}/items` with `{"items": [{"uri": …}, …]}`
  removed two tracks in one call, read back (probed 2026-10-20), and 100
  in one call, read back empty (probed 2026-10-21) — so `REMOVE_BATCH` is
  100, like add. Above 100 is untested. The old "no batch delete" note was
  never tested. A dev-mode app CAN publish a playlist: `PUT /playlists/{id}`
  with `{"public": true}` sticks, verified by reading `public` back on
  2026-08-31 — so "make it shareable" costs one call per playlist, not a trip
  through the desktop client. Creation still sends `public: false` unless a
  caller passes `public=True`; whether the CREATE honours it was never probed
  (the PUT-after-create path is the verified one).
- **Playlist reads ask for playability** (since 2026-10-20): `market=from_token`
  plus `is_playable,linked_from` in the fields, stored as each track's
  `playable`. Unprobed in dev mode when written; a 400 drops that process
  back to the old request. A market can return a relinked stand-in, so
  `_slim_track` keeps `linked_from`'s uri — removals and membership need the
  playlist's own. The input switcher's count leaves out `_unplayable` songs
  (`playable` False, or no title: how the API returns a pulled song); they
  stay members everywhere else.

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
- **Filing on create**: `/api/playlists/create` takes a `folder` and, when
  given one, starts a background `sortify/filing.py` job that drives the same
  client-UI mover (zero API calls, ~a minute, shares the client lock with the
  refresh button). Omitting the field means "this role's default" from
  config's `create_folders`, which the create row's dropdown pre-selects and
  every explicit choice rewrites. A landed move patches ONE entry into
  `data/folders.json` (`Store.set_folder_path`) rather than re-importing the
  tree, which would re-mark every home. Filing refuses when another playlist
  shares the name: the client finds the row by filtering on it. A failure is
  never destructive — the playlist exists, at the top level, and
  `GET /api/playlists/filing/{id}` says why. Inputs are creatable here too,
  and their name must match a set rule (or the destination folder must
  define the set), because for a pattern set the name IS the membership.
- **Known-good extract**: `~/kode/spotify/spotify-library/folders.json` (Aug 2026) is
  already in the stored mapping shape — copying it straight into
  `data/folders.json` restores paths without touching home marking or Spotify.
- **Moving playlists between folders**: `.venv/bin/spfolders move "<name>"
  ("<folder>" | --out)` drives the box's own client UI (display `:94`,
  OCR-guided, zero API calls) and verifies against the rootlist; `--dry-run`
  to preview. It shares a lock with the refresh button. After moves, re-run
  the folder re-import to update `data/folders.json`.
- **Not every folder can be targeted, and that is a client limit.** The mover
  types the destination's leaf into "Move to folder" and clicks the first
  match. That search matches folder names AND their ancestry, and draws each
  hit as name-over-parent-path — so the query text appears on other folders'
  rows and the wanted row need not be first, or even on screen. Filing into
  `input` on 2026-09-20 hit the parent line of `input / inputlister /
  matra-esque` and moved the playlist in there. `_check_leaf_unique` now
  refuses any destination another row could match: another folder whose name
  contains the leaf, or the target having subfolders. On this account that
  leaves 52 of 88 folders targetable; the rest have to be moved by hand.
  Don't narrow the guard without a mover that can identify the row it clicks.

## Split a home into parts

- The Split tab (since 2026-10): pick a home, choose 2–6 parts, get a
  suggestion, drag songs between parts on the phone, tap a song to open
  the house preview player on it (it walks on down that part, via
  `GET /api/splitparts/{pid}/preview`, zero Spotify calls), then Done. Spec:
  `docs/superpowers/specs/2026-10-20-split-into-parts-design.md`.
- **A hold on a song plays it in Spotify and opens it in Now**
  (`POST /api/splitparts/{pid}/play`, 1 call, `play_in_context`): inside
  the playlist being split, so the music carries on down it. It closes the
  preview player without that player's own resume call — the play is the
  music coming back. On success it switches to the Now view so the song's
  card is up for filing (since 2026-10-21; it used to stay in Split): no
  poll at once, but Next's settle repoll (`repollAfterPlaybackChange`,
  900 ms), and `/play` arms the same settle window as Next so an answer
  still on the song from before is cached only briefly. The cost is the
  Now polls the view makes anyway. A failed play stays in Split with the
  error. The draft is untouched. A press that moves is still a drag, a
  quick tap still previews.
- **Removing a part** asks where its songs go: each to its best fit, or all
  of them to one part you name (`to` in the remove body). Zero calls.
- **Nothing is written to Spotify before Done** — with one exception the
  user triggers per song: the row's "Move to another home" icon (the Now
  card's magnifier)
  (`POST /api/splitparts/{pid}/elsewhere`, 3 calls, no undo) first reads the
  split playlist's snapshot and refuses (409, Done's "changed — re-read"
  message) if Spotify moved on since the draft, then adds the song to the
  picked playlist and only then removes it from the one being split, so a
  failure can leave it in both but never in neither. A destination whose
  cached tracks already hold the song gets no second add — but only when
  its snapshot, read then (1 call), still matches the cached one; a stale
  cache could otherwise skip the add for a song since removed there and
  leave it in neither. Still 3 calls; it makes the retry after a "now in
  both playlists" error a clean recovery.
  It takes the removal's snapshot into the draft, so Done does not see
  "changed".
  Its targets (`GET .../targets`) are the other homes in the cached
  listing, zero calls — homes only since 2026-10-21: inputs and subsets
  buried them in the search. Opening that picker closes the preview
  player first: the player sits above the picker, and a tap meant for a
  lower row landed on it, so the pick never happened.
  Beside it, the row's tray icon, "Move back to a buffer" (2026-10-21),
  is the same move over the other list: `GET .../targets?kind=buffer`
  gives every input (`_effective_input_ids`, so archived ones are out;
  never Liked Songs, a read-only one or the split playlist) with its set
  key, from config and the cached listing, zero calls. The picker shows
  the set as the grey line, like the Now card's "Move to buffer…", with
  no create row and no preview. `/elsewhere` accepts a `to_id` from
  either list, with every guard unchanged; the magnifier's list stays
  homes only. The two lists are cached per split apart (`spTargets`,
  `spBuffers`).
  Suggest reads the snapshot (1 call) and the track list only when it
  moved (~1 per 100).
- The grouping (`sortify/splitparts.py`) is pure and offline: artist tags,
  Last.fm getSimilar and artist-similar, average-linkage clustering cut at
  exactly N with slivers folded in. Weights are constants in that file.
  The Last.fm fill-in that feeds it stops after 10 consecutive failures and
  groups with whatever is already stored.
- Drafts live in `data/splitparts.json`, one per home; every move is saved
  at once. Once Done has started for a draft, Suggest, moves, part changes,
  Re-read and Discard are all refused (409) until Done finishes; the UI
  then shows a locked "Finish Done" screen with the stored choices, and a
  resumed Done always uses the choices it started with. That screen's
  "Abandon" (`POST /api/splitparts/{pid}/abandon`, zero calls) is the way
  out of a failure that repeats: it drops the draft and keeps whatever was
  already created.
- Done creates and fills the new parts, then adds the into-parts (below),
  and touches the original only after all of them (🐾 rename and dropped from the home lists, or batched
  removal). It is resumable per add batch (no duplicates), marks every
  created part a home on every pass, and refuses a folder that wasn't
  offered on a first attempt (a resume doesn't refuse one, since filing
  fails without damage). Batch removal takes 100 songs per call
  (`REMOVE_BATCH`, probed 2026-10-21), so "becomes a part" costs one call
  per 100 songs moved out of the original.
- **A part can go into a home you already have** instead of becoming a
  new playlist (since 2026-10-21): the Done screen's "add to an existing
  home" picks one of the `/targets` homes, sent as `into: {part key: home
  id}` (the plan takes repeated `into=p2:ID` query pairs). Nothing is
  created, marked or filed for it, and the twin-name check skips it. Its
  songs are only added, in batches of 100, after a snapshot read of the
  home; "already there" is decided from the cached tracks only when that
  snapshot matches them, else from a fresh read (`_cached_tracks`), on
  every pass, a resume too. That makes a resume exact (landed batches are
  in the mirrored cache, or found by the re-read when an add's answer was
  lost), and a song a stale cache wrongly listed is never skipped and
  then removed from the original. The plan prices 1 snapshot + the adds
  per into-part, plus the read only when nothing is cached. The "at least
  2" rule counts destinations (new + into + the kept part). Targets are
  checked on a first attempt only: a resume keeps its stored home even
  once it stopped being offered, since refusing would strand the split
  and finishing only adds. The summary's `into` says how many were really
  added (`count`) and how many were there already.
- Replaced the 2026-08 split (piles, sittings, the queued materialiser):
  all of that code is gone; its data files are moved aside as
  `*.bak-2026-10-20` at rollout.
