# Creating home playlists from inside sortify

Design, 2026-08-21. Approved in the 2026-08-21 brainstorming session.
Every decision below was explicitly approved by the user; this document
records them, it does not reopen them.

## Problem

Every playlist sortify can file into must first be created in the Spotify
client and then picked up by **Refresh** (~21 paginated calls, ~1 minute).
So the moment that actually produces new homes — a song is playing, it
plainly belongs somewhere that does not exist yet — is the moment sortify
cannot help with. The user leaves for the Spotify client, creates the
playlist, comes back, refreshes, and by then the song has moved on.

The fix is one call: `POST /me/playlists`. `Spotify.create_playlist`
already exists and is already budget-accounted (`sortify/spotify.py`) —
sittings and the queued materialiser use it. What is missing is everything
around it: giving the new playlist the home role, making that role survive
a folder ingest, and making the playlist visible and usable without a
Refresh.

### Scope

Home playlists only. A companion idea from the same session — first-class
**subset** playlists (curated selections that do not count as a final home,
identified by `{braces}` or an emoji prefix) — was explored and
deliberately deferred. Findings worth keeping for that later session are in
[Appendix: deferred subset work](#appendix-deferred-subset-work).

## Constraints this design is built around

- **Budget.** 1 call per created home; 2 when created from the picker with
  a track to file. Nothing bulk, nothing background, nothing on the polling
  path. (`CLAUDE.md`, non-negotiable.)
- **The playlist listing is manual-refresh only.** `my_playlists()` serves
  `cache.json`'s `playlist_list` until `/api/refresh` re-reads it. A
  playlist created app-side is therefore invisible — and, worse, *unusable*
  — until the user pays for a Refresh, unless we write it into that cache
  ourselves.
- **`/api/folders` re-derives `home_ids` from the desktop folder tree.** A
  playlist created in-app has no folder path, so the next ingest would
  silently demote it.
- **`_cached_tracks` keys the track cache on `snapshot_id`.** A cache entry
  whose `snapshot_id` is falsy or mismatched is refetched. Getting this
  wrong for a new home costs one call on *every* profile rebuild — i.e.
  every 10 minutes, forever, to re-read a playlist we know is empty.

## Design

### 1. Endpoint

`POST /api/playlists/create`, body `{name, role: "home"}`. One Spotify
call. Returns the new listing item so the client can render the row
in place.

`role` is explicit and today accepts only `"home"` — anything else is a
400. It exists rather than being implied because the two deferred roles
(inputs, subsets) differ from a home in exactly this field and nothing
else; a caller stating which one it wants is clearer than an endpoint whose
name has to change when the second one arrives.

Validation runs before anything is spent (zero calls). The name is
rejected when it matches:

- `input_name_pattern` (`^\[.+\]$`). A home named `[Foo]` would be re-read
  as an **input** by `_effective_input_ids` on the very next request — the
  config `home_ids` list loses to the pattern union — so creating it would
  silently produce the opposite of what was asked for.
- `home_name_exclude_patterns` (`{x}`, `<x>`, `__x__`) or an emoji prefix
  (`home_exclude_emoji_names`). These can never be homes: the name-shape
  rules in `sortify/folders.py` drop them from every folder ingest.

Duplicate names are **allowed**, with a note in the response. Spotify
permits duplicate playlist names; refusing would misrepresent what is
possible.

The validation lives in `sortify/folders.py` next to the rules it consults,
as a pure function taking the name and the relevant config values. It is
unit-testable without an API client, a Store, or the DOM — the same shape
as `home_name_excluded`.

### 2. Stickiness — surviving a folder ingest

New config key: `sticky_home_ids` (list of ids marked as homes from inside
sortify).

`/api/folders` **unions** it into the set it computes from the tree, after
the same `& editable` and `- inputs` filters the tree-derived ids get. The
folder tree keeps full authority over every playlist it can actually see;
it simply stops deleting knowledge it never had. A home created in sortify
stays a home whether or not the user ever files it into a `ROOT` folder in
the Spotify client and re-exports.

The reconciliation that keeps this honest: `/api/config` intersects
`sticky_home_ids` with the incoming `home_ids`. Without it, switching
**Home** off in the Playlists view would demote the playlist until the next
ingest resurrected it — a role that cannot be revoked is a bug, not
durability.

### 3. Visibility — `remember_playlist`, and the snapshot trap

`Spotify.remember_playlist(item)`: the exact inverse of the existing
`forget_playlists`. Same `_LIST_LOCK`, same read-modify-write discipline,
zero network. The item is shaped exactly as `_fetch_my_playlists` produces
it (`id`, `name`, `owner`, `editable`, `total`, `snapshot_id`, `image`,
`description`), so nothing downstream can tell a remembered playlist from a
fetched one.

Reason for the lock, unchanged from `forget_playlists`: a `/api/refresh`
landing between this read and its write still wins, and correctly so — it
has just asked Spotify what actually exists.

**The snapshot trap.** `_cached_tracks(pid, snapshot_id)` serves the cache
only when `snapshot_id` is truthy *and* equal to the cached entry's. Two
things follow:

1. The new playlist's track cache is **seeded**, not fetched:
   `cache["playlists"][id] = {"snapshot_id": …, "tracks": [], "fetched_at":
   now}`. We just created it; it is empty by construction, and reading it
   back would be one call to learn nothing.
2. The `snapshot_id` written into the listing entry and the one seeded into
   the track cache must be **the same non-empty value**. Use the one from
   the create response if it carries one. If it does not, write the
   sentinel `"created:<id>"` into both. It only ever has to equal itself,
   and the first real `/api/refresh` replaces it with the truth. This is
   the same trust the app already places in its own cache between
   refreshes, where `_cache_move` and `_apply_snapshot` keep it current.

Whether the Feb-2026 dev-mode create response carries `snapshot_id` is
**unverified, and must not be probed for** — no speculative call to find
out. Log the response keys on the first real creation; if it is always
present, delete the sentinel branch then.

### 4. Usable immediately

After a successful creation, clear `_profile_state` and reset `built_at` —
the same move `/api/config` already makes after a hints save, and for the
same reason. Otherwise the new home does not appear in the `homes` payload,
the picker, or the triage view for up to `PROFILE_TTL` (10 minutes).

**Consequence, stated because it is the main thing the user will notice:**
an empty home builds an empty profile, scores 0 against every track, and is
therefore **never suggested** until it holds tracks. It is reachable only
from **Add to…**. This is correct — a suggestion engine should not
recommend a playlist it knows nothing about — and it is the argument for
the picker entry point below being the primary one: creating a home and
putting its first track in it should be one gesture.

### 5. Two entry points, both priced

House style: every control that spends states its cost before it is
pressed.

- **Playlists view** — a "New home playlist" row above the list: name
  field, button reading `Create (1 call)`. On success the row appears in
  place, already marked **Home**, with no Refresh.
- **Add to… picker** — when the filter text matches no playlist, a row:
  `Create home "late night" and file this track here (2 calls)`. It
  creates, marks, and adds; the Now card then lands in its ordinary
  `✓ filed to …` state. This is the moment of need the whole feature
  exists for.

The new home's folder path is blank until the user re-exports the folder
tree from the desktop client. The row says so once, quietly — it is not an
error, and `select_home_ids` never needed to see it for the playlist to
work as a home.

### 6. Tests

All zero-call, per `CLAUDE.md`.

- **Name validation**, pure function: rejects `[x]`, `{x}`, `<x>`,
  `__x__`, and emoji-prefixed names; accepts ordinary ones.
- **`remember_playlist` round-trip**: the item appears in the listing with
  no HTTP, and `forget_playlists` still removes it.
- **Sticky union**: a folder ingest whose tree never mentions the created
  id keeps it a home; toggling **Home** off through `/api/config` drops it
  from `sticky_home_ids`, and a subsequent ingest does not resurrect it.
- **The leak guard**: create a home, then build profiles twice; total spend
  is exactly **1** call. This is the test that would have caught the
  snapshot trap, and it is the reason the trap is written down rather than
  fixed silently. Neighbourhood: `tests/test_no_proactive_work.py`.

## Files this touches

| File | Change |
| --- | --- |
| `sortify/folders.py` | pure name-validation function beside `home_name_excluded` |
| `sortify/spotify.py` | `remember_playlist`, inverse of `forget_playlists` |
| `sortify/app.py` | `POST /api/playlists/create`; `sticky_home_ids` union in `/api/folders`; intersection in `/api/config`; profile-state clear |
| `sortify/static/app.js` | "New home playlist" row; picker no-match creation row |
| `sortify/static/style.css` | styling for both |
| `tests/` | the four groups above |

## Non-goals

- **Creating inputs or subsets.** Homes only. Inputs already self-register
  through `input_name_pattern`, so creating one is a strictly smaller
  problem that can reuse this endpoint later.
- **Renaming, deleting, or reordering playlists from sortify.** Deletion
  machinery exists (`unfollow_playlist`) but belongs to the sitting-cleanup
  flow, which is careful about a different thing.
- **Placing the new playlist in a folder.** The Web API has no folder
  concept at all; only the desktop client can do it, and only a fresh
  spotify-folders export can tell sortify about it.

## Appendix: deferred subset work

A **subset** would be a curated selection — a best-of or a mood — that does
*not* count as a final home: filing into one must not mark a song as
sorted, and the song must stay suggestible into a real home. The session
established these before the scope was narrowed; they are recorded so the
survey does not have to be repeated.

**The identification rule the user chose:** a subset is any playlist whose
name is wrapped in `{braces}` or begins with an emoji.

**What that rule actually selects, measured against `data/cache.json` on
2026-08-21** (zero API calls — a local cache read):

- **124 matching playlists, 122 editable** — 72 `{braces}`, 52 emoji.
- **None** currently carries a home or input role, so there is no collision
  with existing config.
- 7156 tracks across them; largest 1231, median 20; **1 of 122** has its
  tracks cached today.
- The emoji space is not one concept but at least four: 🐾 (30), 🧸 (9),
  🔈 (4 — 167/551/161/533 tracks, the *derived supersets* `CLAUDE.md`
  names), ↗/➡/↘ (7 tempo markers, 2–6 tracks each), plus 🅱 (not even
  owned by this account) and 👼.

**Consequences that must shape any future subset design:**

- A chip row of 122 targets on the Now card is not a design. The approved
  direction was: chips for the handful of subsets last filed into
  (remembered server-side so a reload keeps them), everything else behind
  **Add to…**.
- Loading all 122 track lists eagerly in `_ensure_profiles` is ~122+ calls
  paced at `WINDOW_CAP` — roughly **10 minutes of stall on a polling
  endpoint**, and exactly the unrequested bulk fetch `CLAUDE.md` forbids.
  Membership (`✓`) must be lazy: a subset's tracks are read once, on first
  use, at 1 call for the median 20-track subset (`playlist_tracks`
  paginates at 100).
- Subsets stay **out of the suggestion engine** entirely (approved). They
  are manual targets, so the measured invariants in `sortify/suggest.py`
  are untouched.
- Filing into a subset must pass `from_id: null`, and `/api/act` should
  reject a `from_id` paired with a subset `to_id`: a song filed into a
  best-of still needs its home, so it must not leave the input.
- **A real bug this would expose:** `btn-undo-now` pops the last key of
  `filedUris`. A subset add writes no such key, so undoing one would clear
  an unrelated song's filed state. The client needs an ordered
  `{uri, kind}` action log, clearing `filedUris` only for `kind === "home"`.
- The four 🔈 supersets are swept in by the naming rule. If something
  regenerates them, manual adds there are lost. No `subset_exclude_ids`
  escape hatch was designed; the user's stated fallback is a rename.
- The emoji rule in `folders.py` is today **exclusion-only** — it keeps
  derived names out of homes and never records that they are subsets.
  Whatever asserts subsethood must guarantee the implication *subset ⇒
  home-excluded*, with a test, so the two rule sets cannot drift.
