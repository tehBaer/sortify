# Playlist naming rules — design (handoff from brainstorm)

Brainstormed 2026-08-20 in a boxdash-repo chat; approved by Bjørn for
implementation inside sortify. This doc is the handoff — continue in the
sortify repo.

## Goal

Ongoing enforcement of playlist naming conventions, with every rename
individually verified by the user before it is applied. Not a one-time
cleanup: new playlists keep appearing, so the checker stays.

## Rules (approved)

Categories come from sortify's existing data — the In/Home roles marked in
the Playlists view plus the folder tree from spotify-folders. No manual
re-classification.

1. **Home playlists** (role = Home): name must be ALL CAPS.
   Violation → propose `name.upper()`.
2. **Input playlists** (role = In): name must be wrapped in `[…]`.
   Violation → propose `[name]`.
3. **Emoji-prefixed playlists**: the emoji prefix IS the subset marker —
   these are valid as-is and are never flagged. `folders.starts_with_emoji`
   already implements the detection.
4. **Subset folders in `{…}`**: deferred. Bjørn has no folder rules yet but
   is open to them. Two constraints when this is picked up: (a) the Web API
   cannot rename folders, so folder violations can only be *flagged* with a
   "manual — rename in the desktop app" note; (b) there is no known signal
   yet for which folders count as "subset" folders — that question is
   unresolved. Keep the rule engine extensible so this slots in later.

## Approved UI approach (option A)

A collapsible **"Naming" panel at the top of the Playlists view**: shows
"N naming issues" when violations exist, disappears when everything
conforms. Expanded, it lists each violation as `current name → proposed
name` with a per-row **Rename** button — approve one at a time, never bulk.

(Rejected alternative: per-row ⚠ badges on the playlist list — scatters the
review across a long list and folder violations would have no row.)

## Backend shape

- New small module `sortify/naming.py`: pure rule functions
  (category → expected form → proposed rename). Reuse `folders.py` helpers
  (`_is_caps`, `starts_with_emoji`) rather than duplicating them.
- Two endpoints in `app.py`:
  - `GET /api/naming` — list of violations `{playlist_id, current, proposed, rule}`.
  - `POST /api/naming/{playlist_id}/rename` — apply one proposal via
    `PUT /playlists/{id}` `{"name": …}` through the existing
    `SpotifyClient.request`. The playlist-modify scopes are already granted
    (sortify adds/removes tracks today).
- Only rename playlists the user owns (see `_foreign_playlist_error` for
  the existing ownership pattern).

## Testing

Pure rule functions in `naming.py` get unit tests in `tests/` (existing
pytest setup). Edge cases worth covering: names already conforming, names
with emoji + role marked Home/In (emoji exemption wins — confirm this
precedence with Bjørn if it comes up), non-alphabetic names where
`upper()` is a no-op, already-bracketed inputs.

## Next step in the sortify chat

The design above is approved at the approach level. Next: invoke
superpowers:writing-plans to produce the implementation plan, then
implement with TDD.
