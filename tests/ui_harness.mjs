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
    this.dataset = {}; this.children = []; this.href = ""; this.style = {};
    const cls = new Set();
    this.classList = {
      add: (c) => cls.add(c),
      remove: (c) => cls.delete(c),
      toggle: (c, force) => {
        const on = force === undefined ? !cls.has(c) : !!force;
        if (on) cls.add(c); else cls.delete(c);
        return on;
      },
      contains: (c) => cls.has(c),
    };
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
  document, fetch: fetchStub, setTimeout, clearTimeout, setInterval, clearInterval, console,
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
  check("M the tooltip states the honest server-side pace range and the floor disclosure, not just the price",
        /one per track/.test(rows[0]) && /server-side worker/.test(rows[0]) &&
        /at least/.test(rows[0]) && !/Leave this tab open/.test(rows[0]) &&
        /runs on the server/.test(rows[0]) && /closing it/.test(rows[0]), rows[0]);

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
  // The check above is deliberately synchronous (pins that the body is
  // logged before any await, per the misclick contract) — but queuePiles'
  // own trailing re-read + queue-poll are still pending after it. Drain
  // them here so they can't land, unattributed, inside a later test's tick().
  await tick();

  resetLog();
  run(`queuePiles(["p1"], 3)`);        // single pile goes through the same gate
  check("single-pile save is a one-pile queue",
        JSON.stringify(bodies("/api/split/PL8/queue")[0]?.pile_ids) === '["p1"]');
  await tick();

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
  await tick();

  // Review fix (Important 1): a 409 here means the on-screen state was
  // already stale — a poll must reconcile it immediately rather than
  // leaving a dead Pause button until the next scheduled tick (or none, if
  // polling had already stopped).
  routes["POST /api/split/PL8/queue/pause"] = {
    status: 409, body: { detail: "queue is stopped, not running — nothing to pause" } };
  resetLog();
  run(`pauseQueue()`);
  await tick();
  check("a refused pause polls the queue so the UI reconciles immediately",
        gets("/api/split/PL8/queue") === 1, `${gets("/api/split/PL8/queue")} GET(s)`);

  // Review fix (Minor 2): the same in-flight guard the spend buttons use,
  // now covering Pause/Resume/Cancel too.
  {
    let release;
    const pending = new Promise((r) => (release = r));
    routes["POST /api/split/PL8/queue/pause"] = () => pending;
    resetLog();
    run(`pauseQueue()`);
    run(`pauseQueue()`);   // second click, first still in flight
    await tick();
    check("two clicks on Pause post exactly once",
          posts("/api/split/PL8/queue/pause") === 1,
          `${posts("/api/split/PL8/queue/pause")} POST(s)`);
    release({ status: 200, body: { ok: true } });
    await tick();
  }

  const restartPanel = run(`renderQueuePanel({ queue: { state: "paused", stop_reason: null,
    pending: ["p2", "p3"], current: null,
    progress: { pile_index: 1, pile_count: 8, track: 40, track_total: 309 } },
    pacing: { rate_per_min: 2.5, ceiling: 7.0, max_clean_rate: 2.1 } })`);
  check("a restart's leftover running state renders as paused with Resume",
        restartPanel.includes("Resume"));

  // Review fix (Important 2): Resume must not render enabled when there's
  // nothing left to resume — cancel leaves exactly this shape (stopped,
  // pending: [], current: null) and resume() 409s "nothing queued" against
  // it, so a state-only gate left Resume a permanent dead end after Cancel.
  const resumeTag = (html) => (html.match(/<button id="btn-queue-resume"[^>]*>/) || [""])[0];
  const cancelledPanel = run(`renderQueuePanel({ queue: { state: "stopped", stop_reason: "cancelled",
    pending: [], current: null, progress: {} }, pacing: {} })`);
  check("a cancelled, empty queue renders Resume disabled",
        /disabled/.test(resumeTag(cancelledPanel)), cancelledPanel);
  const pausedWithWorkPanel = run(`renderQueuePanel({ queue: { state: "paused", stop_reason: null,
    pending: ["p2"], current: null,
    progress: { pile_index: 1, pile_count: 8, track: 40, track_total: 309 } },
    pacing: { rate_per_min: 2.5, ceiling: 7.0, max_clean_rate: 2.1 } })`);
  check("a paused queue with pending work renders Resume enabled",
        !/disabled/.test(resumeTag(pausedWithWorkPanel)), pausedWithWorkPanel);

  // Review fix (Minor 1): a quota trip is the severe case (Development
  // Mode's daily allowance, gone until the window resets — see CLAUDE.md)
  // and must not look like a routine 429 note.
  const quotaPanel = run(`renderQueuePanel({ queue: { state: "stopped", stop_reason: "quota",
    pending: [], current: null, progress: {} }, pacing: {} })`);
  check("a quota trip renders its own distinct, prominent marker",
        /queue-stop-quota/.test(quotaPanel) && /quota tripped/i.test(quotaPanel) &&
        /resume is manual/i.test(quotaPanel),
        quotaPanel);
  const rateLimitPanel = run(`renderQueuePanel({ queue: { state: "sleeping", stop_reason: "rate limited",
    pending: ["p2"], current: "p1", progress: {} }, pacing: {} })`);
  check("an ordinary rate-limit note does NOT get the quota marker",
        !/queue-stop-quota/.test(rateLimitPanel), rateLimitPanel);

  // M-3: spend vs. cap+reserve, and the pacing side's last 429 — both already
  // ride along in the GET's progress/pacing objects (free, local reads).
  const spendPanel = run(`renderQueuePanel({ queue: { state: "running", stop_reason: null,
    pending: ["p2"], current: "p1",
    progress: { pile_index: 1, pile_count: 2, track: 1, track_total: 3,
                spent_today: 210, bulk_today: 40, daily_cap: 600, reserve: 150 } },
    pacing: { rate_per_min: 3.4, ceiling: 7.0,
              history_429: [{ kind: "rate", rate: 1.8, when: 1755500000 },
                             { kind: "quota", rate: 3.4, when: 1755500600 }] } })`);
  check("M the queue panel shows spend vs. cap and reserve",
        /210\/600/.test(spendPanel) && /bulk 40/.test(spendPanel) && /reserve 150/.test(spendPanel),
        spendPanel);
  check("M the queue panel shows the LAST of pacing's history_429, not an earlier one",
        /last 429/.test(spendPanel) && /quota/.test(spendPanel) && /3\.4\/min/.test(spendPanel) &&
        !spendPanel.includes(">last 429: rate at"),
        spendPanel);
  const noHistoryPanel = run(`renderQueuePanel({ queue: { state: "running", stop_reason: null,
    pending: [], current: "p1", progress: { pile_count: 1 } },
    pacing: { rate_per_min: 1.8, ceiling: 7.0, history_429: [] } })`);
  check("M no last-429 line when pacing has no history yet",
        !/last 429/.test(noHistoryPanel), noHistoryPanel);

  // Review fix (Minor 3): pins the disclosed-but-untested claim — a
  // single-pile save in flight must not block save-all's guard key, and
  // vice versa, since the two are different (split, pile-set) signatures.
  {
    let release;
    const pending = new Promise((r) => (release = r));
    routes["POST /api/split/PL8/queue"] = () => pending;
    resetLog();
    run(`queuePiles(["p1"], 3)`);
    run(`queuePiles(null, 5)`);
    await tick();
    check("a single-pile save in flight doesn't block save-all's guard key (and vice versa)",
          posts("/api/split/PL8/queue") === 2,
          `${posts("/api/split/PL8/queue")} POST(s)`);
    release({ status: 200, body: { ok: true, queued: ["p1", "p2"], total_calls: 5 } });
    await tick();
  }

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

