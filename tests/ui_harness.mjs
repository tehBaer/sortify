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
//
// ---- two hazards that have each cost a debugging session ------------------
//
// ONE LONG-LIVED CONTEXT. Every block shares one vm, one app.js instance, and
// one set of globals; blocks run in file order and there is no reset between
// them. So scenario N's leftovers become scenario N+1's mystery, and the
// symptom always appears in the LATER block:
//   - `view-now.hidden` left true makes the next block's pollNow a silent
//     no-op, and its card assertions then read a stale card;
//   - timers outlive their block. resumeSpotify schedules a repoll ~900ms out
//     on a bare setTimeout that stopNowPolling cannot cancel; it lands inside
//     whatever block is running by then and overwrites nowState with ITS
//     fixture (this is why PV3 hides the Now view and MT waits one out);
//   - the preview player's per-playlist position stash persists across
//     blocks, so two blocks sharing a playlist id must also differ in the
//     playing track uri or the second one silently resumes the first's walk.
// Open a block by setting the state it needs rather than inheriting it, and
// close it by restoring what it changed — in a `finally`, so a throwing block
// does not take the rest of the file down with it.
//
// `playing` IS NOT `is_playing`. /api/now sets `playing: true` whenever a
// track object exists at all and reports the transport separately as
// `is_playing` — a paused Spotify is `playing: true, is_playing: false`. A
// fixture that stages only `playing` is a paused player, so anything that
// asks "was music actually running" reads false and the block passes for the
// wrong reason. Staging a playing track means setting BOTH.
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
  // "button" is modeled for real (one fresh El per <button> tag in document
  // order) because makeListRow destructures its role chips positionally and
  // wires paint()/onclick straight onto them — an empty stub would throw
  // before the row's markup (what SM's checks actually read) ever gets
  // built. Other selectors stay unmodeled: nothing else needs them, and O1
  // pins the pure gating function specifically to avoid depending on this.
  querySelectorAll(sel) {
    if (sel === "button") return [...this._html.matchAll(/<button[^>]*>/g)].map(() => new El("_btn"));
    return [];
  }
  querySelector() { return new El("_anon"); }
  appendChild(c) { this.children.push(c); }
  setAttribute(k, v) { this[k] = String(v); }
  scrollIntoView() {} focus() {} addEventListener() {}
}

const reg = {};
const $$ = (id) => (reg[id] ||= new El(id));
function registerIds(html) {
  for (const m of html.matchAll(/id="([^"]+)"/g)) $$(m[1]);
}

const handlers = {};

