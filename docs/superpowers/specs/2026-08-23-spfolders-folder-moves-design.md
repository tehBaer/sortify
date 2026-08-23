# spfolders — moving playlists between folders via the desktop client

**Date:** 2026-08-23
**Status:** approved design, implementation deferred until the repo is clear of
parallel sessions
**Depends on:** `sortify/rootlist.py` (headless client + rootlist extraction,
merged 2026-08-23)

## Why

The Web API has no folder endpoints — it cannot create, rename, or move
anything in the folder tree, and never will on the current dev-mode surface.
The box now has the Spotify snap client installed and logged in (2026-08-23
spike), and its UI *can* do all of these. This spec adds a CLI tool that
drives that UI to move playlists between folders, verified against the
client's own LevelDB rootlist after every action.

Zero Web API calls anywhere in this feature: the client syncs over its own
protocol (not dev-mode quota), and verification is pure local disk.

## Shape

A `spfolders` console-script entry point beside `spx`:

```
spfolders tree [--sync]                      # print the hierarchy
spfolders move "<playlist>" "<folder path>"  # move into a folder
spfolders move "<playlist>" --out            # move up to top level
spfolders move ... --dry-run                 # print the plan, touch nothing
```

- `tree` reads the cache via `rootlist.extract_tree()`; `--sync` runs
  `rootlist.sync_client()` first. Output shows the cache mtime so staleness
  is visible.
- `move` resolves both names against the extracted tree **before** any UI
  work. Ambiguous playlist names (duplicates exist) and unknown folder paths
  fail here, with candidates listed. Folder paths use the stored
  `"Parent / Child"` join format.
- Not in scope: folder create/rename/delete as CLI verbs, batch moves, and
  any sortify web-UI integration. All are natural follow-ups once the move
  mechanism has proven itself in real use.

## Mechanism

New module `sortify/clientui.py` owns the client session and driver
primitives:

- **Session**: Xvfb on display `:94` (the refresh button owns `:93`), then
  `snap run spotify --disable-gpu --force-renderer-accessibility`. Wait for
  the main window via `xdotool search`, with a hard timeout.
- **Primary driver — accessibility tree**: with
  `--force-renderer-accessibility` the CEF renderer exposes AT-SPI; find the
  sidebar row and menu items by accessible name (pyatspi), never by pixel
  position. This survives layout, theme, and resolution changes.
- **Fallback driver — OCR**: if the a11y tree is hollow (to be established
  during implementation), screenshot the display (`xwd`/`import`), locate
  text with tesseract, and interact keyboard-first via xdotool: focus the
  library filter box, type the playlist name, arrow to the row, context-menu
  key, walk the "Move to folder" submenu.
- **Step discipline**: every step asserts the UI state it expects (window
  present, filter focused, menu open, item found) before acting. Any
  assertion failure aborts the run — no speculative clicks. A move is
  atomic-by-abort: the only two outcomes are "verified moved" and "verified
  not moved"; anything else is a loud error.

`sortify/foldermove.py` owns orchestration: name resolution against the
tree, the move plan, the verify loop, and retry policy.

## Verification

After the UI reports the action done: terminate the client (flushes the
LevelDB), run `rootlist.extract_tree()`, and assert the playlist's new path
is exactly the requested destination. One retry of the whole move on
failure, then give up with both trees (before/after) printed. The rootlist
is ground truth; the UI is never trusted on its own word.

## Concurrency

The snap client is single-instance per profile: a second launch attaches to
the first, which would drive UI events into the wrong session. `spfolders`
takes an exclusive file lock (`~/state/spotify/client-ui.lock`) around the
whole session. Follow-up (one line, peer's file): `rootlist.sync_client()`
takes the same lock, so the refresh button and `spfolders` can never fight
over the client.

## Testing

- **Pure-logic tests** in the normal suite (zero client, zero API): name
  resolution incl. ambiguity, folder-path matching, tree diffing for the
  verify step, plan construction for `--dry-run`.
- **Live proof** (`pytest -m clientui`, excluded from the default run): the
  scratch-object cycle — create a scratch folder and scratch playlist via
  the client UI, move the playlist in, verify from the cache, move it out,
  verify again, delete both. Never touches real folders or playlists. This
  test is the acceptance gate: the tool is not "ready" until it passes.
- User-approved policy (2026-08-23): scratch-object live testing is OK;
  real-tree moves happen only on explicit user command.

## Risks

- **Spotify redesigns break the driver.** Accepted: this is best-effort
  tooling by nature. The step discipline turns a redesign into "aborts with
  a clear error", never a half-move. The vendored-parser + rootlist verify
  layers are independent of the UI and unaffected.
- **The a11y tree may be empty** in the snap build. The OCR fallback is the
  hedge; if both fail, the spec's answer is to stop, not to pixel-click on
  hardcoded coordinates.
- **Cache flush timing**: the LevelDB may lag the UI action. The verify loop
  re-extracts with a short backoff before declaring failure, and the retry
  re-checks the *before* state first so a slow flush is never double-moved.
