# Emoji Subsets, One Shared Role Rule, and Filing on Create — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Subsets are defined by their name (an emoji first, 🗄️ excepted) in one shared module used by sortify and spotify-autoqueuer; 🗄️ lists have no role; new buffers and homes are filed into folders.

**Architecture:** A pure module `playlist_roles` lives in `~/kode/spotify/spotify-ledger` (Python canonical `playlist_roles.py`, plain-JS port `playlistRoles.js`), the same arrangement as the skip and account ledgers. sortify symlinks the Python file in; autoqueuer copies the JS file into `src/web/` (browser ES module, no build step) and a vitest pins it byte-identical and in agreement with Python. Data (input sets, home ids, folders) stays in sortify's `data/config.json` and `data/folders.json`.

**Tech Stack:** Python 3 / FastAPI / pytest (sortify); Node 22 / TypeScript / vitest / plain browser ES modules (autoqueuer); vanilla JS + `tests/ui_harness.mjs` (sortify frontend).

**Spec:** `docs/superpowers/specs/2026-10-01-emoji-subsets-design.md` (sortify repo). One deviation, decided while planning: autoqueuer's port is `playlistRoles.js`, not `.ts`, because its only consumer (`src/web/libraryView.js`) is a browser module served without a build.

## Global Constraints

- **Zero Spotify calls** in every test and every verification step. The only live calls this plan makes are the user's own taps after deploy.
- Subset rule: name (stripped) starts with an emoji — `ord(c) >= 0x1F000` or Unicode category `So` — and does not start with 🗄 (U+1F5C4, U+FE0F optional).
- Precedence: **archived > input > home > subset**. A subset must be `editable`; where ownership is unknown (autoqueuer), treat it as editable.
- Marking a subset = renaming: `mark_subset_name`, 1 Spotify call; unmarking = `unmark_subset_name`, 1 call. `subset_ids` is removed from config and code.
- `SUBSET_EMOJI = "🐾"`; archive prefix written as `"🗄️ "` (U+1F5C4 U+FE0F space).
- New buffers default to folder `[Filter]`. New homes from the Now card are filed into a chosen folder; folders the mover cannot target are shown disabled with the reason.
- Do not narrow `_check_leaf_unique` (sortify CLAUDE.md). Do not decouple the shared ledgers.
- sortify: `.venv/bin/pytest -q` and `node tests/ui_harness.mjs` green before each sortify commit. autoqueuer: `npx vitest run` and `npx tsc --noEmit -p .` green. spotify-ledger: `python3 -m unittest discover -s . -p 'test_*.py'` green.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Work on `master`/`main` in place (no other session is in these repos; worktrees must be siblings if used — see memory).
- Restarting `sortify.service`: check `data/queue.json` state first and that no other Claude session is driving the service.

## Review Focus

1. **A name that is only an emoji** (`👼`) being unmarked would become empty — the endpoint must refuse (400) and leave the name alone. Test in Task 5.
2. **Archive prefix without U+FE0F** (`🗄 x`, typed on a keyboard that drops the selector) must still count as archived. Test in Task 1.
3. **A leading space before the emoji** (` 🧸 lungt` existed) must classify the same as without it. Test in Task 1.
4. **Marking a playlist that is a home or an input** must be refused, not renamed into a name that changes nothing (home/input beat subset). Test in Task 5.
5. **A stale browser tab still POSTing `subset_ids` to `/api/config`** must not 422 or resurrect anything — the field is ignored. Test in Task 5.

---

### Task 1: The shared Python module `playlist_roles.py`

**Files:**
- Create: `~/kode/spotify/spotify-ledger/playlist_roles.py`
- Create: `~/kode/spotify/spotify-ledger/test_playlist_roles.py`

**Interfaces:**
- Produces (all pure, no I/O):
  - `ARCHIVE_EMOJI: str = "\U0001F5C4"`, `ARCHIVE_PREFIX: str = "\U0001F5C4️ "`, `SUBSET_EMOJI: str = "\U0001F43E"`
  - `starts_with_emoji(name: str) -> bool`
  - `is_archived(name: str) -> bool`
  - `is_subset_name(name: str) -> bool`
  - `mark_subset_name(name: str) -> str`
  - `unmark_subset_name(name: str) -> str` (may return `""`)
  - `role_of(name: str, *, is_input: bool = False, is_home: bool = False, editable: bool = True) -> str | None` — one of `"archived" | "input" | "home" | "subset" | None`
  - `input_set_of(name: str, path: str | None, sets: list[dict]) -> str | None` (moved verbatim from `sortify/inputsets.set_of`)
  - `home_name_excluded(name: str, patterns: list[str], emoji: bool) -> bool` (moved verbatim from `sortify/folders.home_name_excluded`)

- [ ] **Step 1: Write the failing tests**

`~/kode/spotify/spotify-ledger/test_playlist_roles.py`:

```python
"""Run: python3 -m unittest discover -s . -p 'test_*.py'"""

import unittest

from playlist_roles import (
    ARCHIVE_PREFIX, home_name_excluded, input_set_of, is_archived, is_subset_name,
    mark_subset_name, role_of, starts_with_emoji, unmark_subset_name,
)

SETS = [{"key": "buffer", "pattern": r"^\[.+\]$"},
        {"key": "other", "pattern": r"^<.*>$"},
        {"key": "the-bomb", "path_segment": "THE BOMB"}]


class TestNames(unittest.TestCase):
    def test_emoji_detection_covers_symbols_and_pictographs(self):
        for n in ("🐾 x", "↗️ MOVE", "👼", "🔈 haze", " 🧸 lungt", "🗄️ x"):
            self.assertTrue(starts_with_emoji(n), n)
        for n in ("", "  ", "tabletop", "{tabletop}", "[Hazy]", "∞ Sound"[1:]):
            self.assertFalse(starts_with_emoji(n), n)

    def test_archived_with_or_without_the_variation_selector(self):
        self.assertTrue(is_archived("🗄️ 🐾 eksotisk"))
        self.assertTrue(is_archived("\U0001F5C4 bare"))
        self.assertTrue(is_archived("  🗄️ spaced"))
        self.assertFalse(is_archived("🐾 tabletop"))
        self.assertFalse(is_archived("tabletop 🗄️"))

    def test_subset_name_is_emoji_first_but_not_archive(self):
        self.assertTrue(is_subset_name("🐾 tabletop"))
        self.assertTrue(is_subset_name(" 🧸 lungt"))
        self.assertTrue(is_subset_name("👼"))
        self.assertFalse(is_subset_name("🗄️ 🐾 eksotisk"))
        self.assertFalse(is_subset_name("{tabletop}"))

    def test_mark(self):
        self.assertEqual(mark_subset_name("tabletop"), "🐾 tabletop")
        self.assertEqual(mark_subset_name("  tabletop "), "🐾 tabletop")
        self.assertEqual(mark_subset_name("🐾 tabletop"), "🐾 tabletop")   # already one
        self.assertEqual(mark_subset_name("🧸 fett"), "🧸 fett")           # any emoji already counts
        self.assertEqual(mark_subset_name("🗄️ 🐾 eksotisk"), "🐾 eksotisk")  # un-archive keeps its emoji
        self.assertEqual(mark_subset_name("🗄️ ↗️ MOVE"), "↗️ MOVE")
        self.assertEqual(mark_subset_name("🗄️ plain"), "🐾 plain")

    def test_unmark(self):
        self.assertEqual(unmark_subset_name("🐾 tabletop"), "tabletop")
        self.assertEqual(unmark_subset_name("↗️ MOVE"), "MOVE")
        self.assertEqual(unmark_subset_name(" 🧸 lungt"), "lungt")
        self.assertEqual(unmark_subset_name("👨‍👩‍👧 family"), "family")  # ZWJ sequence is one cluster
        self.assertEqual(unmark_subset_name("👼"), "")
        self.assertEqual(unmark_subset_name("tabletop"), "tabletop")


class TestRoleOf(unittest.TestCase):
    def test_precedence_archived_input_home_subset(self):
        self.assertEqual(role_of("🗄️ [Hazy]", is_input=True), "archived")
        self.assertEqual(role_of("🗄️ x", is_home=True), "archived")
        self.assertEqual(role_of("🐾 x", is_input=True, is_home=True), "input")
        self.assertEqual(role_of("🐾 x", is_home=True), "home")
        self.assertEqual(role_of("🐾 x"), "subset")
        self.assertIsNone(role_of("plain"))

    def test_a_subset_must_be_ours(self):
        self.assertIsNone(role_of("🅱️arvakt", editable=False))
        self.assertEqual(role_of("🅱️arvakt", editable=True), "subset")


class TestMovedRules(unittest.TestCase):
    def test_input_set_of(self):
        self.assertEqual(input_set_of("[Hazy]", None, SETS), "buffer")
        self.assertEqual(input_set_of(" <ethno> ", None, SETS), "other")
        self.assertEqual(input_set_of("funk · soul", "THE BOMB", SETS), "the-bomb")
        self.assertIsNone(input_set_of("funk", "THE BOMB SQUAD", SETS))
        self.assertIsNone(input_set_of("plain", "ROOT / Hazy", SETS))

    def test_home_name_excluded(self):
        pats = [r"^__.+__$", r"^\{.*\}$", r"^<.*>$"]
        self.assertTrue(home_name_excluded("🐾 x", pats, True))
        self.assertFalse(home_name_excluded("🐾 x", pats, False))
        self.assertTrue(home_name_excluded("{x}", pats, False))
        self.assertFalse(home_name_excluded("DARK SOAR", pats, True))

    def test_archive_prefix_constant(self):
        self.assertEqual(ARCHIVE_PREFIX, "\U0001F5C4️ ")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/kode/spotify/spotify-ledger && python3 -m unittest test_playlist_roles -v`