// ============================================================================
// V — Now-tab presentation pins (2026-08 visual refresh): cooldown countdown
// card, hidden-until-useful Undo, sitting banner de-dupe, nav active state.
// ============================================================================
{
  // V5 — the nav marks which view you're in; triage/split count as the
  // Playlists flow (that's where the user came from and goes back to).
  run(`show("now")`);
  check("V5 Now link is active in the Now view",
        $$("nav-now").classList.contains("active") === true &&
        $$("nav-lists").classList.contains("active") === false, "");
  run(`show("triage")`);
  check("V5 triage marks the Playlists link active",
        $$("nav-lists").classList.contains("active") === true &&
        $$("nav-now").classList.contains("active") === false, "");
  run(`show("now")`);
}

{
  // V1 — a cooldown renders a countdown card with real h/m arithmetic and a
  // resume time, never Math.round'ed hours ("~178 min" used to say "3 hours",
  // "~25 min" said "0 hours").
  routes["GET /api/now?force=1"] = {
    status: 200,
    body: { playing: false, cooldown: "cooldown — try again in ~178 min", poll_after_ms: 999999 },
  };
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  let card = $$("now-card").innerHTML;
  check("V1 a 178-min cooldown shows h/m, not a rounded hour count",
        /2h 58m/.test(card) && !/3 hours/.test(card), JSON.stringify(card.slice(0, 120)));
  check("V1 the card names the resume time of day",
        /around <b>\d{1,2}:\d{2}<\/b>/.test(card), JSON.stringify(card.slice(0, 160)));

  routes["GET /api/now?force=1"] = {
    status: 200,
    body: { playing: false, cooldown: "cooldown — try again in ~25 min", poll_after_ms: 999999 },
  };
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  card = $$("now-card").innerHTML;
  check("V1 a 25-min cooldown never says '0 hours'",
        /25m/.test(card) && !/0 hours/.test(card), JSON.stringify(card.slice(0, 120)));

  // V1c — the first countdown must be computed from ONE clock read.
  //
  // renderNowProblem derived `until` from Date.now() and then formatted
  // `until - Date.now()`, reading the clock a second time. A cooldown of
  // "~178 min" is a whole number of minutes, so it lands exactly on a minute
  // boundary: one millisecond crossing between those two reads turns
  // "2h 58m 00s" into "2h 57m 59s", and the h/m check above fails. That is
  // what made this file fail about one run in thirty — nothing to do with the
  // ticker, which writes cb-time's textContent and cannot alter now-card's
  // innerHTML at all.
  //
  // Advancing the clock 1ms on every read makes the worst case the only case,
  // so this pins the bug deterministically instead of one run in thirty.
  const RealDate = ctx.Date;
  function DriftingDate(...a) { return new RealDate(...a); }
  DriftingDate.now = (() => { let n = 0; return () => RealDate.now() + n++; })();
  ctx.Date = DriftingDate;
  try {
    routes["GET /api/now?force=1"] = {
      status: 200,
      body: { playing: false, cooldown: "cooldown — try again in ~178 min", poll_after_ms: 999999 },
    };
    run(`pollNow(true)`);
    await tick();
    run("stopNowPolling()");
    card = $$("now-card").innerHTML;
    check("V1c the countdown does not lose a second to a second clock read",
          /2h 58m/.test(card) && !/3 hours/.test(card), JSON.stringify(card.slice(0, 200)));
  } finally {
    ctx.Date = RealDate;
  }

  // V2 — the 1s countdown ticker must die the moment a real answer lands,
  // or a long-lived tab accumulates intervals.
  routes["GET /api/now?force=1"] = { status: 200, body: { playing: false, poll_after_ms: 999999 } };
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  let timer;
  try { timer = run("nowCooldownTimer"); } catch (e) { timer = "missing: " + e.message; }
  check("V2 the countdown ticker is cleared once a real answer lands",
        timer === null, String(timer));
}

