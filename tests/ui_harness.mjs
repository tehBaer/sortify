// Regression checks for the client-side call-discipline and lockout bugs the
// final whole-branch review found. app.js is loaded into a stub DOM inside a
// vm context; `fetch` is a scripted router that records every request, so
// this makes **zero real Spotify calls** and needs no server running.
//
// NOT a pytest module (it is JavaScript, and the project deliberately has no
// JS toolchain — no dependencies, no build step). It is a hand-run
// diagnostic in the same spirit as fuzz_sittings.py:
//
//     node tests/ui_harness.mjs                     # the working tree
//     node tests/ui_harness.mjs /tmp/app-before.js  # any other copy
//
// Exits non-zero if any check fails. What it pins, and why each matters —
// all were measured failing against the commit before the fix:
//
//   C1  a reservation with playlist_id:null must still show the sitting bar,
//       disable every Start button, and leave Finish clickable. Otherwise
//       the split is locked with its only escape outside the UI entirely.
//   C2  three clicks on "Split it" must issue exactly one paid POST.
//   I1  finishing with nowState===null must not throw on either exit.
//   I2  no keep/reject may fire from the keyboard while an error card is up.
//   I3  two Finish clicks must spend exactly one unfollow, and cleared:false
//       must report what the resync actually found.
//   I4  a failed startSitting must re-sync instead of hiding a live sitting.
//
// The stub DOM models only what these paths touch, but the three details it
// does model are load-bearing: assigning `innerHTML` replaces the subtree (so
// renderPiles' row clearing is real, not additive), ids inside assigned
// markup become addressable afterwards, and responses are deep-cloned —
// app.js keeps `nowState.sitting` by reference and mutates its `decided` map
// in place, so a shared fixture would leak one scenario's decision into the
// next scenario's "server" answer and mask the very bug under test.
import fs from "node:fs";
import path from "node:path";
import url from "node:url";
import vm from "node:vm";

const HERE = path.dirname(url.fileURLToPath(import.meta.url));
const APP = process.argv[2] || path.join(HERE, "..", "sortify", "static", "app.js");
const src = fs.readFileSync(APP, "utf8");

// ---- stub DOM --------------------------------------------------------------