Expected: `ModuleNotFoundError: No module named 'playlist_roles'`

- [ ] **Step 3: Implement**

`~/kode/spotify/spotify-ledger/playlist_roles.py`:

```python
"""Which role a playlist plays, shared by every app on the account.

Canonical module; playlistRoles.js is its port for spotify-autoqueuer (a
byte-identical copy lives in its src/web/). Pure: no I/O, no clock. The data
these rules run over — input sets, home ids, folder paths — stays in
sortify's data/config.json and data/folders.json.

The roles, in precedence order (the first that applies wins):

  archived  name starts with 🗄 (U+1F5C4, U+FE0F optional) — no role at all,
            so an archived inbox leaves the Now view without leaving its folder
  input     a buffer: explicitly marked, or matching an input set's rule
  home      a filing destination (resolved by sortify from the folder tree)
  subset    name starts with any other emoji, and the playlist is ours

A subset is its NAME since 2026-10-01: marking one puts 🐾 on it, unmarking
takes the leading emoji off (one rename each). It used to be an id list
(`subset_ids`), which other tools could not see without reading sortify's
config and re-implementing its rules.
"""

from __future__ import annotations

import re
import unicodedata

ARCHIVE_EMOJI = "\U0001F5C4"            # 🗄
ARCHIVE_PREFIX = ARCHIVE_EMOJI + "️ "
SUBSET_EMOJI = "\U0001F43E"             # 🐾

_JOINERS = {"︎", "️", "‍", "⃣"}


def _is_emoji_char(c: str) -> bool:
    return ord(c) >= 0x1F000 or unicodedata.category(c) == "So"


def starts_with_emoji(name: str) -> bool:
    """True for names opening with an emoji/symbol."""
    s = (name or "").strip()
    return bool(s) and _is_emoji_char(s[0])


def is_archived(name: str) -> bool:
    return (name or "").strip().startswith(ARCHIVE_EMOJI)


def is_subset_name(name: str) -> bool:
    return starts_with_emoji(name) and not is_archived(name)


def _drop_leading_emoji(s: str) -> str:
    """`s` without its first emoji cluster (emoji + joiners/selectors/ZWJ
    partners) and the whitespace after it."""
    i = 0
    if i < len(s) and _is_emoji_char(s[i]):
        i += 1
        while i < len(s):
            if s[i] in _JOINERS:
                i += 1
                if s[i - 1] == "‍" and i < len(s) and _is_emoji_char(s[i]):
                    i += 1
            elif 0x1F3FB <= ord(s[i]) <= 0x1F3FF:   # skin tone modifier
                i += 1
            else:
                break
    return s[i:].lstrip()


def mark_subset_name(name: str) -> str:
    """The name that makes this playlist a subset.

    An archived name loses its 🗄 and keeps whatever emoji it had under it;
    a plain name gets 🐾; a name already led by an emoji is already a subset.
    """
    s = (name or "").strip()
    if is_archived(s):
        s = _drop_leading_emoji(s)
    if starts_with_emoji(s):
        return s
    return f"{SUBSET_EMOJI} {s}"


def unmark_subset_name(name: str) -> str:
    """The name without its leading emoji. "" when the emoji WAS the name —
    callers must refuse that rename rather than blank the playlist."""
    s = (name or "").strip()
    return _drop_leading_emoji(s) if starts_with_emoji(s) else s


def role_of(name: str, *, is_input: bool = False, is_home: bool = False,
            editable: bool = True) -> str | None:
    """The playlist's role: "archived", "input", "home", "subset" or None.

    `editable=False` (not ours) can never be a subset: we could not mark or
    unmark it. Callers that do not know ownership pass the default.
    """
    if is_archived(name):
        return "archived"
    if is_input:
        return "input"
    if is_home:
        return "home"
    if editable and is_subset_name(name):
        return "subset"
    return None


def input_set_of(name: str, path: str | None, sets: list[dict]) -> str | None:
    """The key of the first input set matching this playlist, or None.

    A set matches EITHER by name pattern (full match on the stripped name) OR
    by a whole folder segment ("THE BOMB" must not match "THE BOMB SQUAD").
    """
    stripped = (name or "").strip()
    segments = (path or "").split(" / ")
    for s in sets:
        pattern = s.get("pattern")
        if pattern and re.fullmatch(pattern, stripped):
            return s["key"]
        segment = s.get("path_segment")
        if segment and segment in segments:
            return s["key"]
    return None


def home_name_excluded(name: str, patterns: list[str], emoji: bool) -> bool:
    """Name shapes that are never filing destinations: an emoji prefix (when
    `emoji`), plus configured regexes such as __start__, {…} and <…>."""
    s = (name or "").strip()
    if emoji and starts_with_emoji(s):
        return True
    return any(re.fullmatch(p, s) for p in patterns)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd ~/kode/spotify/spotify-ledger && python3 -m unittest discover -s . -p 'test_*.py'`
Expected: all OK (the 44 existing + the new ones).

- [ ] **Step 5: Commit**

```bash
cd ~/kode/spotify/spotify-ledger && git add playlist_roles.py test_playlist_roles.py && git commit -m "feat: playlist_roles — one rule for archived, input, home and subset

A subset is a name led by an emoji, 🗄 excepted; 🗄 means archived and no
role at all. Precedence archived > input > home > subset. input_set_of and
home_name_excluded move here from sortify so autoqueuer can share them.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The JS port, and autoqueuer's agreement test

**Files:**
- Create: `~/kode/spotify/spotify-ledger/playlistRoles.js`
- Create: `~/kode/spotify/spotify-autoqueuer/src/web/playlistRoles.js` (byte-identical copy)
- Create: `~/kode/spotify/spotify-autoqueuer/tests/playlistRoles.test.ts`

**Interfaces:**
- Consumes: Task 1's Python module (the agreement test shells out to it).
- Produces (ES module exports): `ARCHIVE_EMOJI`, `SUBSET_EMOJI`, `startsWithEmoji(name)`, `isArchived(name)`, `isSubsetName(name)`, `markSubsetName(name)`, `unmarkSubsetName(name)`, `roleOf(name, { isInput = false, isHome = false, editable = true } = {})`, `inputSetOf(name, path, sets)`.

- [ ] **Step 1: Write the failing test**

`~/kode/spotify/spotify-autoqueuer/tests/playlistRoles.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import {
  isArchived, isSubsetName, markSubsetName, unmarkSubsetName, roleOf, inputSetOf, startsWithEmoji,
} from "../src/web/playlistRoles.js";

// playlistRoles.js is a copy of ~/kode/spotify/spotify-ledger/playlistRoles.js, the port of
// playlist_roles.py. The rules are shared with sortify; these pin the copy and the agreement.

const NAMES = ["🐾 tabletop", "↗️ MOVE", "👼", " 🧸 lungt", "🗄️ 🐾 eksotisk", "\u{1F5C4} bare",
  "🗄️ ↗️ MOVE", "tabletop", "{tabletop}", "[Hazy]", "", "👨‍👩‍👧 family", "∞ Sound Meditation 🕉️"];
const SETS = [{ key: "buffer", pattern: "^\\[.+\\]$" }, { key: "the-bomb", path_segment: "THE BOMB" }];
const canonical = join(homedir(), "kode", "spotify", "spotify-ledger");

