# Emoji subsets, one shared role rule, and filing that actually files — design

Date: 2026-10-01. Supersedes the "no name convention" rule in
`2026-08-28-subset-playlists-design.md` and in sortify's CLAUDE.md.

## What the user asked for

- One source of truth for which playlists are **buffers**, **homes** and
  **subsets**, used by every Spotify tool that groups playlists by role.
- **A subset is a playlist whose name starts with an emoji.**
- Every playlist that started with an emoji and was not a tracked subset is
  now **archived**, prefixed 🗄️ — the user goes through them afterwards.
- New homes and new buffers land in the right folder.

Decided in conversation:

| Question | Answer |
| --- | --- |
| 🗄️ lists | archived: no role at all — not a buffer, home or subset |
| marking a subset | marking IS renaming: 🐾 goes on the name (1 call); `subset_ids` is deleted |
| where the rule lives | a shared module in `spotify-ledger`, Python canonical + TS copy (option A) |
| new homes | the create row offers ROOT folders, pre-selected to the top guess's folder |
| new buffers | `[Filter]`, where 8 of the 10 already are |

## Already done (data, 2026-10-01)

62 renames, 83 calls including the listing refresh: the 11 tracked subsets
without an emoji became `🐾 <name>` (`{braces}` dropped), and the 51 other
emoji-led playlists became `🗄️ <old name>` — the old emoji kept, because
replacing it collided (`↗️ MOVE` / `➡️ MOVE`, two `🐾 femme`). Log with old →
new for undo: `~/scratch/data/subset-renames/done.jsonl`. `🅱️arvakt` is not
ours and was left alone. Under the code as it stands nothing changed role:
subsets are still marked by id, and emoji names were already never homes.

## Who uses roles

Only **sortify** and **spotify-autoqueuer**'s library view (Homes / Subsets
groups, scope chips). playlistener, spotify-backup and wrapped-who do not
classify playlists. Today autoqueuer reads sortify's `config.json` and
re-implements the resolution in TS (`curated.ts`, `libraryView.js`); that
copy is what drifts.

## 1. The shared module: `spotify-ledger/playlist_roles.py` (+ `playlistRoles.ts`)

Same arrangement as `skip_ledger`: Python canonical, symlinked into
`sortify/sortify/playlist_roles.py`; the TS port copied into
`spotify-autoqueuer/src/spotify/playlistRoles.ts` with a vitest asserting it is
byte-identical. Pure functions, no I/O. **Rules live here; data stays in
sortify's `config.json`** (input sets, home folder prefixes and excludes,
sticky homes, `home_ids`).

```
ARCHIVE_EMOJI = "🗄"      # U+1F5C4; the trailing U+FE0F is optional
SUBSET_EMOJI  = "🐾"

starts_with_emoji(name)   # moved from sortify/folders.py, unchanged logic
is_archived(name)         # stripped name starts with U+1F5C4
is_subset_name(name)      # starts_with_emoji and not is_archived
input_set_of(name, folder_path, sets)   # moved from sortify/inputsets.py
home_name_excluded(name, patterns, emoji)  # moved from sortify/folders.py
mark_subset_name(name)    # "🗄️ x" -> "x" if x is emoji-led, else "🐾 x"; plain "x" -> "🐾 x"
unmark_subset_name(name)  # drops the leading emoji cluster and the space after it
resolve_roles(playlists, folders, cfg) -> {id: "input:<set>" | "home" | "subset" | "archived"}
```

Precedence in `resolve_roles`: **archived > input > home > subset**.
Archived beats input too, so archiving an inbox takes it out of the Now view
without having to move it out of a folder-defined set. A subset must also be
`editable` (ours); autoqueuer's listing has no ownership, so there it is the
name rule alone — the one place the two may differ, documented in both.

`starts_with_emoji` keeps today's definition (`ord >= 0x1F000` or Unicode
category `So`), so `↗️`, `👼` and `🔈` all count. The TS port mirrors it with
`\p{So}` plus the same code-point floor.

## 2. sortify

- `_effective_subset_ids` becomes `resolve_roles(...) == "subset"`. `subset_ids`
  is removed from `ConfigIn`, from `/api/config` and from config.json once
  every listed id is 🐾-named (true after the renames).
- **Mark / unmark** (the Lists chip and the subset create): a rename through
  `sp.rename_playlist`, 1 call, using `mark_subset_name`/`unmark_subset_name`.
  The chip's optimistic flip waits for the rename; a failed call leaves the
  name and the role untouched.
- **Create a subset**: the typed name is passed through `mark_subset_name`, so
  "tabletop" is created as `🐾 tabletop`.
- **Quick adds**: targets must resolve to subsets — by name now. Explore's
  `create_name` becomes `🐾 utforsk #{n}` in config.
- **`/api/act` guard and the picker** key on `resolve_roles`, as today on the
  marked set, so their reach cannot drift apart.
- **Archived** lists are no role: not offered in any picker, not inputs.
  The naming checker already exempts emoji-led names.
- `folders.starts_with_emoji`, `home_name_excluded` and `inputsets.set_of`
  become re-exports of the module, so existing imports keep working.
- CLAUDE.md: the "Subsets" paragraph is rewritten to the new rule and says
  why the old "no name convention" rule was dropped (marking by rename works
  from any row, so the 200-row chip limit no longer decides anything).

## 3. spotify-autoqueuer

- `curated.ts` stops reading `subset_ids`; it still reads `home_ids`, input
  sets and folders from sortify's files.
- `libraryView.resolveRoles` calls the shared module's `resolveRoles` over the
  names it has: subsets by name, 🗄️ lists in no group.

## 4. Filing on create

Root cause: a filing job only starts when the role has a destination, and
neither did — `create_folders` is `{"home": null}` (top level) and has no
`input` key at all. Since 2026-09-20 no job has started: no
`/api/playlists/filing` poll appears in the log, and all ten homes created in
sortify sit outside any folder.

- **Buffers**: `create_folders.input = "[Filter]"`. The Now card's "new
  buffer" omits the folder, so it takes this default.
- **Homes from the Now card**: the create row shows the ROOT folders as chips,
  pre-selected to the folder of the song's top-guessed home (else the last
  folder used for homes). The chosen folder is sent explicitly, and becomes
  the role's default as today.
- `_check_leaf_unique` still governs: a chip whose folder the mover cannot
  target is shown disabled with the reason, not offered and then failed.
- Job state stays in memory, but a failure is now also logged
  (`log.warning("filing %s failed: %s")`) so the next "it didn't move" has
  evidence after a restart.

The ten homes and `[FILTER Y]` already at the top level are **not** moved by
this change; moving them is a separate, zero-call `spfolders move` batch the
user can approve with a folder per playlist.

## Testing (zero Spotify calls)

- `spotify-ledger`: unit tests for every function above, including 🗄️ with
  and without U+FE0F, `↗️`, a leading space, and the precedence order.
- autoqueuer: byte-identical check, a Python↔TS agreement test over a fixed
  name list, and `libraryView` tests rewritten from `subsetIds` to names.
- sortify: pytest for resolve, mark/unmark (rename stubbed), create, quick add
  and the act guard; `tests/ui_harness.mjs` for the chip and the folder chips;
  filing tests for the `[Filter]` default and the explicit home folder.

## Out of scope

Reviewing the 51 🗄️ lists (next, with the user); moving the existing
top-level homes; any automatic re-import of the folder tree.