class El {
  constructor(id) {
    this.id = id; this._html = ""; this.hidden = false; this.disabled = false;
    this.textContent = ""; this.value = ""; this.className = "";
    this.dataset = {}; this.children = []; this.href = "";
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  // Assigning innerHTML replaces the subtree — so appended children go too,
  // exactly as in a real DOM. renderPiles() relies on this to clear rows.
  set innerHTML(v) { this._html = String(v); this.children = []; registerIds(this._html); }
  get innerHTML() { return this._html; }
  querySelectorAll() { return []; }
  querySelector() { return new El("_anon"); }
  appendChild(c) { this.children.push(c); }
  scrollIntoView() {} focus() {} addEventListener() {}
}

const reg = {};
const $$ = (id) => (reg[id] ||= new El(id));
function registerIds(html) {
  for (const m of html.matchAll(/id="([^"]+)"/g)) $$(m[1]);
}

const handlers = {};
const document = {
  hidden: false,
  getElementById: $$,
  createElement: () => new El("_created"),
  addEventListener: (t, f) => ((handlers[t] ||= []).push(f)),
};

// ---- scripted fetch --------------------------------------------------------

const log = [];        // {method, path}
let routes = {};       // "METHOD path" -> value | fn -> {status, body} or Promise
function key(path, opts) { return `${opts?.method || "GET"} ${path}`; }

async function fetchStub(path, opts) {
  const k = key(path, opts);
  // The body is logged too: the materialise checks are about *which number*
  // the client sends back, which a method+path log cannot see.
  log.push({ method: opts?.method || "GET", path, body: opts?.body });
  let r = routes[k];
  if (typeof r === "function") r = r();
  r = await r;
  if (!r) r = { status: 200, body: {} };
  // Shorthand for the common "just a success body" case: a route value with
  // no `status` field is treated as an implicit 200 whose body IS the value
  // itself, so a test can write `{ ok: true, queued: [...] }` directly
  // instead of `{ status: 200, body: { ok: true, queued: [...] } }`.
  if (r.status === undefined) r = { status: 200, body: r };
  return {
    status: r.status, ok: r.status >= 200 && r.status < 300,
    // Deep-cloned: app.js keeps `nowState.sitting` by reference and mutates
    // its `decided` map in place (applyDecision). Handing out the fixture
    // itself would let one scenario's decision leak into the next poll's
    // "server" answer.
    json: async () => structuredClone(r.body),
  };
}

const ctx = vm.createContext({
  document, fetch: fetchStub, setTimeout, clearTimeout, console,
  Date, Math, Number, Object, JSON, Error, Map, Set, Promise, String, Array,
});
Object.defineProperty(ctx, "globalThis", { value: ctx });

// ---- helpers ---------------------------------------------------------------

const run = (code) => vm.runInContext(code, ctx);
const tick = () => new Promise((r) => setImmediate(() => setImmediate(() => setImmediate(r))));
const posts = (p) => log.filter((c) => c.method === "POST" && c.path === p).length;
const gets = (p) => log.filter((c) => c.method === "GET" && c.path === p).length;
// Every logged body for a given path, parsed — the queue checks care about
// *which* request carried a given field, not just how many fired.
const bodies = (p) => log.filter((c) => c.path === p).map((c) => c.body ? JSON.parse(c.body) : undefined);

const results = [];
function check(name, pass, detail) {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

// Shaped like what _pile_progress actually sends, materialise fields
// included: the client never computes a call cost, so a pile row without
// `materialise_calls` renders its Save button disabled — which would make the
// "no disabled buttons" assertions below pass or fail for the wrong reason.
const PILES = [
  { id: "p1", name: "one", tags: ["a"], uris: ["u1", "u2"], decided: 0, total: 2,
    materialise_calls: 3, materialised: null },
  { id: "p2", name: "two", tags: ["b"], uris: ["u3"], decided: 0, total: 1,
    materialise_calls: 2, materialised: null },
];
const splitBody = (active) => ({
  piles: PILES, decided: {}, active_sitting: active,
  params: { resolution: 1, min_pile: 15 },
});

function resetLog() { log.length = 0; }

// ---- boot ------------------------------------------------------------------

routes["GET /api/status"] = { status: 200, body: { authed: true, me: { name: "me" } } };
routes["GET /api/now?force=1"] = { status: 200, body: { playing: false, poll_after_ms: 999999 } };

run(src);
await tick();
$$("picker").hidden = true;
run("stopNowPolling()");

// ============================================================================
// C1 — a reservation with playlist_id:null must be visible and finishable
// ============================================================================
{
  resetLog();
  routes["GET /api/split/PL1"] = {
    status: 200,
    body: splitBody({ playlist_id: null, pile_id: "p1", uris: [], claim: "c1" }),
  };
  run(`openSplit("PL1", "PL1")`);
  await tick();

  const barShown = $$("split-sitting-bar").hidden === false;
  check("C1 split-view sitting bar is shown for a playlist_id:null reservation",
        barShown, `hidden=${$$("split-sitting-bar").hidden}`);
  check("C1 bar text explains the stuck reservation",
        /never created|reserved/i.test($$("split-sitting-status").textContent),
        JSON.stringify($$("split-sitting-status").textContent.slice(0, 80)));

  const rows = $$("piles").children.map((c) => c.innerHTML);
  const allDisabled = rows.length === 2 && rows.every((h) => /<button[^>]*disabled/.test(h));
  check("C1 every pile's Start button is disabled (backend would 409 them)",
        allDisabled, `${rows.length} rows`);

  const sittingGlobal = run("sitting && sitting.splitId");
  check("C1 the `sitting` global is populated (Now-view bar has something to show)",
        sittingGlobal === "PL1", `sitting.splitId=${sittingGlobal}`);

  // The escape has to be reachable by a click.
  routes["POST /api/split/PL1/sitting/finish"] = { status: 200, body: { cleared: true } };
  resetLog();
  run(`$("btn-split-finish-sitting").onclick()`);
  await tick();
  check("C1 clicking Finish reaches the server-side escape",
        posts("/api/split/PL1/sitting/finish") === 1,
        `${posts("/api/split/PL1/sitting/finish")} POST(s)`);
  check("C1 after finishing, the split is usable again (bar hidden, Starts enabled)",
        $$("split-sitting-bar").hidden === true &&
        $$("piles").children.every((c) => !/<button[^>]*disabled/.test(c.innerHTML)),
        `bar hidden=${$$("split-sitting-bar").hidden}`);
}

// ============================================================================
// C2 — "Split it" must not multiply the paid read
// ============================================================================
{
  routes["GET /api/split/PL2"] = { status: 404, body: { detail: "no split for that playlist" } };
  let release;
  const pending = new Promise((r) => (release = r));
  routes["POST /api/split/PL2"] = () => pending;

  run(`openSplit("PL2", "PL2")`);
  await tick();
  resetLog();

  const btn = $$("btn-do-split");
  check("C2 the paid offer rendered a Split button", !!btn.id, btn.id);

  btn.onclick(); btn.onclick(); btn.onclick();   // three clicks in one tick
  await tick();

  check("C2 three clicks issue exactly one paid POST",
        posts("/api/split/PL2") === 1, `${posts("/api/split/PL2")} POST(s)`);
  check("C2 the button is disabled and relabelled for the duration",
        btn.disabled === true && /splitting/i.test(btn.textContent),
        `disabled=${btn.disabled} label=${JSON.stringify(btn.textContent)}`);

  release({ status: 200, body: { piles: PILES, tagged: 3, untagged: 0 } });
  routes["GET /api/split/PL2"] = { status: 200, body: splitBody(null) };
  await tick();
  check("C2 the offer is cleared once the split lands",
        $$("split-empty").innerHTML === "", JSON.stringify($$("split-empty").innerHTML.slice(0, 40)));
}

// ============================================================================
// C2b — a FAILED split must re-offer the button (re-enabled, original label)
// ============================================================================
{
  routes["GET /api/split/PL3"] = { status: 404, body: { detail: "no split for that playlist" } };
  routes["POST /api/split/PL3"] = { status: 502, body: { detail: "Last.fm exploded" } };
  run(`openSplit("PL3", "PL3")`);
  await tick();
  const btn = $$("btn-do-split");
  const label = btn.textContent;
  btn.onclick();
  await tick();
  check("C2b after a failure the Split button is usable again with its price label",
        btn.disabled === false && btn.textContent === label,
        `disabled=${btn.disabled} label=${JSON.stringify(btn.textContent)}`);

  // …and a 404 from the POST explains the Refresh, not "unknown playlist".
  routes["POST /api/split/PL3"] = { status: 404, body: { detail: "unknown playlist" } };
  btn.onclick();
  await tick();
  check("deferred-note: a 404 from create_split points at Refresh",
        /refresh/i.test($$("toast").textContent),
        JSON.stringify($$("toast").textContent.slice(0, 90)));
}

// ============================================================================
// I1 — renderNow() with nowState === null must not throw
// ============================================================================
{
  routes["GET /api/split/PL4"] = {
    status: 200,
    body: splitBody({ playlist_id: "SP4", pile_id: "p1", uris: ["u1"], claim: "c4" }),
  };
  run(`openSplit("PL4", "PL4")`);
  await tick();
  run(`nowState = null; show("now");`);          // Finish before any successful poll
  routes["POST /api/split/PL4/sitting/finish"] = { status: 200, body: { cleared: true } };

  let threw = null;
  process.once("unhandledRejection", (e) => (threw = e));
  try {
    run(`$("btn-now-finish-sitting").onclick()`);
    await tick();
  } catch (e) { threw = e; }
  check("I1 finish with nowState===null does not throw",
        threw === null, threw ? String(threw) : "");
  check("I1 the success toast survives",
        /finished/i.test($$("toast").textContent),
        JSON.stringify($$("toast").textContent.slice(0, 60)));

  // …and the 404 exit too.
  run(`sitting = {splitId:"PL4", pileName:"one", uris:["u1"], decided:{}}; nowState = null;`);
  routes["POST /api/split/PL4/sitting/finish"] = { status: 404, body: { detail: "no active sitting" } };
  threw = null;
  try {
    run(`$("btn-now-finish-sitting").onclick()`);
    await tick();
  } catch (e) { threw = e; }
  check("I1 the 404 finish exit does not throw either",
        threw === null && /clearing it here/i.test($$("toast").textContent),
        threw ? String(threw) : JSON.stringify($$("toast").textContent.slice(0, 60)));
}

// ============================================================================
// I2 — no irreversible keep from the error screen
// ============================================================================
{
  run(`show("now");`);
  // A good poll first: a live sitting card with a suggestion under key "1".
  routes["GET /api/now?force=1"] = {
    status: 200,
    body: {
      playing: true, poll_after_ms: 999999,
      track: { uri: "spotify:track:u1", name: "T", artists: [{ name: "A" }], sortable: true, image: null },
      context: { id: "SP5", name: "s" },
      sitting: { split_id: "PL5", pile_id: "p1", pile_name: "one", uris: ["spotify:track:u1"], decided: {} },
      suggestions: [{ playlist_id: "H1", pct: 90, reasons: [], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      inputs: [],
    },
  };
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  routes["POST /api/split/PL5/decide"] = { status: 200, body: { changed: true, decision: { action: "keep", to_id: "H1" }, remaining: 0 } };

  resetLog();
  handlers.keydown[0]({ key: "1", target: { tagName: "BODY" } });
  await tick();
  check("I2 baseline: `1` keeps while the live card is on screen",
        posts("/api/split/PL5/decide") === 1, `${posts("/api/split/PL5/decide")} POST(s)`);

  // Re-poll first: the keep above wrote `decided` into nowState.sitting, and
  // a decided track short-circuits the keyboard for a legitimate reason
  // ("final — nothing left to press"), which would mask the bug under test.
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("I2 the track is undecided again after a fresh poll",
        run(`Object.keys(nowState.sitting.decided).length`) === 0, "");

  // Now a failed poll: nowState is deliberately left intact, card replaced.
  routes["GET /api/now"] = { status: 500, body: { detail: "boom" } };
  run(`pollNow()`);
  await tick();
  run("stopNowPolling()");
  resetLog();
  handlers.keydown[0]({ key: "1", target: { tagName: "BODY" } });
  handlers.keydown[0]({ key: "r", target: { tagName: "BODY" } });
  await tick();
  check("I2 no keep (and no reject) fires from the error screen",
        posts("/api/split/PL5/decide") === 0, `${posts("/api/split/PL5/decide")} POST(s)`);
}

// ============================================================================
// I3 — Finish must not spend twice, and must not misdirect on cleared:false
// ============================================================================
{
  routes["GET /api/split/PL6"] = {
    status: 200,
    body: splitBody({ playlist_id: "SP6", pile_id: "p1", uris: ["u1"], claim: "c6" }),
  };
  run(`openSplit("PL6", "PL6")`);
  await tick();

  let release;
  const pending = new Promise((r) => (release = r));
  routes["POST /api/split/PL6/sitting/finish"] = () => pending;
  resetLog();
  run(`$("btn-split-finish-sitting").onclick()`);
  run(`$("btn-split-finish-sitting").onclick()`);   // second click, first in flight
  await tick();
  check("I3 two Finish clicks spend exactly one unfollow",
        posts("/api/split/PL6/sitting/finish") === 1,
        `${posts("/api/split/PL6/sitting/finish")} POST(s)`);
  check("I3 both Finish buttons are disabled while in flight",
        $$("btn-split-finish-sitting").disabled === true &&
        $$("btn-now-finish-sitting").disabled === true, "");

  // cleared:false with nothing actually active any more -> honest wording.
  routes["GET /api/split/PL6"] = { status: 200, body: splitBody(null) };
  release({ status: 200, body: { cleared: false } });
  await tick();
  check("I3 cleared:false + nothing active says so instead of 'finish it again'",
        /already finished/i.test($$("toast").textContent) &&
        !/finish it again/i.test($$("toast").textContent),
        JSON.stringify($$("toast").textContent.slice(0, 90)));
  check("I3 buttons re-enabled afterwards",
        $$("btn-split-finish-sitting").disabled === false &&
        $$("btn-split-finish-sitting").textContent === "Finish sitting", "");
}

// ============================================================================
// I4 — a failed startSitting must re-sync, not hide a live reservation
// ============================================================================
{
  routes["GET /api/split/PL7"] = { status: 200, body: splitBody(null) };
  run(`openSplit("PL7", "PL7")`);
  await tick();
  check("I4 precondition: no sitting, Start buttons enabled",
        $$("split-sitting-bar").hidden === true &&
        $$("piles").children.every((c) => !/<button[^>]*disabled/.test(c.innerHTML)), "");

  routes["POST /api/split/PL7/sitting"] = { status: 429, body: { detail: "rate limited" } };
  // The server holds a reservation WITH a playlist id — a real ▶ playlist exists.
  routes["GET /api/split/PL7"] = {
    status: 200,
    body: splitBody({ playlist_id: "SP7", pile_id: "p1", uris: ["u1", "u2"], claim: "c7" }),
  };
  resetLog();
  run(`startSitting("p1", "one")`);
  await tick();
  check("I4 the failure triggers a free re-read of the split",
        gets("/api/split/PL7") === 1, `${gets("/api/split/PL7")} GET(s)`);
  check("I4 the live sitting is now visible with its Finish button",
        $$("split-sitting-bar").hidden === false &&
        $$("piles").children.every((c) => /<button[^>]*disabled/.test(c.innerHTML)),
        JSON.stringify($$("split-sitting-status").textContent.slice(0, 70)));
}

// ============================================================================
// O1 — the ownership guard: a non-owned playlist must never offer a live
// split button. (The playlist-row DOM itself is out of reach here — the
// stub's querySelectorAll() always returns [], by design, since it only
// models the ids the sitting/piles paths touch — so this pins the pure
// gating function renderLists() calls per row rather than the wired-up
// button. The zero-Spotify-calls guarantee itself is pinned server-side, in
// tests/test_split_api.py; this only pins that the UI won't let a click
// through in the first place.)
// ============================================================================
{
  const notOwned = run(`splitDisabledReason({ editable: false })`);
  check("O1 a non-owned playlist gets a disabled reason",
        typeof notOwned === "string" && notOwned.length > 0, JSON.stringify(notOwned));
  check("O1 the reason says what to do about it",
        /copy/i.test(notOwned), JSON.stringify(notOwned));

  const owned = run(`splitDisabledReason({ editable: true })`);
  check("O1 an owned playlist gets no disabled reason",
        owned === null, JSON.stringify(owned));
}

// ============================================================================
// M — saving piles into permanent playlists via the queue. The price must be
// on the button before the click, it must be the server's number echoed
// back unchanged — that echo is the only thing standing between a misclick
// on the 309-track pile and 310 silent Spotify calls — and every price is a
// FLOOR (finding I2), not a promise of the final total.
// ============================================================================
{
  routes["GET /api/split/PL8"] = {
    status: 200,
    body: {
      ...splitBody(null),
      piles: [
        { id: "p1", name: "fresh", tags: [], uris: ["u1", "u2"], decided: 0, total: 2,
          materialise_calls: 3, materialised: null },
        { id: "p2", name: "half done", tags: [], uris: ["u3", "u4", "u5"], decided: 0, total: 3,
          materialise_calls: 2, materialised: { playlist_id: "SAVED2", added: 1, name: "half done", stale: false } },
        { id: "p3", name: "complete", tags: [], uris: ["u6"], decided: 0, total: 1,
          materialise_calls: 0, materialised: { playlist_id: "SAVED3", added: 1, name: "complete", stale: false } },
        { id: "p4", name: "changed", tags: [], uris: ["u7"], decided: 0, total: 1,
          materialise_calls: 2, materialised: { playlist_id: "SAVED4", added: 1, name: "old", stale: true } },
      ],
    },
  };
  run(`openSplit("PL8", "PL8")`);
  await tick();
  const rows = $$("piles").children.map((c) => c.innerHTML);

  check("M the price is on the button before the click, as a floor",
        /Save as playlist \(≥ 3 calls\)/.test(rows[0]), rows[0].slice(-120));
  check("M a partly-saved pile offers to resume at the price of what's left",
        /Resume saving \(≥ 2 calls\)/.test(rows[1]) && /1 of 3 saved so far/.test(rows[1]),
        rows[1].slice(-160));
  check("M a finished pile is not clickable and says so",
        /Saved as a playlist/.test(rows[2]) && /<button[^>]*disabled[^>]*>Saved as a playlist/.test(rows[2]),
        rows[2].slice(-140));
  check("M a re-clustered pile is offered as a NEW playlist, at full price",
        /Save as a new playlist \(≥ 2 calls\)/.test(rows[3]) && /pile has changed/.test(rows[3]),
        rows[3].slice(-180));
  check("M the tooltip states the wait and the floor disclosure, not just the price",
        /one per track/.test(rows[0]) && /min at sortify/.test(rows[0]) && /at least/.test(rows[0]), "");

  // A double-click on the SAME pile (identical queue signature) must not
  // double-queue — the same misclick guard materialisePile had, now scoped
  // per (split, pile-set) so it doesn't block an unrelated save-all/other-pile
  // click (see the next block).
  {
    let release;
    const pending = new Promise((r) => (release = r));
    routes["POST /api/split/PL8/queue"] = () => pending;
    resetLog();
    run(`queuePiles(["p1"], 3)`);
    run(`queuePiles(["p1"], 3)`);   // second click, first in flight
    await tick();
    check("M two clicks on the same pile issue exactly one paid POST",
          posts("/api/split/PL8/queue") === 1,
          `${posts("/api/split/PL8/queue")} POST(s)`);
    release({ status: 200, body: { ok: true, queued: ["p1"], total_calls: 3 } });
    await tick();
  }

  // A refusal (the cost guard, or a queue already running) must re-read too
  // — the row that produced the stale number is exactly what needs
  // replacing.
  routes["POST /api/split/PL8/queue"] = {
    status: 409, body: { detail: "cost has changed: saving these piles now spends 2 Spotify calls" } };
  resetLog();
  run(`queuePiles(["p1"], 3)`);
  await tick();
  check("M a refused enqueue says why and re-reads the split for free",
        /cost has changed/.test($$("toast").textContent) && gets("/api/split/PL8") === 1,
        `${gets("/api/split/PL8")} GET(s)`);

  // The queue replaces the one-shot save. Same misclick contract: the number
  // POSTed is the number the button displayed, now summed across piles.
  resetLog();
  routes["POST /api/split/PL8/queue"] = { ok: true, queued: ["p2", "p1"], total_calls: 5 };
  run(`queuePiles(null, 5)`);          // "Save all" — null means every pile
  check("save-all posts the summed price it displayed",
        bodies("/api/split/PL8/queue")[0]?.expected_calls === 5,
        JSON.stringify(bodies("/api/split/PL8/queue")[0]));

  resetLog();
  run(`queuePiles(["p1"], 3)`);        // single pile goes through the same gate
  check("single-pile save is a one-pile queue",
        JSON.stringify(bodies("/api/split/PL8/queue")[0]?.pile_ids) === '["p1"]');

  // Finding I2: every displayed price is a floor, not a ceiling — request()
  // retries a transient 429 up to 3x and each attempt is charged.
  const saveAllLabel5 = run(`renderSaveAllLabel(5)`);
  check("the price label discloses it is a floor",
        /at least|minst|floor/i.test(saveAllLabel5), saveAllLabel5);

  // Pause is one click and free; the button reflects the effective state.
  resetLog();
  routes["POST /api/split/PL8/queue/pause"] = { ok: true };
  run(`pauseQueue()`);
  check("pause posts exactly once and nowhere else",
        posts("/api/split/PL8/queue/pause") === 1 && log.length === 1);

  const restartPanel = run(`renderQueuePanel({ queue: { state: "paused", stop_reason: null,
    progress: { pile_index: 1, pile_count: 8, track: 40, track_total: 309 } },
    pacing: { rate_per_min: 2.5, ceiling: 7.0, max_clean_rate: 2.1 } })`);
  check("a restart's leftover running state renders as paused with Resume",
        restartPanel.includes("Resume"));

  // A server that sends no cost must not be guessed at.
  routes["GET /api/split/PL9"] = {
    status: 200,
    body: { ...splitBody(null), piles: [{ id: "p1", name: "n", tags: [], uris: ["u1"], decided: 0, total: 1 }] },
  };
  run(`openSplit("PL9", "PL9")`);
  await tick();
  resetLog();
  const row = $$("piles").children[0].innerHTML;
  check("M an unpriced pile is offered as un-clickable rather than guessed at",
        /<button[^>]*disabled[^>]*>Save as playlist</.test(row), row.slice(-120));
  run(`queuePiles(["p1"], null)`);
  await tick();
  check("M and calling it with no price spends nothing",
        posts("/api/split/PL9/queue") === 0,
        `${posts("/api/split/PL9/queue")} POST(s)`);
}

// ---- summary ---------------------------------------------------------------
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