describe("playlistRoles", () => {
  it("classifies like the spec says", () => {
    expect(isSubsetName("🐾 tabletop")).toBe(true);
    expect(isSubsetName("🗄️ 🐾 eksotisk")).toBe(false);
    expect(isArchived("\u{1F5C4} bare")).toBe(true);
    expect(startsWithEmoji(" 🧸 lungt")).toBe(true);
    expect(roleOf("🗄️ [Hazy]", { isInput: true })).toBe("archived");
    expect(roleOf("🐾 x", { isHome: true })).toBe("home");
    expect(roleOf("🐾 x")).toBe("subset");
    expect(roleOf("🅱️arvakt", { editable: false })).toBeNull();
    expect(markSubsetName("tabletop")).toBe("🐾 tabletop");
    expect(markSubsetName("🗄️ ↗️ MOVE")).toBe("↗️ MOVE");
    expect(unmarkSubsetName("👼")).toBe("");
    expect(inputSetOf("x", "THE BOMB SQUAD", SETS)).toBeNull();
    expect(inputSetOf("x", "THE BOMB", SETS)).toBe("the-bomb");
  });

  it.skipIf(!existsSync(join(canonical, "playlistRoles.js")))("is byte-identical to the canonical copy", async () => {
    expect(await readFile("src/web/playlistRoles.js", "utf8"))
      .toBe(await readFile(join(canonical, "playlistRoles.js"), "utf8"));
  });

  it.skipIf(!existsSync(join(canonical, "playlist_roles.py")))("agrees with the Python module on every name", () => {
    const py = JSON.parse(execFileSync("python3", ["-c", [
      "import sys, json; sys.path.insert(0, sys.argv[1])",
      "from playlist_roles import *",
      "names = json.loads(sys.argv[2])",
      "print(json.dumps([[starts_with_emoji(n), is_archived(n), is_subset_name(n),",
      "  mark_subset_name(n), unmark_subset_name(n), role_of(n)] for n in names]))",
    ].join("\n"), canonical, JSON.stringify(NAMES)]).toString());
    const js = NAMES.map((n) => [startsWithEmoji(n), isArchived(n), isSubsetName(n),
      markSubsetName(n), unmarkSubsetName(n), roleOf(n)]);
    expect(js).toEqual(py);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd ~/kode/spotify/spotify-autoqueuer && npx vitest run tests/playlistRoles.test.ts`
Expected: FAIL — cannot resolve `../src/web/playlistRoles.js`.

- [ ] **Step 3: Implement the canonical JS, then copy it**

`~/kode/spotify/spotify-ledger/playlistRoles.js`:

```js
// Which role a playlist plays, shared by every app on the account.
//
// Port of ~/kode/spotify/spotify-ledger/playlist_roles.py (canonical) — see its
// docstring for the rules. A byte-identical copy lives in spotify-autoqueuer's
// src/web/, served to the browser as a plain ES module (no build step), and a
// vitest there pins both the copy and its agreement with the Python.
//
// Precedence: archived (🗄 first) > input > home > subset (any other emoji first,
// and ours). A subset is its NAME: marking adds 🐾, unmarking drops the emoji.

export const ARCHIVE_EMOJI = "\u{1F5C4}";
export const SUBSET_EMOJI = "\u{1F43E}";

const JOINERS = new Set(["︎", "️", "‍", "⃣"]);
const SYMBOL = /^\p{So}$/u;

function isEmojiChar(c) {
  return c.codePointAt(0) >= 0x1F000 || SYMBOL.test(c);
}

// Python's str.strip(): whitespace by Unicode, which is what JS trim() also uses.
const first = (s) => [...s][0];

export function startsWithEmoji(name) {
  const s = (name || "").trim();
  return !!s && isEmojiChar(first(s));
}

export function isArchived(name) {
  return (name || "").trim().startsWith(ARCHIVE_EMOJI);
}

export function isSubsetName(name) {
  return startsWithEmoji(name) && !isArchived(name);
}

function dropLeadingEmoji(s) {
  const cps = [...s];
  let i = 0;
  if (i < cps.length && isEmojiChar(cps[i])) {
    i += 1;
    while (i < cps.length) {
      if (JOINERS.has(cps[i])) {
        i += 1;
        if (cps[i - 1] === "‍" && i < cps.length && isEmojiChar(cps[i])) i += 1;
      } else if (cps[i].codePointAt(0) >= 0x1F3FB && cps[i].codePointAt(0) <= 0x1F3FF) {
        i += 1;
      } else {
        break;
      }
    }
  }
  return cps.slice(i).join("").trimStart();
}

export function markSubsetName(name) {
  let s = (name || "").trim();
  if (isArchived(s)) s = dropLeadingEmoji(s);
  if (startsWithEmoji(s)) return s;
  return `${SUBSET_EMOJI} ${s}`;
}

export function unmarkSubsetName(name) {
  const s = (name || "").trim();
  return startsWithEmoji(s) ? dropLeadingEmoji(s) : s;
}

export function roleOf(name, { isInput = false, isHome = false, editable = true } = {}) {
  if (isArchived(name)) return "archived";
  if (isInput) return "input";
  if (isHome) return "home";
  if (editable && isSubsetName(name)) return "subset";
  return null;
}

// Python's re.fullmatch: the patterns sortify uses are plain enough to mean the
// same in JS. One that does not compile here matches nothing rather than throwing.
export function inputSetOf(name, path, sets) {
  const stripped = (name || "").trim();
  const segments = (path || "").split(" / ");
  for (const s of sets || []) {
    if (s.pattern) {
      let re = null;
      try { re = new RegExp(`^(?:${s.pattern})$`); } catch { /* not valid JS regex */ }
      if (re && re.test(stripped)) return s.key;
    }
    if (s.path_segment && segments.includes(s.path_segment)) return s.key;
  }
  return null;
}
```

Then: `cp ~/kode/spotify/spotify-ledger/playlistRoles.js ~/kode/spotify/spotify-autoqueuer/src/web/playlistRoles.js`

- [ ] **Step 4: Run to verify it passes**

Run: `cd ~/kode/spotify/spotify-autoqueuer && npx vitest run tests/playlistRoles.test.ts`
Expected: 3 passed. If the agreement test fails on `∞ Sound Meditation 🕉️` (U+221E is category Sm, not So), both sides must say `false` — fix whichever side differs, never the test's name list.

- [ ] **Step 5: Commit both repos**

```bash
cd ~/kode/spotify/spotify-ledger && git add playlistRoles.js && git commit -m "feat: playlistRoles.js — the browser port of playlist_roles

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
cd ~/kode/spotify/spotify-autoqueuer && git add src/web/playlistRoles.js tests/playlistRoles.test.ts && git commit -m "feat: copy in the shared playlist role rules, pinned to the Python

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: autoqueuer's library view uses the shared rule

**Files:**
- Modify: `~/kode/spotify/spotify-autoqueuer/src/server/curated.ts` (whole file's `CuratedSets` / `readCuratedSets`)
- Modify: `~/kode/spotify/spotify-autoqueuer/src/web/libraryView.js:12-35` (`resolveRoles`)
- Modify: `~/kode/spotify/spotify-autoqueuer/src/web/library.js:12,104` (stop reading `subsetIds` from `/api/curated`)
- Test: `tests/curated.test.ts`, `tests/libraryView.test.ts`

**Interfaces:**
- Consumes: `roleOf`, `inputSetOf` from `./playlistRoles.js` (Task 2).
- Produces: `/api/curated` returns `{ homeIds: string[], inputIds: string[], inputSets: {key, pattern?, path_segment?}[], folderPaths: Record<string,string> }`. `resolveRoles({ all, homeIds, inputIds, inputSets, folderPaths })` returns `{ homeIds, subsetIds }` (same return shape as today, so `scopeCounts`/`buildSections` are untouched).

- [ ] **Step 1: Rewrite the tests**

In `tests/curated.test.ts` replace every expected `{ homeIds, subsetIds, inputIds, inputPatterns }` with the new shape. The first test becomes:

```ts
  it("returns home_ids, explicit inputs, input sets and folder paths; subset_ids is ignored", async () => {
    const path = await tmpFile(JSON.stringify({
      home_ids: ["h1", "h2"], subset_ids: ["s1"], input_ids: ["i1"],
      input_sets: [{ key: "buffer", pattern: "^\\[.+\\]$" }], client_id: "irrelevant",
    }));
    await writeFile(join(dirname(path), "folders.json"), JSON.stringify({ x1: { path: "THE BOMB" } }));
    expect(await readCuratedSets(path)).toEqual({
      homeIds: ["h1", "h2"], inputIds: ["i1"],
      inputSets: [{ key: "buffer", pattern: "^\\[.+\\]$" }], folderPaths: { x1: "THE BOMB" },
    });
  });
```

and the empty case everywhere is `{ homeIds: [], inputIds: [], inputSets: [], folderPaths: {} }`. Keep the legacy `input_name_pattern` test, expecting `inputSets: [{ key: "buffer", pattern: <it> }]`. Delete assertions about folder-segment ids being folded into `inputIds` — that now happens in `resolveRoles`.

In `tests/libraryView.test.ts`, rename the fixture so the subset is identified by name: `p("s1", "🐾 Mellow subset")`, add `p("a1", "🗄️ 🐾 Old one")`, drop the `subsetIds` constant, and compute it once:

```ts
const { homeIds, subsetIds } = resolveRoles({ all, homeIds: ["h1"], inputIds: [], inputSets: [], folderPaths: {} });
```

Replace the `resolveRoles` describe block with:

```ts
describe("resolveRoles (shared rule: archived > input > home > subset)", () => {
  const lib = [p("h1", "Home"), p("s1", "🐾 Sub"), p("b1", "[Inbox]"), p("t1", "funk"),
    p("a1", "🗄️ 🐾 Gone"), p("hs", "🐾 Also home"), p("n1", "plain")];
  const sets = [{ key: "buffer", pattern: "^\\[.+\\]$" }, { key: "the-bomb", path_segment: "THE BOMB" }];

  it("subsets come from the name, and archived lists are in no group", () => {
    expect(resolveRoles({ all: lib, homeIds: ["h1"], inputIds: [], inputSets: [], folderPaths: {} }))
      .toEqual({ homeIds: ["h1"], subsetIds: ["s1", "hs"] });
  });

  it("an input is never a home or a subset — explicit, pattern or folder", () => {
    const r = resolveRoles({ all: lib, homeIds: ["h1", "t1"], inputIds: ["s1"], inputSets: sets,
      folderPaths: { t1: "THE BOMB" } });
    expect(r).toEqual({ homeIds: ["h1"], subsetIds: ["hs"] });
  });

  it("a home is never a subset, and an archived home is no home", () => {
    expect(resolveRoles({ all: lib, homeIds: ["hs", "a1"], inputIds: [], inputSets: [], folderPaths: {} }))
      .toEqual({ homeIds: ["hs"], subsetIds: ["s1"] });
  });
});
```

Update the remaining `buildSections`/`scopeCounts` tests' expected Subsets items to `p("s1", "🐾 Mellow subset")`.

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/kode/spotify/spotify-autoqueuer && npx vitest run tests/curated.test.ts tests/libraryView.test.ts`
Expected: FAIL (old shapes).

- [ ] **Step 3: Implement**

`src/server/curated.ts` — replace the interface and `readCuratedSets` (keep `defaultSortifyConfigPath`, `idList`, `inputSets`, `readJson`), and rewrite the header comment's role paragraph to: "Roles are resolved in the browser by the shared playlistRoles.js (archived > input > home > subset); this only ships the data those rules run over."

```ts
export interface CuratedSets {
  homeIds: string[];
  inputIds: string[];                   // explicitly marked inputs only
  inputSets: InputSet[];                // sortify's input_sets (or the legacy single pattern)
  folderPaths: Record<string, string>;  // playlistId -> "A / B", from folders.json
}

const EMPTY: CuratedSets = { homeIds: [], inputIds: [], inputSets: [], folderPaths: {} };

export async function readCuratedSets(path: string): Promise<CuratedSets> {
  const cfg = await readJson(path);
  if (!cfg) return { ...EMPTY, folderPaths: {} };
  const folders = (await readJson(join(dirname(path), "folders.json"))) ?? {};
  const folderPaths: Record<string, string> = {};
  for (const [id, entry] of Object.entries(folders)) {
    const p = entry && typeof entry === "object" ? (entry as { path?: unknown }).path : null;
    if (typeof p === "string") folderPaths[id] = p;
  }
  return { homeIds: idList(cfg.home_ids), inputIds: idList(cfg.input_ids), inputSets: inputSets(cfg), folderPaths };
}
```

Change `interface InputSet { pattern?: unknown; path_segment?: unknown }` to `export interface InputSet { key?: string; pattern?: string; path_segment?: string }` and make `inputSets()` return the legacy pattern as `[{ key: "buffer", pattern: cfg.input_name_pattern }]`.

`src/web/libraryView.js` — replace `resolveRoles` and its doc comment:

```js
import { roleOf, inputSetOf } from "./playlistRoles.js";

/** sortify's roles via the shared rule (playlistRoles.js — archived > input > home > subset).
 *  Subsets are names led by an emoji; 🗄️ lists are in no group. We do not know ownership, so
 *  every emoji-led playlist counts — sortify additionally requires it to be yours. */
export function resolveRoles({ all, homeIds = [], inputIds = [], inputSets = [], folderPaths = {} }) {
  const explicit = new Set(inputIds);
  const homeSet = new Set(homeIds);
  const homes = [], subsets = [];
  for (const p of all) {
    const role = roleOf(p.name, {
      isInput: explicit.has(p.id) || !!inputSetOf(p.name, folderPaths[p.id], inputSets),
      isHome: homeSet.has(p.id),
    });
    if (role === "home") homes.push(p.id);
    else if (role === "subset") subsets.push(p.id);
  }
  return { homeIds: homes, subsetIds: subsets };
}
```

Note: `homeIds` in config that are not in `all` disappear from the result. That matches the old behaviour's effect on the grid (only playlists in `all` are ever drawn); `scopeCounts` counted `all` only already.

`src/web/library.js`: line 12 comment becomes `let subsetIds = []; // resolved from names by the shared rule`; line 104 stays `({ homeIds, subsetIds } = resolveRoles({ all, ...curated }));` — it now receives the new curated shape. Also update the comment at top of `CuratedSets` usage in `src/server/app.ts:32` to drop "subset ids".

- [ ] **Step 4: Run all autoqueuer tests and the typecheck**

Run: `cd ~/kode/spotify/spotify-autoqueuer && npx vitest run && npx tsc --noEmit -p .`
Expected: all pass, no type errors.

- [ ] **Step 5: Commit**

```bash
cd ~/kode/spotify/spotify-autoqueuer && git add -A src tests && git commit -m "feat: the library's Subsets group comes from names, via the shared rule

/api/curated stops shipping subset_ids and ships the data the rule needs
(explicit inputs, input sets, folder paths); resolveRoles runs roleOf over
each playlist. 🗄️ lists fall out of every group.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: sortify resolves roles by the shared rule

**Files:**
- Create: symlink `sortify/playlist_roles.py -> ../../spotify-ledger/playlist_roles.py`
- Modify: `sortify/folders.py:15-32` (`starts_with_emoji`, `home_name_excluded` become re-exports)
- Modify: `sortify/inputsets.py:43-56` (`set_of` becomes a re-export of `input_set_of`)
- Modify: `sortify/app.py:240-275` (`playlists()` role), `:598-608` (`_effective_input_ids`), `:610-635` (`_effective_subset_ids`), `:822-838` (`_subset_targets_payload` docstring)
- Test: `tests/test_subsets.py` (rewrite), plus fixture updates in `tests/test_act_profile_sync.py`, `test_homeless.py`, `test_remove_from_home.py`, `test_sweep_inputs.py`, `test_listing_totals.py`, `test_input_counts.py`, `test_quick_adds.py`, `test_create_filing.py`, `test_create_subset.py`

**Interfaces:**
- Consumes: `playlist_roles.role_of`, `is_archived`, `starts_with_emoji`, `home_name_excluded`, `input_set_of` (Task 1).
- Produces: `_effective_subset_ids(cfg, playlists) -> set[str]` (same signature, now name-based); `_effective_input_ids(cfg, playlists) -> set[str]` (now drops archived); `/api/playlists` rows carry `role` (`None` for archived), new `archived: bool`, and `subset_eligible: bool` = editable and role is `None` or `"subset"`.

- [ ] **Step 1: Link the module and re-export**

```bash
cd ~/kode/spotify/sortify && ln -s ../../spotify-ledger/playlist_roles.py sortify/playlist_roles.py && git add sortify/playlist_roles.py
```

`sortify/folders.py` — delete the bodies of `starts_with_emoji` and `home_name_excluded` and replace with (keep `import re`, `import unicodedata` only if still used elsewhere in the file; run the tests to find out):

```python
# The name rules are shared with spotify-autoqueuer (playlist_roles.py, symlinked
# from ~/kode/spotify/spotify-ledger). Re-exported so existing imports keep working.
from .playlist_roles import home_name_excluded, starts_with_emoji  # noqa: F401
```

`sortify/inputsets.py` — replace `set_of`'s body:

```python
from .playlist_roles import input_set_of


def set_of(name: str, path: str | None, sets: list[dict]) -> str | None:
    """The key of the first set matching this playlist, or None (shared rule)."""
    return input_set_of(name, path, sets)
```

Run: `.venv/bin/pytest -q` — Expected: still all green (pure move).

- [ ] **Step 2: Rewrite `tests/test_subsets.py` (failing)**

Read the current file first; keep its fixture style (it builds `appmod.store` config and a cached `playlist_list`). Replace every test that sets `subset_ids` with name-based equivalents. The required cases:

```python
def test_an_emoji_led_playlist_of_ours_is_a_subset(listing):
    listing([pl("S1", "🐾 best of"), pl("P1", "plain")])
    assert appmod._effective_subset_ids(appmod.store.config(), items()) == {"S1"}


def test_archived_is_no_subset_and_no_input(listing, config):
    config(input_ids=["A1"])
    listing([pl("A1", "🗄️ [Old inbox]"), pl("A2", "🗄️ 🐾 old best")])
    cfg, its = appmod.store.config(), items()
    assert appmod._effective_subset_ids(cfg, its) == set()
    assert appmod._effective_input_ids(cfg, its) == set()


def test_not_ours_is_no_subset(listing):
    listing([pl("X1", "🅱️arvakt", editable=False)])
    assert appmod._effective_subset_ids(appmod.store.config(), items()) == set()


def test_home_and_input_beat_subset(listing, config):
    config(home_ids=["H1"], input_ids=["I1"])
    listing([pl("H1", "🐾 home-ish"), pl("I1", "🐾 inbox-ish")])
    assert appmod._effective_subset_ids(appmod.store.config(), items()) == set()


def test_playlists_view_marks_role_archived_and_eligibility(client, listing):
    listing([pl("S1", "🐾 a"), pl("A1", "🗄️ b"), pl("P1", "c"), pl("X1", "d", editable=False)])
    rows = {r["id"]: r for r in client.get("/api/playlists").json()["playlists"]}
    assert (rows["S1"]["role"], rows["S1"]["subset_eligible"]) == ("subset", True)
    assert (rows["A1"]["role"], rows["A1"]["archived"]) == (None, True)
    assert rows["P1"]["subset_eligible"] is True
    assert rows["X1"]["subset_eligible"] is False


def test_subset_ids_in_config_is_ignored(listing, config):
    config(subset_ids=["P1"])
    listing([pl("P1", "plain")])
    assert appmod._effective_subset_ids(appmod.store.config(), items()) == set()
```

(`pl`, `items`, `listing`, `config`, `client` are the helpers/fixtures already in that file or `conftest.py`; where a name differs, use the file's own — do not invent a second set of helpers.) Delete the old tests whose whole point was the opt-in list (marking by id, dropping a stale id).

Run: `.venv/bin/pytest -q tests/test_subsets.py` — Expected: FAIL on the new cases.

- [ ] **Step 3: Implement in `sortify/app.py`**

Add near the other local imports: `from . import playlist_roles as roles`.

`_effective_input_ids` (line ~598):

```python
def _effective_input_ids(cfg: dict, playlists: list[dict]) -> set[str]:
    """Explicitly marked inputs plus everything matching any input SET, minus
    anything archived (🗄️ first — no role at all, playlist_roles.py).

    Set rules (name patterns like ^\\[.+\\]$, or a folder segment such as
    THE BOMB) live in config so a stale browser tab saving roles can never
    un-mark the real inputs. See sortify/inputsets.py.
    """
    ids = set(cfg.get("input_ids", [])) | inputsets.matched_ids(playlists, store.folders(), cfg)
    archived = {p["id"] for p in playlists if roles.is_archived(p.get("name", ""))}
    return ids - archived
```

`_effective_subset_ids` (line ~610), docstring included:

```python
def _effective_subset_ids(cfg: dict, playlists: list[dict]) -> set[str]:
    """The subsets: playlists of ours whose name starts with an emoji other
    than 🗄️ (playlist_roles.py, shared with spotify-autoqueuer).

    Since 2026-10-01 the NAME is the whole definition. It used to be an
    opt-in id list (`subset_ids`) because the Lists chip could only mark the
    ~200 rows it draws; marking is now a rename the chip does itself, so the
    name reaches every playlist and other tools can see it without reading
    sortify's config. Inputs and homes still win, and archived is no role.
    """
    inputs = _effective_input_ids(cfg, playlists)
    homes = set(cfg.get("home_ids") or [])
    return {
        p["id"] for p in playlists
        if roles.role_of(p.get("name", ""), is_input=p["id"] in inputs,
                         is_home=p["id"] in homes, editable=bool(p.get("editable"))) == "subset"
    }
```

In `playlists()` (line ~260), replace the role / `subset_eligible` block:

```python
    for p in out:
        p["folder"] = (folders.get(p["id"]) or {}).get("path")
        role = roles.role_of(
            p["name"], is_input=p["id"] in inputs,
            is_home=p["id"] in cfg.get("home_ids", []),
            editable=bool(p.get("editable")) and p["id"] != LIKED_ID)
        p["archived"] = role == "archived"
        p["role"] = None if role == "archived" else role
        # The Subset chip renames: 🐾 on, leading emoji off. Only on our own
        # playlists, and never on a home or input — those roles win anyway.
        p["subset_eligible"] = (bool(p.get("editable")) and p["id"] != LIKED_ID
                                and role in (None, "subset"))
```

and delete the now-unused `subsets = _effective_subset_ids(cfg, items)` line above the loop.

Rewrite `_subset_targets_payload`'s docstring first paragraph to: "The subsets — the Add-to-subset picker's list: every playlist of ours named with an emoji (🗄️ excepted). See `_effective_subset_ids`."

- [ ] **Step 4: Run the whole suite and fix fixtures**

Run: `.venv/bin/pytest -q`
Expected failures are tests elsewhere that marked a subset via `subset_ids` (files listed above). In each, give the fixture playlist an emoji-led name instead (e.g. `"{best}"` → `"🐾 best"`) and drop the `subset_ids` key. Do not change what those tests assert. Re-run until green.

- [ ] **Step 5: Commit**

```bash
cd ~/kode/spotify/sortify && git add -A sortify tests && git commit -m "feat: subsets are emoji-led names, archived lists have no role

Roles come from the shared playlist_roles module (symlinked from
spotify-ledger): archived > input > home > subset. subset_ids is no longer
read. folders/inputsets re-export the moved rules.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Marking is renaming — endpoint, chip, config save

**Files:**
- Modify: `sortify/app.py:80-90` (`ConfigIn` — remove `subset_ids`), `:428-440` (`set_config` — remove the `subset_ids=` line and its comment), add endpoint after `apply_naming_rename` (~line 425)
- Modify: `sortify/static/app.js:229-236` (`subsetChipHidden` comment), `:289-292` (`bSubset.onclick`), `:509-512` (`saveConfig`)
- Test: create `tests/test_mark_subset.py`; modify `tests/ui_harness.mjs:2481-2508`

**Interfaces:**
- Consumes: `roles.mark_subset_name`, `roles.unmark_subset_name`, `roles.role_of` (Task 1); `_effective_input_ids` (Task 4); `sp.rename_playlist(id, name)` (existing, 1 call, patches the cached listing).
- Produces: `POST /api/playlists/{playlist_id}/subset` body `{"on": bool}` → `200 {"playlist_id", "name", "role"}`; errors 404 (not in listing), 400 (not ours / would empty the name), 409 (it is a home or input).

- [ ] **Step 1: Write the failing pytest**

`tests/test_mark_subset.py` (use the same fixture helpers as `tests/test_subsets.py`; the rename is stubbed so no call is made):

```python
"""Marking a subset is a rename (1 call), and unmarking is too."""

import pytest
from fastapi.testclient import TestClient

from sortify import app as appmod
from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)


@pytest.fixture
def renames(monkeypatch):
    calls = []
    def fake(pid, name):
        calls.append((pid, name))
        cache = appmod.store.cache()
        for p in cache["playlist_list"]["items"]:
            if p["id"] == pid:
                p["name"] = name
        appmod.store.save_cache(cache)
    monkeypatch.setattr(appmod.sp, "rename_playlist", fake)
    return calls


def seed(playlists, **cfg):
    cache = appmod.store.cache()
    cache["playlist_list"] = {"fetched_at": 0, "items": playlists}
    appmod.store.save_cache(cache)
    appmod.store.update_config(home_ids=[], input_ids=[], input_sets=[], **cfg)


def pl(pid, name, editable=True):
    return {"id": pid, "name": name, "editable": editable, "total": 0, "snapshot_id": "s",
            "owner": "me", "image": None, "description": ""}


def post(pid, on):
    return TestClient(appmod.app).post(f"/api/playlists/{pid}/subset", json={"on": on})


def test_marking_puts_a_paw_on_the_name(renames):
    seed([pl("P1", "tabletop")])
    r = post("P1", True)
    assert r.status_code == 200 and r.json() == {"playlist_id": "P1", "name": "🐾 tabletop", "role": "subset"}
    assert renames == [("P1", "🐾 tabletop")]


def test_unmarking_takes_the_emoji_off(renames):
    seed([pl("P1", "🐾 tabletop")])
    assert post("P1", False).json()["name"] == "tabletop"


def test_marking_an_archived_list_unarchives_it(renames):
    seed([pl("A1", "🗄️ 🐾 eksotisk")])
    assert post("A1", True).json() == {"playlist_id": "A1", "name": "🐾 eksotisk", "role": "subset"}


def test_already_marked_costs_nothing(renames):
    seed([pl("P1", "🧸 fett")])
    assert post("P1", True).status_code == 200 and renames == []


def test_an_emoji_only_name_cannot_be_unmarked(renames):
    seed([pl("P1", "👼")])
    r = post("P1", False)
    assert r.status_code == 400 and renames == []


def test_not_ours_is_refused(renames):
    seed([pl("X1", "plain", editable=False)])
    assert post("X1", True).status_code == 400 and renames == []


def test_a_home_or_input_is_refused(renames):
    seed([pl("H1", "DARK SOAR"), pl("I1", "[Hazy]")], home_ids=["H1"],
         input_sets=[{"key": "buffer", "pattern": r"^\[.+\]$"}])
    assert post("H1", True).status_code == 409
    assert post("I1", True).status_code == 409
    assert renames == []


def test_unknown_playlist_is_404(renames):
    seed([])
    assert post("NOPE", True).status_code == 404


def test_a_stale_tab_posting_subset_ids_is_ignored():
    seed([pl("P1", "plain")])
    r = TestClient(appmod.app).post("/api/config", json={
        "input_ids": [], "home_ids": [], "home_hints": {}, "subset_ids": ["P1"]})
    assert r.status_code == 200
    assert "subset_ids" not in appmod.store.config() or appmod.store.config()["subset_ids"] != ["P1"]
```

Run: `.venv/bin/pytest -q tests/test_mark_subset.py` — Expected: FAIL (404 route not found for the endpoint tests; the stale-tab test currently writes `subset_ids`).

- [ ] **Step 2: Implement the endpoint and drop `subset_ids` from config saves**

In `sortify/app.py`, after `apply_naming_rename`:

```python
class SubsetMarkIn(BaseModel):
    on: bool


@app.post("/api/playlists/{playlist_id}/subset")
def mark_subset(playlist_id: str, body: SubsetMarkIn):
    """Make a playlist a subset, or stop it being one — by renaming it.

    A subset is its name (an emoji first, 🗄️ excepted; playlist_roles.py), so
    marking puts 🐾 on and unmarking takes the leading emoji off. One Spotify
    call, none when the name already says so. Reads the cached listing only.
    """
    cfg = store.config()
    listing = (store.cache().get("playlist_list") or {}).get("items") or []
    p = next((x for x in listing if x["id"] == playlist_id), None)
    if p is None:
        raise HTTPException(404, "that playlist is not in the cached listing — Refresh first")
    if not p.get("editable"):
        raise HTTPException(400, "not yours to rename, so it cannot be marked a subset")
    inputs = _effective_input_ids(cfg, listing)
    is_home = playlist_id in (cfg.get("home_ids") or [])
    if body.on and (playlist_id in inputs or is_home):
        raise HTTPException(
            409, f"{p['name']!r} is {'an input' if playlist_id in inputs else 'a home'} — "
                 "that role wins over subset, so a 🐾 would change nothing")
    new = roles.mark_subset_name(p["name"]) if body.on else roles.unmark_subset_name(p["name"])
    if not new.strip():
        raise HTTPException(400, f"{p['name']!r} is only an emoji — removing it would leave no name")
    if new != p["name"]:
        sp.rename_playlist(playlist_id, new)
    role = roles.role_of(new, is_input=playlist_id in inputs, is_home=is_home, editable=True)
    return {"playlist_id": playlist_id, "name": new, "role": None if role == "archived" else role}
```

In `ConfigIn`, delete the `subset_ids` field and its comment. In `set_config`, delete `subset_ids=sorted(set(body.subset_ids)),` and replace the "Marking subsets used to be refused…" comment with: `# Subsets are not saved here: a subset is its name (the Subset chip renames).` Pydantic ignores unknown fields, so a stale tab's `subset_ids` is dropped.

Run: `.venv/bin/pytest -q tests/test_mark_subset.py` — Expected: PASS.

- [ ] **Step 3: Update the UI harness (failing)**

In `tests/ui_harness.mjs`, replace the two `SM saving sends subset_ids` / `SM and does not put it in home_ids` checks (lines ~2500-2508) with:

```js
  run(`roles["s1"] = "subset"`);
  await run(`saveConfig()`);
  await tick();
  const sent = bodies("/api/config").slice(-1)[0];
  check("SM saving roles no longer sends subset_ids — a subset is its name",
        sent && !("subset_ids" in sent), JSON.stringify(sent));
  check("SM and does not put it in home_ids",
        !(sent.home_ids || []).includes("s1"), JSON.stringify(sent.home_ids));
```

Append a new block before the `// ---- summary` line:

```js
// ============================================================================
// SR — the Subset chip renames: 🐾 on, the leading emoji off, one call each.
{
  routes["GET /api/playlists"] = { status: 200, body: {
    playlists: [{ id: "PS1", name: "tabletop", role: null, editable: true, total: 3,
                  subset_eligible: true, folder: null, split: null, hints: "" }],
    folder_paths: [], create_folders: {} } };
  routes["POST /api/playlists/PS1/subset"] = { status: 200, body:
    { playlist_id: "PS1", name: "🐾 tabletop", role: "subset" } };
  try {
    run(`show("lists")`);
    await run(`loadPlaylists()`);
    await tick();
    const row = $$("pl-list").children.find((c) => /tabletop/.test(c.innerHTML));
    const chip = row?.querySelectorAll("button")[2];
    resetLog();
    await chip.onclick();
    await tick();
    const b = bodies("/api/playlists/PS1/subset").slice(-1)[0];
    check("SR tapping Subset posts on:true for that playlist", b && b.on === true, JSON.stringify(b));
    check("SR ...and the row shows the new name", /🐾 tabletop/.test(row.innerHTML), row.innerHTML.slice(0, 200));
    check("SR ...and the chip is on", run(`roles["PS1"]`) === "subset", String(run(`roles["PS1"]`)));
  } finally {
    run(`roles = {}`);
  }
}
```

Before writing this block, read how the existing Lists-view blocks load playlists (search the harness for `loadPlaylists` or the function that fetches `/api/playlists`, and for the id of the list container) and use those exact names — `loadPlaylists` and `pl-list` above are the expected names; correct them to the real ones if they differ. Run: `node tests/ui_harness.mjs` — Expected: the SR checks FAIL.

- [ ] **Step 4: Implement the chip**

`sortify/static/app.js` — `bSubset.onclick` (line ~289):

```js
  // A subset is its NAME (an emoji first, 🗄️ excepted — shared with
  // spotify-autoqueuer), so the chip renames on the spot: one call, no Save.
  bSubset.onclick = async () => {
    const on = roles[p.id] !== "subset";
    bSubset.disabled = true;
    try {
      const res = await api(`/api/playlists/${encodeURIComponent(p.id)}/subset`, { on });
      p.name = res.name;
      roles[p.id] = res.role === "subset" ? "subset" : null;
      row.querySelector(".name").textContent = res.name;
    } catch (e) {
      toast(e.message);
    } finally {
      bSubset.disabled = false;
      paint();
    }
  };
```

`saveConfig` (line ~509): delete the `subset_ids` line and send `{ input_ids, home_ids, home_hints: hintTexts }`. Update the `subsetChipHidden` comment's last sentence to say eligibility is "ours, and not a home or input".

Run: `node tests/ui_harness.mjs && .venv/bin/pytest -q` — Expected: both green.

- [ ] **Step 5: Commit**

```bash
cd ~/kode/spotify/sortify && git add -A sortify tests && git commit -m "feat: the Subset chip renames — 🐾 on, the leading emoji off

POST /api/playlists/{id}/subset is one rename (none when the name already
says so). Refuses an emoji-only name, a playlist not ours, and a home or
input. /api/config no longer takes subset_ids.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Creating subsets and explore lists, and retiring `subset_ids`

**Files:**
- Modify: `sortify/app.py:452-560` (`create_playlist_api` — subset branch and docstring), `:3698-3745` (`explore`)
- Modify: `data/config.json` (data, not committed: delete `subset_ids`, set explore `create_name`)
- Test: `tests/test_create_subset.py`, `tests/test_quick_adds.py`

**Interfaces:**
- Consumes: `roles.mark_subset_name` (Task 1).
- Produces: a subset created through `/api/playlists/create` or `/api/explore` is named `mark_subset_name(<typed>)`; nothing writes `subset_ids` any more.

- [ ] **Step 1: Write the failing tests**

In `tests/test_create_subset.py`, replace assertions that the new id lands in `subset_ids` with:

```python
def test_a_new_subset_is_created_with_a_paw(client, created):
    r = client.post("/api/playlists/create", json={"name": "tabletop", "role": "subset"})
    assert r.status_code == 200
    assert created[-1] == "🐾 tabletop"                 # the name sent to Spotify
    assert r.json()["playlist"]["name"] == "🐾 tabletop"
    assert "subset_ids" not in appmod.store.config() or not appmod.store.config()["subset_ids"]


def test_a_typed_emoji_name_is_kept(client, created):
    client.post("/api/playlists/create", json={"name": "🧸 cosy", "role": "subset"})
    assert created[-1] == "🧸 cosy"
```

(`created` = the file's existing stub of `sp.create_playlist_full` recording names; reuse its real name.) In `tests/test_quick_adds.py`, the explore-create test asserts the created name is `mark_subset_name(create_name)` — for `create_name: "Utforsk"` that is `"🐾 Utforsk"` — and drops any `subset_ids` assertion.

Run: `.venv/bin/pytest -q tests/test_create_subset.py tests/test_quick_adds.py` — Expected: FAIL.

- [ ] **Step 2: Implement**

In `create_playlist_api`, right after `name = body.name.strip()`:

```python
    if subset:
        # A subset is its name (playlist_roles.py): created with 🐾 unless it
        # already starts with an emoji.
        name = roles.mark_subset_name(name)
```

and pass `name` (not `body.name`) to `creatable_home_name_problem`. Delete the `if subset: store.update_config(subset_ids=...)` branch (keep the `elif is_input` / `else` branches, turning `elif` into `if`). Rewrite the docstring paragraph beginning "The two roles differ in exactly two places" to: "A subset is never a filing destination, so it is never marked home/sticky, and it is created with an emoji-led name — which is what makes it a subset (playlist_roles.py). The home name rules are not applied to it; the input pattern still is."

In `explore`, before `sp.create_playlist_full(name)`: `name = roles.mark_subset_name(name)`; in the `store.update_config(...)` call delete the `subset_ids=` argument; update the docstring's "marks it a subset" to "names it as a subset (🐾)".

Run: `.venv/bin/pytest -q` — Expected: green.

- [ ] **Step 3: Retire `subset_ids` from the live config (data)**

Verify first that every listed id is already a subset by name, then remove the key and tidy the explore name. Zero Spotify calls:

```bash
cd ~/kode/spotify/sortify && .venv/bin/python - <<'EOF'
import json, sys
sys.path.insert(0, ".")
from sortify.store import Store
from sortify import playlist_roles as roles
s = Store(); cfg = s.config()
names = {p["id"]: p for p in s.cache()["playlist_list"]["items"]}
bad = [i for i in cfg.get("subset_ids", []) if not roles.is_subset_name(names.get(i, {}).get("name", ""))]
assert not bad, f"not subset-named yet: {bad}"
cfg.pop("subset_ids", None)
for e in cfg.get("quick_adds") or []:
    if e.get("key") == "explore":
        e["create_name"] = "utforsk"
s.save_config(cfg)
print("subset_ids removed; explore create_name =", [e.get("create_name") for e in cfg["quick_adds"] if e["key"] == "explore"])
EOF
```

Expected: `subset_ids removed; explore create_name = ['utforsk']`. If the assert fires, stop and report the ids — do not rename anything from here.

- [ ] **Step 4: Commit (code only; data/ is not tracked)**

```bash
cd ~/kode/spotify/sortify && git add -A sortify tests && git commit -m "feat: new subsets and explore lists are created with a paw

Nothing writes subset_ids any more; the live config's list is retired.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Filing new buffers and homes

**Files:**
- Modify: `sortify/foldermove.py:89-127` (extract `leaf_collisions`)
- Modify: `sortify/filing.py:113-125` (log failures)
- Modify: `sortify/app.py:3862-3910` (`_suggestion_payload` — add `home_folders`, `home_folder_default`), add helper `_home_folder_choices`
- Modify: `sortify/static/app.js:2605-2618` (`nowCreateAndFile`), `:3026-3055` (picker create row), `sortify/static/style.css` (chip row)
- Modify: `data/config.json` (data: `create_folders.input = "[Filter]"`)
- Test: `tests/test_create_filing.py`, `tests/test_foldermove.py` (or the existing mover test file — find it with `grep -l _check_leaf_unique tests/`), `tests/ui_harness.mjs`

**Interfaces:**
- Produces:
  - `foldermove.leaf_collisions(paths: list[str], dest_path: str) -> list[str]` — the other folder paths the client's search would also offer; `_check_leaf_unique(tree, dest)` raises when it is non-empty (unchanged behaviour).
  - `_home_folder_choices(cfg: dict, top_home_id: str | None) -> tuple[list[dict], str | None]` → `([{"path": str, "blocked": str | None}], default_path)`.
  - `/api/now/suggest` payload gains `"home_folders"` and `"home_folder_default"`.
  - Frontend: `nowCreateAndFile(name, folder)`; `openPicker`'s `onCreate(typed, folder)`.

- [ ] **Step 1: Failing tests — mover helper, filing log, payload**

In the mover test file:

```python
from sortify.foldermove import leaf_collisions

PATHS = ["[Filter]", "ROOT", "ROOT / Hazy", "ROOT / Hominin", "ROOT / Hominin / OLD", "input",
         "input / inputlister"]

def test_leaf_collisions_finds_subfolders_and_name_overlaps():
    assert leaf_collisions(PATHS, "ROOT / Hazy") == []
    assert leaf_collisions(PATHS, "[Filter]") == []
    assert leaf_collisions(PATHS, "ROOT / Hominin") == ["ROOT / Hominin / OLD"]
    assert "input / inputlister" in leaf_collisions(PATHS, "input")
```

In `tests/test_create_filing.py`:

```python
def test_a_failed_filing_is_logged(caplog):
    from sortify import filing
    def boom():
        raise filing.FilingError("the client is busy")
    with caplog.at_level("WARNING"):
        filing.start("P9", "x", "ROOT / Hazy", runner=boom, threaded=False)
    assert filing.status("P9")["state"] == "failed"
    assert any("P9" in r.getMessage() and "client is busy" in r.getMessage() for r in caplog.records)


def test_home_folder_choices(monkeypatch):
    from sortify import app as appmod
    monkeypatch.setattr(appmod.store, "folders", lambda: {
        "H1": {"path": "ROOT / Hazy"}, "H2": {"path": "ROOT / Hominin"},
        "X1": {"path": "ROOT / Hominin / OLD"}, "B1": {"path": "[Filter]"}})
    cfg = {"home_ids": ["H1", "H2"], "create_folders": {"home": None}}
    choices, default = appmod._home_folder_choices(cfg, "H1")
    assert [c["path"] for c in choices] == ["ROOT / Hazy", "ROOT / Hominin"]
    assert choices[0]["blocked"] is None and "ROOT / Hominin / OLD" in choices[1]["blocked"]
    assert default == "ROOT / Hazy"
    assert appmod._home_folder_choices(cfg, "H2")[1] is None      # top guess's folder is blocked
    cfg["create_folders"]["home"] = "ROOT / Hazy"
    assert appmod._home_folder_choices(cfg, None)[1] == "ROOT / Hazy"
```

Run: `.venv/bin/pytest -q tests/test_create_filing.py <mover test file>` — Expected: FAIL (ImportError / AttributeError).

- [ ] **Step 2: Implement backend**

`sortify/foldermove.py` — split `_check_leaf_unique`:

```python
def leaf_collisions(paths: list[str], dest_path: str) -> list[str]:
    """The other folders the client's "Move to folder" search would offer
    alongside `dest_path` (see _check_leaf_unique for why that matters)."""
    low = dest_path.split(" / ")[-1].lower()
    return [p for p in paths
            if p != dest_path
            and (low in p.split(" / ")[-1].lower()
                 or any(low in seg.lower() for seg in p.split(" / ")[:-1]))]
```

and in `_check_leaf_unique` replace the `leaf`/`low`/`paths`/`hits` computation with `leaf = dest_path.split(" / ")[-1]` and `hits = leaf_collisions(_folder_paths(tree), dest_path)` (the docstring and the raise stay as they are).

`sortify/filing.py` — add `import logging` and `log = logging.getLogger("uvicorn.error")` at the top; in `run()`:

```python
        except FilingError as e:
            log.warning("filing %s into %r failed: %s", playlist_id, dest_path, e)
            _set(playlist_id, state="failed", folder=None, error=str(e))
            return
        except Exception as e:  # a broken seam must not kill the thread silently
            log.warning("filing %s into %r failed: %r", playlist_id, dest_path, e)
            _set(playlist_id, state="failed", folder=None, error=repr(e))
            return
```

`sortify/app.py` — add above `_suggestion_payload`:

```python
def _home_folder_choices(cfg: dict, top_home_id: str | None) -> tuple[list[dict], str | None]:
    """The folders a new home can be filed into from the Now card, and which
    one to pre-select. 0 calls: folders.json only.

    The choices are the folders today's homes live in. One the desktop
    client's folder search cannot single out (subfolders, or a name inside
    another folder's) is listed with the reason instead of offered — the
    mover would refuse it after the create (foldermove._check_leaf_unique).
    Pre-selected: the top guess's folder, else the last folder used for homes.
    """
    folders = store.folders()
    every = folder_paths(folders)
    homes = sorted({(folders.get(h) or {}).get("path") for h in cfg.get("home_ids") or []} - {None, ""})
    choices = []
    for path in homes:
        hits = foldermove.leaf_collisions(every, path)
        choices.append({"path": path, "blocked": (
            f"the client's folder search would also offer {', '.join(hits[:3])} — "
            "file this one by hand") if hits else None})
    ok = {c["path"] for c in choices if not c["blocked"]}
    for cand in ((folders.get(top_home_id) or {}).get("path") if top_home_id else None,
                 (cfg.get("create_folders") or {}).get("home")):
        if cand in ok:
            return choices, cand
    return choices, None
```

(add `from . import foldermove` with the other imports if it is not imported yet; `folder_paths` is already imported from `.folders`). In `_suggestion_payload`, compute the suggestions once into a local and add two keys:

```python
    suggestions = sugg.suggest(
        track, state["profiles"], tag_artists, track_map, artist_map,
        state.get("playlist_artists"),
    ) if sortable else []
    home_folders, home_folder_default = _home_folder_choices(
        store.config(), suggestions[0]["playlist_id"] if suggestions else None)
```

then use `"suggestions": suggestions,` and add `"home_folders": home_folders, "home_folder_default": home_folder_default,` to the returned dict.

Run: `.venv/bin/pytest -q` — Expected: green.

- [ ] **Step 3: Failing UI harness block**

Append before `// ---- summary` (model the setup on the MB block — `setNow`, `pollNow(true)`, `stopNowPolling`):

```js
// ============================================================================
// FH — a home created from the Now card is filed into the folder you pick.
{
  const paint = async (over) => {
    setNow({
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:fh1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[One]", is_input: true }, sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 80, reasons: [], already: false }],
      subsets: [], subset_targets: [],
      homes: [{ id: "H1", name: "Home One", folder: "ROOT / Hazy" }],
      inputs: [{ id: "IN1", name: "[One]", has_track: true, set: "buffer", total: 9 }],
      home_folders: [{ path: "ROOT / Hazy", blocked: null },
                     { path: "ROOT / Hominin", blocked: "the client's folder search would also offer ROOT / Hominin / OLD — file this one by hand" }],
      home_folder_default: "ROOT / Hazy",
      ...over,
    });
    run(`show("now"); filedUris = {}; removedUri = null; nowActions = 0; nowActionLog = []; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };
  await paint({});
  try {
    run(`openPicker(nowState.homes, nowFile, nowCreateAndFile, null)`);
    run(`$("picker-filter").value = "Night drive"; $("picker-filter").oninput({ target: { value: "night drive" } })`);
    const kids = () => $$("picker-list").children;
    const chips = kids().find((c) => c.className === "picker-folders");
    check("FH the create-home row offers the home folders", !!chips && /ROOT \/ Hazy/.test(chips.innerHTML),
          JSON.stringify(kids().map((c) => c.className)));
    check("FH ...the top guess's folder pre-selected",
          /class="chip on"[^>]*>ROOT \/ Hazy</.test(chips?.innerHTML || "") &&
          run(`$("picker-list").children.find((c) => c.className === "picker-folders").dataset.chosen`) === "ROOT / Hazy",
          chips?.innerHTML);
    check("FH ...and a folder the mover can't reach disabled, with why",
          /disabled[^>]*title="the client's folder search/.test(chips?.innerHTML || ""), chips?.innerHTML);
    routes["POST /api/playlists/create"] = { status: 200, body: {
      playlist: { id: "H9", name: "Night drive", role: "home", total: 0, folder: "ROOT / Hazy" }, filing: true } };
    routes["POST /api/act"] = { status: 200, body: {} };
    resetLog();
    const create = kids().find((c) => /Create home/.test(c.innerHTML));
    await create.onclick();
    await tick(); await tick();
    const cr = bodies("/api/playlists/create").slice(-1)[0];
    check("FH creating sends the chosen folder", cr && cr.folder === "ROOT / Hazy" && cr.role === "home",
          JSON.stringify(cr));
  } finally {
    run(`closePicker()`);
  }
}
```

The chip markup this block expects: a `div` with `className = "picker-folders"` and `dataset.chosen` holding the selected path, containing one `<button class="chip">` per folder (`class="chip on"` when selected; `disabled title="<reason>"` when blocked). Run: `node tests/ui_harness.mjs` — Expected: the FH checks FAIL.

- [ ] **Step 4: Implement the frontend**

`nowCreateAndFile` (line ~2607):

```js
async function nowCreateAndFile(name, folder) {
  try {
    // `folder` undefined -> omitted -> the server's default for homes.
    const { p, note, filing } = await createPlaylist(name, "home", folder);
```

(rest unchanged).

In `openPicker`'s create-row block, for the home case (`!subset && !buffer`), replace the final `else` branch so it builds the chips when `nowState?.home_folders?.length` and `onCreate === nowCreateAndFile`:

```js
      } else {
        b.innerHTML = `<span class="p-name">${subset
          ? `Create subset “${esc(typed)}” and add this track to it`
          : buffer
          ? `Create buffer “${esc(bufferName(typed))}” and move this track there`
          : `Create home “${esc(typed)}” and file this track there`}</span>` +
          `<span class="p-sub">${price}</span>`;
        // A new home is filed into a folder you pick (2026-10-01): the
        // folders today's homes live in, the top guess's pre-selected. A
        // folder the client's search cannot single out is shown disabled
        // with why, never offered and then failed after the create.
        let folders = null;
        if (!subset && !buffer && onCreate === nowCreateAndFile && nowState?.home_folders?.length) {
          folders = document.createElement("div");
          folders.className = "picker-folders";
          folders.dataset.chosen = nowState.home_folder_default || "";
          const paintChips = () => {
            folders.innerHTML = nowState.home_folders.map((f) => f.blocked
              ? `<button class="chip" disabled title="${esc(f.blocked)}">${esc(f.path)}</button>`
              : `<button class="chip${f.path === folders.dataset.chosen ? " on" : ""}" data-path="${esc(f.path)}">${esc(f.path)}</button>`
            ).join("");
            for (const c of folders.querySelectorAll("button[data-path]")) {
              c.onclick = (ev) => { ev.stopPropagation(); folders.dataset.chosen = c.dataset.path; paintChips(); };
            }
          };
          paintChips();
        }
        b.onclick = () => {
          closePicker();
          if (folders) onCreate(typed, folders.dataset.chosen || undefined);
          else onCreate(typed);
        };
        list.appendChild(b);
        if (folders) list.appendChild(folders);
        return;
      }
      list.appendChild(b);
```

Make sure the `if ((subset || buffer) && !typed)` branch above still ends with its own `list.appendChild(b)` (move the shared `list.appendChild(b)` into that branch if the restructure removed it). If the stub DOM lacks `querySelectorAll("button[data-path]")` support for attribute selectors, read the harness's stub (`querySelectorAll` implementation near the top of `tests/ui_harness.mjs`) and extend it minimally — that is test plumbing, not product code.

`sortify/static/style.css` — append:

```css
/* The folder a new home is filed into, under the create-home row. */
.picker-folders { display: flex; flex-wrap: wrap; gap: 6px; padding: 4px 12px 10px; }
.picker-folders .chip { font-size: 13px; }
.picker-folders .chip.on { outline: 2px solid currentColor; }
.picker-folders .chip:disabled { opacity: .45; }
```

Run: `node tests/ui_harness.mjs && .venv/bin/pytest -q` — Expected: both green.

- [ ] **Step 5: Buffers default to `[Filter]` (data) and commit**

```bash
cd ~/kode/spotify/sortify && .venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, ".")
from sortify.store import Store
s = Store(); cf = dict(s.config().get("create_folders") or {})
cf["input"] = "[Filter]"
s.update_config(create_folders=cf)
print(s.config()["create_folders"])
EOF
git add -A sortify tests && git commit -m "fix: new buffers and homes are filed into a folder

No filing job had started since 2026-09-20: homes defaulted to the top level
and buffers had no default at all. Buffers now default to [Filter] (config);
a home created from the Now card is filed into a folder chosen from chips —
the top guess's pre-selected, unreachable ones disabled with why. A failed
filing is logged, so the evidence survives a restart.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Expected config print: `{'subset': 'input', 'home': None, 'input': '[Filter]'}`.

---

### Task 8: Docs, deploy, verify

**Files:**
- Modify: `sortify/CLAUDE.md` (the **Subsets** paragraph and the **Quick adds** line about marked subsets)
- Modify: `~/kode/spotify/spotify-ledger/README.md` (new "Playlist roles" section)

- [ ] **Step 1: Rewrite sortify CLAUDE.md's Subsets paragraph**

Replace the paragraph beginning "- **Subsets** are non-exclusive selections" with:

```markdown
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
```

In the **Quick adds** paragraph, change "so the targets must be marked subsets" to "so the targets must be subsets (emoji-named)" and "marks it a subset" to "names it with 🐾".

- [ ] **Step 2: README section in spotify-ledger**

Append to `~/kode/spotify/spotify-ledger/README.md` before `## Caps`:

```markdown
## Playlist roles

A third shared file, unrelated to quota: which role a playlist plays.

| Path | Role |
| --- | --- |
| `playlist_roles.py` | canonical rules (symlinked into `sortify/sortify/`) |
| `playlistRoles.js` | browser port, **copied** into `spotify-autoqueuer/src/web/`; a vitest checks it stays byte-identical and agrees with the Python |

Precedence, first match wins: **archived** (name starts with 🗄, U+FE0F
optional — no role at all) > **input** (explicit, or an input set's name
pattern / folder segment) > **home** (sortify's resolved `home_ids`) >
**subset** (name starts with any other emoji, and the playlist is ours).
Marking a subset is renaming it: `mark_subset_name` puts 🐾 on (an archived
name loses its 🗄 and keeps the emoji under it), `unmark_subset_name` takes
the leading emoji cluster off. The data — input sets, home ids, folders —
stays in sortify's `data/`. Set 2026-10-01.
```

Commit both repos (`docs: …`, with the Co-Authored-By line).

- [ ] **Step 3: Deploy**

Check before restarting: `data/queue.json` state is not `running`, and `list_sessions` shows no other running session in sortify or autoqueuer. Then:

```bash
cd ~/kode/spotify/spotify-autoqueuer && npm run build && systemctl --user restart spotify-mixer sortify && sleep 3 && systemctl --user is-active spotify-mixer sortify
```

Expected: `active` twice. Confirm each commit predates the start: `git -C ~/kode/spotify/sortify log -1 --format=%cd` earlier than `systemctl --user show sortify -p ExecMainStartTimestamp`.

- [ ] **Step 4: Verify locally (zero calls)**

```bash
curl -s http://127.0.0.1:8800/api/playlists | python3 -c "import json,sys; d=json.load(sys.stdin)['playlists']; print('subsets', sum(p['role']=='subset' for p in d), 'archived', sum(p.get('archived') for p in d))"
curl -s http://127.0.0.1:8899/api/curated | python3 -c "import json,sys; d=json.load(sys.stdin); print(sorted(d))"
cd ~/kode/spotify/sortify && .venv/bin/spx budget | head -1
```

Expected: `subsets 12 archived 51` (the 12 🐾 lists; 🅱️arvakt is not ours), `['folderPaths', 'homeIds', 'inputIds', 'inputSets']`, and a budget unchanged from before the deploy. Report the numbers to the user.

- [ ] **Step 5: Push**

Only when the user asks.