// A real DOM runs EVERY listener registered for an event; addressing one by
// index silently couples the checks to registration order, so adding a
// listener anywhere earlier in app.js would make an unrelated check fail.
function fireKey(key, target = { tagName: "BODY" }) {
  const ev = { key, target, stopPropagation() {}, preventDefault() {} };
  for (const h of handlers.keydown || []) h(ev);
}
const document = {
  hidden: false,
  getElementById: $$,
  createElement: () => new El("_created"),
  addEventListener: (t, f) => ((handlers[t] ||= []).push(f)),
  body: new El("_body"),   // blind mode toggles a class here
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

// The preview player is the only code that constructs Audio. It records
// every instance so a check can prove the OLD clip was paused before the new
// one started — two live Audio objects is the "previews over each other" bug.
class AudioStub {
  constructor(src) {
    this.src = src; this.currentTime = 0; this.duration = 30; this.paused = false;
    AudioStub.made.push(this);
  }
  // Setting src runs the media element's load algorithm, and an empty src
  // resolves to the document — not a media resource — so the browser fires
  // `error` on the RETIRED element a task later (verified in Brave). Every
  // stop in the player clears src exactly this way, so a stub that stays
  // silent here hides any handler left live on a clip that is already over.
  set src(v) {
    this.__src = v;
    if (v === "") setTimeout(() => { if (this.onerror) this.onerror(); }, 0);
  }
  get src() { return this.__src; }
  play() { this.paused = false; return AudioStub.refuse ? Promise.reject(new Error("blocked")) : Promise.resolve(); }
  pause() { this.paused = true; }
}
AudioStub.made = [];
AudioStub.refuse = false;

const ctx = vm.createContext({
  document, fetch: fetchStub, setTimeout, clearTimeout, setInterval, clearInterval, console,
  Audio: AudioStub,
  Date, Math, Number, Object, JSON, Error, Map, Set, Promise, String, Array,
  // Blind mode persists its toggle here; an in-memory map is enough.
  localStorage: (() => {
    const m = new Map();
    return { getItem: (k) => (m.has(k) ? m.get(k) : null),
             setItem: (k, v) => m.set(k, String(v)),
             removeItem: (k) => m.delete(k) };
  })(),
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

// Two-phase now card: pollNow asks /api/now?light=1[&force=1] for the track
// and /api/now/suggest[?force=1] for the suggestion side, merging the second
// answer in when it lands. A scenario still describes the card as ONE full
// body; this registers it as all four routes, split exactly the way the
// server splits it — so a check that reads the finished card keeps working,
// and a check that wants the in-between state can override a single route.
function setNow(bodyOrRoute) {
  const status = bodyOrRoute.status ?? 200;
  const body = bodyOrRoute.status === undefined ? bodyOrRoute : bodyOrRoute.body;
  const light = { status, body };
  routes["GET /api/now?light=1"] = light;
  routes["GET /api/now?light=1&force=1"] = light;
  const sugg = status === 200 && body.playing ? {
    status: 200,
    body: {
      playing: true, track_uri: body.track?.uri,
      context: body.context ?? null,
      suggestions: body.suggestions || [], subsets: body.subsets || [],
      subset_targets: body.subset_targets || [], homes: body.homes || [],
      inputs: body.inputs || [],
      homeless_id: body.homeless_id ?? null,
    },
  } : { status: 200, body: { playing: false } };
  routes["GET /api/now/suggest"] = sugg;
  routes["GET /api/now/suggest?force=1"] = sugg;
}

// ---- boot ------------------------------------------------------------------

routes["GET /api/status"] = { status: 200, body: { authed: true, me: { name: "me" } } };
setNow({ status: 200, body: { playing: false, poll_after_ms: 999999 } });

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
  setNow({
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
  });
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  routes["POST /api/split/PL5/decide"] = { status: 200, body: { changed: true, decision: { action: "keep", to_id: "H1" }, remaining: 0 } };

  resetLog();
  fireKey("1");
  await tick();
  check("I2 baseline: `1` keeps while the live card is on screen",
        posts("/api/split/PL5/decide") === 1, `${posts("/api/split/PL5/decide")} POST(s)`);
  check("I2 the kept card celebrates with the same big drawn check",
        $$("now-card").innerHTML.includes('class="done-mark"') &&
        /kept to/.test($$("now-card").innerHTML),
        `mark=${$$("now-card").innerHTML.includes('class="done-mark"')}`);

  // Re-poll first: the keep above wrote `decided` into nowState.sitting, and
  // a decided track short-circuits the keyboard for a legitimate reason
  // ("final — nothing left to press"), which would mask the bug under test.
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("I2 the track is undecided again after a fresh poll",
        run(`Object.keys(nowState.sitting.decided).length`) === 0, "");

  // Now a failed poll: nowState is deliberately left intact, card replaced.
  routes["GET /api/now?light=1"] = { status: 500, body: { detail: "boom" } };
  run(`pollNow()`);
  await tick();
  run("stopNowPolling()");
  resetLog();
  fireKey("1");
  fireKey("r");
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
  // V6 — the input switcher and now-actions live in the merged header, shown
  // only on the Now view; show() carries the body class that gates them.
  check("V6 leaving Now drops the on-now body class",
        run(`document.body.classList.contains("on-now")`) === false, "");
  run(`show("now")`);
  check("V6 the Now view sets the on-now body class",
        run(`document.body.classList.contains("on-now")`) === true, "");
}

{
  // V1 — a cooldown renders a countdown card with real h/m arithmetic and a
  // resume time, never Math.round'ed hours ("~178 min" used to say "3 hours",
  // "~25 min" said "0 hours").
  setNow({
    status: 200,
    body: { playing: false, cooldown: "cooldown — try again in ~178 min", poll_after_ms: 999999 },
  });
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  let card = $$("now-card").innerHTML;
  check("V1 a 178-min cooldown shows h/m, not a rounded hour count",
        /2h 58m/.test(card) && !/3 hours/.test(card), JSON.stringify(card.slice(0, 120)));
  check("V1 the card names the resume time of day",
        /around <b>\d{1,2}:\d{2}<\/b>/.test(card), JSON.stringify(card.slice(0, 160)));

  setNow({
    status: 200,
    body: { playing: false, cooldown: "cooldown — try again in ~25 min", poll_after_ms: 999999 },
  });
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
    setNow({
      status: 200,
      body: { playing: false, cooldown: "cooldown — try again in ~178 min", poll_after_ms: 999999 },
    });
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
  setNow({ status: 200, body: { playing: false, poll_after_ms: 999999 } });
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
  setNow({
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
  });
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
  setNow({
    status: 200,
    body: {
      playing: true, poll_after_ms: 999999,
      track: { uri: "spotify:track:u9", name: "T", artists: [{ name: "A" }], sortable: true, image: null },
      context: { id: "SP9", name: "sit" },
      sitting: { split_id: "PL9", pile_id: "p1", pile_name: "dreamy pile",
                 uris: ["spotify:track:u9", "u2"], decided: { u2: { action: "keep", to_id: "H1" } } },
      suggestions: [], homes: [], inputs: [],
    },
  });
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  const status = $$("now-sitting-status").innerHTML;
  check("V4 the sitting banner names the pile and the progress",
        $$("now-sitting-bar").hidden === false && /dreamy pile/.test(status) && /1 of 2 left/.test(status),
        JSON.stringify(status.slice(0, 100)));
  // The old #now-context line is gone; the input-switch trigger inherited
  // the "what's playing from" role, and it must not echo the pile either.
  check("V4 nothing above the banner repeats the pile name",
        !/dreamy pile/.test($$("input-switch-label").textContent),
        JSON.stringify($$("input-switch-label").textContent));
}

// ============================================================================
// W — weak guesses: sub-threshold suggestions render as labeled guesses,
// visually distinct from confident ones, in both the ordinary now card and
// the sitting decide list. The server only ever sends weak entries when
// NOTHING was confident, so the lead-in hint keys off suggestions[0].
// ============================================================================
{
  setNow({
    status: 200,
    body: {
      playing: true, poll_after_ms: 999999,
      track: { uri: "spotify:track:w1", name: "T", artists: [{ name: "A" }], sortable: true, image: null },
      context: { id: "IN1", name: "[In]", is_input: true },
      sitting: null,
      suggestions: [
        { playlist_id: "H1", pct: 3, reasons: ["1 similar track already here"], already: false, weak: true },
        { playlist_id: "H2", pct: 2, reasons: ["artist tags: cumbia"], already: false, weak: true },
      ],
      homes: [{ id: "H1", name: "Guess One", folder: "" }, { id: "H2", name: "Guess Two", folder: "" }],
      inputs: [],
    },
  });
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  const card = $$("now-card").innerHTML;
  check("W1 the now card leads weak entries with the guesses hint",
        /No confident match — closest guesses:/.test(card), JSON.stringify(card.slice(0, 200)));
  check("W1 weak entries render as .sugg.weak buttons, reasons intact",
        (card.match(/class="sugg weak"/g) || []).length === 2 &&
        /similar track already here/.test(card), JSON.stringify(card.slice(0, 200)));
  check("W1 the empty-list hint does not double up",
        !/No confident match — use Add to…/.test(card), "");

  // Confident entries must render exactly as before — no hint, no weak class.
  routes["GET /api/now/suggest?force=1"].body.suggestions = [
    { playlist_id: "H1", pct: 90, reasons: ["3 tracks by A here"], already: false },
  ];
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("W1 confident suggestions keep today's rendering",
        !/closest guesses/.test($$("now-card").innerHTML) &&
        !/class="sugg weak"/.test($$("now-card").innerHTML), "");

  // And the sitting decide list gets the same treatment.
  routes["GET /api/now?light=1"].body.sitting =
    { split_id: "PLW", pile_id: "p1", pile_name: "pile", uris: ["spotify:track:w1"], decided: {} };
  routes["GET /api/now/suggest?force=1"].body.suggestions = [
    { playlist_id: "H1", pct: 3, reasons: ["artist tags: cumbia"], already: false, weak: true },
  ];
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  const decide = $$("now-card").innerHTML;
  check("W2 the decide list leads weak entries with the guesses hint",
        /No confident match — closest guesses:/.test(decide) &&
        (decide.match(/class="sugg weak"/g) || []).length === 1,
        JSON.stringify(decide.slice(0, 200)));
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

// ============================================================================
// C-tab — the Split tab: picker lists eligible playlists, Back returns here
// ============================================================================
{
  resetLog();
  routes["GET /api/playlists"] = { playlists: [
    { id: "liked", name: "Liked Songs", total: 900, editable: false, role: null },
    { id: "PB", name: "big", total: 250, editable: true, role: null },
    { id: "PS", name: "small", total: 30, editable: true, role: null },
    { id: "PB2", name: "bigger", total: 400, editable: true, role: null },
  ], fetched_at: 0 };
  // GET queue is deliberately global regardless of the path id (M4).
  routes["GET /api/split/_/queue"] = {
    queue: { state: "paused", playlist_id: "PB2", pending: ["p2"], current: null },
    pacing: {} };
  run("showSplitPicker()");
  await tick();
  const rows = $$("splitpick-list").children;
  check("C-tab picker shows only 100+-track, non-Liked playlists",
        rows.length === 2, `${rows.length} rows`);
  check("C-tab the in-progress split is pinned first",
        /bigger/.test(rows[0]?.innerHTML || ""),
        (rows[0]?.innerHTML || "").slice(0, 60));
  check("C-tab the picker view is the visible one",
        $$("view-splitpick").hidden === false);
  check("C-tab opening the picker costs zero Spotify-priced POSTs",
        log.every((c) => c.method === "GET"), JSON.stringify(log));

  routes["GET /api/split/PB2"] = { status: 200, body: splitBody(null) };
  routes["GET /api/split/PB2/queue"] = { queue: { state: "done", playlist_id: "PB2",
    pending: [], current: null }, pacing: {} };
  rows[0].onclick();
  await tick();
  check("C-tab clicking a row opens the split view",
        $$("view-split").hidden === false);
  run(`$("btn-split-back").onclick()`);
  await tick();
  check("C-tab Back returns to the picker, not Playlists",
        $$("view-splitpick").hidden === false && $$("view-lists").hidden === true);
}

// ============================================================================
// RN — the rename offer: the only price this branch computes client-side
// ============================================================================
// The server re-derives this number and 409s if it disagrees, so the two
// computations have to walk the same collection. Nothing covered the offer at
// all until review finding I2 — which was exactly a mismatch between them —
// so the count, the quoted price, and the number the POST carries are all
// pinned here.
{
  const matPiles = (names) => names.map((n, i) => ({
    id: `p${i}`, name: n.replace(/^\{src\} · /, ""), tags: ["a"], uris: ["u1"],
    decided: 0, total: 1, materialise_calls: 2,
    materialised: { playlist_id: `SP${i}`, name: n, pile_id: `p${i}` },
  }));
  const renameBody = (names) => ({
    piles: matPiles(names), decided: {}, active_sitting: null,
    params: { resolution: 1, min_pile: 15 },
  });

  resetLog();
  routes["GET /api/split/PRN"] = { status: 200, body: renameBody(
    ["jazz · funk", "{src} · already done", "dub · techno"]) };
  await run(`openSplit("PRN", "{src}")`);
  await tick();
  const btn = $$("btn-rename-outputs");
  check("RN the offer appears when outputs still carry bare pile names",
        btn.hidden === false, `hidden=${btn.hidden}`);
  check("RN it counts only the ones missing the prefix, and prices them 1:1",
        /Rename 2 saved playlists/.test(btn.textContent)
        && /\(2 calls\)/.test(btn.textContent),
        JSON.stringify(btn.textContent));

  routes["POST /api/split/PRN/rename_outputs"] = { ok: true, renamed: [] };
  resetLog();
  await run(`$("btn-rename-outputs").onclick()`);
  await tick();
  check("RN pressing it POSTs exactly once",
        posts("/api/split/PRN/rename_outputs") === 1,
        `${posts("/api/split/PRN/rename_outputs")} POST(s)`);
  // The echo the server validates against. If the button ever displays one
  // number and sends another, the action 409s and nothing is renamed.
  check("RN the POST echoes the same number the button displayed",
        bodies("/api/split/PRN/rename_outputs")[0]?.expected_calls === 2,
        JSON.stringify(bodies("/api/split/PRN/rename_outputs")[0]));
  check("RN the offer is withdrawn once it succeeds",
        $$("btn-rename-outputs").hidden === true,
        `hidden=${$$("btn-rename-outputs").hidden}`);

  // Nothing to do must not advertise a paid action that would rename nothing.
  routes["GET /api/split/PRN2"] = { status: 200, body: renameBody(
    ["{src} · jazz · funk", "{src} · dub · techno"]) };
  await run(`openSplit("PRN2", "{src}")`);
  await tick();
  check("RN no offer when every output already carries the prefix",
        $$("btn-rename-outputs").hidden === true,
        `hidden=${$$("btn-rename-outputs").hidden}`);
}

// ============================================================================
// S1 — tablet share: one GET for targets, one POST per send, right body
// ============================================================================
{
  check("S1 openSharePop exists", run(`typeof openSharePop`) === "function",
        run(`typeof openSharePop`));
  routes["GET /api/share/targets"] =
    { targets: ["Alice Example", "bob99"], updated: 123 };
  let release;
  const pending = new Promise((r) => (release = r));
  routes["POST /api/share/track"] = () => pending;

  run(`nowState = { playing: true, track: {
         uri: "spotify:track:t1", name: "Song", artists: [{ name: "A" }] } }`);
  resetLog();
  await run(`openSharePop()`);
  await tick();
  check("S1 opening the picker fetches the cached targets once",
        gets("/api/share/targets") === 1, `${gets("/api/share/targets")} GET(s)`);
  const pop = $$("share-pop");
  check("S1 the picker rendered a button per target",
        /Alice Example/.test(pop.innerHTML) && /bob99/.test(pop.innerHTML),
        JSON.stringify(pop.innerHTML.slice(0, 80)));

  const target = $$("share-t-1");   // bob99
  target.onclick(); target.onclick();          // double-click must not double-send
  await tick();
  check("S1 two clicks send exactly one share",
        posts("/api/share/track") === 1, `${posts("/api/share/track")} POST(s)`);
  check("S1 the POST carries title, artist and friend — never a track id",
        JSON.stringify(bodies("/api/share/track")[0]) ===
        JSON.stringify({ title: "Song", artist: "A", friend: "bob99" }),
        JSON.stringify(bodies("/api/share/track")[0]));
  release({ status: 200, body: { ok: true, targets: ["bob99"] } });
  await tick();
  check("S1 the picker closes once the share lands",
        pop.hidden === true, `hidden=${pop.hidden}`);
}

// ============================================================================
// NB — single now bar: the input popover replaces the <select>
// ============================================================================
{
  check("NB openInputPop exists", run(`typeof openInputPop`) === "function",
        String(run(`typeof openInputPop`)));
  try {
    const d = { playing: true, is_playing: true,
      context: { id: "in2", name: "[B]", is_input: true },
      inputs: [
        { id: "in1", name: "[A]", set: "buffer", has_track: false },
        { id: "in2", name: "[B]", set: "buffer", has_track: false },
        { id: "o1", name: "<kept>", set: "other", has_track: true },
        { id: "o2", name: "<folded away>", set: "other", has_track: false },
      ] };
    run(`nowSetsExpanded = false; paintNowControls(${JSON.stringify(d)})`);
    check("NB the trigger is shown wearing the playing input's name",
          $$("btn-input-switch").hidden === false
          && /\[B\]/.test($$("input-switch-label").textContent),
          `hidden=${$$("btn-input-switch").hidden} label=` +
          JSON.stringify($$("input-switch-label").textContent));

    run(`openInputPop()`);
    const pop = $$("input-pop");
    check("NB opening renders a row per visible input into #input-pop",
          pop.hidden === false && /ip-in1/.test(pop.innerHTML)
          && /\[A\]/.test(pop.innerHTML),
          `hidden=${pop.hidden} html=${JSON.stringify(pop.innerHTML.slice(0, 120))}`);
    check("NB a folded set still peeks the row that contains the track",
          /ip-o1/.test(pop.innerHTML) && !/ip-o2/.test(pop.innerHTML),
          JSON.stringify(pop.innerHTML.slice(0, 200)));
    check("NB the fold toggle lives inside the panel, not the top bar",
          /btn-now-sets/.test(pop.innerHTML), JSON.stringify(pop.innerHTML.slice(0, 200)));

    run(`$("btn-now-sets").onclick()`);
    check("NB expanding inside the panel reveals the folded row",
          /ip-o2/.test($$("input-pop").innerHTML),
          JSON.stringify($$("input-pop").innerHTML.slice(0, 200)));
    run(`$("btn-now-sets").onclick()`);   // fold back for a stable end state

    routes["POST /api/player/play"] = { ok: true };
    resetLog();
    run(`openInputPop()`);
    await run(`$("ip-in1").onclick()`);
    await tick();
    check("NB picking a row starts that input exactly once",
          posts("/api/player/play") === 1
          && bodies("/api/player/play")[0]?.input_id === "in1",
          `${posts("/api/player/play")} POST(s), body=` +
          JSON.stringify(bodies("/api/player/play")[0]));
    check("NB and the panel closes on pick",
          $$("input-pop").hidden === true, `hidden=${$$("input-pop").hidden}`);

    // The trigger never leaves the bar: hiding it made every other top-row
    // button jump when the first poll landed. Unusable states PARK it —
    // still there, disabled, saying why.
    run(`paintNowControls(${JSON.stringify({ cooldown: true, inputs: d.inputs })})`);
    check("NB a cooldown parks the trigger instead of hiding it",
          $$("btn-input-switch").hidden === false
          && $$("btn-input-switch").disabled === true
          && /cooling down/.test($$("input-switch-label").textContent),
          `hidden=${$$("btn-input-switch").hidden} disabled=${$$("btn-input-switch").disabled} ` +
          `label=${JSON.stringify($$("input-switch-label").textContent)}`);
    run(`paintNowControls(${JSON.stringify({ inputs: [] })})`);
    check("NB no inputs parks the trigger too — the bar never reflows",
          $$("btn-input-switch").hidden === false
          && $$("btn-input-switch").disabled === true,
          `hidden=${$$("btn-input-switch").hidden} disabled=${$$("btn-input-switch").disabled}`);
    run(`paintNowControls(${JSON.stringify(d)})`);
    check("NB a usable state re-arms the parked trigger",
          $$("btn-input-switch").disabled === false
          && /\[B\]/.test($$("input-switch-label").textContent),
          `disabled=${$$("btn-input-switch").disabled}`);
  } catch (e) {
    check("NB scenario ran without throwing", false, String(e));
  }
}

// ============================================================================
// RD — the random buffer list row. The draw is pure client-side arithmetic
// over data the poll already carried, so the ONLY Spotify call it can ever
// cost is the one play it starts. The properties that matter: it never draws
// outside the buffer set, and it never hands back what is already playing —
// a "random" button that restarts the current list reads as broken.
// ============================================================================
{
  check("RD pickRandomBuffer exists",
        run(`typeof pickRandomBuffer`) === "function",
        String(run(`typeof pickRandomBuffer`)));
  try {
    const ins = [
      { id: "in1", name: "[A]", set: "buffer" },
      { id: "in2", name: "[B]", set: "buffer" },
      { id: "in3", name: "[C]", set: "buffer" },
      { id: "o1", name: "<other>", set: "other" },
      { id: "tb", name: "Prog", set: "the-bomb" },
      { id: "liked", name: "Liked Songs", set: "buffer" },
    ];
    const J = JSON.stringify(ins);
    // rand() is injected, so the draw is checked exhaustively rather than
    // sampled: every slot of the pool, not "it looked random twice".
    const drawn = [0, 0.34, 0.67, 0.999].map((r) =>
      run(`pickRandomBuffer(${J}, null, () => ${r})?.id`));
    check("RD the pool is the buffer set only — never other, the-bomb or liked",
          drawn.every((id) => ["in1", "in2", "in3"].includes(id)),
          JSON.stringify(drawn));
    check("RD and every buffer list is reachable — no slot is unreachable",
          new Set(drawn).size === 3, JSON.stringify(drawn));

    const excl = [0, 0.4, 0.9].map((r) =>
      run(`pickRandomBuffer(${J}, "in2", () => ${r})?.id`));
    check("RD the playing list is out of the pool, whichever slot is drawn",
          excl.every((id) => id === "in1" || id === "in3") && !excl.includes("in2"),
          JSON.stringify(excl));

    // Excluding the current list can empty the pool. Returning null there
    // would make the row inert exactly when it is the only list you have.
    const solo = run(
      `pickRandomBuffer([{ id: "in1", name: "[A]", set: "buffer" }], "in1", () => 0)?.id`);
    check("RD the sole buffer list is still drawn when it is also the current",
          solo === "in1", String(solo));
    check("RD an empty pool draws nothing rather than throwing",
          run(`pickRandomBuffer([{ id: "o1", set: "other" }], null, () => 0)`) === null,
          String(run(`pickRandomBuffer([{ id: "o1", set: "other" }], null, () => 0)`)));

    // --- the row in the popover -------------------------------------------
    const d = { playing: true, is_playing: true,
      context: { id: "in2", name: "[B]", is_input: true }, inputs: ins };
    run(`nowSetsExpanded = false; paintNowControls(${JSON.stringify(d)})`);
    run(`openInputPop()`);
    check("RD the row renders at the head of the buffer set",
          /ip-random/.test($$("input-pop").innerHTML)
          && $$("input-pop").innerHTML.indexOf("ip-random")
             < $$("input-pop").innerHTML.indexOf("ip-in1"),
          JSON.stringify($$("input-pop").innerHTML.slice(0, 240)));

    routes["POST /api/player/play"] = { ok: true };
    resetLog();
    await run(`$("ip-random").onclick()`);
    await tick();
    const started = bodies("/api/player/play")[0]?.input_id;
    check("RD clicking it starts exactly one buffer list, never the current one",
          posts("/api/player/play") === 1
          && ["in1", "in3"].includes(started),
          `${posts("/api/player/play")} POST(s), input_id=${started}`);
    check("RD and the toast names the list it drew, so the pick is not a black box",
          new RegExp(started === "in1" ? "\\[A\\]" : "\\[C\\]").test($$("toast").textContent),
          JSON.stringify($$("toast").textContent));
    check("RD the panel closes on the draw, like any other pick",
          $$("input-pop").hidden === true, `hidden=${$$("input-pop").hidden}`);

    // One buffer list makes "random" a lie — there is nothing to draw from.
    run(`paintNowControls(${JSON.stringify({ playing: false, inputs: [
      { id: "in1", name: "[A]", set: "buffer" },
      { id: "o1", name: "<other>", set: "other" }] })})`);
    run(`openInputPop()`);
    check("RD a single buffer list hides the row instead of offering a fake draw",
          !/ip-random/.test($$("input-pop").innerHTML)
          && /ip-in1/.test($$("input-pop").innerHTML),
          JSON.stringify($$("input-pop").innerHTML.slice(0, 200)));
    run(`closeInputPop()`);
  } catch (e) {
    check("RD scenario ran without throwing", false, String(e));
  }
}

// ============================================================================
// PV — hold-to-preview: the affordance, the swallowed tap, and the ONE
// budgeted resume call. The resume is real Spotify budget, so "exactly one"
// is the property that matters most here.
// ============================================================================
{
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    routes["GET /api/playlist_preview/h1"] = {
      clips: [{ name: "Song A", artist: "Artist A", url: "https://cdn/a.mp3" },
              { name: "Song B", artist: "Artist B", url: "https://cdn/b.mp3" }],
      next_offset: null, total: 12, tracks: [],
    };
    routes["POST /api/preview_resume"] = { ok: true };
    setNow({ status: 200, body: { playing: false, poll_after_ms: 999999 } });
    // Something IS playing: that is what makes a resume owed on close.
    run(`nowState = { playing: true, is_playing: true, track: { uri: "spotify:track:x" }, homes: new Map() }`);

    const row = new El("pv-row");
    ctx.__row = row;
    ctx.__filed = 0;
    run(`previewHold.attach(__row, "h1", "Deep Cuts",
           { label: "File here", run: () => { globalThis.__filed++; } })`);

    check("PV the row is marked holdable and wears the waveform hint",
          row.classList.contains("holdable")
          && row.children.some((c) => c.className === "hold-hint"),
          `children=${JSON.stringify(row.children.map((c) => c.className))}`);

    // Press, then let the hold mature.
    row.onpointerdown({ clientX: 10, clientY: 10 });
    check("PV pressing paints the countdown on the row",
          row.classList.contains("holding"), "class holding present");
    await new Promise((r) => setTimeout(r, 700));
    await tick();

    check("PV the matured hold opens the player and fetches one page",
          $$("preview-pop").hidden === false && gets("/api/playlist_preview/h1") === 1,
          `hidden=${$$("preview-pop").hidden}, ${gets("/api/playlist_preview/h1")} GET(s)`);
    check("PV the countdown is cleared once it fires",
          !row.classList.contains("holding"), "class holding gone");
    check("PV the hold swallows the row's tap, so nothing is filed by accident",
          run("previewHold.consumeClick()") === true, "click consumed");
    check("PV the first clip is playing",
          AudioStub.made.length === 1 && AudioStub.made[0].src === "https://cdn/a.mp3",
          `${AudioStub.made.length} Audio, src=${AudioStub.made[0]?.src}`);

    // Advancing must stop the old clip before starting the next one.
    run(`$("pv-next").onclick()`);
    await tick();
    check("PV next pauses the previous clip before starting the new one",
          AudioStub.made.length === 2 && AudioStub.made[0].paused === true
          && AudioStub.made[1].src === "https://cdn/b.mp3",
          `made=${AudioStub.made.length}, prevPaused=${AudioStub.made[0]?.paused}`);

    // The popup carries the row's own action: preview then choose, one gesture.
    check("PV the player offers the held row's action", /File here/.test($$("preview-pop").innerHTML),
          "action button rendered");
    run(`$("pv-act").onclick()`);
    await tick();
    check("PV taking that action files exactly once and closes the player",
          ctx.__filed === 1 && $$("preview-pop").hidden === true,
          `filed=${ctx.__filed}, hidden=${$$("preview-pop").hidden}`);
    check("PV closing spends exactly ONE budgeted resume call",
          posts("/api/preview_resume") === 1, `${posts("/api/preview_resume")} POST(s)`);

    // Closing an already-closed player must not spend a second one.
    run("previewHold.stop()");
    await tick();
    check("PV a second close spends no further resume call",
          posts("/api/preview_resume") === 1, `${posts("/api/preview_resume")} POST(s)`);

  } catch (e) {
    check("PV scenario ran without throwing", false, String(e));
  }
}

// ============================================================================
// PV2 — leaving the view must not leave clips playing behind the user. Its
// own block: it must still be exercised when the block above dies early.
// ============================================================================
{
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    run("previewHold.stop()");
    const row2 = new El("pv-row2");
    ctx.__row2 = row2;
    run(`previewHold.attach(__row2, "h1", "Deep Cuts")`);
    row2.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    const opened = $$("preview-pop").hidden === false;
    const playing = AudioStub.made.length > 0;
    run(`show("lists")`);
    await tick();
    // The stop is a fade-out now, not a cut: give the ~180ms landing time
    // to finish before insisting the audio is actually paused.
    await new Promise((r) => setTimeout(r, 400));
    check("PV2 navigating away stops the player instead of leaving it playing",
          opened && playing && $$("preview-pop").hidden === true
          && AudioStub.made.every((a) => a.paused),
          `opened=${opened}, playing=${playing}, hidden=${$$("preview-pop").hidden}, ` +
          `paused=${JSON.stringify(AudioStub.made.map((a) => a.paused))}`);
  } catch (e) {
    check("PV2 scenario ran without throwing", false, String(e));
  }
}

// ============================================================================
// BR — a remove made in blind mode must name what it removed. The whole point
// of blind mode is that the ear decides, so the card is blurred while the
// decision is open; once the track is gone from the input the decision is
// spent and hiding it only means the input lost a track you never got to see.
// Reuses the peek the tap gesture already sets, so the reveal expires with
// the track instead of quietly turning blind mode off.
// ============================================================================
{
  setNow({
    status: 200,
    body: {
      playing: true, poll_after_ms: 999999,
      track: { uri: "spotify:track:br1", name: "Removed Song",
               artists: [{ name: "Removed Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[In]", is_input: true },
      sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 90, reasons: [], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      inputs: [{ id: "IN1", name: "[In]", has_track: true }],
    },
  });
  run(`show("now"); blindMode = true; applyBlind()`);
  run(`filedUris = {}; nowActions = 0; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("BR the card is blurred while the decision is still open",
        document.body.classList.contains("blind") &&
        !document.body.classList.contains("peeked"),
        `blind=${document.body.classList.contains("blind")} ` +
        `peeked=${document.body.classList.contains("peeked")}`);

  routes["POST /api/act"] = { status: 200, body: {} };
  await run(`nowRemove()`);
  await tick();
  check("BR removing in blind mode reveals what was removed",
        document.body.classList.contains("peeked"),
        `peeked=${document.body.classList.contains("peeked")} ` +
        `toast=${JSON.stringify($$("toast").textContent)} ` +
        `blindMode=${run("blindMode")} ` +
        `nowTrack=${run("nowState && nowState.track && nowState.track.uri")}`);
  check("BR the reveal is pinned to the removed track, not whatever plays next",
        run("peekedUri") === "spotify:track:br1", String(run("peekedUri")));

  // The reveal has to expire with the track. Otherwise the next track arrives
  // already named and blind mode is off without anyone having asked for it.
  routes["GET /api/now?light=1"].body.track =
    { uri: "spotify:track:br2", name: "Next Song",
      artists: [{ name: "Next Artist" }], sortable: true, image: null };
  routes["GET /api/now/suggest?force=1"].body.track_uri = "spotify:track:br2";
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("BR the reveal falls away when the next track starts",
        !document.body.classList.contains("peeked") && run("peekedUri") === null,
        `peeked=${document.body.classList.contains("peeked")} ` +
        `peekedUri=${run("peekedUri")}`);

  run(`blindMode = false; applyBlind()`);   // stable end state for the summary
}

// ============================================================================
// PV3 — the end of the playlist gives the music back, and clips fade.
// Running out of clips must fire the SAME single budgeted resume close()
// would (silence until the user finds the close button serves nobody), keep
// the popup up, and spend nothing further on a following close. Playing a
// clip again after that re-arms exactly one more resume.
// ============================================================================
{
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    routes["GET /api/playlist_preview/h1"] = {
      clips: [{ name: "Song A", artist: "Artist A", url: "https://cdn/a.mp3" },
              { name: "Song B", artist: "Artist B", url: "https://cdn/b.mp3" }],
      next_offset: null, total: 2, tracks: [],
    };
    routes["POST /api/preview_resume"] = { ok: true };
    // A hidden Now view makes pollNow a no-op, so no earlier block's still-in-
    // flight repoll can land here and overwrite nowState with ITS fixture.
    // resumeSpotify schedules one ~900ms out on a bare setTimeout that
    // stopNowPolling cannot cancel; that is what silently replaced this
    // block's is_playing and made the resume below look unspent.
    $$("view-now").hidden = true;
    // A fresh uri, so PV's stash for h1 (same playlist, PV4's territory)
    // cannot leak in here and hand this block a half-walked playlist.
    run(`nowState = { playing: true, is_playing: true, track: { uri: "spotify:track:pv3" }, homes: new Map() }`);

    const row3 = new El("pv3-row");
    ctx.__row3 = row3;
    run(`previewHold.attach(__row3, "h1", "Deep Cuts")`);
    row3.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();

    const first = AudioStub.made[0];
    check("PV3 a clip starts silent and fades in",
          first && (first.volume ?? 1) < 0.5, `volume=${first?.volume}`);
    await new Promise((r) => setTimeout(r, 600));
    check("PV3 the fade-in lands on full volume",
          first.volume === 1, `volume=${first.volume}`);

    first.onended();                       // 30s ran out → auto-advance
    await tick();
    AudioStub.made.at(-1).onended();       // last clip ran out → playlist over
    await tick();
    check("PV3 the end of the playlist resumes the user's music by itself",
          posts("/api/preview_resume") === 1, `${posts("/api/preview_resume")} POST(s)`);
    check("PV3 the popup stays up and says what happened",
          $$("preview-pop").hidden === false
          && /back to your music/.test($$("preview-pop").innerHTML),
          `hidden=${$$("preview-pop").hidden}`);

    // prev after the auto-resume: preview audio takes focus again, so ONE
    // more resume becomes owed — and close must pay exactly it, no more.
    run(`$("pv-prev").onclick()`);
    await tick();
    run("previewHold.stop()");
    await tick();
    check("PV3 replaying after the auto-resume re-arms exactly one more resume",
          posts("/api/preview_resume") === 2, `${posts("/api/preview_resume")} POST(s)`);
  } catch (e) {
    check("PV3 scenario ran without throwing", false, String(e));
  }
}

// ============================================================================
// PV4 — reopening a playlist's preview within the same track session must
// CONTINUE where it left off — same clip, no refetch, zero requests — and a
// new playing track must start the walk over. The stash is per playlist and
// dies with the track, because the preview serves that track's filing.
// ============================================================================
{
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    // A hidden Now view keeps the close→resume repoll (~900ms out) from
    // rewriting nowState mid-scenario and wiping the stash under test.
    $$("view-now").hidden = true;
    routes["GET /api/playlist_preview/h1"] = {
      clips: [{ name: "Song A", artist: "Artist A", url: "https://cdn/a.mp3" },
              { name: "Song B", artist: "Artist B", url: "https://cdn/b.mp3" },
              { name: "Song C", artist: "Artist C", url: "https://cdn/c.mp3" }],
      next_offset: null, total: 3, tracks: [],
    };
    routes["POST /api/preview_resume"] = { ok: true };
    run(`nowState = { playing: true, is_playing: true, track: { uri: "spotify:track:t1" }, homes: new Map() }`);

    const row4 = new El("pv4-row");
    ctx.__row4 = row4;
    run(`previewHold.attach(__row4, "h1", "Deep Cuts")`);
    row4.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    run(`$("pv-next").onclick()`);       // move to clip 2 before leaving
    await tick();
    run("previewHold.stop()");
    await tick();

    row4.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    check("PV4 reopening continues at the clip it left, without refetching",
          gets("/api/playlist_preview/h1") === 1
          && AudioStub.made.at(-1)?.src === "https://cdn/b.mp3"
          && $$("preview-pop").hidden === false,
          `${gets("/api/playlist_preview/h1")} GET(s), last src=${AudioStub.made.at(-1)?.src}`);
    check("PV4 the resumed card names the resumed clip",
          /Song B/.test($$("preview-pop").innerHTML), "pv-now shows Song B");

    // A different playing track is a different decision: start over.
    run("previewHold.stop()");
    await tick();
    run(`nowState = { playing: true, is_playing: true, track: { uri: "spotify:track:t2" }, homes: new Map() }`);
    row4.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    check("PV4 a new playing track starts the walk over, fetching afresh",
          gets("/api/playlist_preview/h1") === 2
          && AudioStub.made.at(-1)?.src === "https://cdn/a.mp3",
          `${gets("/api/playlist_preview/h1")} GET(s), last src=${AudioStub.made.at(-1)?.src}`);
    run("previewHold.stop()");
    await tick();
  } catch (e) {
    check("PV4 scenario ran without throwing", false, String(e));
  }
}

// ============================================================================
// MT — manual mode's one exception to "no automatic fetches": a song that
// provably played out while the tool is open refetches ONCE. The same uri is
// never chased twice (a stopped player would otherwise loop), auto mode
// leaves the moment to the server's own schedule, and a hidden tab remembers
// instead of fetching — paying its one fetch on return.
// ============================================================================
{
  try {
    run("previewHold.stop(); stopNowPolling(); stopNowTicker()");
    // PV3's resumes schedule force repolls ~900ms out; each of those calls
    // renderNow, whose startNowTicker would silently kill the ticker this
    // block is timing. Let them land and die before counting anything.
    await new Promise((r) => setTimeout(r, 1000));
    await tick();
    run("stopNowPolling(); stopNowTicker()");
    resetLog();
    // The server still reports the played-out track (Spotify not yet
    // advanced): the guard is what keeps this from becoming a poll loop.
    setNow({ status: 200, body: {
      playing: true, is_playing: true, poll_after_ms: 999999,
      track: { uri: "spotify:track:mt1", name: "Ended Song",
               artists: [{ name: "A" }], duration_ms: 5200, sortable: true, image: null },
      progress_ms: 5200,
      context: { id: "IN1", name: "[In]", is_input: true },
      sitting: null, suggestions: [], homes: [], inputs: [],
    } });
    $$("view-now").hidden = false;
    run(`nowManual = true; playedOutUri = null; playedOutWhileHidden = false`);
    run(`startNowTicker({ is_playing: true, progress_ms: 5000 },
                        { uri: "spotify:track:mt1", duration_ms: 5200 })`);
    await new Promise((r) => setTimeout(r, 1300));
    await tick();
    check("MT a played-out song refetches once in manual mode",
          gets("/api/now?light=1") === 1, `${gets("/api/now?light=1")} GET(s)`);
    // renderNow restarted the ticker on the same (still-ended) answer; give
    // it another beat to prove the guard holds and no loop starts.
    await new Promise((r) => setTimeout(r, 1300));
    check("MT the same uri is never chased twice",
          gets("/api/now?light=1") === 1, `${gets("/api/now?light=1")} GET(s)`);

    run(`nowManual = false; stopNowTicker()`);
    run(`startNowTicker({ is_playing: true, progress_ms: 5000 },
                        { uri: "spotify:track:mt2", duration_ms: 5200 })`);
    await new Promise((r) => setTimeout(r, 1300));
    check("MT auto mode leaves the moment to the poll schedule",
          gets("/api/now?light=1") === 1, `${gets("/api/now?light=1")} GET(s)`);

    document.hidden = true;
    run(`nowManual = true; stopNowTicker()`);
    run(`startNowTicker({ is_playing: true, progress_ms: 5000 },
                        { uri: "spotify:track:mt3", duration_ms: 5200 })`);
    await new Promise((r) => setTimeout(r, 1300));
    check("MT nothing fires while the tab is hidden",
          gets("/api/now?light=1") === 1, `${gets("/api/now?light=1")} GET(s)`);
    document.hidden = false;
    for (const h of handlers.visibilitychange || []) h();
    await tick();
    check("MT coming back pays the remembered played-out fetch",
          gets("/api/now?light=1") === 2, `${gets("/api/now?light=1")} GET(s)`);

    run(`nowManual = false; stopNowPolling(); stopNowTicker()`);
  } catch (e) {
    check("MT scenario ran without throwing", false, String(e));
  } finally {
    document.hidden = false;
  }
}

// ============================================================================
// PV5 — the preview player's promises about the USER'S OWN music and the
// user's own decision. Four properties, each one a bug that shipped:
//   * previewing while PAUSED must not spend a budgeted call to start music
//     the user deliberately stopped (/api/now's `playing` merely means a
//     track object exists; `is_playing` is the transport);
//   * the card's action files whatever is playing WHEN TAPPED, so once the
//     player learned to advance the music by itself it could file — and
//     remove from the input — a track the user never judged;
//   * a clip whose CDN token has expired must skip, not strand the medley;
//   * the iOS tap-to-play path must re-arm the resume like the main path,
//     or the music stays paused after an end-of-playlist resume.
// ============================================================================
{
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    $$("view-now").hidden = true;   // same isolation as PV3/PV4
    routes["GET /api/playlist_preview/h5"] = {
      clips: [{ name: "Song A", artist: "Artist A", url: "https://cdn/a5.mp3" },
              { name: "Song B", artist: "Artist B", url: "https://cdn/b5.mp3" }],
      next_offset: null, total: 2, tracks: [],
    };
    routes["POST /api/preview_resume"] = { ok: true };
    routes["POST /api/act"] = { status: 200, body: {} };

    // --- paused: a preview owes nothing back -------------------------------
    run(`nowState = { playing: true, is_playing: false,
                      track: { uri: "spotify:track:p5" }, homes: new Map() }`);
    const row5 = new El("pv5-row");
    ctx.__row5 = row5;
    run(`previewHold.attach(__row5, "h5", "Paused Home")`);
    row5.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    run("previewHold.stop()");
    await tick();
    check("PV5 previewing while paused spends no resume call",
          posts("/api/preview_resume") === 0,
          `${posts("/api/preview_resume")} POST(s)`);

    // --- the action is bound to the track it was opened for -----------------
    resetLog();
    AudioStub.made.length = 0;
    run(`nowState = { playing: true, is_playing: true,
                      track: { uri: "spotify:track:judged" }, homes: new Map() }`);
    ctx.__filed5 = 0;
    const row5b = new El("pv5-row-b");
    ctx.__row5b = row5b;
    run(`previewHold.attach(__row5b, "h5", "Home",
           { label: "File here", run: () => { globalThis.__filed5++; } })`);
    row5b.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    // The music moved on under the open popup — exactly what the
    // end-of-playlist resume and the played-out refetch now cause.
    run(`nowState = { playing: true, is_playing: true,
                      track: { uri: "spotify:track:NOTjudged" }, homes: new Map() }`);
    run(`$("pv-act").onclick()`);
    await tick();
    check("PV5 a decision does not land on a track that arrived after it",
          ctx.__filed5 === 0 && posts("/api/act") === 0,
          `filed=${ctx.__filed5}, ${posts("/api/act")} act POST(s)`);
    check("PV5 and the user is told why nothing happened",
          /moved on/.test($$("toast").textContent),
          JSON.stringify($$("toast").textContent));

    // Same gesture, same track still playing: the action must still work —
    // the guard must not have cost the popup its whole purpose.
    resetLog();
    row5b.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    run(`$("pv-act").onclick()`);
    await tick();
    check("PV5 the same track still files, one gesture as before",
          ctx.__filed5 === 1, `filed=${ctx.__filed5}`);

    // --- an expired clip URL skips instead of stranding the medley ---------
    resetLog();
    AudioStub.made.length = 0;
    const row5c = new El("pv5-row-c");
    ctx.__row5c = row5c;
    run(`previewHold.attach(__row5c, "h5", "Home")`);
    row5c.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    const firstSrc = AudioStub.made.at(-1)?.src;
    AudioStub.made.at(-1).onerror();     // Deezer 403: `error`, never `ended`
    await tick();
    check("PV5 a dead clip advances the medley instead of freezing it",
          firstSrc === "https://cdn/a5.mp3"
          && AudioStub.made.at(-1)?.src === "https://cdn/b5.mp3",
          `first=${firstSrc}, now=${AudioStub.made.at(-1)?.src}`);

    // --- iOS: tap-to-play re-arms the resume the auto-resume spent ---------
    resetLog();
    AudioStub.made.length = 0;
    run("previewHold.stop()");
    await tick();
    resetLog();
    const row5d = new El("pv5-row-d");
    ctx.__row5d = row5d;
    run(`previewHold.attach(__row5d, "h5", "Home")`);
    row5d.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    // This popup RESUMES from the stash the dead-clip section left behind, so
    // it opens on the last clip and one `ended` is the end of the playlist.
    AudioStub.made.at(-1).onended();     // last clip over → playlist over
    await tick();
    const afterAuto = posts("/api/preview_resume");
    // Back a clip, with autoplay refused — the iOS case needsTap exists for.
    AudioStub.refuse = true;
    run(`$("pv-prev").onclick()`);
    await tick();
    const askedForTap = /pv-play/.test($$("preview-pop").innerHTML);
    AudioStub.refuse = false;
    run(`$("pv-play").onclick()`);
    await tick();
    run("previewHold.stop()");
    await tick();
    check("PV5 the end of the playlist resumed the music once",
          afterAuto === 1, `${afterAuto} POST(s)`);
    check("PV5 an autoplay-refused clip asks for the unblocking tap",
          askedForTap, "pv-play offered");
    check("PV5 that tap re-arms the resume, so closing gives the music back",
          posts("/api/preview_resume") === 2,
          `${posts("/api/preview_resume")} POST(s)`);

    // --- one press of next moves ONE clip ----------------------------------
    // Retiring a clip clears its src, which is itself an `error` in a real
    // browser. With the dead-clip handler still attached, that error read as
    // "this clip is broken" and advanced again — and the clip THAT retired
    // fired the next one, so a single press ran the medley to the end of the
    // playlist. Five clips, because with two the runaway looks like an
    // ordinary end-of-playlist.
    resetLog();
    run("previewHold.stop()");
    await tick();
    AudioStub.made.length = 0;
    routes["GET /api/playlist_preview/h6"] = {
      clips: [1, 2, 3, 4, 5].map((n) => ({
        name: `Song ${n}`, artist: `Artist ${n}`, url: `https://cdn/${n}6.mp3` })),
      next_offset: null, total: 5, tracks: [],
    };
    const row5e = new El("pv5-row-e");
    ctx.__row5e = row5e;
    run(`previewHold.attach(__row5e, "h6", "Long Home")`);
    row5e.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();
    const opened = AudioStub.made.at(-1)?.src;
    run(`$("pv-next").onclick()`);
    await new Promise((r) => setTimeout(r, 30));
    await tick();
    check("PV5 next advances one clip, not the whole playlist",
          opened === "https://cdn/16.mp3"
          && AudioStub.made.at(-1)?.src === "https://cdn/26.mp3"
          && AudioStub.made.length === 2,
          `opened=${opened}, now=${AudioStub.made.at(-1)?.src}, made=${AudioStub.made.length}`);
    check("PV5 and the card still names the clip it landed on",
          /Song 2/.test($$("preview-pop").innerHTML),
          JSON.stringify($$("preview-pop").innerHTML.slice(0, 160)));
  } catch (e) {
    check("PV5 scenario ran without throwing", false, String(e));
  } finally {
    // Hand the next block a Now view it can actually poll into: pollNow is a
    // no-op while this is hidden, so leaving it set makes the FOLLOWING
    // block's card go stale and fail for a reason that isn't its own.
    $$("view-now").hidden = false;
    AudioStub.refuse = false;
    run("previewHold.stop(); stopNowPolling()");
  }
}

// ============================================================================
// RB — "Remove from input" lives in the playback strip, not the minor row.
// Sorting is skipping, so the destructive control belongs where the transport
// controls are — but it must stay conditional on an input context, and it
// must NOT sit adjacent to the 64px Next target (mis-tap risk), which is why
// the assertions pin its POSITION and not merely its existence.
// ============================================================================
{
  const nowBody = (isInput) => ({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:rb1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[In]", is_input: isInput },
      sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 90, reasons: [], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      inputs: [{ id: "IN1", name: "[In]", has_track: true }],
    },
  });
  const html = () => $$("now-card").innerHTML;
  const at = (needle) => html().indexOf(needle);

  setNow(nowBody(true));
  run(`filedUris = {}; nowActions = 0; pollNow(true)`);
  await tick();
  run("stopNowPolling()");

  check("RB the remove button renders when playing from an input",
        at('id="btn-now-remove"') !== -1, `index ${at('id="btn-now-remove"')}`);
  // The strip is emitted before the card body, so a remove button that sits
  // inside np-buttons necessarily precedes minor-actions. If it is still in
  // the minor row this ordering inverts — that is the whole check.
  check("RB it sits in the playback strip, not the minor-actions row",
        at('id="btn-now-remove"') !== -1 &&
        at('id="btn-now-remove"') < at('minor-actions'),
        `remove@${at('id="btn-now-remove"')} minor@${at('minor-actions')}`);
  check("RB it is inside the transport button group",
        at('np-buttons') !== -1 &&
        at('np-buttons') < at('id="btn-now-remove"') &&
        at('id="btn-now-remove"') < at('np-next-slot'),
        `buttons@${at('np-buttons')} remove@${at('id="btn-now-remove"')} ` +
        `next-slot@${at('np-next-slot')}`);
  // The freshness signal is the bar itself now: a thin yellow marker sits
  // inside the green fill at the position the server last confirmed, and the
  // fill running ahead of it is local extrapolation — the gap IS the age.
  // No numeric timer anywhere — not on the card, not in the top bar.
  check("RB the card does not carry the np-updated line",
        at('np-updated') === -1, `updated@${at('np-updated')}`);
  check("RB the bar carries the last-update marker inside the fill",
        at('id="np-fill"') !== -1 && at('id="np-fill-mark"') !== -1 &&
        at('id="np-fill"') < at('id="np-fill-mark"'),
        `fill@${at('id="np-fill"')} mark@${at('id="np-fill-mark"')}`);
  // The verb row is a centred pair: Remove left of Next, each in its own
  // slot with the deliberate gap between them, so the two most-pressed
  // buttons read as equals.
  check("RB remove sits left of Next in the verb pair",
        at('id="btn-now-remove"') < at('id="btn-now-next"') &&
        at('np-remove-slot') !== -1,
        `remove@${at('id="btn-now-remove"')} next@${at('id="btn-now-next"')} ` +
        `slot@${at('np-remove-slot')}`);
  // The quiet controls flank the progress bar — previous at its left end
  // (back = left), pause at its right — and both stay out of the verb row.
  check("RB previous flanks the bar's left end, pause its right",
        at('id="btn-now-prev"') !== -1 && at('id="btn-now-toggle"') !== -1 &&
        at('id="btn-now-prev"') < at('np-bar') &&
        at('np-bar') < at('id="btn-now-toggle"') &&
        at('id="btn-now-toggle"') < at('np-buttons'),
        `prev@${at('id="btn-now-prev"')} bar@${at('np-bar')} ` +
        `toggle@${at('id="btn-now-toggle"')} buttons@${at('np-buttons')}`);
  // Share lives on the card itself, in the corner beside the cover — not in
  // the top bar. It renders only for a real spotify:track uri.
  check("RB the share button rides the card, beside the cover",
        at('id="btn-share"') !== -1 && at('id="btn-share"') < at('np-progress'),
        `share@${at('id="btn-share"')} progress@${at('np-progress')}`);

  setNow(nowBody(false));
  run(`filedUris = {}; pollNow(true)`);
  await tick();
  run("stopNowPolling()");

  // Was "no remove button when the context is not an input" — the button used
  // to vanish. It is drawn and greyed now (see RG for the full matrix); what
  // this check still owns is that it is NOT offered as a live destructive
  // control outside an input context.
  check("RB the remove button is not live when the context is not an input",
        at('np-danger') === -1 && at('id="btn-now-remove"') !== -1,
        `danger@${at('np-danger')} remove@${at('id="btn-now-remove"')}`);
  // The slot stays so play/pause and Next never shift sideways when the
  // context changes — the same instinct as 1f8ae2c.
  check("RB the reserved slot survives so the transport never shifts",
        at('np-remove-slot') !== -1, `slot@${at('np-remove-slot')}`);
}

// ============================================================================
// RU — removing swaps the strip's Remove for an Undo, for this track only.
// The undo has to live where the button that caused it was: that is where the
// hand already is. It must expire with the track, or the next song inherits an
// undo for a decision made about a different one.
// ============================================================================
{
  resetLog();
  const nowBody = (uri) => ({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri, name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[In]", is_input: true },
      sitting: null, suggestions: [],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      inputs: [{ id: "IN1", name: "[In]", has_track: true }],
    },
  });
  const html = () => $$("now-card").innerHTML;
  const has = (needle) => html().includes(needle);

  setNow(nowBody("spotify:track:ru1"));
  routes["POST /api/act"] = { status: 200, body: {} };
  routes["POST /api/undo"] = { status: 200, body: { restored_to: "IN1" } };
  run(`filedUris = {}; nowActions = 0; removedUri = null; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("RU the strip starts with Remove and no Undo",
        has('id="btn-now-remove"') && !has('id="btn-now-undo-remove"'),
        `remove=${has('id="btn-now-remove"')} undo=${has('id="btn-now-undo-remove"')}`);

  await run(`nowRemove()`);
  await tick();
  check("RU removing puts Undo where Remove was",
        has('id="btn-now-undo-remove"') && !has('id="btn-now-remove"'),
        `undo=${has('id="btn-now-undo-remove"')} remove=${has('id="btn-now-remove"')}`);
  check("RU the Undo sits in the same reserved slot",
        html().indexOf('np-remove-slot') < html().indexOf('id="btn-now-undo-remove"') &&
        html().indexOf('id="btn-now-undo-remove"') < html().indexOf('np-next-slot'),
        `slot@${html().indexOf('np-remove-slot')} ` +
        `undo@${html().indexOf('id="btn-now-undo-remove"')}`);

  // Guarded: an unwired button is a failed check, not an exception that ends
  // the whole run before the later scenarios get to speak.
  const wired = run(`typeof $("btn-now-undo-remove").onclick === "function"`);
  check("RU the Undo is wired", wired, `onclick=${run(`typeof $("btn-now-undo-remove").onclick`)}`);
  if (wired) { await run(`$("btn-now-undo-remove").onclick()`); await tick(); }
  check("RU pressing it spends exactly one undo",
        posts("/api/undo") === 1, `${posts("/api/undo")} POST(s)`);
  check("RU undoing brings Remove back",
        has('id="btn-now-remove"') && !has('id="btn-now-undo-remove"'),
        `remove=${has('id="btn-now-remove"')} undo=${has('id="btn-now-undo-remove"')}`);

  // Remove again, then let the track change: the offer must not survive it.
  await run(`nowRemove()`);
  await tick();
  setNow(nowBody("spotify:track:ru2"));
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("RU the Undo expires when the next track starts",
        !has('id="btn-now-undo-remove"') && run("removedUri") === null,
        `undo=${has('id="btn-now-undo-remove"')} removedUri=${run("removedUri")}`);
  check("RU and the next track gets its own Remove",
        has('id="btn-now-remove"'), `remove=${has('id="btn-now-remove"')}`);

  // Filing is as undoable as removing: while this track's decision is the
  // top of the undo stack, the same slot offers the way back.
  await run(`nowFile("H1")`);
  await tick();
  check("RU filing swaps Remove for Undo too",
        has('id="btn-now-undo-remove"') && !has('id="btn-now-remove"'),
        `undo=${has('id="btn-now-undo-remove"')} remove=${has('id="btn-now-remove"')}`);
  const undoPosts = posts("/api/undo");
  const wiredFiled = run(`typeof $("btn-now-undo-remove").onclick === "function"`);
  check("RU the filed Undo is wired", wiredFiled, "");
  if (wiredFiled) { await run(`$("btn-now-undo-remove").onclick()`); await tick(); }
  check("RU undoing the filing spends one undo and restores Remove",
        posts("/api/undo") === undoPosts + 1 &&
        has('id="btn-now-remove"') && !has('id="btn-now-undo-remove"'),
        `posts=${posts("/api/undo")} remove=${has('id="btn-now-remove"')}`);
  check("RU and the filed badge is cleared",
        run(`filedUris["spotify:track:ru2"] === undefined`),
        run(`JSON.stringify(filedUris)`));
}

// ============================================================================
// UL — the undo log is ordered, and only home filings own a filed state.
// btn-undo-now used to pop the last KEY of filedUris, which is not the last
// ACTION once subset adds (which write no key) exist: undoing one wiped an
// unrelated track's "filed" badge.
// ============================================================================
{
  resetLog();
  setNow({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:ul1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: null, sitting: null, suggestions: [],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      subsets: [], subset_targets: [{ id: "S1", name: "{sel}", total: 4 }],
      inputs: [],
    },
  });
  routes["POST /api/act"] = { status: 200, body: {} };
  routes["POST /api/undo"] = { status: 200, body: { restored_to: null } };
  // show("now") is load-bearing: pollNow returns immediately on a hidden
  // view-now, so a block inheriting a hidden view never renders at all.
  run(`show("now"); filedUris = {}; nowActions = 0; removedUri = null;
       nowActionLog = []; pollNow(true)`);
  await tick();
  run("stopNowPolling()");

  // An earlier track was filed to a home; this session still remembers it.
  run(`filedUris["spotify:track:earlier"] = "Some Home";
       nowActionLog = [{ uri: "spotify:track:earlier", kind: "home" }]`);
  // Now a subset add on the CURRENT track — writes no filedUris key.
  const wired = run(`typeof nowAddToSubset === "function"`);
  check("UL nowAddToSubset exists", wired, `type=${run(`typeof nowAddToSubset`)}`);
  if (wired) { await run(`nowAddToSubset("S1")`); await tick(); }
  check("UL a subset add writes no filed state",
        run(`filedUris["spotify:track:ul1"] === undefined`),
        `filed=${run(`JSON.stringify(filedUris)`)}`);
  check("UL a subset add is recorded in the ordered log",
        run(`nowActionLog.length === 2 && nowActionLog[1].kind === "subset"`),
        run(`JSON.stringify(nowActionLog)`));

  await run(`$("btn-undo-now").onclick()`);
  await tick();
  check("UL undoing the subset add leaves the earlier home filing alone",
        run(`filedUris["spotify:track:earlier"] === "Some Home"`),
        `filed=${run(`JSON.stringify(filedUris)`)}`);
  check("UL and the log pops the action that was actually undone",
        run(`nowActionLog.length === 1 && nowActionLog[0].kind === "home"`),
        run(`JSON.stringify(nowActionLog)`));
}

// ============================================================================
// UD — undoRemoval must keep nowActionLog in step with nowActions, or a later
// btn-undo-now pops a stale entry: file track A to a home, remove a
// DIFFERENT track B from an input, undo that removal with the strip's own
// button (nowActions and nowActionLog both drop the B entry), then press the
// top btn-undo-now — it must clear A's filed badge (the one action still on
// the log), not silently miss it or touch B again.
// ============================================================================
{
  resetLog();
  setNow({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:ud-a", name: "Song A", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: null, sitting: null, suggestions: [],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      subsets: [], subset_targets: [], inputs: [],
    },
  });
  routes["POST /api/act"] = { status: 200, body: {} };
  routes["POST /api/undo"] = { status: 200, body: { restored_to: "IN1" } };
  // show("now") is load-bearing: pollNow returns immediately on a hidden
  // view-now, so a block inheriting a hidden view never renders at all.
  run(`show("now"); filedUris = {}; nowActions = 0; removedUri = null;
       nowActionLog = []; pollNow(true)`);
  await tick();
  run("stopNowPolling()");

  // File track A to the home.
  await run(`nowFile("H1")`);
  await tick();
  check("UD filing A logs a home entry",
        run(`nowActionLog.length === 1 && nowActionLog[0].uri === "spotify:track:ud-a"`),
        run(`JSON.stringify(nowActionLog)`));

  // Playback moves on to a different track B, sitting in an input.
  run(`nowState.track = { uri: "spotify:track:ud-b", name: "Song B",
         duration_ms: 200000, artists: [{ name: "Artist" }], sortable: true, image: null };
       nowState.context = { id: "IN1", name: "[In]", is_input: true };`);
  await run(`nowRemove()`);
  await tick();
  check("UD removing B logs a second home-kind entry",
        run(`nowActionLog.length === 2 && nowActionLog[1].uri === "spotify:track:ud-b"`),
        run(`JSON.stringify(nowActionLog)`));

  // The strip's own undo restores B — this must pop B's entry, not A's.
  await run(`undoRemoval()`);
  await tick();
  check("UD undoRemoval keeps nowActionLog in step with nowActions",
        run(`nowActionLog.length === 1 && nowActionLog[0].uri === "spotify:track:ud-a"`),
        `log=${run(`JSON.stringify(nowActionLog)`)} actions=${run("nowActions")}`);
  check("UD undoRemoval left B's own filed state cleared, A's untouched",
        run(`filedUris["spotify:track:ud-b"] === undefined &&
             filedUris["spotify:track:ud-a"] === "Home"`),
        `filed=${run(`JSON.stringify(filedUris)`)}`);

  // Now the top undo: only A's action remains on the log, so it must be the
  // one that gets cleared — not a stale leftover from B.
  await run(`$("btn-undo-now").onclick()`);
  await tick();
  check("UD btn-undo-now clears A's filed badge, not a stale leftover",
        run(`filedUris["spotify:track:ud-a"] === undefined`),
        `filed=${run(`JSON.stringify(filedUris)`)}`);
  check("UD and the log is empty, matching nowActions",
        run(`nowActionLog.length === 0`),
        `log=${run(`JSON.stringify(nowActionLog)`)} actions=${run("nowActions")}`);
}

// ============================================================================
// SS — subsets are a destination you pick, never a suggestion.
// They were scored and offered on the filed card until 2026-08-28. The user
// did not want to be suggested into them, so the offers are gone and only
// the picker remains. These checks are written as absences on purpose: the
// most likely way this regresses is a `subsets` array quietly reappearing in
// the payload and being rendered again.
// ============================================================================
{
  resetLog();
  const body = (over) => ({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:ss1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: null, sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 80, reasons: [], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      // Deliberately still present, and deliberately ignored: a stale server
      // (or a reinstated scorer) sending these must not put offers back on
      // the card without someone editing the client too.
      subsets: [{ playlist_id: "S1", name: "{solfest}", pct: 70, already: false,
                  reasons: ["2 tracks by Artist here"] }],
      subset_targets: [{ id: "S1", name: "{solfest}", total: 4 }],
      inputs: [],
      ...over,
    },
  });
  const html = () => $$("now-card").innerHTML;

  setNow(body({}));
  routes["POST /api/act"] = { status: 200, body: {} };
  // show("now") first — see the harness header: pollNow no-ops on a hidden view.
  run(`show("now"); filedUris = {}; nowActions = 0; nowActionLog = [];
       removedUri = null; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("SS an unfiled track is offered no subsets",
        !html().includes("{solfest}"), `html has solfest=${html().includes("{solfest}")}`);
  check("SS the Add to subset button is there",
        html().includes('id="btn-now-subset"'), "missing btn-now-subset");

  await run(`nowFile("H1")`);
  await tick();
  check("SS filing to a home still offers no subsets",
        !html().includes("{solfest}"), `html=${html()}`);
  check("SS and the filed card keeps the Add to subset button",
        html().includes('id="btn-now-subset"'), `html=${html()}`);
  check("SS a payload carrying matches renders none of them",
        !html().includes("sub-offer") && !html().includes("already in"), `html=${html()}`);

  // Adding to a subset by hand still works, and still must not consume the
  // filed state or remove the track from its input.
  await run(`nowAddToSubset("S1")`);
  await tick();
  check("SS adding to a subset does not consume the filed state",
        run(`filedUris["spotify:track:ss1"] === "Home"`),
        run(`JSON.stringify(filedUris)`));
  check("SS and it sends no from_id",
        bodies("/api/act").slice(-1)[0].from_id === null,
        JSON.stringify(bodies("/api/act").slice(-1)[0]));
  check("SS the subset add is logged as its own kind",
        run(`nowActionLog.slice(-1)[0].kind === "subset"`),
        run(`JSON.stringify(nowActionLog)`));
}

// ============================================================================
// SM — the Subset chip marks a playlist, and the mark saves. No name rule,
// and no price: marking reads nothing.
// ============================================================================
{
  resetLog();
  routes["GET /api/playlists"] = {
    status: 200,
    body: {
      playlists: [
        { id: "s1", name: "{solfest}", editable: true, total: 22, role: null,
          folder: null, hints: "", split: null, subset_eligible: true },
        { id: "h1", name: "Ordinary", editable: true, total: 12, role: "home",
          folder: null, hints: "", split: null, subset_eligible: false },
        // A large playlist already marked as a subset: kept as a fixture
        // because size used to matter here (it drove the save's price) and
        // the check that it now doesn't is worth keeping honest.
        { id: "s3", name: "{teh bomb}", editable: true, total: 5000, role: "subset",
          folder: null, hints: "", split: null, subset_eligible: true },
      ],
      fetched_at: 1, sitting_orphans: [],
    },
  };
  routes["POST /api/config"] = { status: 200, body: { ok: true } };
  await run(`loadLists()`);
  await tick();
  check("SM a {} playlist offers a Subset chip",
        $$("playlists").children.some((r) =>
          String(r.innerHTML).includes("r-subset")),
        "no r-subset chip rendered");

  // M2: the client reads the server's subset_eligible answer instead of
  // testing its own hardcoded /^\{.*\}$/, so the chip's visibility gate is
  // just this pure function — see splitDisabledReason for the same pattern.
  check("M2 subsetChipHidden hides a non-{}-eligible row's chip",
        run(`subsetChipHidden({ subset_eligible: false })`) === true,
        String(run(`subsetChipHidden({ subset_eligible: false })`)));
  check("M2 subsetChipHidden shows an eligible row's chip",
        run(`subsetChipHidden({ subset_eligible: true })`) === false,
        String(run(`subsetChipHidden({ subset_eligible: true })`)));

  // Save no longer prices anything: marking a subset reads nothing, so there
  // is no pending cost to state. Pinned as an absence — a price reappearing
  // would mean the scoring machinery (and its per-poll reads) came back.
  // Asserted as "never mentions calls" rather than "equals Save roles": the
  // stub does not parse index.html, so the button's static label is absent
  // here. What matters is that nothing WRITES a price into it, which is
  // exactly what this catches.
  check("SM Save roles never advertises a call cost",
        !/call/i.test($$("btn-save-config").textContent),
        JSON.stringify($$("btn-save-config").textContent));

  run(`roles["s1"] = "subset"`);
  await run(`saveConfig()`);
  await tick();
  const sent = bodies("/api/config").slice(-1)[0];
  check("SM saving sends subset_ids",
        Array.isArray(sent.subset_ids) && sent.subset_ids.includes("s1"),
        JSON.stringify(sent));
  check("SM and does not put it in home_ids",
        !(sent.home_ids || []).includes("s1"), JSON.stringify(sent.home_ids));
}

// ============================================================================
// NM — the header's nav is a menu, and it closes behind you.
// A panel that opens is half a feature; the half that matters is that it
// shuts on every exit — picking a destination, Escape, or a tap outside —
// because a nav panel left open covers the view it just navigated to.
// ============================================================================
{
  resetLog();
  run(`show("now")`);
  check("NM the menu starts closed",
        $$("nav-pop").hidden === true, `hidden=${$$("nav-pop").hidden}`);

  const wired = run(`typeof $("btn-nav-menu").onclick === "function"`);
  check("NM the menu button is wired", wired,
        `onclick=${run(`typeof $("btn-nav-menu").onclick`)}`);

  if (wired) {
    run(`$("btn-nav-menu").onclick({ stopPropagation() {} })`);
    check("NM pressing it opens the panel",
          $$("nav-pop").hidden === false, `hidden=${$$("nav-pop").hidden}`);
    check("NM and the button reports itself expanded",
          $$("btn-nav-menu")["aria-expanded"] === "true",
          `aria-expanded=${$$("btn-nav-menu")["aria-expanded"]}`);

    // Picking a destination must close it — otherwise the panel sits on top
    // of the view it just took you to.
    run(`$("nav-lists").onclick()`);
    await tick();
    check("NM choosing a destination closes it",
          $$("nav-pop").hidden === true, `hidden=${$$("nav-pop").hidden}`);

    run(`$("btn-nav-menu").onclick({ stopPropagation() {} })`);
    check("NM it reopens", $$("nav-pop").hidden === false,
          `hidden=${$$("nav-pop").hidden}`);
    fireKey("Escape");
    check("NM Escape closes it",
          $$("nav-pop").hidden === true, `hidden=${$$("nav-pop").hidden}`);

    // A tap anywhere else dismisses it, same discipline as the input popover.
    run(`$("btn-nav-menu").onclick({ stopPropagation() {} })`);
    for (const h of handlers.click || []) h({ target: {} });
    check("NM a tap outside dismisses it",
          $$("nav-pop").hidden === true, `hidden=${$$("nav-pop").hidden}`);
  }
  run(`show("now")`);   // stable end state for whatever runs after
}

// ============================================================================
// TP — the two-phase card: the track renders the moment the light poll
// answers, with the suggestion side arriving (and filling in) afterwards.
// The lag this kills: presenting a track used to block on the whole
// suggestion pipeline — profile rebuilds, Last.fm round trips — before the
// card could even say the track's name.
// ============================================================================
{
  resetLog();
  run(`show("now"); filedUris = {}; nowActions = 0; removedUri = null; nowSuggestCache = null`);
  const html = () => $$("now-card").innerHTML;
  const tpBody = (uri) => ({
    playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
    track: { uri, name: "Two Phase", duration_ms: 200000,
             artists: [{ name: "Artist" }], sortable: true, image: null },
    context: { id: "IN1", name: "[In]", is_input: true }, sitting: null,
    suggestions: [{ playlist_id: "H1", pct: 90, reasons: ["3 tracks by Artist here"], already: false }],
    homes: [{ id: "H1", name: "Home", folder: "" }],
    inputs: [{ id: "IN1", name: "[In]", has_track: true }],
  });

  // Hold the suggest answer open: the card must go up without it.
  setNow(tpBody("spotify:track:tp1"));
  let release;
  const gate = new Promise((r) => { release = r; });
  const suggRoute = routes["GET /api/now/suggest?force=1"];
  routes["GET /api/now/suggest?force=1"] = () => gate.then(() => suggRoute);
  run(`pollNow(true)`);
  await tick();
  check("TP the track card is up before the suggest answer lands",
        html().includes("Two Phase") && /finding a home…/.test(html()),
        JSON.stringify(html().slice(0, 160)));
  check("TP no suggestion buttons render while pending",
        !html().includes('class="sugg"'), "");
  check("TP the playback strip is live while pending (Remove included)",
        html().includes('id="btn-now-next"') && html().includes('id="btn-now-remove"'),
        `next=${html().includes('id="btn-now-next"')} remove=${html().includes('id="btn-now-remove"')}`);
  release();
  await tick();
  check("TP suggestions fill in when the second answer lands",
        html().includes('class="sugg"') && html().includes("Home") &&
        !/finding a home…/.test(html()),
        JSON.stringify(html().slice(0, 200)));
  run("stopNowPolling()");

  // An unchanged track re-poll answers the suggestion side from the cache.
  resetLog();
  run(`pollNow()`);
  await tick();
  run("stopNowPolling()");
  check("TP an unchanged track re-poll never re-asks the suggest endpoint",
        gets("/api/now/suggest") === 0 && gets("/api/now/suggest?force=1") === 0,
        `${gets("/api/now/suggest")}+${gets("/api/now/suggest?force=1")} GET(s)`);
  check("TP and the cached suggestions are still on the card",
        html().includes('class="sugg"'), "");

  // A suggest answer for a track that is no longer playing is dropped, not
  // painted over the wrong card.
  setNow(tpBody("spotify:track:tp2"));
  routes["GET /api/now/suggest?force=1"] = {
    status: 200,
    body: { ...routes["GET /api/now/suggest?force=1"].body, track_uri: "spotify:track:tp-stale" },
  };
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("TP a stale suggest answer is dropped, the card stays pending",
        /finding a home…/.test(html()) && !html().includes('class="sugg"'),
        JSON.stringify(html().slice(0, 160)));
}

// ============================================================================
// HL — "Homeless": the verdict that no home fits, as a move into the
// buffer that collects those songs. It files (out of the input, into the
// buffer) rather than captures, so the checks below are as much about the
// three cases where the button must NOT appear as about the one where it does
// — an offered move into the playlist you are already listening to, or one
// that duplicates a song already there, is worse than no button.
// ============================================================================
{
  resetLog();
  const body = (over) => ({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:nh1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 80, reasons: [], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      subset_targets: [], subsets: [],
      inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" },
               { id: "NH1", name: "[Homeless]", has_track: false, set: "buffer" }],
      homeless_id: "NH1",
      ...over,
    },
  });
  const html = () => $$("now-card").innerHTML;
  const paint = async (over) => {
    setNow(body(over));
    run(`show("now"); filedUris = {}; nowActions = 0; nowActionLog = [];
         removedUri = null; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };

  routes["POST /api/act"] = { status: 200, body: {} };
  await paint({});
  check("HL the button is offered while playing from an input",
        html().includes('id="btn-now-homeless"'), `html=${html()}`);
  check("HL and the destination gets no capture chip of its own",
        !html().includes('data-in="NH1"') && html().includes('data-in="IN1"'),
        `nh-chip=${html().includes('data-in="NH1"')} in-chip=${html().includes('data-in="IN1"')}`);

  await run(`nowHomeless()`);
  await tick();
  const act = bodies("/api/act").slice(-1)[0];
  check("HL pressing it moves the song out of the input it came from",
        act.to_id === "NH1" && act.from_id === "IN1" && act.action === "move",
        JSON.stringify(act));
  check("HL the card lands filed, named for the buffer and not for a home",
        run(`filedUris["spotify:track:nh1"] === "Homeless"`) &&
        /filed to/.test(html()),
        run(`JSON.stringify(filedUris)`));
  check("HL the filed card celebrates with the big drawn check",
        html().includes('class="done-mark"') && html().includes("<svg"),
        `mark=${html().includes('class="done-mark"')}`);
  check("HL the check draws left to right (path starts at the left tail)",
        html().includes('points="4 12 9 17 20 6"'),
        `points=${(html().match(/points="[^"]*"/) || ["none"])[0]}`);
  check("HL and the strip offers the way back",
        html().includes('id="btn-now-undo-remove"'), `html=${html()}`);

  await paint({ homeless_id: null });
  check("HL no configured destination, no button",
        !html().includes('id="btn-now-homeless"'), `html=${html()}`);

  await paint({ context: { id: "NH1", name: "[Homeless]", is_input: true } });
  check("HL playing from the buffer itself offers no move into it",
        !html().includes('id="btn-now-homeless"'), `html=${html()}`);

  await paint({ inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" },
                         { id: "NH1", name: "[Homeless]", has_track: true, set: "buffer" }] });
  check("HL a song already in the buffer is not offered it twice",
        !html().includes('id="btn-now-homeless"'), `html=${html()}`);

  await paint({ context: null });
  check("HL nothing to move out of, no button",
        !html().includes('id="btn-now-homeless"'), `html=${html()}`);
}

// ============================================================================
// SD — suggestion depth: the list is six long now (sortify/suggest.py's
// TOP_N, raised from 3 on measured accuracy), so the digit shortcuts have to
// reach all six. `7` staying inert is half the point: the guard is a literal
// list of keys, and a too-wide one would index past the end of a short list.
// ============================================================================
{
  resetLog();
  const sugg = (n) => Array.from({ length: n }, (_, i) => (
    { playlist_id: `H${i + 1}`, pct: 90 - i * 5, reasons: [], already: false }));
  const homes = (n) => Array.from({ length: n }, (_, i) => (
    { id: `H${i + 1}`, name: `Home ${i + 1}`, folder: "" }));
  setNow({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:sd1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
      suggestions: sugg(6), homes: homes(6),
      subset_targets: [], subsets: [], inputs: [], homeless_id: null,
    },
  });
  run(`show("now"); filedUris = {}; nowActions = 0; nowActionLog = [];
       removedUri = null; pollNow(true)`);
  await tick();
  run("stopNowPolling()");

  const card = $$("now-card").innerHTML;
  check("SD all six suggestions render, none silently dropped",
        (card.match(/class="sugg"/g) || []).length === 6, `card=${card}`);
  check("SD each carries its own digit hint, 1 through 6",
        [1, 2, 3, 4, 5, 6].every((i) => card.includes(`<kbd>${i}</kbd>`)), `card=${card}`);

  routes["POST /api/act"] = { status: 200, body: {} };
  resetLog();
  fireKey("6");
  await tick();
  check("SD `6` files the sixth suggestion",
        posts("/api/act") === 1 && bodies("/api/act").slice(-1)[0].to_id === "H6",
        `${posts("/api/act")} POST(s) ${JSON.stringify(bodies("/api/act").slice(-1)[0])}`);

  run(`filedUris = {}`);
  resetLog();
  fireKey("7");
  await tick();
  check("SD `7` is not a shortcut — nothing is filed past the end of the list",
        posts("/api/act") === 0, `${posts("/api/act")} POST(s)`);
}

// ============================================================================
// SC — the suggestion block scrolls: six rows is more card than a phone
// screen wants, so the list is capped at roughly three and the rest is a
// swipe away. The cap itself is CSS and out of this harness's reach; what is
// pinned here is the structure the cap depends on — which rows are inside
// the scrolling box, and which text deliberately stays outside it.
// ============================================================================
{
  resetLog();
  const paint = async (suggestions) => {
    setNow({
      status: 200,
      body: {
        playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
        track: { uri: "spotify:track:sc1", name: "Song", duration_ms: 200000,
                 artists: [{ name: "Artist" }], sortable: true, image: null },
        context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
        suggestions,
        homes: Array.from({ length: 6 }, (_, i) => (
          { id: `H${i + 1}`, name: `Home ${i + 1}`, folder: "" })),
        subset_targets: [], subsets: [], inputs: [], homeless_id: null,
      },
    });
    run(`show("now"); filedUris = {}; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
    return $$("now-card").innerHTML;
  };
  const six = Array.from({ length: 6 }, (_, i) => (
    { playlist_id: `H${i + 1}`, pct: 90 - i * 5, reasons: ["why"], already: false }));

  let card = await paint(six);
  const box = card.match(/<div class="sugg-scroll">([\s\S]*?)<\/div>/);
  check("SC every suggestion sits inside the scrolling box",
        !!box && (box[1].match(/class="sugg"/g) || []).length === 6, `card=${card}`);
  check("SC and the buttons keep their handles, so clicks still wire up",
        !!box && ["H1", "H6"].every((id) => box[1].includes(`data-to="${id}"`)), `card=${card}`);

  // Add to… living in the list means the box is never empty — with nothing
  // suggested it holds that one row, which is exactly the case where the user
  // needs it most.
  card = await paint([]);
  const empty = card.match(/<div class="sugg-scroll">([\s\S]*?)<\/div>\s*<p class="hint">/);
  check("SC nothing suggested, and the box holds Add to… by itself",
        !!empty && (empty[1].match(/class="sugg/g) || []).length === 1 &&
        empty[1].includes("sugg-more"), `card=${card}`);

  card = await paint(six.slice(0, 2).map((s) => ({ ...s, weak: true })));
  check("SC the guesses lead-in stays outside the box, not scrolled away",
        card.indexOf("closest guesses") !== -1 &&
        card.indexOf("closest guesses") < card.indexOf("sugg-scroll"), `card=${card}`);

  // The box is one size, always — three rows of whatever a row measures. It
  // used to be sized from where the fourth row started, which meant a card
  // with two suggestions drew a shorter box than one with six and the whole
  // card jumped on every track change. The measuring is layout and out of
  // reach here; the arithmetic on top of it is not.
  check("SC three rows tall, from one row's pitch",
        run("suggScrollHeight(56)") === 168, "");
  check("SC a row's pitch counts the gap under it, not just its box",
        run("suggRowPitch(46, 10)") === 56, "");
  check("SC too few rows to fill it changes nothing — that is the point",
        run("suggScrollHeight(suggRowPitch(46, 10))") === 168, "");

  card = await paint(six);
  const box2 = card.match(/<div class="sugg-scroll">([\s\S]*?)<\/div>\s*<div class="minor-actions">/);
  check("SC Add to… is the last row of the list, not a button below it",
        !!box2 && box2[1].includes('id="btn-now-more"') &&
        box2[1].lastIndexOf('id="btn-now-more"') > box2[1].lastIndexOf('data-to="H6"'),
        `card=${card}`);
  check("SC it looks like its own thing, not a seventh suggestion",
        !!box2 && box2[1].includes("sugg-more"), `card=${card}`);
  check("SC and carries no data-to, so the file wiring cannot pick it up",
        !!box2 && !/id="btn-now-more"[^>]*data-to=/.test(box2[1]), `card=${card}`);
  check("SC the minor actions no longer offer it a second time",
        (card.match(/id="btn-now-more"/g) || []).length === 1, `card=${card}`);
}

// ============================================================================
// SW — the reason line is one line. The clamp itself is CSS, but half of what
// used to overflow was the folder path: real ones run seven segments deep
// ("archive / other / Previous / Old / Diverse / Røde Runde / Anbefalte"),
// which alone is wider than a phone. Only the leaf is shown — the folder the
// playlist actually sits in — and the full path stays in the picker, which
// has the width for it.
// ============================================================================
{
  resetLog();
  setNow({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:sw1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 80, reasons: ["3× Beach House"], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "archive / other / Previous / Old / Diverse" }],
      subset_targets: [], subsets: [], inputs: [], homeless_id: null,
    },
  });
  run(`show("now"); filedUris = {}; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  const why = $$("now-card").innerHTML.match(/class="s-why">([^<]*)</);

  check("SW the reason line names the folder the playlist is in, not the path to it",
        !!why && why[1].includes("Diverse") && !why[1].includes("archive"), `why=${why && why[1]}`);
  check("SW and keeps the evidence beside it",
        !!why && why[1].includes("3× Beach House"), `why=${why && why[1]}`);
  check("SW a home at the top level still reads cleanly",
        run(`folderLeaf("samlemappe")`) === "samlemappe" &&
        run(`folderLeaf("")`) === "" && run(`folderLeaf(null)`) === "", "");
}

// ============================================================================
// HP — Homeless inside the picker. The verdict "none of these fit" is most
// often reached while looking through the homes, not before opening them, so
// it has to be reachable from in there without backing out first. It pins to
// the top and survives filtering: a filter that matches nothing is the
// clearest statement of the case it exists for.
// ============================================================================
{
  resetLog();
  const rows = () => $$("picker-list").children.map((c) => c.className);
  const openWith = async (over) => {
    setNow({
      status: 200,
      body: {
        playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
        track: { uri: "spotify:track:hp1", name: "Song", duration_ms: 200000,
                 artists: [{ name: "Artist" }], sortable: true, image: null },
        context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
        suggestions: [], homes: [{ id: "H1", name: "Home", folder: "" }],
        subset_targets: [], subsets: [],
        inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" },
                 { id: "HL1", name: "[Homeless]", has_track: false, set: "buffer" }],
        homeless_id: "HL1",
        ...over,
      },
    });
    run(`show("now"); filedUris = {}; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
    run(`openNowPicker()`);
  };

  await openWith({});
  check("HP the picker leads with Homeless when the card would offer it",
        rows()[0] && rows()[0].includes("picker-homeless"), `rows=${rows()}`);
  check("HP and the homes follow it",
        rows().slice(1).some((c) => c === "picker-row"), `rows=${rows()}`);

  run(`$("picker-filter").oninput({ target: { value: "nothing matches this" } })`);
  check("HP a filter that matches nothing keeps it — that is its whole case",
        rows()[0] && rows()[0].includes("picker-homeless"), `rows=${rows()}`);

  run("closePicker()");
  await openWith({ context: { id: "H1", name: "Home", is_input: false } });
  check("HP playing outside an input, there is nothing to move out of, and no row",
        !rows().some((c) => c.includes("picker-homeless")), `rows=${rows()}`);

  run("closePicker()");
  await openWith({ homeless_id: null });
  check("HP no destination configured, no row",
        !rows().some((c) => c.includes("picker-homeless")), `rows=${rows()}`);
  run("closePicker()");
}

// ============================================================================
// XM — a removal is not an achievement. Filing a song and taking it out of an
// input both leave the card in its done state, but they are opposite outcomes
// and the card said "filed to nowhere (removed from input)" for the second
// one, under the same green check as the first. Red cross, and it says what
// happened.
// ============================================================================
{
  resetLog();
  const paint = async () => {
    setNow({
      status: 200,
      body: {
        playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
        track: { uri: "spotify:track:xm1", name: "Song", duration_ms: 200000,
                 artists: [{ name: "Artist" }], sortable: true, image: null },
        context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
        suggestions: [], homes: [{ id: "H1", name: "Home", folder: "" }],
        subset_targets: [], subsets: [],
        inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" }],
        homeless_id: null,
      },
    });
    run(`show("now"); filedUris = {}; removedUri = null; nowActionLog = []; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };
  const html = () => $$("now-card").innerHTML;

  routes["POST /api/act"] = { status: 200, body: {} };
  await paint();
  await run(`nowRemove()`);
  await tick();
  check("XM a removed track gets the cross, not the check",
        html().includes("gone-mark") && !html().includes("done-mark"), `html=${html()}`);
  check("XM and the card says it was removed, and from where",
        /removed from/.test(html()) && html().includes("[Hazy]"), `html=${html()}`);
  check("XM no 'filed to nowhere' left anywhere on it",
        !/filed to/.test(html()), `html=${html()}`);

  await paint();
  await run(`nowFile("H1")`);
  await tick();
  check("XM filing is untouched — still the check, still 'filed to'",
        html().includes("done-mark") && !html().includes("gone-mark") &&
        /filed to/.test(html()), `html=${html()}`);
}

// ============================================================================
// RG — Remove is always drawn, and greyed when it cannot act.
//
// It used to vanish whenever the context stopped being an input, which taught
// the eye that the verb comes and goes and answered nothing when it was
// missing. Worse, it stayed live and red for a song that had already LEFT the
// input (file it, reload, and the strip still offered to remove it) — a button
// that lied. Drawn-and-dead fixes both: the slot always holds the same verb,
// and the dead state can say why when pressed.
//
// aria-disabled, not the disabled attribute: a disabled button swallows the
// tap, and being pressable is the whole point of the dead state.
// ============================================================================
{
  const nowBody = ({ isInput = true, hasTrack = true, withInputs = true, sitting = null }) => ({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:rg1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[Grey]", is_input: isInput },
      sitting,
      suggestions: [], homes: [{ id: "H1", name: "Home", folder: "" }],
      subset_targets: [], subsets: [],
      inputs: withInputs ? [{ id: "IN1", name: "[Grey]", has_track: hasTrack, set: "buffer" }] : [],
      homeless_id: null,
    },
  });
  const html = () => $$("now-card").innerHTML;
  // The button's own tag, so a class check cannot be satisfied by some other
  // element on the card that happens to carry the same word.
  const removeTag = () => (html().match(/<button id="btn-now-remove"[^>]*>/) || [""])[0];
  const paint = async (opts) => {
    setNow(nowBody(opts));
    run(`filedUris = {}; removedUri = null; nowActions = 0; nowActionLog = []; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };

  await paint({ isInput: true, hasTrack: true });
  check("RG playing from an input that holds the song: live and destructive",
        removeTag().includes("np-danger") && !removeTag().includes("aria-disabled"),
        `tag=${removeTag()}`);

  await paint({ isInput: false });
  check("RG not playing from an input: still drawn, no longer missing",
        removeTag() !== "", `html=${html().slice(0, 200)}`);
  check("RG ...and drawn dead, not destructive",
        removeTag().includes("np-dead") && !removeTag().includes("np-danger"),
        `tag=${removeTag()}`);
  check("RG ...as aria-disabled, so the tap still reaches a handler",
        removeTag().includes('aria-disabled="true"') && !/\sdisabled/.test(removeTag()),
        `tag=${removeTag()}`);
  check("RG the reserved slot still survives so the transport never shifts",
        html().includes("np-remove-slot"), `html=${html().slice(0, 200)}`);

  // The case that shipped as a lying button: the context IS the input, but the
  // song has already been filed out of it. Only the membership flag knows.
  await paint({ isInput: true, hasTrack: false });
  check("RG playing from an input the song has already left: dead",
        removeTag().includes("np-dead") && !removeTag().includes("np-danger"),
        `tag=${removeTag()}`);
  check("RG ...and the dead title names the list it is no longer in",
        /title="[^"]*\[Grey\]/.test(removeTag()), `tag=${removeTag()}`);

  // Phase 1 carries context.is_input but no membership at all. Treating a
  // missing row as "the song left" flashed a grey button for the second the
  // suggest phase takes.
  await paint({ isInput: true, withInputs: true, hasTrack: true });
  const live1 = run(`removeState({context: {id: "IN1", name: "[Grey]", is_input: true}}).live`);
  check("RG phase 1 (no inputs payload yet) stays live rather than flashing grey",
        live1 === true, `live=${live1}`);

  // A sitting renders the strip too, and an input context never occurs there,
  // so an always-drawn button could only ever be dead weight.
  await paint({ isInput: false, sitting: { split_id: "S1", pile_id: "p1", pile_name: "x",
                                           uris: ["spotify:track:rg1"], decided: {} } });
  check("RG a sitting keeps the slot empty — the verb can never apply there",
        !html().includes('id="btn-now-remove"'), `html=${html().slice(0, 200)}`);

  // Pressing the dead button explains itself instead of doing nothing.
  await paint({ isInput: false });
  resetLog();
  $$("toast").textContent = "";
  run(`$("btn-now-remove").onclick()`);
  await tick();
  check("RG pressing it while dead says why, and spends nothing",
        /input list/i.test($$("toast").textContent) && posts("/api/act") === 0,
        `toast=${JSON.stringify($$("toast").textContent.slice(0, 80))} posts=${posts("/api/act")}`);
}

// ============================================================================
// NP — the press-to-effect gap, made visible on the two verbs.
//
// Next posted, re-enabled itself immediately, and only swapped the card ~900ms
// later when the settle repoll landed. For that second the card looked
// untouched and the button looked ready — so a second press skipped a second
// track. Remove had the same silent moment while /api/act was in flight.
// ============================================================================
{
  const body = (uri) => ({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri, name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[Pend]", is_input: true }, sitting: null,
      suggestions: [], homes: [{ id: "H1", name: "Home", folder: "" }],
      subset_targets: [], subsets: [],
      inputs: [{ id: "IN1", name: "[Pend]", has_track: true, set: "buffer" }],
      homeless_id: null,
    },
  });
  const html = () => $$("now-card").innerHTML;
  const nextTag = () => (html().match(/<button id="btn-now-next"[^>]*>/) || [""])[0];

  setNow(body("spotify:track:np1"));
  routes["POST /api/player/next"] = { status: 200, body: { ok: true } };
  run(`filedUris = {}; removedUri = null; nowActions = 0; nowActionLog = []; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("NP Next is ordinary before the press",
        nextTag() !== "" && !nextTag().includes("np-busy"), `tag=${nextTag()}`);

  resetLog();
  run(`playerNext()`);
  await tick();
  check("NP pressing Next puts the button in its busy state",
        nextTag().includes("np-busy"), `tag=${nextTag()}`);
  check("NP ...and disables it, so the press cannot be repeated",
        /\sdisabled/.test(nextTag()), `tag=${nextTag()}`);

  // The whole point: the old code re-enabled instantly and a second press
  // skipped two tracks.
  run(`playerNext()`);
  await tick();
  check("NP a second press while pending spends nothing more",
        posts("/api/player/next") === 1, `posts=${posts("/api/player/next")}`);

  // The arrival of a DIFFERENT track is the completion signal — that is what
  // the press was for, and it works whichever poll happens to bring it.
  setNow(body("spotify:track:np2"));
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("NP the new track clears the busy state",
        nextTag() !== "" && !nextTag().includes("np-busy"), `tag=${nextTag()}`);

  // Remove's toast names the list, so the feedback says WHAT happened and not
  // merely that something did.
  setNow(body("spotify:track:np3"));
  routes["POST /api/act"] = { status: 200, body: {} };
  run(`filedUris = {}; removedUri = null; nowActionLog = []; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  $$("toast").textContent = "";
  run(`nowRemove()`);
  await tick();
  check("NP the removal toast names the list it left",
        /removed from/i.test($$("toast").textContent) &&
        $$("toast").textContent.includes("[Pend]"),
        `toast=${JSON.stringify($$("toast").textContent.slice(0, 80))}`);
}

// ---- summary ---------------------------------------------------------------
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