{
  // V3 — Undo is hidden while there is nothing it could undo, and appears
  // once a filing lands.
  routes["GET /api/now?force=1"] = {
    status: 200,
    body: {
      playing: true, poll_after_ms: 999999,
      track: { uri: "spotify:track:v1", name: "T", artists: [{ name: "A" }], sortable: true, image: null },
      context: { id: "IN1", name: "[In]", is_input: true },
      sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 90, reasons: [], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      inputs: [{ id: "IN1", name: "[In]", has_track: true }],
    },
  };
  run(`nowActions = 0; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("V3 Undo is hidden while there is nothing to undo",
        $$("btn-undo-now").hidden === true, `hidden=${$$("btn-undo-now").hidden}`);
  routes["POST /api/act"] = { status: 200, body: { note: "filed" } };
  run(`nowFile("H1")`);
  await tick();
  check("V3 Undo appears (enabled) once a filing lands",
        $$("btn-undo-now").hidden === false && $$("btn-undo-now").disabled === false,
        `hidden=${$$("btn-undo-now").hidden} disabled=${$$("btn-undo-now").disabled}`);
}

{
  // V4 — in a sitting, the banner owns the pile name; the context line must
  // not repeat it one line below.
  routes["GET /api/now?force=1"] = {
    status: 200,
    body: {
      playing: true, poll_after_ms: 999999,
      track: { uri: "spotify:track:u9", name: "T", artists: [{ name: "A" }], sortable: true, image: null },
      context: { id: "SP9", name: "sit" },
      sitting: { split_id: "PL9", pile_id: "p1", pile_name: "dreamy pile",
                 uris: ["spotify:track:u9", "u2"], decided: { u2: { action: "keep", to_id: "H1" } } },
      suggestions: [], homes: [], inputs: [],
    },
  };
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  const status = $$("now-sitting-status").innerHTML;
  check("V4 the sitting banner names the pile and the progress",
        $$("now-sitting-bar").hidden === false && /dreamy pile/.test(status) && /1 of 2 left/.test(status),
        JSON.stringify(status.slice(0, 100)));
  check("V4 the context line no longer repeats the pile name",
        $$("now-context").textContent === "",
        JSON.stringify($$("now-context").textContent));
}

// ============================================================================
// P — split progress: a bar that moves, a poll that stops, and failures that
// say what to do next.
//
// The poll is the sensitive part. GET /api/split/{id}/progress reads one
// module dict and cannot reach Spotify (pinned server-side by
// test_split_progress_spends_no_api_calls), which is the only reason a
// once-a-second poll is allowed here at all. What this harness pins is the
// other half: that the poll STOPS. An interval left running after the split
// ends is the shape of the bug that once cost ~600 Spotify calls an hour.
// ============================================================================
{
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const PROG = "/api/split/PL-P/progress";

  routes["GET /api/split/PL-P"] = { status: 404, body: { detail: "no split for that playlist" } };
  let release;
  routes["POST /api/split/PL-P"] = () => new Promise((r) => (release = r));
  // A fast poll_after_ms so the harness doesn't sit for seconds — the client
  // takes its interval from the server, so this also pins that it obeys it
  // rather than hard-coding one of its own.
  routes[`GET ${PROG}`] = { status: 200, body: {
    state: "running", phase: "tagging", done: 120, total: 700, detail: null, poll_after_ms: 5 } };

  run(`openSplit("PL-P", "PL-P")`);
  await tick();
  resetLog();
  // The stub DOM creates elements on demand with hidden=false, so without
  // this the "is it showing?" check below would pass against an app.js that
  // never touches the bar at all. Seed it the way index.html ships it.
  $$("split-progress").hidden = true;
  $$("btn-do-split").onclick();
  await sleep(40);

  check("P1 the progress bar is showing while the split runs",
        $$("split-progress").hidden === false,
        `hidden=${$$("split-progress").hidden}`);
  check("P1 it reports artists done against the total",
        /120/.test($$("split-progress").innerHTML) && /700/.test($$("split-progress").innerHTML),
        JSON.stringify($$("split-progress").innerHTML.slice(0, 120)));
  check("P1 it estimates the time left rather than only a count",
        /min|sec/i.test($$("split-progress").innerHTML),
        JSON.stringify($$("split-progress").innerHTML.slice(0, 120)));

  const polled = gets(PROG);
  check("P1 it polls on the interval the server handed it",
        polled >= 2, `${polled} poll(s) in 40ms at poll_after_ms=5`);

  routes[`GET ${PROG}`] = { status: 200, body: {
    state: "done", phase: "complete", done: 700, total: 700, detail: null, poll_after_ms: 0 } };
  routes["GET /api/split/PL-P"] = { status: 200, body: splitBody(null) };
  release({ status: 200, body: { piles: PILES, tagged: 3, untagged: 0 } });
  await tick();

  const settled = gets(PROG);
  await sleep(60);
  check("P2 polling stops when the split finishes (no orphaned timer)",
        gets(PROG) === settled,
        `${gets(PROG) - settled} extra poll(s) in the 60ms after it finished`);
  check("P2 the bar is hidden once the split is done",
        $$("split-progress").hidden === true, `hidden=${$$("split-progress").hidden}`);
}

// ============================================================================
// P3 — a partial Last.fm failure must say that retrying RESUMES.
//
// The server already saves the partial and says so in its 502 detail, but the
// client dropped that into a 2600ms toast. A user who reads "stopped after
// 431 of 712 artists" three seconds before it vanishes has no way to know
// that pressing Split again resumes instead of starting over, and that the
// several hundred answers already paid for are not lost.
// ============================================================================
{
  routes["GET /api/split/PL-R"] = { status: 404, body: { detail: "no split for that playlist" } };
  routes[`GET /api/split/PL-R/progress`] = { status: 200, body: {
    state: "failed", phase: "tagging", done: 431, total: 712, poll_after_ms: 0,
    detail: "Last.fm tagging stopped after 431 of 712 artists in this playlist" } };
  routes["POST /api/split/PL-R"] = { status: 502, body: {
    detail: "Last.fm tagging stopped after 431 of 712 artists in this playlist " +
            "(Last.fm error 29: rate limited); progress was saved — re-running " +
            "the split will resume instead of starting over." } };

  run(`openSplit("PL-R", "PL-R")`);
  await tick();
  $$("btn-do-split").onclick();
  await tick();

  const card = $$("split-empty").innerHTML;
  check("P3 the partial failure is a persistent card, not a vanishing toast",
        /431/.test(card) && /712/.test(card), JSON.stringify(card.slice(0, 140)));
  check("P3 it says plainly that trying again resumes",
        /resume/i.test(card), JSON.stringify(card.slice(0, 200)));
  check("P3 it offers a button to do exactly that",
        /id="btn-resume-split"/.test(card), JSON.stringify(card.slice(0, 200)));

  resetLog();
  // Guarded: an unwired button is a failed check, not an exception that ends
  // the run before P4 gets to say anything.
  if (typeof $$("btn-resume-split").onclick === "function") $$("btn-resume-split").onclick();
  await tick();
  check("P3 pressing it re-runs the split",
        posts("/api/split/PL-R") === 1, `${posts("/api/split/PL-R")} POST(s)`);
}

// ============================================================================
// P4 — a playlist that isn't ours can never succeed, so it must NOT be
// re-offered as a retry. The 403 is deliberately a different status from the
// two 502s for exactly this reason; the client has to honour that.
// ============================================================================
{
  routes["GET /api/split/PL-F"] = { status: 404, body: { detail: "no split for that playlist" } };
  routes["POST /api/split/PL-F"] = { status: 403, body: {
    detail: '"the bomb" belongs to rightkillthaz, not you. The Feb-2026 dev-mode API ' +
            "won't let sortify read another user's playlist tracks at all, so splitting " +
            'it can never succeed here. Make your own copy first' } };

  run(`openSplit("PL-F", "the bomb")`);
  await tick();
  $$("split-progress").hidden = false;   // see P1 — prove app.js hides it
  $$("btn-do-split").onclick();
  await tick();

  const card = $$("split-empty").innerHTML;
  check("P4 the ownership refusal is explained persistently",
        /belongs to rightkillthaz/.test(card), JSON.stringify(card.slice(0, 140)));
  check("P4 it does not offer a retry of something that can never work",
        !/btn-resume-split/.test(card) && !/btn-do-split/.test(card),
        JSON.stringify(card.slice(0, 200)));
  check("P4 the progress bar is not left up after an instant refusal",
        $$("split-progress").hidden === true, `hidden=${$$("split-progress").hidden}`);
}

// ============================================================================
// R1 — leftover sitting playlists are surfaced, priced, and removable
// ============================================================================
{
  resetLog();
  routes["GET /api/playlists"] = { status: 200, body: {
    playlists: [], fetched_at: 0,
    sitting_orphans: [{ id: "S1", name: "\u25b6 one" }, { id: "S2", name: "\u25b6 two" }] } };

  await run("loadLists()");
  await tick();
  check("R1 the orphan bar is shown when the server reports leftovers",
        $$("pl-orphan-bar").hidden === false, `hidden=${$$("pl-orphan-bar").hidden}`);
  check("R1 it says how many and names them",
        /2 leftover sitting playlists/.test($$("pl-orphan-status").textContent),
        JSON.stringify($$("pl-orphan-status").textContent));
  // Every spending control in this app states its price before it is pressed.
  check("R1 the button prices the removal in Spotify calls",
        /Remove \(2 calls\)/.test($$("btn-clean-sittings").textContent),
        JSON.stringify($$("btn-clean-sittings").textContent));

  routes["POST /api/sittings/cleanup"] = { status: 200, body: {
    ok: true, removed: ["S1", "S2"], remaining: 0, deferred: false } };
  routes["GET /api/playlists"] = { status: 200, body: {
    playlists: [], fetched_at: 0, sitting_orphans: [] } };
  resetLog();
  await run(`$("btn-clean-sittings").onclick()`);
  await tick();
  check("R1 pressing Remove reaches the cleanup endpoint exactly once",
        posts("/api/sittings/cleanup") === 1, `${posts("/api/sittings/cleanup")} POST(s)`);
  check("R1 the bar disappears once the account is clean",
        $$("pl-orphan-bar").hidden === true, `hidden=${$$("pl-orphan-bar").hidden}`);

  // Nothing to remove must not leave a bar advertising an action that would
  // spend a call and find nothing.
  routes["GET /api/playlists"] = { status: 200, body: {
    playlists: [], fetched_at: 0, sitting_orphans: [] } };
  await run("loadLists()");
  await tick();
  check("R1 no bar at all when there are no leftovers",
        $$("pl-orphan-bar").hidden === true, `hidden=${$$("pl-orphan-bar").hidden}`);

  // A start in flight defers the sweep; the user is told to retry, not that
  // nothing was wrong.
  routes["GET /api/playlists"] = { status: 200, body: {
    playlists: [], fetched_at: 0, sitting_orphans: [{ id: "S3", name: "\u25b6 three" }] } };
  routes["POST /api/sittings/cleanup"] = { status: 200, body: {
    ok: true, removed: [], remaining: null, deferred: true } };
  await run("loadLists()");
  await tick();
  await run(`$("btn-clean-sittings").onclick()`);
  await tick();
  check("R1 a deferred sweep says a sitting is starting rather than failing silently",
        /starting right now/.test($$("toast").textContent),
        JSON.stringify($$("toast").textContent));
}

// ---- summary ---------------------------------------------------------------
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
