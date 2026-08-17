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
  log.push({ method: opts?.method || "GET", path });
  let r = routes[k];
  if (typeof r === "function") r = r();
  r = await r;
  if (!r) r = { status: 200, body: {} };
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

const results = [];
function check(name, pass, detail) {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

const PILES = [
  { id: "p1", name: "one", tags: ["a"], uris: ["u1", "u2"], decided: 0, total: 2 },
  { id: "p2", name: "two", tags: ["b"], uris: ["u3"], decided: 0, total: 1 },
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

// ---- summary ---------------------------------------------------------------
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
