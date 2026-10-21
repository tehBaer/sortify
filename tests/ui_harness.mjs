// Regression checks for the client-side call-discipline and lockout bugs the
// final whole-branch review found. app.js is loaded into a stub DOM inside a
// vm context; `fetch` is a scripted router that records every request, so
// this makes **zero real Spotify calls** and needs no server running.
//
// NOT a pytest module (it is JavaScript, and the project deliberately has no
// JS toolchain — no dependencies, no build step). It is a hand-run
// diagnostic, run the same way against any copy of app.js:
//
//     node tests/ui_harness.mjs                     # the working tree
//     node tests/ui_harness.mjs /tmp/app-before.js  # any other copy
//
// Exits non-zero if any check fails. What it pins, and why each matters —
// all were measured failing against the commit before the fix:
//
//   I1  renderNow() with nowState===null must not throw.
//
// The stub DOM models only what these paths touch, but the three details it
// does model are load-bearing: assigning `innerHTML` replaces the subtree (so
// a re-render's row clearing is real, not additive), ids inside assigned
// markup become addressable afterwards, and responses are deep-cloned, so a
// shared fixture object can never leak one scenario's mutation into the next
// scenario's "server" answer and mask the very bug under test.
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
    this.dataset = {}; this.children = []; this.href = "";
    this.style = { setProperty(k, v) { this[k] = String(v); } };
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
  set innerHTML(v) {
    this._html = String(v); this.children = []; this._btns = null; this._qs = null;
    registerIds(this._html);
  }
  get innerHTML() { return this._html; }
  // "button" is modeled for real (one fresh El per <button> tag in document
  // order) because makeListRow destructures its role chips positionally and
  // wires paint()/onclick straight onto them — an empty stub would throw
  // before the row's markup (what SM's checks actually read) ever gets
  // built. Other selectors stay unmodeled: nothing else needs them, and O1
  // pins the pure gating function specifically to avoid depending on this.
  //
  // The buttons (and each querySelector() answer) are memoised until the next
  // innerHTML assignment, as a real DOM's would be — SR needs the SAME chip
  // element that makeListRow wired its onclick onto, and `.name` to stay
  // readable after the click wrote to it.
  querySelectorAll(sel) {
    if (sel !== "button" && sel !== "button[data-path]") return [];
    this._btns ||= [...this._html.matchAll(/<button[^>]*>/g)].map((m) => {
      const b = new El("_btn");
      const d = m[0].match(/data-path="([^"]*)"/);   // attribute selector support: data-path only
      b._path = d ? d[1].replace(/&amp;/g, "&") : null;
      if (d) b.dataset.path = b._path;
      return b;
    });
    return sel === "button" ? this._btns : this._btns.filter((b) => b._path !== null);
  }
  querySelector(sel) {
    this._qs ||= {};
    return (this._qs[sel] ||= new El("_anon"));
  }
  // `_siblings` is the array `c` actually landed in, captured at append time
  // — not re-read off the parent later, so a caller that temporarily swaps
  // in a different `children` (SP3's HTMLCollection-has-no-indexOf proxy)
  // cannot make a later `remove()` see the swapped-in stand-in.
  appendChild(c) { c._siblings = this.children; this.children.push(c); }
  // A real DOM element's own `remove()`, not a parent lookup by the app: a
  // real `HTMLCollection` has no `indexOf`, so the app may only ever call
  // `el.remove()` on itself, never search `parent.children` for it.
  remove() {
    if (!this._siblings) return;
    const i = this._siblings.indexOf(this);
    if (i >= 0) this._siblings.splice(i, 1);
    this._siblings = null;
  }
  setAttribute(k, v) { this[k] = String(v); }
  scrollIntoView() {} focus() {}
  // Recorded, not run: a check that needs one fires it itself (fireClick).
  addEventListener(type, h) { ((this._ls ||= {})[type] ||= []).push(h); }
}

const reg = {};
const $$ = (id) => (reg[id] ||= new El(id));
function registerIds(html) {
  for (const m of html.matchAll(/id="([^"]+)"/g)) $$(m[1]);
}

const handlers = {};
// Listeners registered for the capture phase (third argument `true` or
// `{capture: true}`). They also sit in `handlers` in registration order, so
// every existing loop over `handlers[type]` is unchanged; dispatchClick uses
// this set to run them BEFORE the target, as a real dispatch does.
const captureFns = new Set();

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
  addEventListener: (t, f, o) => {
    if (o === true || (o && o.capture)) captureFns.add(f);
    (handlers[t] ||= []).push(f);
  },
  removeEventListener: (t, f) => {
    const a = handlers[t] || [];
    const i = a.indexOf(f);
    if (i >= 0) a.splice(i, 1);
    captureFns.delete(f);
  },
  body: new El("_body"),   // blind mode toggles a class here
  // The Split tab's drop target is found geometrically; checks script it.
  elementFromPoint: () => null,
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
    // Deep-cloned: app.js holds onto parts of what it's given and can mutate
    // them in place. Handing out the fixture object itself would let one
    // scenario's mutation leak into the next poll's "server" answer.
    json: async () => structuredClone(r.body),
  };
}

// The preview player is the only code that constructs Audio. It records
// every instance so a check can prove the OLD clip was paused before the new
// one started — two live Audio objects is the "previews over each other" bug.
class AudioStub {
  constructor(src) {
    this.src = src; this.currentTime = 0; this.duration = 30; this.paused = false;
    // Counts play() calls on THIS object — the iOS retry fix calls play()
    // again on the same instance rather than making a new one, and "no new
    // Audio, but play() ran twice" is what tells that apart from a no-op.
    this.playCalls = 0;
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
  // `refuse` can be `true` (the iOS gesture refusal, NotAllowedError — kept
  // as the default meaning so every existing check still passes unchanged)
  // or a specific DOMException name string ("NotSupportedError" for a dead
  // clip, "AbortError" for a play() pre-empted by a stop) so a check can
  // tell apart WHICH failure app.js is handling.
  play() {
    this.playCalls++; this.paused = false;
    if (!AudioStub.refuse) return Promise.resolve();
    const name = AudioStub.refuse === true ? "NotAllowedError" : AudioStub.refuse;
    const err = new Error(name);
    err.name = name;
    return Promise.reject(err);
  }
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
  // The public gate sends the page to /gate/start by assigning this.
  location: { href: "" },
  // The Split tab asks before discarding work; checks script the answer.
  confirm: () => true,
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

function resetLog() { log.length = 0; }

// A click as a real browser dispatches one at `el`: document capture-phase
// listeners first, then the target's own onclick, then the document's
// bubble-phase listeners — the last two only if nothing stopped it.
function dispatchClick(el) {
  let stopped = false;
  const ev = { type: "click", target: el, defaultPrevented: false,
               preventDefault() { this.defaultPrevented = true; },
               stopPropagation() { stopped = true; }, stopImmediatePropagation() { stopped = true; } };
  const all = [...(handlers.click || [])];
  for (const h of all) if (captureFns.has(h) && !stopped) h(ev);
  if (!stopped && el.onclick) el.onclick(ev);
  for (const h of all) if (!captureFns.has(h) && !stopped) h(ev);
  return ev;
}

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
      quick_adds: body.quick_adds || [],
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
// I1 — renderNow() with nowState === null must not throw
// ============================================================================
{
  run(`nowState = null; show("now");`);
  let threw = null;
  process.once("unhandledRejection", (e) => (threw = e));
  try {
    run(`renderNow()`);
    await tick();
  } catch (e) { threw = e; }
  check("I1 renderNow() with nowState===null does not throw",
        threw === null, threw ? String(threw) : "");
}

// ============================================================================
// I2 — no keep (and no reject) fires from the keyboard while an error card
// is up. A failed poll leaves `nowState` untouched on purpose (so the last
// known truth stays on screen), which means `nowState.playing` can still
// read true while the card itself shows an error — the `!nowProblem` guard
// in the keydown handler is what stops a stray digit or `r` from filing or
// removing the track the screen no longer shows.
// ============================================================================
{
  setNow({
    status: 200,
    body: {
      playing: true, is_playing: true, poll_after_ms: 999999,
      track: { uri: "spotify:track:i2", name: "Song", artists: [{ name: "A" }], sortable: true, image: null },
      context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 90, reasons: [], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      inputs: [{ id: "IN1", name: "[Hazy]", has_track: true }],
    },
  });
  run(`show("now"); filedUris = {}; nowActions = 0; nowActionLog = []; pollNow(true)`);
  await tick();
  run("stopNowPolling()");

  routes["GET /api/now?light=1"] = { status: 500, body: { detail: "boom" } };
  routes["GET /api/now?light=1&force=1"] = { status: 500, body: { detail: "boom" } };
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("I2 the error card is up while nowState.playing still reads true",
        run(`nowProblem`) === true && run(`nowState.playing`) === true, "");

  resetLog();
  fireKey("1");
  fireKey("r");
  await tick();
  check("I2 no keep (and no reject) fires from the keyboard while an error card is up",
        posts("/api/act") === 0, `${posts("/api/act")} POST(s)`);
}

// ============================================================================
// V — Now-tab presentation pins (2026-08 visual refresh): cooldown countdown
// card, hidden-until-useful Undo, nav active state.
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

// ============================================================================
// W — weak guesses: sub-threshold suggestions render as labeled guesses,
// visually distinct from confident ones, in the now card. The server only
// ever sends weak entries when NOTHING was confident, so the lead-in hint
// keys off suggestions[0].
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
        { id: "in1", name: "[A]", set: "buffer", has_track: false, total: 12 },
        { id: "in2", name: "[B]", set: "buffer", has_track: false, total: 7 },
        { id: "o1", name: "<kept>", set: "other", has_track: true, total: 3 },
        { id: "o2", name: "<folded away>", set: "other", has_track: false, total: 0 },
      ] };
    run(`nowSetsExpanded = false; paintNowControls(${JSON.stringify(d)})`);
    check("NB the trigger is shown wearing the playing input's name",
          $$("btn-input-switch").hidden === false
          && /\[B\]/.test($$("input-switch-label").textContent),
          `hidden=${$$("btn-input-switch").hidden} label=` +
          JSON.stringify($$("input-switch-label").textContent));
    // How much is left in the list you are working through. It reads off the
    // PLAYING input's row, not the first one that happens to match — a bar
    // that counted down someone else's inbox would be worse than no count.
    check("NB the trigger carries the playing input's own count",
          $$("input-switch-count").textContent === "7" && $$("input-switch-count").hidden === false,
          JSON.stringify($$("input-switch-count").textContent));
    // In its own span, so a long name ellipsizes and the count stays.
    check("NB ...beside the name, not inside it",
          $$("input-switch-label").textContent === "[B]", JSON.stringify($$("input-switch-label").textContent));

    run(`openInputPop()`);
    const pop = $$("input-pop");
    check("NB opening renders a row per visible input into #input-pop",
          pop.hidden === false && /ip-in1/.test(pop.innerHTML)
          && /\[A\]/.test(pop.innerHTML),
          `hidden=${pop.hidden} html=${JSON.stringify(pop.innerHTML.slice(0, 120))}`);
    check("NB a folded set still peeks the row that contains the track",
          /ip-o1/.test(pop.innerHTML) && !/ip-o2/.test(pop.innerHTML),
          JSON.stringify(pop.innerHTML.slice(0, 200)));
    check("NB every row carries its own count, so inboxes can be compared",
          /ip-count">12</.test(pop.innerHTML) && /ip-count">7</.test(pop.innerHTML),
          JSON.stringify(pop.innerHTML.slice(0, 300)));
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

    // A count the server could not work out (Liked Songs in the idle payload)
    // arrives as null. Drawing "0" there would be a lie about an empty list.
    const unknown = { playing: true, is_playing: true,
      context: { id: "in9", name: "[U]", is_input: true },
      inputs: [{ id: "in9", name: "[U]", set: "buffer", has_track: false, total: null }] };
    run(`paintNowControls(${JSON.stringify(unknown)}); openInputPop()`);
    check("NB an unknown count draws no number at all, on the row or the face",
          !/ip-count/.test($$("input-pop").innerHTML)
          && $$("input-switch-count").hidden === true && $$("input-switch-count").textContent === "",
          `count=${JSON.stringify($$("input-switch-count").textContent)} ` +
          JSON.stringify($$("input-pop").innerHTML.slice(0, 200)));
    run(`closeInputPop(); paintNowControls(${JSON.stringify(d)})`);

    // Playing something that is not an input at all: the name says so and
    // there is nothing to count down.
    run(`paintNowControls(${JSON.stringify({ ...d,
      context: { id: "zz", name: "someone else's mix", is_input: false } })})`);
    check("NB a non-input context gets no count",
          $$("input-switch-count").hidden === true
          && /not an input/.test($$("input-switch-label").textContent),
          JSON.stringify($$("input-switch-label").textContent));
    run(`paintNowControls(${JSON.stringify(d)})`);
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
// BR — blind by default. The card never names what is playing until the eye
// in the bar opens the panel, and that reveal lasts one track. A remove must
// still name what it removed — once the track is out of the input the
// decision is spent, and hiding it would mean the input lost a track you
// never got to see — so the toast names it, without opening the panel.
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
  run(`show("now"); setReveal(false)`);
  run(`filedUris = {}; nowActions = 0; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("BR the card opens blind — nothing revealed until asked",
        !document.body.classList.contains("peeked"),
        `peeked=${document.body.classList.contains("peeked")}`);
  check("BR ...and the card never names the song, so the eye has nothing to move",
        !/Removed Song|Removed Artist/.test($$("now-card").innerHTML) &&
        !$$("now-card").innerHTML.includes("t-reveal"),
        $$("now-card").innerHTML.slice(0, 300));

  run(`$("btn-reveal").onclick()`);
  check("BR the eye reveals this track",
        document.body.classList.contains("peeked") && run("peekedUri") === "spotify:track:br1" &&
        $$("btn-reveal").classList.contains("on"),
        `peeked=${document.body.classList.contains("peeked")} peekedUri=${run("peekedUri")}`);
  run(`$("btn-reveal").onclick()`);
  check("BR ...and a second press hides it again",
        !document.body.classList.contains("peeked") && run("peekedUri") === null, "");

  routes["POST /api/act"] = { status: 200, body: {} };
  await run(`nowRemove()`);
  await tick();
  check("BR removing names what was removed, song and artist",
        /Removed Song/.test($$("toast").textContent) &&
        /Removed Artist/.test($$("toast").textContent) &&
        /\[In\]/.test($$("toast").textContent),
        `toast=${JSON.stringify($$("toast").textContent)}`);
  check("BR ...without dropping the big panel into the card",
        !document.body.classList.contains("peeked"), "");

  // The reveal has to expire with the track. Otherwise the next track arrives
  // already named without anyone having asked for it.
  run(`$("btn-reveal").onclick()`);
  routes["GET /api/now?light=1"].body.track =
    { uri: "spotify:track:br2", name: "Next Song",
      artists: [{ name: "Next Artist" }], sortable: true, image: null };
  routes["GET /api/now/suggest?force=1"].body.track_uri = "spotify:track:br2";
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("BR the reveal falls away when the next track starts",
        !document.body.classList.contains("peeked") && run("peekedUri") === null &&
        !$$("btn-reveal").classList.contains("on"),
        `peeked=${document.body.classList.contains("peeked")} ` +
        `peekedUri=${run("peekedUri")}`);

  // The cover hides behind the bar: tapping its peek (or pulling the bar,
  // below) expands it INSIDE the card, at the top, with the large image, the
  // title and artist, and share under it.
  routes["GET /api/now?light=1"].body.track =
    { uri: "spotify:track:br3", name: "Cover Song", artists: [{ name: "Cover Artist" }],
      sortable: true, image: "http://img/300", image_large: "http://img/640" };
  routes["GET /api/now/suggest?force=1"].body.track_uri = "spotify:track:br3";
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  const card = () => $$("now-card").innerHTML;
  // Shut, the panel is still there as the strip; open means not shut.
  const panelOpen = () => card().includes('id="cover-panel"') && !card().includes("cover-shut");
  const strip = () => card().includes('class="cover-panel cover-shut') && card().includes('style="--f:0"');
  run(`if (coverOpen) closeCover()`);
  check("CV shut, the cover is a strip at the top of the card",
        strip() && card().indexOf('id="cover-panel"') < card().indexOf("np-progress"), card().slice(0, 300));
  check("CV ...that does not name the song, not even hidden",
        !/Cover Song|Cover Artist|cover-info/.test(card()), card().slice(0, 400));
  run(`$("cover-panel").onclick()`);
  check("CV a tap on the strip opens the large cover inside the card",
        panelOpen() && /id="cover-panel"[\s\S]*src="http:\/\/img\/640"/.test(card()),
        `card=${card().slice(0, 300)}`);
  check("CV ...at the top of the card, above the playback strip",
        card().indexOf('id="cover-panel"') < card().indexOf("np-progress"),
        `panel@${card().indexOf('id="cover-panel"')} strip@${card().indexOf("np-progress")}`);
  check("CV ...with the title and artist under it",
        /id="cover-panel"[\s\S]*cover-title">Cover Song<[\s\S]*cover-artist">Cover Artist</.test(card()),
        card().slice(0, 400));
  check("CV ...expanding on the render that opens it",
        card().includes("cover-panel cover-in"), card().slice(0, 200));
  run(`renderNow()`);
  check("CV ...but not again on the next re-render (a poll)",
        panelOpen() && !card().includes("cover-panel cover-in"), card().slice(0, 200));
  check("CV opening the cover does not reveal the reasons",
        !document.body.classList.contains("peeked"), "");
  check("CV share rides the cover panel",
        /id="cover-panel"[\s\S]*id="btn-share"/.test(card()), card().slice(0, 400));
  $$("share-pop").hidden = true;
  run(`$("btn-share").onclick()`);
  await tick();
  check("CV tapping share closes the cover and opens the share sheet",
        !panelOpen() && $$("share-pop").hidden === false,
        `panel=${panelOpen()} share hidden=${$$("share-pop").hidden}`);
  $$("share-pop").hidden = true;
  run(`$("cover-panel").onclick()`);
  run(`$("cover-panel").onclick()`);
  check("CV a tap on it shuts it back to the strip, folding up",
        !panelOpen() && strip() && card().includes("cover-out"), card().slice(0, 200));
  run(`renderNow()`);
  check("CV ...and folds up only once", strip() && !card().includes("cover-out"), "");
  check("CV the cover button is gone from the bar",
        !/btn-cover/.test(fs.readFileSync(path.join(HERE, "..", "sortify", "static", "index.html"), "utf8")), "");
  run(`$("btn-reveal").onclick()`);
  check("CV the eye unblurs the reasons without opening the cover",
        document.body.classList.contains("peeked") && !panelOpen(), "");
  run(`setReveal(false)`);

  run(`$("cover-panel").onclick()`);
  routes["GET /api/now?light=1"].body.track =
    { uri: "spotify:track:br4", name: "After Song", artists: [{ name: "After Artist" }],
      sortable: true, image: "http://img/x" };
  routes["GET /api/now/suggest?force=1"].body.track_uri = "spotify:track:br4";
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("CV the cover closes when the next track starts",
        !panelOpen() && !/After Song/.test(card()),
        `panel=${panelOpen()}`);

  routes["GET /api/now?light=1"].body.track.image = null;
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  run(`openCover()`);
  check("CV no cover, no panel — a toast says so",
        !panelOpen() && /no cover/.test($$("toast").textContent),
        `toast=${JSON.stringify($$("toast").textContent)}`);

  // The whole top bar can be pulled downward: the cover opens under the
  // finger, and a release decides open or closed. The stub panel has no
  // layout, so its full height falls back to 360px.
  routes["GET /api/now?light=1"].body.track =
    { uri: "spotify:track:br5", name: "Pull Song", artists: [{ name: "Pull Artist" }],
      sortable: true, image: "http://img/300" };
  routes["GET /api/now/suggest?force=1"].body.track_uri = "spotify:track:br5";
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  const bar = $$("topbar");
  const pt = (y, t, x = 100) => ({ clientX: x, clientY: y, timeStamp: t, pointerId: 1 });
  check("PEEK the strip is this song's art", strip() && /src="http:\/\/img\/300"/.test(card()),
        card().slice(0, 300));
  $$("nav-pop").hidden = false;
  bar.onpointerdown({ clientX: 100, clientY: 10, timeStamp: 0, pointerId: 1 });
  bar.onpointermove({ clientX: 100, clientY: 100, timeStamp: 400, pointerId: 1 });
  bar.onpointerup({});
  check("PULL with the menu open, the bar does not pull", !panelOpen() && !run("coverOpen"), "");
  $$("nav-pop").hidden = true;
  // The click a real browser fires after every pointerup, through the bar's
  // capture-phase listeners. Returns whether it was swallowed.
  const click = () => {
    let stopped = false;
    const ev = { stopPropagation() { stopped = true; }, preventDefault() {} };
    for (const h of bar._ls?.click || []) h(ev);
    return stopped;
  };
  bar.onpointerdown(pt(10, 0));
  bar.onpointerup(pt(10, 0));
  check("PULL a plain tap on the bar reaches its button", click() === false, "");
  bar.onpointerdown(pt(10, 0));
  bar.onpointermove(pt(13, 50));
  check("PULL a few pixels is still a tap, not a pull",
        !panelOpen() && run("coverPull") === null, `pull=${run("coverPull")}`);
  bar.onpointermove(pt(100, 400));
  check("PULL dragging down draws the panel at the finger's size",
        panelOpen() && Math.abs(run("coverPull") - 0.25) < 1e-9 &&
        $$("cover-panel").style["--f"] === "0.25" && !card().includes("cover-panel cover-in"),
        `pull=${run("coverPull")} f=${$$("cover-panel").style["--f"]}`);
  run(`renderNow()`);
  check("PULL a re-render mid-pull keeps the pulled size",
        card().includes('style="--f:0.25"'), card().slice(0, 200));
  bar.onpointermove(pt(160, 1000));
  bar.onpointerup(pt(160, 1000));
  const swallowed = click();
  check("PULL released past the line, it opens and the click is not a tap",
        panelOpen() && run("coverOpen") && run("coverPull") === null && swallowed,
        `open=${run("coverOpen")} pull=${run("coverPull")} swallowed=${swallowed}`);
  check("PULL ...and only that one click is swallowed", click() === false, "");
  bar.onpointerdown(pt(10, 0));
  bar.onpointermove(pt(100, 400));
  check("PULL an open cover is not pulled again", run("coverPull") === null, "");
  bar.onpointerup(pt(100, 400));
  click();
  $$("cover-panel").onclick();
  check("PULL ...a tap on it still closes it", !panelOpen() && !run("coverOpen"), "");

  bar.onpointerdown(pt(10, 0));
  bar.onpointermove(pt(40, 400));
  bar.onpointermove(pt(60, 800));
  bar.onpointerup(pt(60, 800));
  click();
  check("PULL released short of the line, it collapses...",
        run("coverPull") === 0 && $$("cover-panel").style["--f"] === "0", "");
  await new Promise((r) => setTimeout(r, 260));
  check("PULL ...and then closes", !panelOpen() && !run("coverOpen"), `open=${run("coverOpen")}`);

  bar.onpointerdown(pt(10, 0));
  bar.onpointermove(pt(30, 10));
  bar.onpointermove(pt(50, 20));
  bar.onpointerup(pt(50, 20));
  click();
  check("PULL a quick flick opens it even when short",
        run("coverOpen") && run("coverPull") === null, `pull=${run("coverPull")}`);
  run(`closeCover()`);

  bar.onpointerdown(pt(10, 0));
  bar.onpointermove(pt(100, 400));
  bar.onpointercancel();
  await new Promise((r) => setTimeout(r, 260));
  check("PULL a cancelled pull closes", !panelOpen() && !run("coverOpen"), "");

  // The strip pulls too; the rest of the card does not (it scrolls).
  const cardEl = $$("now-card");
  const on = (hit) => ({ closest: (sel) => (sel === ".cover-panel" && hit ? {} : null) });
  cardEl.onpointerdown({ ...pt(10, 0), target: on(false) });
  cardEl.onpointermove({ ...pt(100, 400), target: on(false) });
  cardEl.onpointerup(pt(100, 400));
  check("PULL a drag elsewhere on the card does not pull", !panelOpen() && run("coverPull") === null, "");
  cardEl.onpointerdown({ ...pt(10, 0), target: on(true) });
  cardEl.onpointermove({ ...pt(160, 1000), target: on(true) });
  check("PULL dragging the strip itself pulls the cover", panelOpen() && run("coverPull") > 0.4,
        `pull=${run("coverPull")}`);
  cardEl.onpointerup(pt(160, 1000));
  check("PULL ...and letting go past the line opens it", run("coverOpen") && run("coverPull") === null, "");
  click();

  // Open, a swipe up on the cover pushes it back into the strip, the same
  // gesture in reverse: under the finger, then past the line (or a flick)
  // it folds, short of it it springs back open.
  const cardClick = () => {
    let stopped = false;
    const ev = { stopPropagation() { stopped = true; }, preventDefault() {} };
    for (const h of cardEl._ls?.click || []) h(ev);
    return stopped;
  };
  cardEl.onpointerdown({ ...pt(400, 0), target: on(true) });
  cardEl.onpointermove({ ...pt(396, 50), target: on(true) });
  check("PUSH a few pixels up is still a tap", run("coverPull") === null && run("coverOpen"), "");
  cardEl.onpointermove({ ...pt(310, 400), target: on(true) });
  check("PUSH swiping up folds the open cover under the finger",
        panelOpen() && Math.abs(run("coverPull") - 0.75) < 1e-9 && $$("cover-panel").style["--f"] === "0.75",
        `pull=${run("coverPull")} f=${$$("cover-panel").style["--f"]}`);
  cardEl.onpointerup(pt(310, 400));
  check("PUSH let go short of the line, it springs back open and the click is not a tap",
        run("coverOpen") && run("coverPull") === null && cardClick(),
        `open=${run("coverOpen")} pull=${run("coverPull")}`);
  cardEl.onpointerdown({ ...pt(400, 0), target: on(true) });
  cardEl.onpointermove({ ...pt(300, 400), target: on(true) });
  cardEl.onpointermove({ ...pt(200, 800), target: on(true) });
  cardEl.onpointerup(pt(200, 800));
  cardClick();
  check("PUSH let go past the line, it folds into the strip...",
        run("coverPull") === 0 && $$("cover-panel").style["--f"] === "0", `pull=${run("coverPull")}`);
  await new Promise((r) => setTimeout(r, 260));
  check("PUSH ...and then is shut", strip() && !run("coverOpen"), `open=${run("coverOpen")}`);
  run(`openCover()`);
  cardEl.onpointerdown({ ...pt(400, 0), target: on(true) });
  cardEl.onpointermove({ ...pt(380, 10), target: on(true) });
  cardEl.onpointermove({ ...pt(360, 20), target: on(true) });
  cardEl.onpointerup(pt(360, 20));
  cardClick();
  await new Promise((r) => setTimeout(r, 260));
  check("PUSH a quick flick up folds it even when short", strip() && !run("coverOpen"), "");
  run(`openCover()`);
  cardEl.onpointerdown({ ...pt(10, 0), target: on(true) });
  cardEl.onpointermove({ ...pt(100, 400), target: on(true) });
  cardEl.onpointerup(pt(100, 400));
  check("PUSH dragging an open cover down does nothing",
        run("coverOpen") && run("coverPull") === null && cardClick() === false, "");
  bar.onpointerdown(pt(400, 0));
  bar.onpointermove(pt(100, 400));
  bar.onpointerup(pt(100, 400));
  check("PUSH the bar does not push an open cover shut",
        run("coverOpen") && run("coverPull") === null && click() === false, "");
  cardEl.onpointerdown({ ...pt(400, 0), target: on(true) });
  cardEl.onpointermove({ ...pt(200, 400), target: on(true) });
  cardEl.onpointercancel();
  cardClick();
  check("PUSH a cancelled push leaves it open", run("coverOpen") && run("coverPull") === null, "");
  run(`closeCover()`);

  routes["GET /api/now?light=1"].body.track.image = null;
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  $$("toast").textContent = "";
  bar.onpointerdown(pt(10, 0));
  bar.onpointermove(pt(100, 400));
  bar.onpointerup(pt(100, 400));
  check("PEEK no art, no strip", !card().includes('id="cover-panel"'), "");
  check("PULL no cover: the bar does not pull", !panelOpen() && !run("coverOpen"), "");

  // A poll that never reaches the server ("Failed to fetch") says so with a
  // Try again, which polls again at once instead of waiting out the 90s.
  const lightRoute = routes["GET /api/now?light=1&force=1"];
  routes["GET /api/now?light=1&force=1"] = () => { throw new TypeError("Failed to fetch"); };
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("RETRY a failed poll shows the error with a Try again",
        /Failed to fetch/.test(card()) && /id="btn-now-retry"[^>]*>Try again</.test(card()),
        card().slice(0, 200));
  routes["GET /api/now?light=1&force=1"] = lightRoute;
  check("PEEK a failed poll takes the strip with the track", !card().includes('id="cover-panel"'), "");
  resetLog();
  $$("btn-now-retry").onclick();
  check("RETRY ...pressing it disables it against a double tap",
        $$("btn-now-retry").disabled === true, "");
  await tick();
  run("stopNowPolling()");
  check("RETRY ...and polls again at once, putting the card back",
        gets("/api/now?light=1&force=1") === 1 && !/btn-now-retry|Failed to fetch/.test(card()) &&
        !$$("btn-now-refresh").disabled,
        `polls=${gets("/api/now?light=1&force=1")} card=${card().slice(0, 120)}`);

  // Same for the suggestions half of the card.
  const suggRoute = routes["GET /api/now/suggest?force=1"];
  routes["GET /api/now/suggest?force=1"] = () => { throw new TypeError("Failed to fetch"); };
  run(`pollNow(true)`);
  await tick(); await tick();
  run("stopNowPolling()");
  check("RETRY failed suggestions carry a Try again too",
        /suggestions failed: Failed to fetch/.test(card()) && /id="btn-sugg-retry"/.test(card()),
        card().slice(0, 400));
  routes["GET /api/now/suggest?force=1"] = suggRoute;
  resetLog();
  $$("btn-sugg-retry").onclick();
  await tick(); await tick();
  run("stopNowPolling()");
  check("RETRY ...which refetches them and clears the failure",
        gets("/api/now/suggest?force=1") === 1 && !/suggestions failed/.test(card()),
        `sugg=${gets("/api/now/suggest?force=1")}`);
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
  // The verb row is the notched trio now: Remove and Next extend toward each
  // other and the combined Remove+Next circle sits in the notch between them.
  check("RB it is inside the transport button group",
        at('np-buttons') !== -1 &&
        at('np-buttons') < at('id="btn-now-remove"') &&
        at('id="btn-now-remove"') < at('id="btn-now-both"'),
        `buttons@${at('np-buttons')} remove@${at('id="btn-now-remove"')} ` +
        `both@${at('id="btn-now-both"')}`);
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
  // The verb row is a centred trio: Remove left, Next right, the combined
  // circle notched between them — the moat around the circle is the mis-tap
  // buffer the old column gap used to be.
  check("RB remove sits left of Next in the verb trio",
        at('id="btn-now-remove"') < at('id="btn-now-next"') &&
        at('np-trio') !== -1,
        `remove@${at('id="btn-now-remove"')} next@${at('id="btn-now-next"')} ` +
        `trio@${at('np-trio')}`);
  // The quiet controls flank the progress bar — previous at its left end
  // (back = left), pause at its right — and both stay out of the verb row.
  check("RB previous flanks the bar's left end, pause its right",
        at('id="btn-now-prev"') !== -1 && at('id="btn-now-toggle"') !== -1 &&
        at('id="btn-now-prev"') < at('np-bar') &&
        at('np-bar') < at('id="btn-now-toggle"') &&
        at('id="btn-now-toggle"') < at('np-buttons'),
        `prev@${at('id="btn-now-prev"')} bar@${at('np-bar')} ` +
        `toggle@${at('id="btn-now-toggle"')} buttons@${at('np-buttons')}`);
  // Share is not on the card: it rides the full-screen cover (see CV).
  check("RB the share button is not on the card",
        at('id="btn-share"') === -1, `share@${at('id="btn-share"')}`);

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
  // The trio stays so Next never shifts sideways when the context changes —
  // the same instinct as 1f8ae2c, carried into the notched layout.
  check("RB the trio survives so the transport never shifts",
        at('np-trio') !== -1, `trio@${at('np-trio')}`);
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
  check("SM saving roles no longer sends subset_ids — a subset is its name",
        sent && !("subset_ids" in sent), JSON.stringify(sent));
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
        html().includes('class="track-card') && /finding a home…/.test(html()),
        JSON.stringify(html().slice(0, 160)));
  check("TP no suggestion buttons render while pending",
        !html().includes('class="sugg"'), "");
  check("TP the wait sits inside the row box, where the rows themselves land",
        /<div class="sugg-scroll">\s*<p class="sr-only"[^>]*>finding a home…<\/p><div class="sugg-skel"/.test(html()),
        JSON.stringify(html().slice(html().indexOf("sugg-scroll") - 20, 200)));
  // The point of the phase: everything that does NOT depend on the suggest
  // answer is on the card already, so the lower half does not drop in later.
  check("TP the new track's card is granted the slide-in",
        html().includes("card-fresh"), `html=${html().slice(0, 80)}`);
  check("TP the rest of the card is up in phase 1 — Add to…, subset, chips",
        html().includes('id="btn-now-more"') && html().includes('id="btn-now-subset"') &&
        html().includes('class="chip in-chip'),
        `more=${html().includes('id="btn-now-more"')} sub=${html().includes('id="btn-now-subset"')} chips=${html().includes('class="chip in-chip')}`);
  check("TP the playback strip is live while pending (Remove included)",
        html().includes('id="btn-now-next"') && html().includes('id="btn-now-remove"'),
        `next=${html().includes('id="btn-now-next"')} remove=${html().includes('id="btn-now-remove"')}`);
  release();
  await tick();
  check("TP suggestions fill in when the second answer lands",
        html().includes('class="sugg"') && html().includes("Home") &&
        !/finding a home…/.test(html()),
        JSON.stringify(html().slice(0, 200)));
  // The rows arriving is a fill-in, not an arrival: re-running the card's
  // slide-in here made phase 2 look like the card reloading.
  check("TP ...without the card sliding in a second time",
        html().includes("track-card") && !html().includes("card-fresh"),
        `html=${html().slice(0, 80)}`);
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

  // Cold start — no suggest answer has ever landed, so `homes` is genuinely
  // empty and the picker would open on nothing. The row is DRAWN anyway and
  // disabled (2026-09-21): a card whose furniture arrives in pieces is a card
  // you cannot aim at, and "loading" is a truthful thing for a control to
  // say. Pressing it is what must not happen, not seeing it.
  run("nowState = null; nowSuggestCache = null");
  let release2;
  const gate2 = new Promise((r) => { release2 = r; });
  setNow(tpBody("spotify:track:tp3"));   // rewrites the suggest routes...
  const suggRoute3 = routes["GET /api/now/suggest?force=1"];   // ...so gate after
  routes["GET /api/now/suggest?force=1"] = () => gate2.then(() => suggRoute3);
  run(`pollNow(true)`);
  await tick();
  const more = () => (html().match(/<button[^>]*id="btn-now-more"[^>]*>/) || [""])[0];
  check("TP a cold phase 1 draws Add to home… but will not open an empty picker",
        /disabled/.test(more()) && html().includes('id="btn-now-subset"'), `more=${more()}`);
  check("TP ...and says it is still loading rather than sitting there dead",
        /loading your homes/.test(html()), html().slice(0, 400));
  release2();
  await tick();
  run("stopNowPolling()");
  check("TP ...and it goes live the moment the homes land",
        html().includes('id="btn-now-more"') && !/disabled/.test(more()), `more=${more()}`);
}

// ============================================================================
// HL — "Homeless": the verdict that no home fits, as a move into the
// buffer that collects those songs. It files (out of the input, into the
// buffer) rather than captures.
//
// The button used to VANISH in the four cases where the move would be wrong.
// It is a primary control now (2026-09-20), and one that comes and goes is
// one your thumb cannot find, so it is always drawn and DISABLED instead —
// the four cases are still four refusals, just visible ones.
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
  // Filing replaces the list, Add to home…, Homeless and the chips row all
  // at once — ~517px on the device this was measured on. Held open, or the
  // card shortens under the thumb that just pressed it.
  check("HL the filed card holds open the room the filing controls had",
        /class="filing-region filed-region" style="min-height:\d+px"/.test(html()),
        (html().match(/class="filing-region[^"]*" style="[^"]*"/) || ["none"])[0]);
  check("HL the filed card celebrates with the big drawn check",
        html().includes('class="done-mark"') && html().includes("<svg"),
        `mark=${html().includes('class="done-mark"')}`);
  check("HL the check draws left to right (path starts at the left tail)",
        html().includes('points="4 12 9 17 20 6"'),
        `points=${(html().match(/points="[^"]*"/) || ["none"])[0]}`);
  check("HL and the strip offers the way back",
        html().includes('id="btn-now-undo-remove"'), `html=${html()}`);

  // The four refusals. Present in every one of them, and inert in every one.
  const homeless = () => (html().match(/<button id="btn-now-homeless"[^>]*>/) || [""])[0];

  await paint({ homeless_id: null });
  check("HL no configured destination: still there, inert",
        /disabled/.test(homeless()), `btn=${homeless()}`);

  await paint({ context: { id: "NH1", name: "[Homeless]", is_input: true } });
  check("HL playing from the buffer itself: inert rather than gone",
        /disabled/.test(homeless()), `btn=${homeless()}`);

  await paint({ inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" },
                         { id: "NH1", name: "[Homeless]", has_track: true, set: "buffer" }] });
  check("HL a song already in the buffer is refused, visibly",
        /disabled/.test(homeless()), `btn=${homeless()}`);
  check("HL ...and says why, rather than leaving a dead button",
        /title="[^"]*already/.test(homeless()), `btn=${homeless()}`);

  await paint({ context: null });
  check("HL nothing to move out of: inert",
        /disabled/.test(homeless()), `btn=${homeless()}`);

  await paint({});
  check("HL and when the move IS available the button is live",
        /id="btn-now-homeless"/.test(homeless()) && !/disabled/.test(homeless()),
        `btn=${homeless()}`);
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

  // Add to home… moved OUT of the list (2026-09-20), so with nothing
  // suggested the box is genuinely empty — and the way past the guesses is
  // below it, where it does not have to be scrolled to.
  card = await paint([]);
  const empty = card.match(/<div class="sugg-scroll">([\s\S]*?)<\/div>/);
  check("SC nothing suggested leaves the box empty, not holding a control",
        !!empty && (empty[1].match(/data-to="/g) || []).length === 0 &&
        !empty[1].includes("btn-now-more"), `card=${card}`);
  check("SC ...and Add to home… is still on the card, below the box",
        card.indexOf("btn-now-more") > card.indexOf("sugg-scroll"), `card=${card}`);

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
  const box2 = card.match(/<div class="sugg-scroll">([\s\S]*?)<\/div>\s*<div class="home-actions">/);
  check("SC the six guesses fill the box and the controls follow it",
        !!box2 && box2[1].includes('data-to="H6"') && !box2[1].includes('id="btn-now-more"'),
        `card=${card}`);
  check("SC Add to home… carries no data-to, so the file wiring cannot pick it up",
        !/id="btn-now-more"[^>]*data-to=/.test(card), `card=${card}`);
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
  const paint = async (uri = "spotify:track:xm1") => {
    setNow({
      status: 200,
      body: {
        playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
        track: { uri, name: "Song", duration_ms: 200000,
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
  // Once decided, the card may name the song: blind mode is about choosing
  // by ear, and the choice is made.
  check("XM the removed card names the song, title and artist",
        /class="filed-song"[\s\S]*Song[\s\S]*Artist/.test(html()), `html=${html()}`);

  await paint();
  await run(`nowFile("H1")`);
  await tick();
  check("XM filing is untouched — still the check, still 'filed to'",
        html().includes("done-mark") && !html().includes("gone-mark") &&
        /filed to/.test(html()), `html=${html()}`);
  check("XM the filed card names the song too, above 'filed to'",
        /class="filed-song"[\s\S]*Song[\s\S]*Artist[\s\S]*filed to/.test(html()), `html=${html()}`);

  // The draw-on is granted by the render that first shows the mark and by no
  // other. Every poll — and every press of Next — rebuilds this card, and the
  // check used to redraw itself each time, as if the song had just been filed
  // again.
  check("XM the check draws itself on the render that files",
        html().includes("mark-in"), `html=${html()}`);
  run("renderNow()");
  check("XM ...and a re-render leaves it standing, undrawn",
        html().includes("done-mark") && !html().includes("mark-in"), `html=${html()}`);
  // Undoing and re-filing the same track is a new mark, and does draw.
  run("filedUris = {}; renderNow(); filedUris = { 'spotify:track:xm1': 'Home' }; renderNow()");
  check("XM re-filing the same track after an undo draws again",
        html().includes("mark-in"), `html=${html()}`);

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
  check("RG the trio still survives so the transport never shifts",
        html().includes("np-trio"), `html=${html().slice(0, 200)}`);

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

// ============================================================================
// SW — filing and removing ask the server to sweep every input; adding to a
// subset does not. A song put in a best-of has not been sorted, so its copies
// in the inboxes are still waiting for a real decision.
// ============================================================================
{
  const body = {
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:sw1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[A]", is_input: true }, sitting: null,
      suggestions: [], homes: [{ id: "H1", name: "Home", folder: "" }],
      subset_targets: [{ id: "S1", name: "best of", folder: "" }], subsets: [],
      inputs: [{ id: "IN1", name: "[A]", has_track: true, set: "buffer" }],
      homeless_id: null,
    },
  };
  const paint = async () => {
    setNow(body);
    run(`filedUris = {}; removedUri = null; nowActions = 0; nowActionLog = []; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };
  const lastAct = () => bodies("/api/act").slice(-1)[0];

  routes["POST /api/act"] = { status: 200, body: { ok: true, swept: [] } };
  await paint();
  resetLog();
  run(`nowFile("H1")`);
  await tick();
  check("SW filing asks for the sweep",
        lastAct()?.sweep_inputs === true, JSON.stringify(lastAct()));

  await paint();
  resetLog();
  run(`nowRemove()`);
  await tick();
  check("SW removing asks for the sweep",
        lastAct()?.sweep_inputs === true, JSON.stringify(lastAct()));

  await paint();
  resetLog();
  run(`nowAddToSubset("S1")`);
  await tick();
  check("SW adding to a subset does NOT sweep — it is not a filing decision",
        !lastAct()?.sweep_inputs && lastAct()?.from_id === null, JSON.stringify(lastAct()));

  // A delete from a playlist the user was not looking at must not be silent.
  routes["POST /api/act"] = { status: 200, body: { ok: true, swept: ["[B]"] } };
  await paint();
  $$("toast").textContent = "";
  run(`nowRemove()`);
  await tick();
  check("SW the toast names the one other input it emptied",
        $$("toast").textContent.includes("[A]") && $$("toast").textContent.includes("[B]"),
        JSON.stringify($$("toast").textContent.slice(0, 90)));

  routes["POST /api/act"] = { status: 200, body: { ok: true, swept: ["[B]", "[C]", "[D]"] } };
  await paint();
  $$("toast").textContent = "";
  run(`nowFile("H1")`);
  await tick();
  check("SW several get counted rather than listed",
        /3 more inputs/.test($$("toast").textContent),
        JSON.stringify($$("toast").textContent.slice(0, 90)));

  routes["POST /api/act"] = { status: 200, body: { ok: true, swept: [] } };
  await paint();
  $$("toast").textContent = "";
  run(`nowFile("H1")`);
  await tick();
  check("SW an empty sweep says nothing extra — the 98.7% case stays quiet",
        !/more inputs|\+ \[/.test($$("toast").textContent),
        JSON.stringify($$("toast").textContent.slice(0, 90)));
}

// ============================================================================
// CR — the capture row. It used to list every buffer input, opted in or not,
// with the ones holding the song merely tinted: a wall whose only real
// information was which two were highlighted, and which grew with the number
// of inboxes. Now the chips ARE the information — where this song already is
// — and putting it somewhere new goes through the picker, the same shape the
// card already uses for homes.
// ============================================================================
{
  resetLog();
  const html = () => $$("now-card").innerHTML;
  const rows = () => $$("picker-list").children.map((c) => c.innerHTML);
  const paint = async (over) => {
    setNow({
      status: 200,
      body: {
        playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
        track: { uri: "spotify:track:cr1", name: "Song", duration_ms: 200000,
                 artists: [{ name: "Artist" }], sortable: true, image: null },
        context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
        suggestions: [], homes: [{ id: "H1", name: "Home", folder: "" }],
        subset_targets: [], subsets: [],
        inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" },
                 { id: "IN2", name: "[Late night]", has_track: false, set: "buffer" },
                 { id: "IN3", name: "[Sunday]", has_track: false, set: "buffer" },
                 { id: "HL1", name: "[Homeless]", has_track: false, set: "buffer" }],
        homeless_id: "HL1",
        ...over,
      },
    });
    run(`show("now"); filedUris = {}; removedUri = null; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };

  await paint({});
  check("CR only the input holding the song gets a chip",
        html().includes('data-in="IN1"') && !html().includes('data-in="IN2"') &&
        !html().includes('data-in="IN3"'),
        `in1=${html().includes('data-in="IN1"')} in2=${html().includes('data-in="IN2"')}`);
  // A fact, not an offer: the row is where the song IS, and the only thing in
  // it you can press is the one that opens the picker.
  check("CR the chip is inert — a span, not a button",
        /<span class="chip in-chip has" data-in="IN1"/.test(html()),
        JSON.stringify(html().slice(html().indexOf("in-chip") - 30, 120)));
  // The button that used to sit here is gone (2026-09-20): for a new song
  // the inboxes are the card's main rows already, so it only covered a filed
  // song going back into a buffer.
  check("CR the row reports membership and offers nothing",
        !html().includes('id="btn-now-capture"'), "");

  await paint({ inputs: [{ id: "IN1", name: "[Hazy]", has_track: false, set: "buffer" }] });
  check("CR a song in no input shows no chips at all",
        !html().includes("in-chip"), "");
  check("CR ...and with nothing to report the row is gone too",
        !html().includes('class="capture"'), "");

  await paint({});
  // The capture picker went with the button (2026-09-20). Nothing opened it
  // but that control, and for a new song the inboxes are the card's main
  // rows already — captureRows/nowCapture, which the AN block covers.
}

// ============================================================================
// AD — "the list ran out and Spotify carried on by itself". The one state
// where the card is up, everything works, and none of it is doing anything.
//
// Every check here is as much about the silence as about the banner: it fires
// only when the app has provably nothing to do with what is playing, because
// a banner that cries wolf is a banner nobody reads.
// ============================================================================
{
  resetLog();
  const html = () => $$("now-card").innerHTML;
  const paint = async (over) => {
    setNow({
      status: 200,
      body: {
        playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
        track: { uri: "spotify:track:ad1", name: "Song", duration_ms: 200000,
                 artists: [{ name: "Artist" }], sortable: true, image: null },
        context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
        suggestions: [], homes: [{ id: "H1", name: "Home", folder: "" }],
        subset_targets: [], subsets: [],
        inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" }],
        homeless_id: null,
        ...over,
      },
    });
    run(`show("now"); filedUris = {}; removedUri = null; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };
  const adrift = { context: null,
                   inputs: [{ id: "IN1", name: "[Hazy]", has_track: false, set: "buffer" }] };

  // Playing from the input is the normal case, and it also teaches the client
  // which list to name when that list later runs out.
  await paint({});
  const up = () => !$$("adrift-strip").hidden && $$("adrift-strip").innerHTML.includes("btn-now-back");
  const strip = () => $$("adrift-strip").innerHTML;
  check("AD playing from an input says nothing", !up() && !html().includes("np-adrift"), "");
  check("AD ...and the input is remembered for when it runs dry",
        run(`lastInput && lastInput.id`) === "IN1", `lastInput=${run("JSON.stringify(lastInput)")}`);

  await paint(adrift);
  check("AD out of the input with the song in nothing: the strip is up in the bar",
        up(), `strip=${strip()}`);
  check("AD ...not as a banner on the card",
        !html().includes("np-adrift") && !html().includes("btn-now-back"), html().slice(0, 200));
  check("AD ...the switcher names the list that ran out",
        /\[Hazy\] ran out/.test($$("input-switch-label").textContent) &&
        $$("btn-input-switch").classList.contains("adrift"),
        `label=${$$("input-switch-label").textContent}`);
  check("AD ...and the strip offers it back",
        /Play \[Hazy\] again/.test(strip()), strip());

  resetLog();
  run(`$("btn-now-back").onclick()`);
  await tick();
  const play = bodies("/api/player/play").slice(-1)[0];
  check("AD pressing it starts that input again — one call, the existing one",
        play && play.input_id === "IN1", JSON.stringify(play));

  // Dismissed: the offer goes, the amber switcher stays, and it stays gone
  // across polls until you are back in an input.
  await paint(adrift);
  run(`$("btn-adrift-close").onclick()`);
  check("AD ✕ dismisses the strip", $$("adrift-strip").hidden, strip());
  await paint(adrift);
  check("AD ...it stays dismissed on the next poll, the switcher still amber",
        $$("adrift-strip").hidden && $$("btn-input-switch").classList.contains("adrift"), strip());
  await paint({});
  await paint(adrift);
  check("AD ...and comes back once an input has played and run out again", up(), strip());

  // The three silences.
  await paint({ context: null,
                inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" }] });
  check("AD a song still sitting in an input is not adrift", !up(), "");

  await paint({ ...adrift,
                suggestions: [{ playlist_id: "H1", pct: 100, reasons: [], already: true }] });
  check("AD a song already in one of your homes is not adrift either", !up(), "");

  // Deliberately putting a playlist on is not drift, and the strip stays out
  // of it: that is a choice the user just made, not playback drifting.
  await paint({ ...adrift, context: { id: "PL9", name: "Discover Weekly", is_input: false } });
  check("AD a playlist you deliberately put on draws no strip at all",
        !up() && !$$("btn-input-switch").classList.contains("adrift"), strip());
  check("AD ...and the unnameable case it used to botch is gone with it",
        !/something else/.test(html()), JSON.stringify(html().slice(0, 400)));

  // Phase 1 has no membership yet — suggestions are empty because they have
  // not been computed, not because the song is homeless. Reading the one as
  // the other would flash the strip onto every fresh card.
  await paint(adrift);
  run(`nowState.suggPending = true; renderNow()`);
  check("AD the strip never fires while the suggestion side is still pending", !up(), "");

  // The tail as Spotify reports it now (seen 2026-09-28): the input stays
  // the context while autoplay plays songs that are not in it.
  const tail = { inputs: [{ id: "IN2", name: "[Disco]", has_track: false, set: "buffer" }],
                 context: { id: "IN2", name: "[Disco]", is_input: true } };
  await paint(tail);
  check("AD the input still reported as context, the song in nothing: the strip is up",
        up() && /Play \[Disco\] again/.test(strip()), strip());
  check("AD ...the switcher names that input as the one that ran out",
        /\[Disco\] ran out/.test($$("input-switch-label").textContent),
        `label=${$$("input-switch-label").textContent}`);
  run(`$("btn-adrift-close").onclick()`);
  await paint(tail);
  check("AD ...a dismissal holds although the context is an input on every poll",
        $$("adrift-strip").hidden, strip());
  await paint({ ...tail, inputs: [{ id: "IN2", name: "[Disco]", has_track: true, set: "buffer" }] });
  await paint(tail);
  check("AD ...and ends once a song in the input has played", up(), strip());

  await paint(tail);
  run(`filedUris["spotify:track:ad1"] = "Home"; renderNow()`);
  check("AD a song you just filed or removed out of the input is not drift", !up(), strip());
}

// ============================================================================
// LP — the loop toggle. Reading the repeat state needs a scope the app only
// started asking for in Aug 2026, so the control's first duty is to be absent
// when it cannot tell the truth: a toggle showing what it last SET goes wrong
// the moment you change repeat in Spotify itself.
// ============================================================================
{
  resetLog();
  const html = () => $$("now-card").innerHTML;
  const loopTag = () => (html().match(/<button id="btn-now-loop"[^>]*>/) || [""])[0];
  const paint = async (over) => {
    setNow({
      status: 200,
      body: {
        playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
        track: { uri: "spotify:track:lp1", name: "Song", duration_ms: 200000,
                 artists: [{ name: "Artist" }], sortable: true, image: null },
        context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
        suggestions: [], homes: [{ id: "H1", name: "Home", folder: "" }],
        subset_targets: [], subsets: [],
        inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" }],
        homeless_id: null,
        ...over,
      },
    });
    run(`show("now"); filedUris = {}; removedUri = null; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };

  await paint({});
  check("LP no repeat state means no button — it would only be able to lie",
        loopTag() === "", `tag=${loopTag()}`);

  await paint({ repeat: "off" });
  check("LP a token that can read the state gets the button",
        loopTag() !== "", `tag=${loopTag()}`);
  check("LP ...unlit while the list runs out",
        !loopTag().includes("np-loop on") && loopTag().includes('aria-pressed="false"'),
        `tag=${loopTag()}`);

  resetLog();
  run(`$("btn-now-loop").onclick()`);
  await tick();
  const on = bodies("/api/player/repeat").slice(-1)[0];
  check("LP pressing it loops the list — one call, and the context, not the track",
        on && on.state === "context", JSON.stringify(on));
  check("LP the button answers the press without waiting for a poll",
        loopTag().includes("np-loop on"), `tag=${loopTag()}`);
  check("LP ...and no poll is spent confirming what we just set",
        gets("/api/now?light=1") === 0 && gets("/api/now?light=1&force=1") === 0,
        `${gets("/api/now?light=1") + gets("/api/now?light=1&force=1")} poll(s)`);

  resetLog();
  run(`$("btn-now-loop").onclick()`);
  await tick();
  check("LP pressing it again turns looping off",
        (bodies("/api/player/repeat").slice(-1)[0] || {}).state === "off",
        JSON.stringify(bodies("/api/player/repeat").slice(-1)[0]));

  // Spotify's third mode. Nothing here offers it, but if something else set
  // it the button says so rather than showing an indistinguishable "on".
  await paint({ repeat: "track" });
  check("LP repeat-one is shown as itself",
        loopTag().includes("np-loop on") && html().includes("loop-one"), `tag=${loopTag()}`);
  resetLog();
  run(`$("btn-now-loop").onclick()`);
  await tick();
  check("LP ...and pressing it stops looping rather than cycling onwards",
        (bodies("/api/player/repeat").slice(-1)[0] || {}).state === "off",
        JSON.stringify(bodies("/api/player/repeat").slice(-1)[0]));

  // A refused press must not leave the button showing a state Spotify is not
  // in — the optimistic paint is a promise, and this is it being kept.
  await paint({ repeat: "off" });
  routes["POST /api/player/repeat"] = { status: 400, body: { detail: "no active device" } };
  run(`$("btn-now-loop").onclick()`);
  await tick();
  check("LP a refused press puts the button back where it was",
        !loopTag().includes("np-loop on"), `tag=${loopTag()}`);
  check("LP ...and says why",
        /no active device/.test($$("toast").textContent),
        JSON.stringify($$("toast").textContent.slice(0, 80)));
  routes["POST /api/player/repeat"] = { status: 200, body: { ok: true } };
}

// ============================================================================
// UF — taking a song back out of a home it is already in.
//
// The row's own tap keeps its old meaning (file here / clear the inboxes), so
// the eject is a separate, smaller control and only "already there" rows get
// one. What it must NOT do is as load-bearing as what it does: no inbox
// sweep, and no done-card — the song is undecided again, and a card that
// congratulated itself would hide the suggestions you now need.
// ============================================================================
{
  resetLog();
  const html = () => $$("now-card").innerHTML;
  setNow({
    playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
    track: { uri: "spotify:track:uf1", name: "Song", duration_ms: 200000,
             artists: [{ name: "Artist" }], sortable: true, image: null },
    context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
    suggestions: [
      { playlist_id: "H1", pct: 100, already: true, weak: false, reasons: ["artist"] },
      { playlist_id: "H2", pct: 44, already: false, weak: false, reasons: ["tags"] },
    ],
    homes: [{ id: "H1", name: "Home One", folder: "" },
            { id: "H2", name: "Home Two", folder: "" }],
    subset_targets: [], subsets: [],
    inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer", total: 9 }],
    homeless_id: null,
  });
  run(`show("now"); filedUris = {}; removedUri = null; pollNow(true)`);
  await tick();
  run("stopNowPolling()");

  check("UF the already-there row gets an eject control",
        /class="sugg-eject" data-out="H1"/.test(html()), JSON.stringify(
          (html().match(/<button class="sugg-eject"[^>]*>/) || [""])[0]));
  check("UF ...and an ordinary guess does not — there is nothing to take out",
        !/data-out="H2"/.test(html()),
        JSON.stringify(html().match(/data-out="[^"]*"/g)));
  check("UF the row itself keeps its filing action",
        /class="sugg already[^"]*" data-to="H1"/.test(html()),
        JSON.stringify((html().match(/<button class="sugg already[^>]*>/) || [""])[0]));

  routes["POST /api/act"] = { status: 200, body: { ok: true, note: null, can_undo: true, swept: [] } };
  routes["POST /api/undo"] = { status: 200, body: { ok: true } };
  resetLog();
  await run(`nowUnfile("H1")`);
  await tick();
  const act = bodies("/api/act")[0];
  check("UF ejecting removes from that home alone",
        posts("/api/act") === 1 && act?.action === "remove" && act?.from_id === "H1",
        JSON.stringify(act));
  check("UF ...and never sweeps the inboxes — the song is undecided, not rejected",
        act?.sweep_inputs === false && !act?.to_id, JSON.stringify(act));
  check("UF the card stays live rather than landing in its done state",
        !/done-msg/.test(html()) && /data-to="H2"/.test(html()),
        JSON.stringify(html().slice(0, 120)));
  check("UF the row stops claiming already-there without waiting for a poll",
        !/data-out="H1"/.test(html()) && /class="sugg" data-to="H1"/.test(html()),
        JSON.stringify((html().match(/<button class="sugg[^>]*data-to="H1"[^>]*>/) || [""])[0]));

  await run(`undoLastNowAction()`);
  await tick();
  check("UF undo puts the badge back rather than leaving a stale row",
        /data-out="H1"/.test(html()), JSON.stringify(
          (html().match(/<button class="sugg[^>]*data-to="H1"[^>]*>/) || [""])[0]));
  run(`show("lists")`);
}

// ============================================================================
// CS — creating a subset from the card, at the moment of need.
//
// The price on the row is the promise: a subset add never removes anything (a
// song in a selection has not been sorted), so it is always create + add, and
// never the 3 calls the home row quotes when filing out of an input.
// ============================================================================
{
  resetLog();
  setNow({
    playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
    track: { uri: "spotify:track:cs1", name: "Song", duration_ms: 200000,
             artists: [{ name: "Artist" }], sortable: true, image: null },
    context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
    suggestions: [], subsets: [],
    homes: [{ id: "H1", name: "Home One", folder: "" }],
    subset_targets: [{ id: "S1", name: "best of", total: 4, folder: null }],
    inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer", total: 9 }],
    homeless_id: null,
  });
  run(`show("now"); filedUris = {}; removedUri = null; pollNow(true)`);
  await tick();
  run("stopNowPolling()");

  // The bug this block was written past: with subsets already marked, the
  // picker renders rows, and a create row gated on "nothing matched" can
  // never appear. Nine marked subsets meant the create path was unreachable
  // in the real app while this block's own typed-a-new-name case passed.
  run(`$("btn-now-subset").onclick()`);
  const lastRow = () => ($$("picker-list").children.slice(-1)[0] || {}).innerHTML || "";
  check("CS the create row is offered with the picker merely open",
        /Create a new subset/.test(lastRow()),
        JSON.stringify($$("picker-list").children.map((c) => c.innerHTML.slice(0, 60))));
  check("CS ...and with no name typed it asks for one instead of firing blind",
        /type a name above/.test(lastRow()), JSON.stringify(lastRow()));
  resetLog();
  await $$("picker-list").children.slice(-1)[0].onclick();
  check("CS ...spending nothing when it does",
        posts("/api/playlists/create") === 0 && $$("picker").hidden === false,
        `${posts("/api/playlists/create")} POST(s), hidden=${$$("picker").hidden}`);

  run(`$("picker-filter").value = "best"; $("picker-filter").oninput({ target: { value: "best" } })`);
  check("CS ...and still offered when the typed name DOES match an existing one",
        $$("picker-list").children.some((c) => /Create subset/.test(c.innerHTML))
        && $$("picker-list").children.some((c) => /best of/.test(c.innerHTML)),
        JSON.stringify($$("picker-list").children.map((c) => c.innerHTML.slice(0, 60))));

  run(`$("picker-filter").value = "krautrock"; $("picker-filter").oninput({ target: { value: "krautrock" } })`);
  const made = $$("picker-list").children.slice(-1)[0];
  check("CS a subset picker matching nothing offers to create one",
        /Create subset/.test(made?.innerHTML || ""), JSON.stringify(made?.innerHTML));
  check("CS ...named what was typed",
        /krautrock/.test(made?.innerHTML || ""), JSON.stringify(made?.innerHTML));
  check("CS ...priced at two calls even though the context is an input",
        /2 calls/.test(made?.innerHTML || "") && !/3 calls/.test(made?.innerHTML || ""),
        JSON.stringify(made?.innerHTML));

  routes["POST /api/playlists/create"] = { status: 200, body: {
    playlist: { id: "S9", name: "krautrock", role: "subset", total: 0, folder: null },
    note: null } };
  routes["POST /api/act"] = { status: 200, body: { ok: true, note: null, can_undo: true, swept: [] } };
  resetLog();
  await made.onclick();
  await tick();
  const created = bodies("/api/playlists/create")[0];
  check("CS creating sends the subset role, not home",
        posts("/api/playlists/create") === 1 && created?.role === "subset"
        && created?.name === "krautrock", JSON.stringify(created));
  const added = bodies("/api/act")[0];
  check("CS ...and the track goes straight into it",
        posts("/api/act") === 1 && added?.to_id === "S9", JSON.stringify(added));
  check("CS ...without leaving its input — a subset add is not a filing",
        added?.from_id === null || added?.from_id === undefined, JSON.stringify(added));

  // The home picker's create row must be untouched by the subset wording.
  run(`openNowPicker()`);
  run(`$("picker-filter").value = "zzz"; $("picker-filter").oninput({ target: { value: "zzz" } })`);
  const homeMade = $$("picker-list").children.slice(-1)[0];
  check("CS the home picker still offers a HOME, priced with its removal",
        /Create home/.test(homeMade?.innerHTML || "")
        && /3 calls/.test(homeMade?.innerHTML || ""),
        JSON.stringify(homeMade?.innerHTML));
  run(`closePicker(); show("lists")`);
}

// ============================================================================
// NC — the verb row is a notched trio: Remove and Next extend toward each
// other and a combined Remove+Next circle sits in the notch between them,
// carved free by a moat (the moat IS the mis-tap buffer the old column gap
// used to be). The circle is the Remove verb plus a skip — one press, both
// legs, remove first — so it mirrors Remove's live/dead matrix exactly, and
// it dissolves back to the two-slot layout in the states where combining is
// moot: an Undo on offer, or a sitting. Direction chosen 2026-09-01 from the
// "Remove + Next Overlap" mockups.
// ============================================================================
{
  const nowBody = (opts = {}) => ({
    status: 200,
    body: {
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: opts.uri || "spotify:track:nc1", name: "Song",
               duration_ms: 200000, artists: [{ name: "Artist" }],
               sortable: true, image: null },
      context: { id: "IN1", name: "[Notch]", is_input: opts.isInput !== false },
      sitting: opts.sitting || null, suggestions: [],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      subset_targets: [], subsets: [],
      inputs: [{ id: "IN1", name: "[Notch]", has_track: true, set: "buffer" }],
      homeless_id: null,
    },
  });
  const html = () => $$("now-card").innerHTML;
  const at = (needle) => html().indexOf(needle);
  const has = (needle) => html().includes(needle);
  const bothTag = () => (html().match(/<button id="btn-now-both"[^>]*>/) || [""])[0];
  const removeTag = () => (html().match(/<button id="btn-now-remove"[^>]*>/) || [""])[0];
  const paint = async (opts) => {
    setNow(nowBody(opts));
    run(`filedUris = {}; removedUri = null; nowActions = 0; nowActionLog = []; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };
  try {
    // Open by setting the state the block needs: a hidden Now view makes
    // pollNow a silent no-op and every assertion below would read whatever
    // card the previous block left behind.
    $$("view-now").hidden = false;
    routes["POST /api/act"] = { status: 200, body: {} };
    routes["POST /api/player/next"] = { status: 200, body: { ok: true } };
    await paint({ uri: "spotify:track:nc1" });

    check("NC an input context renders the notched trio, not the slot pair",
          at("np-trio") !== -1 && at("np-remove-slot") === -1 && at("np-next-slot") === -1,
          `trio@${at("np-trio")} rslot@${at("np-remove-slot")} nslot@${at("np-next-slot")}`);
    // .np-buttons is a two-column grid for the slot pair; a trio left inside
    // it lands in the first column and sits ~25px left of the card's centre.
    // The modifier is what opts the wrapper out, so the markup must carry it.
    check("NC the trio's wrapper opts out of the two-column grid, so it centres",
          has("np-buttons np-buttons-trio"),
          `wrapper=${(html().match(/<div class="np-buttons[^"]*"/) || [""])[0]}`);
    check("NC order: Remove, then the circle, then Next",
          at('id="btn-now-remove"') !== -1 &&
          at('id="btn-now-remove"') < at('id="btn-now-both"') &&
          at('id="btn-now-both"') < at('id="btn-now-next"'),
          `remove@${at('id="btn-now-remove"')} both@${at('id="btn-now-both"')} ` +
          `next@${at('id="btn-now-next"')}`);
    check("NC the live circle is live — no dead class, no aria-disabled, both legs named",
          bothTag() !== "" && !bothTag().includes("np-dead") &&
          !bothTag().includes("aria-disabled") && /skip/i.test(bothTag()),
          `tag=${bothTag()}`);
    check("NC remove keeps its danger identity inside the trio",
          at("np-trio") !== -1 && removeTag().includes("np-danger"),
          `trio@${at("np-trio")} tag=${removeTag()}`);

    // One press, both legs, remove first. The wiring is a failed check, not
    // an exception that takes the rest of the block down.
    resetLog();
    const wired = run(`typeof $("btn-now-both")?.onclick === "function"`);
    check("NC the circle is wired", !!wired, `onclick=${wired}`);
    if (wired) { run(`$("btn-now-both").onclick()`); await tick(); }
    const actIdx = log.findIndex((c) => c.path === "/api/act");
    const nextIdx = log.findIndex((c) => c.path === "/api/player/next");
    check("NC one press spends the remove leg, then the skip, in that order",
          posts("/api/act") === 1 && posts("/api/player/next") === 1 &&
          actIdx !== -1 && nextIdx !== -1 && actIdx < nextIdx,
          `act@${actIdx} next@${nextIdx} acts=${posts("/api/act")} skips=${posts("/api/player/next")}`);
    const actBody = bodies("/api/act")[0] || {};
    check("NC the remove leg is Remove itself — same action, same sweep",
          actBody.action === "remove" && actBody.from_id === "IN1" &&
          actBody.sweep_inputs === true, JSON.stringify(actBody));
    // While the press is settling, a re-render must hold the trio: the
    // removal has already landed (removedUri is set), and without the hold
    // the strip would flash its Undo swap for the second the skip takes.
    run("renderNow()");
    check("NC in flight the circle is busy and the trio holds — no Undo flash",
          bothTag().includes("np-busy") && has("np-trio") &&
          !has('id="btn-now-undo-remove"'),
          `tag=${bothTag()} undo=${has('id="btn-now-undo-remove"')}`);
    if (wired) { run(`$("btn-now-both").onclick()`); await tick(); }
    check("NC a second press while pending spends nothing",
          posts("/api/act") === 1 && posts("/api/player/next") === 1,
          `acts=${posts("/api/act")} skips=${posts("/api/player/next")}`);

    // The new track is the finish line, exactly as for Next alone: pending
    // clears, the undo offer expires with the track it was about, and the
    // fresh card gets a fresh trio.
    setNow(nowBody({ uri: "spotify:track:nc2" }));
    run(`pollNow(true)`);
    await tick();
    run("stopNowPolling()");
    check("NC the settling track clears the pending and the undo offer",
          run("npPending") === null && run("removedUri") === null &&
          has("np-trio") && has('id="btn-now-both"') &&
          !has('id="btn-now-undo-remove"'),
          `pending=${JSON.stringify(run("npPending"))} undo=${has('id="btn-now-undo-remove"')}`);

    // The circle mirrors Remove's matrix: outside an input it is drawn dead —
    // aria-disabled, not disabled, so the tap still reaches the explanation.
    await paint({ isInput: false, uri: "spotify:track:nc3" });
    check("NC outside an input the circle is drawn dead",
          bothTag().includes("np-dead") && bothTag().includes('aria-disabled="true"') &&
          !/\sdisabled/.test(bothTag()), `tag=${bothTag()}`);
    resetLog();
    $$("toast").textContent = "";
    const deadWired = run(`typeof $("btn-now-both")?.onclick === "function"`);
    if (deadWired) { run(`$("btn-now-both").onclick()`); await tick(); }
    check("NC pressing the dead circle says why, and spends nothing",
          /input list/i.test($$("toast").textContent) &&
          posts("/api/act") === 0 && posts("/api/player/next") === 0,
          `toast=${JSON.stringify($$("toast").textContent.slice(0, 80))} ` +
          `acts=${posts("/api/act")} skips=${posts("/api/player/next")}`);

    // A failed remove leg aborts the skip: removing is the decision, skipping
    // is only its consequence, and skipping a track that was NOT removed
    // would lose it un-decided.
    await paint({ uri: "spotify:track:nc4" });
    routes["POST /api/act"] = { status: 500, body: { error: "nope" } };
    resetLog();
    const wired4 = run(`typeof $("btn-now-both")?.onclick === "function"`);
    if (wired4) { run(`$("btn-now-both").onclick()`); await tick(); }
    check("NC a failed remove leg aborts the skip and releases the press",
          has('id="btn-now-both"') && posts("/api/act") === 1 &&
          posts("/api/player/next") === 0 && run("npPending") === null,
          `acts=${posts("/api/act")} skips=${posts("/api/player/next")} ` +
          `pending=${JSON.stringify(run("npPending"))}`);

    // A lone Remove dissolves the trio: the Undo takes the old two-slot
    // layout, because a combined verb for an already-removed track is moot.
    routes["POST /api/act"] = { status: 200, body: {} };
    await paint({ uri: "spotify:track:nc5" });
    await run(`nowRemove()`);
    await tick();
    check("NC a lone Remove dissolves the trio back to the slot pair",
          !has('id="btn-now-both"') && !has("np-trio") &&
          !has("np-buttons-trio") &&
          has("np-remove-slot") && has('id="btn-now-undo-remove"'),
          `both=${has('id="btn-now-both"')} trio=${has("np-trio")} ` +
          `undo=${has('id="btn-now-undo-remove"')}`);

  } finally {
    routes["POST /api/act"] = { status: 200, body: {} };
    run("stopNowPolling()");
    $$("view-now").hidden = false;
  }
}

// ============================================================================
// SD — only a SPENT decision takes the Remove slot.
//
// The strip swapped Remove for Undo after any action on the playing track.
// But three of them leave the song exactly where it was in its inbox: adding
// it to a subset (a selection is not a filing — the song still needs a home),
// capturing it into another input, and taking it back out of a home. In all
// three the song is still undecided, so Remove still has something to do and
// must not be swapped away. The undo for them is the top bar's, one tap away.
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
      sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 100, already: true, weak: false, reasons: ["a"] }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      subset_targets: [{ id: "S1", name: "best of", total: 4, folder: null }],
      inputs: [{ id: "IN1", name: "[In]", has_track: true, set: "buffer", total: 9 },
               { id: "IN2", name: "[Other]", has_track: false, set: "buffer", total: 2 }],
      homeless_id: null,
    },
  });
  const html = () => $$("now-card").innerHTML;
  const has = (needle) => html().includes(needle);
  const strip = () => ({ remove: has('id="btn-now-remove"'),
                         undo: has('id="btn-now-undo-remove"') });

  routes["POST /api/act"] = { status: 200, body: { ok: true, swept: [] } };
  routes["POST /api/undo"] = { status: 200, body: { ok: true } };

  const fresh = async (uri) => {
    setNow(nowBody(uri));
    run(`filedUris = {}; nowActions = 0; removedUri = null; nowActionLog = []; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };

  await fresh("spotify:track:sd1");
  await run(`nowAddToSubset("S1")`);
  await tick();
  check("SD a subset add leaves Remove in place — the song is still undecided",
        strip().remove && !strip().undo, JSON.stringify(strip()));
  check("SD ...and the undo for it is still offered in the top bar",
        run(`$("btn-undo-now").hidden`) === false && run("nowActions") === 1,
        `hidden=${run(`$("btn-undo-now").hidden`)} actions=${run("nowActions")}`);

  await fresh("spotify:track:sd2");
  await run(`nowCapture("IN2")`);
  await tick();
  check("SD capturing into another input leaves Remove in place too",
        strip().remove && !strip().undo, JSON.stringify(strip()));

  await fresh("spotify:track:sd3");
  await run(`nowUnfile("H1")`);
  await tick();
  check("SD taking it back out of a home leaves Remove in place",
        strip().remove && !strip().undo, JSON.stringify(strip()));

  // The two that DO spend the decision keep their Undo.
  await fresh("spotify:track:sd4");
  await run(`nowFile("H1")`);
  await tick();
  check("SD filing to a home still swaps Remove for Undo",
        strip().undo && !strip().remove, JSON.stringify(strip()));

  await fresh("spotify:track:sd5");
  await run(`nowRemove()`);
  await tick();
  check("SD removing still swaps Remove for Undo",
        strip().undo && !strip().remove, JSON.stringify(strip()));

  run(`filedUris = {}; nowActions = 0; removedUri = null; nowActionLog = []; show("lists")`);
}

// ============================================================================
// TU — the toast carries the undo for actions that do not spend the decision.
//
// A subset add, a capture and an un-file all leave Remove in the strip (see
// SD), and #btn-undo-now is display:none by request — so without this the
// only way back is the `u` key, which a phone does not have. The undo is
// deliberately guarded: if anything else has acted since, pressing it must
// refuse rather than take back somebody else's decision.
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
      sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 100, already: true, weak: false, reasons: ["a"] }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      subset_targets: [{ id: "S1", name: "best of", total: 4, folder: null }],
      inputs: [{ id: "IN1", name: "[In]", has_track: true, set: "buffer", total: 9 },
               { id: "IN2", name: "[Other]", has_track: false, set: "buffer", total: 2 }],
      homeless_id: null,
    },
  });
  const toastHtml = () => $$("toast").innerHTML || "";
  routes["POST /api/act"] = { status: 200, body: { ok: true, swept: [] } };
  routes["POST /api/undo"] = { status: 200, body: { ok: true } };

  const fresh = async (uri) => {
    setNow(nowBody(uri));
    run(`filedUris = {}; nowActions = 0; removedUri = null; nowActionLog = []; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };

  await fresh("spotify:track:tu1");
  await run(`nowAddToSubset("S1")`);
  await tick();
  check("TU a subset add's toast offers an Undo",
        /id="toast-action"/.test(toastHtml()) && /best of/.test(toastHtml()),
        JSON.stringify(toastHtml().slice(0, 140)));

  resetLog();
  // Guarded: an unwired button is a failed check, not an exception that ends
  // the whole run before the later scenarios get to speak.
  const wired = run(`typeof $("toast-action").onclick === "function"`);
  check("TU the toast's Undo is wired", wired,
        `onclick=${run(`typeof $("toast-action").onclick`)}`);
  if (wired) { await run(`$("toast-action").onclick()`); await tick(); }
  check("TU pressing it spends exactly one undo",
        posts("/api/undo") === 1, `${posts("/api/undo")} POST(s)`);
  check("TU ...and the action log shrinks with it",
        run("nowActionLog.length") === 0 && run("nowActions") === 0,
        `log=${run("nowActionLog.length")} actions=${run("nowActions")}`);

  await fresh("spotify:track:tu2");
  await run(`nowCapture("IN2")`);
  await tick();
  check("TU a capture's toast offers one too",
        /id="toast-action"/.test(toastHtml()), JSON.stringify(toastHtml().slice(0, 140)));

  await fresh("spotify:track:tu3");
  await run(`nowUnfile("H1")`);
  await tick();
  check("TU an un-file's toast offers one too",
        /id="toast-action"/.test(toastHtml()), JSON.stringify(toastHtml().slice(0, 140)));

  // The guard. The six-second life of this toast is long enough for another
  // decision to land on the stack, and /api/undo only ever pops the top — so
  // a press that fired blind would take back the NEWER action. Staged by
  // pushing onto the log directly, which is exactly what the second action
  // would have done, without a second toast overwriting the button first.
  await fresh("spotify:track:tu4");
  await run(`nowAddToSubset("S1")`);
  await tick();
  run(`nowActionLog.push({ uri: "spotify:track:someone-else", kind: "subset" })`);
  resetLog();
  const guarded = run(`typeof $("toast-action").onclick === "function"`);
  if (guarded) { await run(`$("toast-action").onclick()`); await tick(); }
  check("TU a stale press undoes nothing rather than the newer action",
        posts("/api/undo") === 0 && run("nowActionLog.length") === 2,
        `${posts("/api/undo")} POST(s), log=${run("nowActionLog.length")}`);
  check("TU ...and says why instead of failing silently",
        /too late/.test($$("toast").textContent || ""),
        JSON.stringify($$("toast").textContent));

  // A filing keeps its undo in the strip, not in the toast — two undo
  // affordances for one action is one too many.
  await fresh("spotify:track:tu5");
  await run(`nowFile("H1")`);
  await tick();
  check("TU a filing's toast carries no undo — the strip has it",
        !/id="toast-action"/.test(toastHtml()), JSON.stringify(toastHtml().slice(0, 140)));

  run(`filedUris = {}; nowActions = 0; removedUri = null; nowActionLog = []; show("lists")`);
}

// ============================================================================
// AA — the card names neither artist nor album.
//
// They once had a line each in a panel the eye opened, which pushed the
// card down. Since 2026-10-19 the card carries no title, artist or album at
// all (an album name leaks the artist just as well), so the eye only
// unblurs the reasons and nothing moves.
// ============================================================================
{
  resetLog();
  setNow({
    playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
    track: { uri: "spotify:track:aa1", name: "Song", duration_ms: 200000,
             artists: [{ name: "Ar One" }, { name: "Ar Two" }],
             album: "The Album", sortable: true, image: null },
    context: { id: "IN1", name: "[In]", is_input: true },
    sitting: null, suggestions: [], homes: [], subset_targets: [],
    inputs: [{ id: "IN1", name: "[In]", has_track: true, set: "buffer", total: 3 }],
    homeless_id: null,
  });
  run(`show("now"); filedUris = {}; nowActions = 0; removedUri = null; nowActionLog = []; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  const html = () => $$("now-card").innerHTML;

  check("AA the card names neither the artists nor the album",
        !/Ar One|Ar Two|The Album/.test(html()) && html().includes('class="track-card'),
        JSON.stringify(html().slice(0, 200)));

  // A track with no album must not leave a stray dash or a visible gap.
  setNow({
    playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
    track: { uri: "spotify:track:aa2", name: "Song", duration_ms: 200000,
             artists: [{ name: "Ar One" }], sortable: true, image: null },
    context: { id: "IN1", name: "[In]", is_input: true },
    sitting: null, suggestions: [], homes: [], subset_targets: [],
    inputs: [{ id: "IN1", name: "[In]", has_track: true, set: "buffer", total: 3 }],
    homeless_id: null,
  });
  run(`pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  check("AA ...nor does a track with no album",
        !/Ar One/.test(html()) && html().includes('class="track-card'),
        JSON.stringify(html().slice(0, 200)));
  run(`show("lists")`);
}

// ============================================================================
// PW — "that is not the song". Deezer matches by text, so a preview is
// sometimes a remix or another track entirely. Marking one must name the
// exact RECORDING (the deezer_id), drop it from the medley in hand, and move
// on — leaving it in the list would walk Next straight back onto it.
// ============================================================================
{
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    routes["GET /api/playlist_preview/pw1"] = {
      clips: [{ name: "Song A", artist: "Artist A", url: "https://cdn/a.mp3",
                uri: "spotify:track:a", deezer_id: 11 },
              { name: "Song B", artist: "Artist B", url: "https://cdn/b.mp3",
                uri: "spotify:track:b", deezer_id: 22 }],
      next_offset: null, total: 12, tracks: [],
    };
    routes["POST /api/preview_reject"] = { ok: true, rejected: [11] };
    routes["POST /api/preview_resume"] = { ok: true };
    setNow({ status: 200, body: { playing: false, poll_after_ms: 999999 } });
    run(`nowState = { playing: true, is_playing: true, track: { uri: "spotify:track:x" }, homes: new Map() }`);

    const row = new El("pw-row");
    ctx.__pwrow = row;
    run(`previewHold.attach(__pwrow, "pw1", "Deep Cuts")`);
    row.onpointerdown({ clientX: 10, clientY: 10 });
    await new Promise((r) => setTimeout(r, 700));
    await tick();

    check("PW the player offers a way to say the clip is wrong",
          $$("preview-pop").innerHTML.includes('id="pv-wrong"'),
          JSON.stringify($$("preview-pop").innerHTML.slice(0, 200)));
    check("PW the first clip is playing before it is marked",
          AudioStub.made.at(-1)?.src === "https://cdn/a.mp3",
          `src=${AudioStub.made.at(-1)?.src}`);

    resetLog();
    const wired = run(`typeof $("pv-wrong").onclick === "function"`);
    check("PW the control is wired", wired, `onclick=${run(`typeof $("pv-wrong").onclick`)}`);
    if (wired) { await run(`$("pv-wrong").onclick()`); await tick(); }
    const sent = bodies("/api/preview_reject")[0];
    check("PW marking names the recording, not just the title",
          posts("/api/preview_reject") === 1 && sent?.deezer_id === 11
          && sent?.uri === "spotify:track:a",
          JSON.stringify(sent));
    check("PW ...and carries the names, so the file reads as something human",
          sent?.artist === "Artist A" && sent?.title === "Song A", JSON.stringify(sent));
    check("PW marking moves straight on to the next clip",
          AudioStub.made.at(-1)?.src === "https://cdn/b.mp3",
          `src=${AudioStub.made.at(-1)?.src}`);

    // The rejected clip must be gone from the medley in hand: Prev from the
    // clip after it would otherwise play the very thing just rejected.
    run(`$("pv-prev").onclick()`);
    await tick();
    check("PW ...and going back does not land on the rejected clip again",
          AudioStub.made.at(-1)?.src !== "https://cdn/a.mp3",
          `src=${AudioStub.made.at(-1)?.src}`);

    run("previewHold.stop()");
  } catch (e) {
    check("PW scenario ran without throwing", false, String(e));
  }
}

// ============================================================================
// AB — Add to… is two targets, because this app is used on a phone.
//
// The picker focused its filter unconditionally, so every route in threw the
// on-screen keyboard over the list you were trying to read. The wide button
// opens it to scroll; the magnifier beside it opens it ready to type. Both
// reach the same picker — the only difference is the keyboard.
// ============================================================================
{
  resetLog();
  setNow({
    playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
    track: { uri: "spotify:track:ab1", name: "Song", duration_ms: 200000,
             artists: [{ name: "Ar" }], sortable: true, image: null },
    context: { id: "IN1", name: "[In]", is_input: true },
    sitting: null, suggestions: [],
    homes: [{ id: "H1", name: "Home One", folder: "" }],
    subset_targets: [],
    inputs: [{ id: "IN1", name: "[In]", has_track: true, set: "buffer", total: 3 }],
    homeless_id: null,
  });
  run(`show("now"); filedUris = {}; nowActions = 0; removedUri = null; nowActionLog = []; pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  const html = () => $$("now-card").innerHTML;

  check("AB the row carries both targets",
        html().includes('id="btn-now-more"') && html().includes('id="btn-now-search"'),
        JSON.stringify((html().match(/id="btn-now-(more|search)"/g) || [])));

  // The stub records focus() calls, so "did the keyboard come up" is
  // directly observable rather than inferred.
  run(`$("picker-filter").focused = 0;
       $("picker-filter").focus = function () { this.focused++; };`);

  run(`$("btn-now-more").onclick()`);
  check("AB the wide button opens the picker",
        run(`$("picker").hidden`) === false, `hidden=${run(`$("picker").hidden`)}`);
  check("AB ...without raising the keyboard",
        run(`$("picker-filter").focused`) === 0,
        `focused=${run(`$("picker-filter").focused`)}`);
  check("AB ...and still shows the homes to scroll",
        $$("picker-list").children.some((c) => /Home One/.test(c.innerHTML)),
        JSON.stringify($$("picker-list").children.map((c) => c.innerHTML.slice(0, 40))));

  run(`closePicker(); $("btn-now-search").onclick()`);
  check("AB the magnifier opens the same picker",
        run(`$("picker").hidden`) === false, `hidden=${run(`$("picker").hidden`)}`);
  check("AB ...and does raise the keyboard",
        run(`$("picker-filter").focused`) === 1,
        `focused=${run(`$("picker-filter").focused`)}`);

  // Every other picker is unchanged: they focus as they always have.
  run(`closePicker(); $("btn-now-subset").onclick()`);
  check("AB the subset picker still focuses, unchanged",
        run(`$("picker-filter").focused`) === 2,
        `focused=${run(`$("picker-filter").focused`)}`);

  run(`closePicker(); show("lists")`);
}

// ============================================================================
// AN — a new song, and the question that actually fits it. Adrift is the app
// having established that this track is in no inbox and in no home, so "which
// of your homes" is the wrong question to lead with: the song has never been
// triaged at all. The card asks the one that applies — which inbox does this
// go in — and leaves the home list one tap below as the rarer answer.
//
// The fixture keeps a weak home suggestion in play throughout. That is what
// makes these checks falsifiable: without the branch the card would happily
// draw that row, so "no home row" is a real assertion rather than an artifact
// of an empty suggestions array.
// ============================================================================
{
  resetLog();
  const html = () => $$("now-card").innerHTML;
  const paint = async (over) => {
    setNow({
      status: 200,
      body: {
        playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
        track: { uri: "spotify:track:an1", name: "Song", duration_ms: 200000,
                 artists: [{ name: "Artist" }], sortable: true, image: null },
        context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
        suggestions: [], homes: [{ id: "H1", name: "Home", folder: "" }],
        subset_targets: [], subsets: [],
        inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer", total: 4 }],
        homeless_id: null,
        ...over,
      },
    });
    run(`show("now"); filedUris = {}; removedUri = null; nowActions = 0;
         nowActionLog = []; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };
  const weak = [{ playlist_id: "H1", pct: 12, reasons: ["thin"], already: false, weak: true }];
  // Adrift: playing outside any input, and in none of them. Three inboxes
  // across two sets, so ordering and set labelling are both observable.
  const adrift = {
    context: null,
    suggestions: weak,
    inputs: [{ id: "IN2", name: "[Deep]", has_track: false, set: "buffer", total: 9 },
             { id: "OT1", name: "<Old>", has_track: false, set: "other", total: 2 },
             { id: "IN1", name: "[Hazy]", has_track: false, set: "buffer", total: 4 }],
  };

  // The ordinary path is untouched — playing from an inbox still asks homes.
  await paint({ suggestions: weak });
  check("AN filing from an inbox still offers homes",
        html().includes('data-to="H1"') && !html().includes("data-cap"),
        `html=${html().slice(0, 200)}`);
  check("AN ...one to a row, because a ranked guess needs its reason read",
        !/cap-grid/.test(html()), `html=${html().slice(0, 200)}`);

  await paint(adrift);
  check("AN a new song is offered inboxes instead",
        html().includes('data-cap="IN1"') && html().includes('data-cap="IN2"') &&
        html().includes('data-cap="OT1"'), `html=${html().slice(0, 500)}`);
  check("AN ...and the home suggestion it would otherwise have drawn is gone",
        !html().includes('data-to="H1"'), `html=${html().slice(0, 500)}`);
  check("AN ...with the buffer set first, whatever order the server sent",
        html().indexOf('data-cap="IN2"') < html().indexOf('data-cap="OT1"') &&
        html().indexOf('data-cap="IN1"') < html().indexOf('data-cap="OT1"'),
        `html=${html().slice(0, 500)}`);
  check("AN ...and every inbox named",
        /\[Hazy\]/.test(html()) && /\[Deep\]/.test(html()) && /&lt;Old&gt;/.test(html()),
        `html=${html().slice(0, 500)}`);

  // The homes are still reachable, just demoted: this row is untouched code,
  // and the check exists to catch a branch that swallowed it.
  check("AN the home list stays one tap below",
        html().includes('id="btn-now-more"'), `html=${html().slice(0, 500)}`);

  // Two inboxes to a row. An inbox row is a name and a short sub-line, not a
  // ranked guess with a reason to read, so it does not need the width a home
  // suggestion does — and this is the one list where you want every option in
  // view at once instead of scrolling a ranking. The box keeps its height and
  // shows twice as many.
  check("AN the inbox list lays out two to a row",
        /class="sugg-scroll cap-grid"/.test(html()), `html=${html().slice(0, 500)}`);

  // The capture chip row is the same offer in miniature; with the main row
  // doing it, drawing it twice two centimetres apart is just noise. Nothing
  // is lost — adrift means no inbox holds the song, so its membership chips
  // are empty by definition.
  check("AN the capture chip row stands down while the main row does its job",
        !html().includes('id="btn-now-capture"'), `html=${html().slice(0, 500)}`);
  check("AN ...and the hint pointing at Add to… goes with it",
        !/No confident match/.test(html()), `html=${html().slice(0, 500)}`);

  // The banner stops claiming nothing is being filed — that was true when the
  // card was a dead end and is a lie the moment it offers a destination.
  check("AN the strip is up in the bar, and the card says nothing can't be done",
        !$$("adrift-strip").hidden && !/nothing you play here is being filed/.test(html()),
        `html=${html().slice(0, 500)}`);

  // The case the whole feature is for, and the one that forced the rows and
  // the banner apart. Discover Weekly is where new songs come FROM, so it must
  // get the inbox rows — and it must not get the banner, because putting it on
  // is a choice rather than playback drifting out of an inbox on its own.
  await paint({ ...adrift,
                context: { id: "PL9", name: "Discover Weekly", is_input: false } });
  check("AN a song met on Discover Weekly still gets its inboxes",
        html().includes('data-cap="IN1"') && html().includes('data-cap="IN2"'),
        `html=${html().slice(0, 500)}`);
  check("AN ...and is not scolded for being there",
        $$("adrift-strip").hidden, `strip=${$$("adrift-strip").innerHTML}`);
  check("AN ...and the home suggestion is still withheld",
        !html().includes('data-to="H1"'), `html=${html().slice(0, 500)}`);

  // A context with no name is the same case — that is how Discover Weekly
  // actually arrives, since it is absent from the cached listing the server
  // names contexts from. It must not fall back to the banner.
  await paint({ ...adrift, context: { id: "PL9", name: null, is_input: false } });
  check("AN an unnameable playlist behaves the same, not like autoplay",
        html().includes('data-cap="IN1"') && $$("adrift-strip").hidden,
        `html=${html().slice(0, 500)}`);

  // The act itself: an ADD, never a move. A song playing out in the wild has
  // no input to be taken out of, and from_id must stay null — the same shape
  // nowCapture has always sent (see CR). Above all it must not sweep: sweeping
  // is how a FILING settles a song across every inbox, and doing it here would
  // empty the inboxes on the way to putting the song into one.
  //
  // Called directly rather than through the row, because the stub's
  // querySelectorAll only models "button" (see O1) — the rows' wiring is out
  // of reach here exactly as the home rows' is. What the markup carries is
  // pinned above; this pins what the tap does.
  resetLog();
  await run(`nowCapture("IN2")`);
  await tick();
  const cap = bodies("/api/act").slice(-1)[0];
  check("AN tapping one captures the song into that inbox — an add, not a move",
        cap && cap.to_id === "IN2" && cap.from_id === null && cap.action === "move" &&
        !cap.sweep_inputs, JSON.stringify(cap));

  // Homeless is a verdict reached by filing, not an inbox you park a stranger
  // in, and openCapturePicker has always excluded it. The rows agree.
  await paint({ ...adrift, homeless_id: "IN2" });
  check("AN the Homeless buffer is not offered as a destination",
        !html().includes('data-cap="IN2"') && html().includes('data-cap="IN1"'),
        `html=${html().slice(0, 500)}`);

  // An inbox already holding the song would be a no-op offer — and its
  // presence is what makes the song not adrift in the first place.
  await paint({ ...adrift, suggestions: weak,
                inputs: [{ id: "IN1", name: "[Hazy]", has_track: false, set: "buffer", total: 4 }] });
  check("AN one inbox left is still a row, not a special case",
        html().includes('data-cap="IN1"'), `html=${html().slice(0, 500)}`);

  // Phase 1. The rows do NOT wait for the suggestion phase, unlike the
  // banner — and the difference is what each one needs to know. The banner
  // needs membership, which only phase 2 answers. The rows need the list of
  // inboxes, which is not per-track at all and rides across the phase
  // boundary already (see pollNow's carry-over). "Am I playing out of an
  // inbox" is answered by the light payload's own context.is_input, so a new
  // song can be offered its inboxes the moment the card goes up.
  run(`nowState.suggPending = true; renderNow()`);
  check("AN a new song gets its inboxes in phase 1, without waiting",
        html().includes('data-cap="IN1"'), `html=${html().slice(0, 300)}`);
  check("AN ...while the banner still waits for membership",
        !html().includes("np-adrift"), `html=${html().slice(0, 300)}`);

  // The other half of that bet: playing OUT of an inbox is the ordinary
  // filing case, and it must not flash inbox rows on the way to the homes it
  // is actually going to show.
  await paint({ suggestions: weak });
  run(`nowState.suggPending = true; renderNow()`);
  check("AN filing from an inbox still waits, and says so",
        !html().includes("data-cap") && /finding a home/.test(html()),
        `html=${html().slice(0, 300)}`);

  // has_track carried into phase 1 describes the PREVIOUS track — the light
  // payload sends no inputs at all while playing — so it is not evidence
  // about this song and must not hide an inbox. Phase 2 brings the real
  // flags and the filter starts applying again.
  await paint({ ...adrift,
                inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer", total: 4 },
                         { id: "IN2", name: "[Deep]", has_track: false, set: "buffer", total: 9 }] });
  run(`nowState.suggPending = true; renderNow()`);
  check("AN phase 1 ignores stale membership rather than hiding an inbox",
        html().includes('data-cap="IN1"') && html().includes('data-cap="IN2"'),
        `html=${html().slice(0, 300)}`);

  run(`show("lists")`);
}

// ============================================================================
// NF — creating a playlist picks a folder, and the filing job is visible
// ============================================================================
// The Web API has no folders, so a created playlist lands at the top level
// and a desktop-client move (about a minute, zero API calls) files it. Three
// things have to hold in the UI: the dropdown can only offer folders the
// server actually knows, the choice per role is remembered, and the wait is
// never silent — a filing that fails leaves a real playlist at the top
// level, and saying nothing would look like the playlist was lost.
{
  resetLog();
  routes["GET /api/playlists"] = { status: 200, body: {
    playlists: [], fetched_at: 0, sitting_orphans: [],
    folder_paths: ["ROOT / Rock", "THE BOMB"],
    create_folders: { home: "ROOT / Rock", input: "THE BOMB" } } };
  await run("loadLists()");
  await tick();

  const opts = () => $$("new-pl-folder").innerHTML;
  check("NF the folder dropdown offers every folder the server knows",
        /ROOT \/ Rock/.test(opts()) && /THE BOMB/.test(opts()), JSON.stringify(opts()));
  check("NF ...and a top-level choice, which is what no folder means",
        /value=""/.test(opts()), JSON.stringify(opts()));
  check("NF the home default is pre-selected",
        $$("new-pl-folder").value === "ROOT / Rock", `value=${$$("new-pl-folder").value}`);

  $$("new-pl-role").value = "input";
  await run(`$("new-pl-role").onchange()`);
  check("NF switching to buffer pre-selects the buffer's own default",
        $$("new-pl-folder").value === "THE BOMB", `value=${$$("new-pl-folder").value}`);

  // Creating sends the chosen folder, and the wait is announced.
  routes["POST /api/playlists/create"] = { status: 200, body: {
    playlist: { id: "N1", name: "[Hazy]", role: "input", folder: "THE BOMB",
                total: 0, editable: true, owner: "me" },
    note: null, filing: true } };
  routes["GET /api/playlists/filing/N1"] = { status: 200, body: {
    state: "filing", folder: "THE BOMB", error: null } };
  resetLog();
  $$("new-home-name").value = "[Hazy]";
  await run(`$("btn-new-home").onclick()`);
  await tick();
  const sent = bodies("/api/playlists/create")[0];
  check("NF the create request carries the role and the chosen folder",
        sent && sent.role === "input" && sent.folder === "THE BOMB", JSON.stringify(sent));
  check("NF the wait is visible while the client is being driven",
        $$("new-pl-filing").hidden === false && /THE BOMB/.test($$("new-pl-filing").textContent),
        JSON.stringify($$("new-pl-filing").textContent));

  // A landed move fills the folder in; the row is already in the list.
  routes["GET /api/playlists/filing/N1"] = { status: 200, body: {
    state: "filed", folder: "THE BOMB", error: null } };
  await run(`pollFiling("N1", "[Hazy]")`);
  await tick();
  check("NF a landed filing says where it went",
        /THE BOMB/.test($$("toast").textContent) && $$("new-pl-filing").hidden === true,
        JSON.stringify($$("toast").textContent));
  check("NF ...and the row knows its folder without a Refresh",
        run(`(playlistData.find((p) => p.id === "N1") || {}).folder`) === "THE BOMB",
        run(`JSON.stringify(playlistData)`));

  // A failed move must name the playlist AND say it still exists.
  routes["GET /api/playlists/filing/N1"] = { status: 200, body: {
    state: "failed", folder: null, error: "the client is not installed" } };
  await run(`pollFiling("N1", "[Hazy]")`);
  await tick();
  check("NF a failed filing says the playlist stayed at the top level",
        /top level/.test($$("toast").textContent), JSON.stringify($$("toast").textContent));
  check("NF ...and reports why",
        /not installed/.test($$("toast").textContent), JSON.stringify($$("toast").textContent));

  // Top level is a real choice, not the absence of one: it must be sent as
  // such, or the server would fall back to the role's stored default and
  // file the playlist somewhere the user just chose against.
  $$("new-pl-role").value = "home";
  await run(`$("new-pl-role").onchange()`);
  $$("new-pl-folder").value = "";
  routes["POST /api/playlists/create"] = { status: 200, body: {
    playlist: { id: "N2", name: "Late Night", role: "home", folder: null,
                total: 0, editable: true, owner: "me" },
    note: null, filing: false } };
  resetLog();
  $$("new-home-name").value = "Late Night";
  await run(`$("btn-new-home").onclick()`);
  await tick();
  const sent2 = bodies("/api/playlists/create")[0];
  check("NF choosing the top level sends null, not an omitted field",
        sent2 && "folder" in sent2 && sent2.folder === null, JSON.stringify(sent2));
  check("NF nothing is filed when there is no folder to file into",
        gets("/api/playlists/filing/N2") === 0 && $$("new-pl-filing").hidden === true,
        `${gets("/api/playlists/filing/N2")} status GET(s)`);

  // A home created from the Now card's picker is the same act as one created
  // in the Lists view, so it gets the same folder — but it must not RESTATE
  // the choice: omitting the field is how the client says "the stored
  // default", and sending null here would file it at the top level instead.
  routes["POST /api/playlists/create"] = { status: 200, body: {
    playlist: { id: "N3", name: "From the card", role: "home", folder: "ROOT / Rock",
                total: 0, editable: true, owner: "me" },
    note: null, filing: true } };
  routes["GET /api/playlists/filing/N3"] = { status: 200, body: {
    state: "filed", folder: "ROOT / Rock", error: null } };
  resetLog();
  await run(`nowCreateAndFile("From the card")`);
  await tick();
  const sent3 = bodies("/api/playlists/create")[0];
  check("NF a home created from the Now card lets the server apply the default",
        sent3 && !("folder" in sent3), JSON.stringify(sent3));
  check("NF ...and its filing is watched too, not left silent",
        gets("/api/playlists/filing/N3") >= 1, `${gets("/api/playlists/filing/N3")} status GET(s)`);
}

// ============================================================================
// QA — the Now card's one-tap destinations (Star, Explore artist, rotation)
// ============================================================================
// A quick add is a subset add with the picker skipped, so it must go through
// the same /api/act shape (from_id null — a selection is not a filing) and
// leave the undo stack usable. Two things are specific to these buttons: a
// song already in the target must not be added a second time (Spotify stores
// duplicates without complaint), and the explore button's playlist does not
// exist until the first press resolves it.
{
  resetLog();
  const QUICK = [
    { key: "star", label: "Star", playlist_id: "star1", name: "🐾 topp", total: 344, has_track: false },
    { key: "explore", label: "Explore artist", playlist_id: null, name: null, total: null, has_track: null },
    { key: "rotation", label: "Add to rotation", playlist_id: "rot1", name: "{project 17}", total: 12, has_track: true },
  ];
  const track = { uri: "spotify:track:qa", name: "Song", sortable: true,
                  artists: [{ id: "ar9", name: "Nine" }], duration_ms: 200000 };
  setNow({ playing: true, is_playing: true, track, progress_ms: 1000,
           poll_after_ms: 999999, suggestions: [], homes: [], inputs: [],
           quick_adds: QUICK });
  run(`show("now")`);
  await run("pollNow(true)");
  await tick();

  const card = () => $$("now-card").innerHTML;
  check("QA the card draws one button per configured destination",
        /id="qa-star"/.test(card()) && /id="qa-explore"/.test(card()) && /id="qa-rotation"/.test(card()),
        card().slice(0, 400));
  check("QA each button wears its own label, not the playlist's name",
        /Explore artist/.test(card()) && /Add to rotation/.test(card()), card().slice(0, 400));
  check("QA a destination the song is already in says so",
        /id="qa-rotation"[^>]*class="[^"]*qa-on/.test(card()) ||
        /class="[^"]*qa-on[^"]*"[^>]*id="qa-rotation"/.test(card()), card().slice(0, 600));

  // The ordinary press: one paid add, shaped exactly like a subset add.
  routes["POST /api/act"] = { status: 200, body: { ok: true } };
  resetLog();
  await run(`$("qa-star").onclick()`);
  await tick();
  const act = bodies("/api/act")[0];
  check("QA pressing Star adds to its playlist and nothing else",
        posts("/api/act") === 1 && act.to_id === "star1" && act.uri === track.uri,
        JSON.stringify(act));
  check("QA ...and sends from_id null, because a selection is not a filing",
        act.from_id === null, JSON.stringify(act));
  check("QA ...and offers an undo, like every other add",
        /Undo/.test($$("toast").innerHTML), JSON.stringify($$("toast").innerHTML));

  // Already in it: pressing again must cost nothing, or the 344-track list
  // quietly grows a second copy of the song.
  resetLog();
  await run(`$("qa-rotation").onclick()`);
  await tick();
  check("QA a destination that already holds the song spends no call",
        posts("/api/act") === 0, `${posts("/api/act")} POST(s)`);
  check("QA ...and says why nothing happened",
        /already/.test($$("toast").textContent), JSON.stringify($$("toast").textContent));

  // Explore: resolve (creating the playlist server-side) and then add.
  routes["POST /api/explore"] = { status: 200, body: {
    playlist_id: "utf1", name: "Utforsk", created: true } };
  resetLog();
  await run(`$("qa-explore").onclick()`);
  await tick();
  check("QA Explore artist resolves its playlist before adding",
        posts("/api/explore") === 1 && posts("/api/act") === 1,
        `${posts("/api/explore")} explore, ${posts("/api/act")} act`);
  check("QA ...adds to the playlist the server just named",
        bodies("/api/act")[0].to_id === "utf1", JSON.stringify(bodies("/api/act")[0]));
  check("QA ...and tells the server which artist to remember",
        bodies("/api/explore")[0].artist_id === "ar9", JSON.stringify(bodies("/api/explore")[0]));

  check("QA ...and refuses to add the same song twice once it has landed",
        run(`nowState.quickAdds.find((q) => q.key === "explore").has_track`) === true,
        run(`JSON.stringify(nowState.quickAdds)`));

  // The id it learned is kept for the rest of the card's life, so pressing
  // for the next song is one call and not two.
  run(`nowState.quickAdds.find((q) => q.key === "explore").has_track = false`);
  resetLog();
  await run(`$("qa-explore").onclick()`);
  await tick();
  check("QA a later Explore press resolves nothing and just adds",
        posts("/api/explore") === 0 && posts("/api/act") === 1,
        `${posts("/api/explore")} explore, ${posts("/api/act")} act`);
  check("QA ...to the playlist it learned the first time",
        bodies("/api/act")[0].to_id === "utf1", JSON.stringify(bodies("/api/act")[0]));

  // Phase 1 of the two-phase card carries the buttons, for the same reason it
  // carries the inbox list: they answer "where can this go", which changes
  // with the config and not with the song.
  run(`nowState.suggPending = true; renderNow()`);
  check("QA the buttons survive the light phase rather than flashing away",
        /id="qa-star"/.test(card()), card().slice(0, 400));

  run(`show("lists")`);
}

// ============================================================================
// NC — the card's shape: less height up top, the filing controls out of the
// scroll box, and the inbox row reduced to what it reports
// ============================================================================
// This app is used on a phone, so vertical space is the scarce resource and
// "where is the button" matters more than "is the button pretty". Three
// structural facts are pinned here because CSS cannot restore them if the
// markup loses them: the cover no longer stacks above the title, the two
// filing controls are OUTSIDE the scrolling list (they were the list's last
// row, which meant scrolling to reach them), and the inbox row no longer
// carries a button — only the chips that say where the song already is.
{
  resetLog();
  const paint = async (over) => {
    setNow({ playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:nc", name: "Song", duration_ms: 200000,
               artists: [{ id: "a1", name: "Artist" }], sortable: true, image: null,
               album: "Album" },
      context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 80, reasons: [], already: false }],
      homes: [{ id: "H1", name: "Home", folder: "" }],
      subset_targets: [], subsets: [],
      inputs: [{ id: "IN1", name: "[Hazy]", has_track: true, set: "buffer" },
               { id: "NH1", name: "[Homeless]", has_track: false, set: "buffer" }],
      homeless_id: "NH1",
      quick_adds: [{ key: "star", label: "Star", playlist_id: "P1", name: "topp",
                     total: 3, has_track: false }],
      ...over });
    run(`show("now"); filedUris = {}; nowActions = 0; nowActionLog = [];
         removedUri = null; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };
  const html = () => $$("now-card").innerHTML;
  await paint({});

  // The card names nothing — no title, artist, album or cover — so pressing
  // the eye (which only unblurs the reasons) moves nothing on screen.
  check("NC the card carries no title, artist, album or cover",
        !/t-reveal|t-name|t-artist|t-album/.test(html()) &&
        !html().includes('class="art"') && !html().includes("<img"), html().slice(0, 400));

  // The filing controls: out of the scroll box.
  const scroll = () => {
    const at = html().indexOf('class="sugg-scroll');
    return html().slice(at, html().indexOf("</div>", html().lastIndexOf('class="sugg', at + 200)));
  };
  check("NC Add to home… is not inside the scrolling list",
        html().includes('id="btn-now-more"') && !scroll().includes('id="btn-now-more"'),
        `in-scroll=${scroll().includes('id="btn-now-more"')}`);
  check("NC Homeless is not inside it either",
        !scroll().includes('id="btn-now-homeless"'),
        `in-scroll=${scroll().includes('id="btn-now-homeless"')}`);
  check("NC the reach-past-the-guesses button says what it reaches",
        /Add to home…/.test(html()), html().slice(0, 600));
  check("NC search sits to the left of it",
        html().indexOf('id="btn-now-search"') < html().indexOf('id="btn-now-more"'),
        `search=${html().indexOf('id="btn-now-search"')} more=${html().indexOf('id="btn-now-more"')}`);

  // The inbox row: a report, not a control.
  check("NC the inbox row keeps the chips that say where the song is",
        /data-in="IN1"/.test(html()), html().slice(-400));
  check("NC ...and no longer carries a capture button",
        !/id="btn-now-capture"/.test(html()), html().slice(-400));

  run(`show("lists")`);
}

// ============================================================================
// SK — the wait for suggestions is drawn, not written
// ============================================================================
// Phase 2 takes as long as a profile build and a Last.fm lookup take, and a
// single pulsing line left the card's lower half visibly empty meanwhile —
// which reads as "nothing is coming" rather than "not yet". Placeholder
// tiles in the rows' own shape say how much is coming and where it will
// land, and the box stops changing height when it arrives.
//
// The sentence stays in the markup for a screen reader: tiles announce
// nothing, and "finding a home…" is the only thing here that says what the
// wait is for.
{
  resetLog();
  const html = () => $$("now-card").innerHTML;
  setNow({ playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
    track: { uri: "spotify:track:sk", name: "Song", duration_ms: 200000,
             artists: [{ name: "Artist" }], sortable: true, image: null },
    context: { id: "IN1", name: "[Hazy]", is_input: true }, sitting: null,
    suggestions: [], homes: [], inputs: [], subset_targets: [], homeless_id: null });
  run(`show("now"); pollNow(true)`);
  await tick();
  run("stopNowPolling()");
  // The card as it stands between the two phases: a track, no verdict yet.
  run(`nowState.suggPending = true; nowState.suggestions = []; renderNow()`);

  check("SK the wait draws placeholder tiles, not one line of text",
        (html().match(/class="sugg-skel"/g) || []).length >= 3,
        `tiles=${(html().match(/class="sugg-skel"/g) || []).length}`);
  check("SK a tile carries the rows' two bars, so the shape is the rows' shape",
        /class="skel-bar skel-name"/.test(html()) && /class="skel-bar skel-why"/.test(html()),
        html().slice(html().indexOf("sugg-skel") - 40, html().indexOf("sugg-skel") + 260));
  check("SK the tiles sit in the list's own box, where the rows will appear",
        html().indexOf('class="sugg-scroll') < html().indexOf("sugg-skel"),
        `box=${html().indexOf('class="sugg-scroll')} skel=${html().indexOf("sugg-skel")}`);
  check("SK the wait is still announced for a screen reader",
        /finding a home…/.test(html()) && /class="[^"]*sr-only/.test(html()),
        html().slice(html().indexOf("finding a home") - 120, html().indexOf("finding a home") + 40));
  check("SK a tile is not pressable — there is nothing behind it yet",
        !/<button[^>]*class="sugg-skel"/.test(html()), "");
  // Nothing may move when the answer lands, so a tile is exactly as tall as
  // the row that replaces it — measured from the last real row this browser
  // drew (capSuggScroll), not from a constant. Under the stub DOM nothing
  // has layout, so the fallback is what appears here.
  check("SK a tile is sized from a measured row, not from the text it hides",
        /min-height:75px/.test(html()), html().slice(html().indexOf("sugg-skel") - 40, 200));
  // The one variable-content line above the box. It says something only when
  // the guesses come back weak or empty — and takes the same height either
  // way, or the box drops the moment the answer lands.
  check("SK the line above the box is reserved while the guesses are still out",
        /class="hint sugg-lead"/.test(html()), html().slice(0, 400));

  // And when the answer lands the tiles go, rather than sitting under it.
  run(`nowState.suggPending = false;
       nowState.suggestions = [{ playlist_id: "H1", pct: 70, reasons: ["x"], already: false }];
       nowState.homes = new Map([["H1", { id: "H1", name: "Home", folder: "" }]]);
       renderNow()`);
  check("SK the tiles are gone once the guesses land",
        !/sugg-skel/.test(html()) && /data-to="H1"/.test(html()), html().slice(0, 300));
  check("SK ...and the reserved line is still there, holding the same room",
        /class="hint sugg-lead"/.test(html()), html().slice(0, 300));

  run(`show("lists")`);
}

// ============================================================================
// MB — Move to buffer…: the song belongs in a different inbox. Reachable from
// any song, beside Homeless; out of the inbox you are playing when there is
// one (a filing, swept like Homeless), a plain add when there is not; and
// the picker makes a new buffer, bracketed so it lands in the buffer set.
// ============================================================================
{
  const html = () => $$("now-card").innerHTML;
  const paint = async (over) => {
    setNow({
      playing: true, is_playing: true, progress_ms: 1000, poll_after_ms: 999999,
      track: { uri: "spotify:track:mb1", name: "Song", duration_ms: 200000,
               artists: [{ name: "Artist" }], sortable: true, image: null },
      context: { id: "IN1", name: "[One]", is_input: true }, sitting: null,
      suggestions: [{ playlist_id: "H1", pct: 80, reasons: [], already: false }],
      subsets: [], subset_targets: [],
      homes: [{ id: "H1", name: "Home One", folder: "" }],
      inputs: [{ id: "IN1", name: "[One]", has_track: true, set: "buffer", total: 9 },
               { id: "IN2", name: "[Two]", has_track: false, set: "buffer", total: 3 },
               { id: "IN3", name: "<Old>", has_track: false, set: "other", total: 5 },
               { id: "NH1", name: "[Homeless]", has_track: false, set: "buffer" },
               { id: "liked", name: "Liked Songs", has_track: false }],
      homeless_id: "NH1",
      ...over,
    });
    run(`show("now"); filedUris = {}; removedUri = null; nowActions = 0;
         nowActionLog = []; pollNow(true)`);
    await tick();
    run("stopNowPolling()");
  };
  await paint({});
  check("MB the card offers Move to buffer… beside Homeless",
        /class="homeless-row">[\s\S]*btn-now-homeless[\s\S]*id="btn-now-buffer"/.test(html()),
        html().slice(html().indexOf("homeless-row") - 20, 400));

  run(`$("btn-now-buffer").onclick()`);
  const names = () => $$("picker-list").children.map((c) => c.innerHTML);
  check("MB the picker lists the other inboxes, any set",
        names().some((h) => /\[Two\]/.test(h)) && names().some((h) => /&lt;Old&gt;/.test(h)),
        JSON.stringify(names().map((h) => h.slice(0, 50))));
  check("MB ...but not the one playing, Homeless, or Liked Songs",
        !names().some((h) => /\[One\]|\[Homeless\]|Liked Songs/.test(h)),
        JSON.stringify(names().map((h) => h.slice(0, 50))));
  check("MB ...and always offers to make a new buffer",
        /Create a new buffer/.test(names().slice(-1)[0] || ""), JSON.stringify(names().slice(-1)));

  resetLog();
  routes["POST /api/act"] = { status: 200, body: {} };
  await $$("picker-list").children.find((c) => /\[Two\]/.test(c.innerHTML)).onclick();
  await tick(); await tick();   // the row's onclick does not return its promise
  const mv = bodies("/api/act").slice(-1)[0];
  check("MB playing from an inbox, it MOVES: out of it, into the other, swept",
        mv && mv.from_id === "IN1" && mv.to_id === "IN2" && mv.sweep_inputs === true,
        JSON.stringify(mv));
  check("MB ...and the card lands filed to it",
        /filed to <b>\[Two\]<\/b>/.test(html()), html().slice(0, 300));

  // Outside the inboxes there is nothing to move out of: an add, and the
  // card stays live for its home.
  await paint({ context: { id: "PL9", name: "Discover Weekly", is_input: false } });
  run(`$("btn-now-buffer").onclick()`);
  resetLog();
  await $$("picker-list").children.find((c) => /\[Two\]/.test(c.innerHTML)).onclick();
  await tick();
  const add = bodies("/api/act").slice(-1)[0];
  check("MB playing from elsewhere, it adds: no from_id, no sweep",
        add && add.from_id === null && add.to_id === "IN2" && !add.sweep_inputs,
        JSON.stringify(add));
  check("MB ...and the card is not marked filed", !/filed to/.test(html()), html().slice(0, 300));

  // A new buffer, bracketed on the way in.
  await paint({});
  run(`$("btn-now-buffer").onclick()`);
  run(`$("picker-filter").value = "late night"; $("picker-filter").oninput({ target: { value: "late night" } })`);
  const mk = $$("picker-list").children.slice(-1)[0];
  check("MB typing a name offers that buffer, bracketed",
        /Create buffer “\[late night\]”/.test(mk?.innerHTML || ""), JSON.stringify(mk?.innerHTML));
  check("MB ...priced create + add + remove from the inbox playing",
        /3 calls/.test(mk?.innerHTML || ""), JSON.stringify(mk?.innerHTML));
  routes["POST /api/playlists/create"] = { status: 200, body: {
    playlist: { id: "IN9", name: "[late night]", role: "input", total: 0, folder: null } } };
  resetLog();
  run(`$("picker-filter").value = "late night"`);
  await mk.onclick();
  await tick(); await tick(); await tick();
  const cr = bodies("/api/playlists/create").slice(-1)[0];
  check("MB creating sends the bracketed name as an input, in the role's default folder",
        cr && cr.name === "[late night]" && cr.role === "input" && cr.folder === undefined,
        JSON.stringify(cr));
  const mv2 = bodies("/api/act").slice(-1)[0];
  check("MB ...and then moves the song into it",
        mv2 && mv2.from_id === "IN1" && mv2.to_id === "IN9", JSON.stringify(mv2));
  check("MB an already-bracketed name is left as typed",
        run(`bufferName("[x]")`) === "[x]" && run(`bufferName(" y ")`) === "[y]", "");

  run(`show("lists")`);
}

// ============================================================================
// SR — the Subset chip renames: 🐾 on, the leading emoji off, one call each.
{
  routes["GET /api/playlists"] = { status: 200, body: {
    playlists: [{ id: "PS1", name: "tabletop", role: null, editable: true, total: 3,
                  subset_eligible: true, folder: null, split: null, hints: "" }],
    fetched_at: 1, sitting_orphans: [] } };
  routes["POST /api/playlists/PS1/subset"] = { status: 200, body:
    { playlist_id: "PS1", name: "\u{1F43E} tabletop", role: "subset" } };
  try {
    run(`show("lists")`);
    await run(`loadLists()`);
    await tick();
    const row = $$("playlists").children.find((c) => /tabletop/.test(c.innerHTML));
    const chip = row?.querySelectorAll("button")[2];
    resetLog();
    await chip.onclick();
    await tick();
    const b = bodies("/api/playlists/PS1/subset").slice(-1)[0];
    check("SR tapping Subset posts on:true for that playlist", b && b.on === true, JSON.stringify(b));
    check("SR ...and the row shows the new name",
          row.querySelector(".name").textContent === "\u{1F43E} tabletop",
          row.querySelector(".name").textContent);
    check("SR ...and the chip is on", run(`roles["PS1"]`) === "subset", String(run(`roles["PS1"]`)));
  } finally {
    run(`roles = {}`);
  }
}

// SF — a refused rename leaves the row exactly as it was, chip usable again.
{
  routes["GET /api/playlists"] = { status: 200, body: {
    playlists: [{ id: "PS2", name: "plainlist", role: null, editable: true, total: 3,
                  subset_eligible: true, folder: null, split: null, hints: "" }],
    fetched_at: 1, sitting_orphans: [] } };
  routes["POST /api/playlists/PS2/subset"] = { status: 409, body: { detail: "refused" } };
  try {
    run(`show("lists")`);
    await run(`loadLists()`);
    await tick();
    const row = $$("playlists").children.find((c) => /plainlist/.test(c.innerHTML));
    const chip = row?.querySelectorAll("button")[2];
    resetLog();
    await chip.onclick();
    await tick();
    check("SF a refused mark still posted once", bodies("/api/playlists/PS2/subset").length === 1,
          String(bodies("/api/playlists/PS2/subset").length));
    check("SF ...the chip is enabled again", chip.disabled === false, String(chip.disabled));
    check("SF ...the role is unchanged", run(`roles["PS2"]`) === null, String(run(`roles["PS2"]`)));
    check("SF ...and so is the row's name",
          row.querySelector(".name").textContent !== "\u{1F43E} plainlist" &&
          run(`playlistData.find((p) => p.id === "PS2").name`) === "plainlist",
          run(`playlistData.find((p) => p.id === "PS2").name`));
  } finally {
    delete routes["POST /api/playlists/PS2/subset"];
    run(`roles = {}`);
  }
}

// AR — an archived row offers only the Subset chip: the way back from 🗄️.
{
  routes["GET /api/playlists"] = { status: 200, body: {
    playlists: [{ id: "PA1", name: "\u{1F5C4}\uFE0F old best", role: null, archived: true,
                  editable: true, total: 3, subset_eligible: true, folder: null, split: null,
                  hints: "" }],
    fetched_at: 1, sitting_orphans: [] } };
  routes["POST /api/playlists/PA1/subset"] = { status: 200, body:
    { playlist_id: "PA1", name: "\u{1F43E} old best", role: "subset" } };
  try {
    run(`show("lists")`);
    await run(`loadLists()`);
    await tick();
    const row = $$("playlists").children.find((c) => /old best/.test(c.innerHTML));
    const [bIn, bHome, bSubset] = row?.querySelectorAll("button") || [];
    check("AR an archived row hides Buffer and Home", bIn?.hidden === true && bHome?.hidden === true,
          `buffer hidden=${bIn?.hidden} home hidden=${bHome?.hidden}`);
    check("AR ...and shows Subset", bSubset?.hidden === false, String(bSubset?.hidden));
    resetLog();
    await bSubset.onclick();
    await tick();
    const b = bodies("/api/playlists/PA1/subset").slice(-1)[0];
    check("AR tapping Subset marks it (un-archives)", b && b.on === true, JSON.stringify(b));
    check("AR ...after which the row is an ordinary subset row",
          run(`roles["PA1"]`) === "subset" && bIn.hidden === false && bHome.hidden === false &&
          row.querySelector(".name").textContent === "\u{1F43E} old best",
          `role=${run(`roles["PA1"]`)} buffer hidden=${bIn.hidden}`);
  } finally {
    delete routes["POST /api/playlists/PA1/subset"];
    run(`roles = {}`);
  }
}

// ============================================================================
// FH — a home created from the Now card is filed into the folder you pick.
// ============================================================================
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
                     { path: "ROOT / Dusk", blocked: null },
                     { path: "ROOT / Hominin", blocked: "the client's folder search would also offer ROOT / Hominin / OLD — file this one by hand" }],
      home_folder_default: "ROOT / Hazy",
      ...over,
    });
    run(`show("now"); filedUris = {}; removedUri = null; nowActions = 0;
         nowActionLog = []; pollNow(true)`);
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
          chips?.dataset.chosen === "ROOT / Hazy",
          chips?.innerHTML);
    check("FH ...and a folder the mover can't reach disabled",
          /<button class="chip" disabled[^>]*>ROOT \/ Hominin</.test(chips?.innerHTML || ""), chips?.innerHTML);
    // Phones never show a `title` tooltip: the reason has to be on screen.
    const visible = (chips?.innerHTML || "").replace(/<[^>]*>/g, " ").replace(/&#39;/g, "'");
    check("FH ...with why, as visible text naming the folder",
          /ROOT \/ Hominin[^]*client's folder search would also offer/.test(visible), visible);
    check("FH a Top level chip is offered, and is not selected while a folder is",
          /<button class="chip" data-path="">Top level</.test(chips?.innerHTML || ""), chips?.innerHTML);
    routes["POST /api/playlists/create"] = { status: 200, body: {
      playlist: { id: "H9", name: "Night drive", role: "home", total: 0, folder: "ROOT / Hazy" }, filing: true } };
    routes["POST /api/act"] = { status: 200, body: {} };
    // Tapping another chip moves the selection, and the create sends it.
    const dusk = chips.querySelectorAll("button[data-path]").find((c) => c.dataset.path === "ROOT / Dusk");
    dusk.onclick({ stopPropagation() {} });
    check("FH tapping a chip moves the selection",
          chips.dataset.chosen === "ROOT / Dusk" && /class="chip on" data-path="ROOT \/ Dusk"/.test(chips.innerHTML),
          chips.innerHTML);
    // The filter repaints the list; the choice must survive it.
    run(`$("picker-filter").value = "Night drives"; $("picker-filter").oninput({ target: { value: "night drives" } })`);
    const chips2 = kids().find((c) => c.className === "picker-folders");
    check("FH typing in the filter keeps the chosen folder",
          chips2?.dataset.chosen === "ROOT / Dusk" && /class="chip on" data-path="ROOT \/ Dusk"/.test(chips2?.innerHTML || ""),
          chips2?.innerHTML);
    run(`$("picker-filter").value = "Night drive"; $("picker-filter").oninput({ target: { value: "night drive" } })`);
    resetLog();
    const create = kids().find((c) => /Create home/.test(c.innerHTML));
    await create.onclick();
    await tick(); await tick();
    const cr = bodies("/api/playlists/create").slice(-1)[0];
    check("FH creating sends the chosen folder", cr && cr.folder === "ROOT / Dusk" && cr.role === "home",
          JSON.stringify(cr));

    // Tapping the selected chip again is the opt-out: top level, sent as null.
    // A new name: "Night drive" is a home now (the create above added it),
    // and a filter that matches a home draws no create row.
    run(`openPicker(nowState.homes, nowFile, nowCreateAndFile, null)`);
    run(`$("picker-filter").value = "Moon walk"; $("picker-filter").oninput({ target: { value: "moon walk" } })`);
    const c3 = kids().find((c) => c.className === "picker-folders");
    const hazy = c3?.querySelectorAll("button[data-path]").find((c) => c.dataset.path === "ROOT / Hazy");
    hazy?.onclick({ stopPropagation() {} });
    const c3b = kids().find((c) => c.className === "picker-folders");
    check("FH tapping the selected chip selects Top level",
          /<button class="chip on" data-path="">Top level</.test(c3b?.innerHTML || ""), c3b?.innerHTML);
    resetLog();
    await kids().find((c) => /Create home/.test(c.innerHTML))?.onclick();
    await tick(); await tick();
    const cr3 = bodies("/api/playlists/create").slice(-1)[0];
    check("FH ...and creating then sends folder: null (the top level, not the stored default)",
          cr3 && "folder" in cr3 && cr3.folder === null, JSON.stringify(cr3));
  } finally {
    run(`closePicker()`);
  }

  // With no valid pre-selection, Top level is what is selected and sent —
  // never the server's stored default, which nothing on screen shows.
  await paint({ home_folder_default: null });
  try {
    run(`openPicker(nowState.homes, nowFile, nowCreateAndFile, null)`);
    run(`$("picker-filter").value = "Night drive"; $("picker-filter").oninput({ target: { value: "night drive" } })`);
    const kids = () => $$("picker-list").children;
    const c4 = kids().find((c) => c.className === "picker-folders");
    check("FH with no default, Top level is selected",
          /<button class="chip on" data-path="">Top level</.test(c4?.innerHTML || ""), c4?.innerHTML);
    resetLog();
    await kids().find((c) => /Create home/.test(c.innerHTML))?.onclick();
    await tick(); await tick();
    const cr4 = bodies("/api/playlists/create").slice(-1)[0];
    check("FH ...and is sent as folder: null", cr4 && "folder" in cr4 && cr4.folder === null,
          JSON.stringify(cr4));
  } finally {
    run(`closePicker()`);
  }
}

// ============================================================================
// GT — the public port's gate. A 401 that says `gate` means the visitor is
// not signed in on the public port: go sign in with Spotify. It must not be
// mistaken for the owner's Spotify login having lapsed (needs_auth), which
// shows the setup view instead.
{
  run(`show("now"); location.href = ""`);
  routes["GET /api/status"] = { status: 401, body: { detail: "sign in with Spotify", gate: true, needs_auth: true } };
  await run(`api("/api/status").catch(() => {})`);
  check("GT a gate 401 sends the page to /gate/start",
        run("location.href") === "/gate/start", `href=${run("location.href")}`);
  check("GT ...and does not show the Spotify setup view",
        $$("view-setup").hidden === true, `setup hidden=${$$("view-setup").hidden}`);
  delete routes["GET /api/status"];
}

// ============================================================================
// SP — split a home into parts: the picker, the counter, Suggest, the parts.
// Spec: docs/superpowers/specs/2026-10-20-split-into-parts-design.md.
// ============================================================================
{
  resetLog();
  const HOMES = { homes: [
    { id: "H1", name: "Big Home", total: 24, folder: "ROOT / Mix", splittable: true, reason: null, draft: false },
    { id: "H2", name: "Tiny", total: 5, folder: null, splittable: false, reason: "too small to split — fewer than 10 songs", draft: false },
  ] };
  const DRAFT = {
    playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "rock", uris: ["u1", "u2", "u3"] },
            { key: "p2", name: "jazz", uris: ["u4", "u5"] }],
    unknown: ["u3"], hint: null, scores: { "2": 0.3 },
    songs: { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
             u3: { name: "Three", artists: "C", local: false }, u4: { name: "Four", artists: "D", local: false },
             u5: { name: "Five", artists: "E", local: false } },
    tracks: [], done: null,
  };
  routes["GET /api/splitparts/homes"] = { status: 200, body: HOMES };
  routes["GET /api/splitparts/H1"] = { status: 404, body: { detail: "no split in progress for this playlist" } };
  await run(`showSplit()`);
  await tick();
  const homesHtml = () => $$("sp-homes").innerHTML;
  check("SP the Split tab lists homes",
        /Big Home/.test(homesHtml()) && /Tiny/.test(homesHtml()), homesHtml().slice(0, 300));
  check("SP ...a too-small home is listed but disabled, with why",
        /<button[^>]*data-id="H2"[^>]*disabled/.test(homesHtml()) && /fewer than 10/.test(homesHtml()),
        homesHtml());
  $$("sp-filter").value = "tin";
  run(`renderSpHomes()`);
  check("SP the filter narrows the list", !/Big Home/.test(homesHtml()) && /Tiny/.test(homesHtml()), homesHtml());
  $$("sp-filter").value = "";
  run(`renderSpHomes()`);

  await run(`openSpHome(spHomes[0])`);
  await tick();
  check("SP a home without a draft opens on the counter, at 2",
        $$("sp-setup").hidden === false && $$("sp-parts").hidden === true && $$("sp-n").textContent === "2",
        `setup=${$$("sp-setup").hidden} n=${$$("sp-n").textContent}`);
  run(`$("sp-minus").onclick()`);
  check("SP ...the counter does not go below 2", $$("sp-n").textContent === "2", $$("sp-n").textContent);
  for (let i = 0; i < 6; i++) run(`$("sp-plus").onclick()`);
  check("SP ...nor above 6", $$("sp-n").textContent === "6", $$("sp-n").textContent);
  // Lands on 3 — not the 2 the counter defaults to — so the POST below can
  // tell "the chosen number" apart from "whatever the default happens to be".
  run(`$("sp-minus").onclick(); $("sp-minus").onclick(); $("sp-minus").onclick()`);
  check("SP ...the counter now reads the chosen 3", $$("sp-n").textContent === "3", $$("sp-n").textContent);

  routes["POST /api/splitparts/H1/suggest"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "done", phase: "done", done: 0, total: 0, message: null, summary: null } };
  routes["GET /api/splitparts/H1"] = { status: 200, body: DRAFT };
  await run(`$("sp-suggest").onclick()`);
  await tick(); await tick();
  const sent = log.find((l) => l.method === "POST" && l.path === "/api/splitparts/H1/suggest");
  check("SP Suggest posts the chosen number", sent && JSON.parse(sent.body).n === 3, JSON.stringify(sent));
  check("SP ...and lands on the parts, one chip per part with its count",
        $$("sp-parts").hidden === false &&
        /data-key="p1"[^>]*>[\s\S]*rock[\s\S]*3/.test($$("sp-chips").innerHTML) &&
        /data-key="p2"[\s\S]*jazz[\s\S]*2/.test($$("sp-chips").innerHTML),
        $$("sp-chips").innerHTML);
  check("SP ...the first part's songs are listed, title and artist",
        /One[\s\S]*A/.test($$("sp-list").innerHTML) && !/Four/.test($$("sp-list").innerHTML),
        $$("sp-list").innerHTML);
  check("SP ...a song without Last.fm data carries the ? mark",
        /data-uri="u3"[\s\S]*sp-unknown/.test($$("sp-list").innerHTML), $$("sp-list").innerHTML);
  run(`spShowPart("p2")`);
  check("SP tapping another chip lists that part",
        /Four/.test($$("sp-list").innerHTML) && !/One/.test($$("sp-list").innerHTML), $$("sp-list").innerHTML);

  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "error", phase: "", done: 0, total: 0, message: "not enough Last.fm data on these songs to suggest a split", summary: null } };
  await run(`spSuggest(2)`);
  await tick(); await tick();
  check("SP a failed suggestion shows its reason on the counter screen",
        $$("sp-setup").hidden === false && /not enough Last\.fm data/.test($$("sp-setup-msg").textContent),
        $$("sp-setup-msg").textContent);

  routes["GET /api/splitparts/H1"] = { status: 200, body: DRAFT };
  await run(`openSpHome(spHomes[0])`);
  await tick();
  check("SP a home with a draft opens straight on its parts", $$("sp-parts").hidden === false, "");

  // Fix round 1, finding 2: a Suggest started on one home must not keep
  // polling — or clobber the screen — once a different home is opened.
  // H1's progress never resolves ("running" forever), so the only way its
  // poll stops is if opening another home stops it.
  routes["POST /api/splitparts/H1/suggest"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "running", phase: "tagging", done: 1, total: 5, message: null, summary: null } };
  resetLog();
  await run(`spSuggest(2)`);
  check("SP starting Suggest polls the open home's progress once",
        gets("/api/splitparts/H1/progress") === 1, `${gets("/api/splitparts/H1/progress")} GET(s)`);

  const DRAFT_H3 = {
    playlist_id: "H3", name: "Third Home", snapshot_id: "s3", n: 2,
    parts: [{ key: "q1", name: "folk", uris: ["v1"] }, { key: "q2", name: "ambient", uris: ["v2"] }],
    unknown: [], hint: null, scores: { "2": 0.4 },
    songs: { v1: { name: "Six", artists: "F", local: false }, v2: { name: "Seven", artists: "G", local: false } },
    tracks: [], done: null,
  };
  routes["GET /api/splitparts/H3"] = { status: 200, body: DRAFT_H3 };
  resetLog();
  await run(`openSpHome({ id: "H3", name: "Third Home" })`);
  await tick();
  check("SP opening another home shows its own parts, not the old one's",
        /folk/.test($$("sp-chips").innerHTML) && !/rock/.test($$("sp-chips").innerHTML),
        $$("sp-chips").innerHTML);
  run(`spShowPart("q2")`);
  check("SP ...and the new home's chip tap sticks",
        run(`spShown`) === "q2", run(`spShown`));

  resetLog();
  // H1's poll was scheduled 1000ms out while it was "running"; a real wait
  // (not just a tick) is what gives a surviving timer the chance to fire.
  await new Promise((r) => setTimeout(r, 1100));
  check("SP the old home's abandoned poll makes no further request",
        gets("/api/splitparts/H1/progress") === 0 && gets("/api/splitparts/H3/progress") === 0,
        `H1=${gets("/api/splitparts/H1/progress")} H3=${gets("/api/splitparts/H3/progress")}`);
  check("SP ...and does not snap the new home's screen back to its first part",
        run(`spShown`) === "q2", run(`spShown`));

  run(`spStopPolling(); show("lists")`);
}

// ============================================================================
// SP2 — the parts screen: a touch that moves drags; songs drop on chips;
// parts come and go. (A touch that does not move opens the house player — SPP.)
// ============================================================================
{
  resetLog();
  AudioStub.made.length = 0; AudioStub.refuse = false;
  const songs = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                  u3: { name: "Three", artists: "C", local: false }, u4: { name: "Four", artists: "D", local: false } };
  const D = (parts, extra = {}) => ({ playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2, parts,
    unknown: [], hint: null, scores: {}, songs, tracks: [], done: null, ...extra });
  const two = () => [{ key: "p1", name: "rock", uris: ["u1", "u2"] }, { key: "p2", name: "jazz", uris: ["u3", "u4"] }];
  run(`show("split")`);
  // spDraft & co are `let` bindings in app.js, not properties of the vm's
  // global object: assign them inside run(), never through ctx.
  run(`spDraft = ${JSON.stringify(D(two()))}`);
  run(`spPid = "H1"; spShown = "p1"; spSection("sp-work"); renderSpParts(); nowState = { is_playing: true }`);

  // drag onto another part's chip
  routes["POST /api/splitparts/H1/move"] = { status: 200,
    body: D([{ key: "p1", name: "rock", uris: ["u2"] }, { key: "p2", name: "jazz", uris: ["u3", "u4", "u1"] }]) };
  document.elementFromPoint = () => ({ closest: () => ({ dataset: { key: "p2" } }) });
  run(`spPointerDown("u1", 10, 10)`);
  run(`spPointerMove(10, 40)`);
  check("SP2 moving the finger lifts the song at once and lights the chip under it",
        run("spDropKey") === "p2" && document.body.children.some((c) => c.className === "sp-ghost"),
        `drop=${run("spDropKey")}`);
  await run(`spPointerUp(10, 40)`);
  await tick();
  const mv = log.find((l) => l.path === "/api/splitparts/H1/move");
  check("SP2 releasing over a chip moves the song to that part",
        mv && JSON.parse(mv.body).uri === "u1" && JSON.parse(mv.body).to === "p2", JSON.stringify(mv));
  check("SP2 ...and the list redraws without it", !/data-uri="u1"/.test($$("sp-list").innerHTML) &&
        run("spDropKey") === null, $$("sp-list").innerHTML);

  resetLog();
  document.elementFromPoint = () => null;
  run(`spPointerDown("u2", 10, 10); spPointerMove(10, 40)`);
  await run(`spPointerUp(10, 40)`);
  check("SP2 releasing away from every chip moves nothing", !log.some((l) => l.path.endsWith("/move")), "");
  document.elementFromPoint = () => ({ closest: () => ({ dataset: { key: "p1" } }) });
  run(`spPointerDown("u2", 10, 10); spPointerMove(10, 40)`);
  await run(`spPointerUp(10, 40)`);
  check("SP2 ...and so does releasing over its own part", !log.some((l) => l.path.endsWith("/move")), "");
  document.elementFromPoint = () => null;

  // add a part
  routes["POST /api/splitparts/H1/parts"] = { status: 200,
    body: D([...two(), { key: "p3", name: "New part", uris: [] }]) };
  check("SP2 the chip row ends with an add chip", /id="sp-add"/.test($$("sp-chips").innerHTML), $$("sp-chips").innerHTML);
  await run(`spAddPart()`);
  check("SP2 adding a part shows the new, empty part",
        run("spShown") === "p3" && /no songs in this part yet/.test($$("sp-list").innerHTML), "");

  // remove the shown part: asks with destinations, then shows another part
  run(`spDraft = ${JSON.stringify(D(two()))}`);
  run(`spShown = "p2"; renderSpParts()`);
  routes["POST /api/splitparts/H1/parts/p2/remove"] = (() => {
    let n = 0;
    return () => (n++ === 0
      ? { status: 200, body: { moves: { p1: 2 } } }
      : { status: 200, body: D([{ key: "p1", name: "rock", uris: ["u1", "u2", "u3", "u4"] }, { key: "p9", name: "x", uris: [] }]) });
  })();
  let asked = "";
  ctx.confirm = (m) => { asked = m; return false; };
  await run(`spRemovePart("p2")`);
  check("SP2 removing a part asks first, naming where its songs go",
        /jazz/.test(asked) && /2 songs/.test(asked) && /rock: 2/.test(asked), asked);
  check("SP2 ...and a no changes nothing",
        log.filter((l) => l.path.endsWith("/remove")).length === 1 && run("spShown") === "p2", "");
  ctx.confirm = () => true;
  await run(`spRemovePart("p2")`);
  check("SP removing the shown part shows another one",
        run("spShown") === "p1" && /data-uri="u3"/.test($$("sp-list").innerHTML), `shown=${run("spShown")}`);

  // the hint and Start over
  run(`spDraft = ${JSON.stringify(D(two(), { hint: 3 }))}`);
  run(`renderSpParts()`);
  check("SP2 a cleaner count is offered", $$("sp-hint").hidden === false && /3/.test($$("sp-hint").innerHTML),
        $$("sp-hint").innerHTML);
  routes["POST /api/splitparts/H1/suggest"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "running", phase: "grouping", done: 0, total: 0, message: null, summary: null } };
  ctx.confirm = (m) => { asked = m; return true; };
  run(`spDraft.edited = true`);   // asking is for a draft holding manual edits (SPF)
  await run(`spResuggest(3)`);
  const sg = log.find((l) => l.path === "/api/splitparts/H1/suggest");
  check("SP2 trying it on an edited draft asks first, then re-suggests with that count",
        /moves/.test(asked) && sg && JSON.parse(sg.body).n === 3, asked);
  run(`spStopPolling()`);

  ctx.confirm = () => true;
  run(`show("lists")`);
}

// ============================================================================
// SP3 — fix round 1: the drop survives a real HTMLCollection, and multi-touch
// safety. (The split's own audio player and its fixes went with it in favour
// of the house player — SPP.)
// ============================================================================
{
  const songs = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                  u3: { name: "Three", artists: "C", local: false } };
  const D = (parts, extra = {}) => ({ playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2, parts,
    unknown: [], hint: null, scores: {}, songs, tracks: [], done: null, ...extra });
  const two = () => [{ key: "p1", name: "rock", uris: ["u1", "u2"] }, { key: "p2", name: "jazz", uris: ["u3"] }];
  run(`show("split")`);
  run(`spDraft = ${JSON.stringify(D(two()))}`);
  run(`spPid = "H1"; spShown = "p1"; spSection("sp-work"); renderSpParts(); nowState = { is_playing: true }`);

  // fix 1 — dropping a lifted song must never call .indexOf() on
  // document.body.children: a real HTMLCollection has none, and the old
  // branch that did threw before spHighlight(null) or the move ever ran.
  resetLog();
  routes["POST /api/splitparts/H1/move"] = { status: 200,
    body: D([{ key: "p1", name: "rock", uris: ["u2"] }, { key: "p2", name: "jazz", uris: ["u3", "u1"] }]) };
  document.elementFromPoint = () => ({ closest: () => ({ dataset: { key: "p2" } }) });
  run(`spPointerDown("u1", 10, 10)`);
  run(`spPointerMove(10, 40)`);
  // Stand in for a real HTMLCollection for the span of this one drop: it has
  // no indexOf, so app code reaching for one throws exactly as it would in
  // a real browser. Swapped back right after — later checks' `.some()` call
  // is the stub's own bookkeeping, not something app code depends on.
  const realChildren = document.body.children;
  document.body.children = new Proxy(realChildren, {
    get(t, p) { if (p === "indexOf") throw new Error("a real HTMLCollection has no indexOf"); return t[p]; },
  });
  let dropErr = null;
  try { await run(`spPointerUp(10, 40)`); } catch (e) { dropErr = e; }
  document.body.children = realChildren;
  check("SP3 dropping a lifted song never reaches for document.body.children.indexOf",
        dropErr === null, String(dropErr));
  check("SP3 ...the ghost is actually gone (the stub's own remove(), not app code)",
        !document.body.children.some((c) => c.className === "sp-ghost"), "");
  check("SP3 ...the chip highlight clears instead of staying lit", run("spDropKey") === null, run("spDropKey"));
  const mv1 = log.find((l) => l.path === "/api/splitparts/H1/move");
  check("SP3 ...and the song actually moved, instead of the drop silently doing nothing",
        mv1 && JSON.parse(mv1.body).uri === "u1" && JSON.parse(mv1.body).to === "p2", JSON.stringify(mv1));
  document.elementFromPoint = () => null;

  // fix 5 — two fingers: a second touch must end the first finger's drag
  // (and its ghost), and the first finger's later release must do nothing.
  run(`spDraft = ${JSON.stringify(D(two()))}`);
  run(`spShown = "p1"; renderSpParts()`);
  resetLog();
  AudioStub.made.length = 0;
  run(`spPointerDown("u1", 10, 10, 1)`);
  run(`spPointerMove(10, 40, 1)`);
  check("SP3 the first finger's drag lifts", document.body.children.some((c) => c.className === "sp-ghost"), "");
  run(`spPointerDown("u3", 10, 10, 2)`);
  check("SP3 a second finger touching down ends the first finger's drag and its ghost",
        !document.body.children.some((c) => c.className === "sp-ghost") && run("spDropKey") === null, "");
  await run(`spPointerUp(10, 40, 1)`);
  await tick();
  check("SP3 the first finger's now-stale release does nothing",
        !log.some((l) => l.path.endsWith("/move")) && !log.some((l) => l.path.includes("/preview")) &&
        $$("preview-pop").hidden === true, `pop hidden=${$$("preview-pop").hidden}`);
  run(`spPointerCancel(2)`);   // tidy up the second finger too

  run(`show("lists")`);
}

// ============================================================================
// SPD — Done: name, folder, what happens to the original, the price; then
// progress, and a summary or a way back. Fix round 1: a started Done is
// never invisible to the screen, a stale /plan answer never wins, an
// emptied name never falls back to a bad typed one, and Back → parts →
// Done… keeps what was typed. (Named SPD, not SD — SD already names an
// unrelated block above — and not SP3, which Task 9's own fix round uses.)
// ============================================================================
{
  resetLog();
  const songs = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                  u3: { name: "Three", artists: "C", local: false } };
  const DRAFT = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "rock", uris: ["u1", "u2"] }, { key: "p2", name: "jazz", uris: ["u3"] }],
    unknown: [], hint: null, scores: {}, songs, tracks: [], done: null };
  run(`show("split"); spPid = "H1"; spDoneChoicePid = null; spPlan = null;
       spDraft = ${JSON.stringify(DRAFT)}; spShown = "p1"; spSection("sp-work"); renderSpParts()`);
  check("SPD the Done button sits under the chips, not grouped with Remove/Start over",
        /id="sp-open-done"/.test($$("sp-done-row").innerHTML) && !/id="sp-open-done"/.test($$("sp-actions").innerHTML),
        `row=${$$("sp-done-row").innerHTML} actions=${$$("sp-actions").innerHTML}`);

  // adjustment 1 — opening Done must not leave a clip playing over it.
  AudioStub.made.length = 0; AudioStub.refuse = false;
  run(`nowState = { is_playing: false }`);
  routes["GET /api/splitparts/H1/preview?key=p1&start=u1"] = { clips: [{ name: "One", artist: "A", url: "https://cdn/spd-u1.mp3", uri: "u1", deezer_id: 1 }],
                                                              next_offset: null, tracks: [], total: 2 };
  run(`spPointerDown("u1", 10, 10)`);
  await run(`spPointerUp(10, 10)`);
  await tick();
  check("SPD the house player is open before Done opens",
        $$("preview-pop").hidden === false && AudioStub.made.at(-1)?.src === "https://cdn/spd-u1.mp3", "");

  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 6, creates: 2, empty: [], local: ["Home demo"], removals: 0,
    folders: [{ path: "ROOT / Mix", blocked: null }, { path: "ROOT / Deep", blocked: "file this one by hand" }],
    folder: "ROOT / Mix" } };
  await run(`spOpenDone()`);
  await new Promise((r) => setTimeout(r, 300));   // the player's soft landing is a 180ms fade
  check("SPD opening Done closes the player", $$("preview-pop").hidden === true && AudioStub.made.at(-1)?.paused === true, "");
  resetLog();   // drop the preview_resume POST and the plan GET — not part of the price check below
  const doneHtml = () => $$("sp-done").innerHTML;
  check("SPD the Done screen names each part by its KEY (not position), prefilled, placeholder holds the suggested name",
        /id="sp-name-p1"[^>]*value="rock"[^>]*placeholder="rock"/.test(doneHtml()) &&
        /id="sp-name-p2"[^>]*value="jazz"[^>]*placeholder="jazz"/.test(doneHtml()), doneHtml().slice(0, 400));
  check("SPD ...defaults to the original's folder, and a folder the mover can't reach is disabled",
        $$("sp-folder").value === "ROOT / Mix" && /value="ROOT \/ Deep" disabled/.test(doneHtml()), doneHtml());
  check("SPD ...keeps the original as a subset by default, renamed with the subset mark",
        /id="sp-orig-subset"[^>]*checked/.test(doneHtml()) && /renamed with the subset mark/.test(doneHtml()), doneHtml());
  check("SPD ...warns about local files that stay behind", /Home demo/.test(doneHtml()) && /local file/.test(doneHtml()), "");
  check("SPD ...and shows the price before anything happens, the subset wording in the cost line too",
        /2 new playlists/.test(doneHtml()) && /about 6 calls/.test(doneHtml()) &&
        /renames the original with the subset mark/.test(doneHtml()) &&
        !log.some((l) => l.method === "POST"), doneHtml());

  routes["GET /api/splitparts/H1/plan?original=part&part=p1"] = { status: 200, body: {
    calls: 4, creates: 1, empty: [], local: [], removals: 1,
    folders: [{ path: "ROOT / Mix", blocked: null }], folder: "ROOT / Mix" } };
  run(`spDoneChoice.original = "part"; spDoneChoice.part = "p1"`);
  await run(`spRefreshPlan()`);
  check("SPD choosing 'becomes a part' re-prices it, with the removals",
        /1 new playlist\b/.test(doneHtml()) && /removes 1 song/.test(doneHtml()) && /about 4 calls/.test(doneHtml()), doneHtml());
  check("SPD ...and that part's name field says it keeps the original's name",
        /keeps the name/.test(doneHtml()) && /Big Home/.test(doneHtml()), doneHtml());

  // fix round 1, item 3 — an emptied name falls back to the SUGGESTED name,
  // never to a bad name that was typed and then cleared; Confirm sends the
  // typed names/folder/choice and ends on a summary.
  run(`spDoneChoice.original = "subset"; spDoneChoice.part = null`);
  await run(`spRefreshPlan()`);
  $$("sp-name-p1").value = "[bad]";
  $$("sp-name-p2").value = "Jazzy";
  routes["POST /api/splitparts/H1/done"] = { status: 400, body: { detail: "'[bad]': looks like an input, not a home" } };
  resetLog();
  await run(`spConfirmDone()`);
  await tick();
  check("SPD a bad typed name is refused with a toast, the form stays up and Confirm works again",
        /looks like an input/.test($$("toast").textContent) && $$("sp-done").hidden === false &&
        $$("sp-confirm").disabled === false, $$("toast").textContent);

  $$("sp-name-p1").value = "";   // cleared, not retyped — must NOT send "[bad]"
  routes["POST /api/splitparts/H1/done"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "done", phase: "done", done: 2, total: 2, message: null,
    summary: { created: [{ id: "N1", name: "rock", count: 2 }, { id: "N2", name: "Jazzy", count: 1 }],
               original: "subset", empty: [], local: ["Home demo"] } } };
  resetLog();
  await run(`spConfirmDone()`);
  await tick();
  const done = log.find((l) => l.method === "POST" && l.path === "/api/splitparts/H1/done");
  const body = done && JSON.parse(done.body);
  check("SPD Confirm sends the SUGGESTED name for a cleared field (never the cleared bad one), the typed one, the folder and the choice",
        body && body.names.p1 === "rock" && body.names.p2 === "Jazzy" && body.folder === "ROOT / Mix" &&
        body.original === "subset" && body.part === null, JSON.stringify(body));
  check("SPD ...and ends on a summary of what was made",
        /rock/.test(doneHtml()) && /Jazzy/.test(doneHtml()) && /2 songs/.test(doneHtml()) && /id="sp-finish"/.test(doneHtml()) &&
        run("spDraft") === null, doneHtml());

  // the playlist changed: a way to re-read
  run(`spDraft = ${JSON.stringify(DRAFT)}`);
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "changed", phase: "", done: 0, total: 2,
    message: "the playlist changed in Spotify since the suggestion — re-read it to continue", summary: null } };
  await run(`spConfirmDone()`);
  check("SPD a changed playlist offers Re-read", /id="sp-reread"/.test(doneHtml()) && /changed in Spotify/.test(doneHtml()), doneHtml());
  routes["POST /api/splitparts/H1/reread"] = { status: 200, body: { ...DRAFT, unknown: ["u3"] } };
  await run(`spReread()`);
  check("SPD ...which lands back on the parts", $$("sp-parts").hidden === false && run("spDraft.unknown.length") === 1, "");

  // an error that never wrote anything (draft.done still null): the plain
  // "Back / Done again" form, not the locked finish screen.
  routes["GET /api/splitparts/H1"] = { status: 200, body: { ...DRAFT, unknown: ["u3"] } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "error", phase: "", done: 1, total: 2,
    message: "Spotify API 429: rate limited — your split is saved; press Done again to continue", summary: null } };
  resetLog();
  await run(`spConfirmDone()`);
  check("SPD a failed Done that never started writing says why and offers Done again",
        /press Done again/.test(doneHtml()) && /id="sp-retry"/.test(doneHtml()) && !/id="sp-finish-done"/.test(doneHtml()), doneHtml());
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "running", phase: "creating", done: 1, total: 2, message: null, summary: null } };
  await run(`$("sp-retry").onclick()`);
  check("SPD ...and Done again posts again",
        log.filter((l) => l.method === "POST" && l.path === "/api/splitparts/H1/done").length === 2, "");
  run(`spStopPolling()`);

  // adjustment 3 — the Confirm button posts only once for a double tap; the
  // guard is a plain flag, not the disabled attribute, since calling the
  // function directly (as this check does) bypasses a disabled button.
  resetLog();
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "running", phase: "creating", done: 0, total: 2, message: null, summary: null } };
  run(`spConfirmDone(); spConfirmDone();`);
  await tick();
  check("SPD two quick taps on Confirm post once",
        log.filter((l) => l.method === "POST" && l.path === "/api/splitparts/H1/done").length === 1, "");
  run(`spStopPolling()`);

  // ---- fix round 1, item 1: a started Done is never invisible ------------

  const STARTED_DRAFT = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "rock", uris: ["u1", "u2"] }, { key: "p2", name: "jazz", uris: ["u3"] }],
    unknown: [], hint: null, scores: {}, songs, tracks: [],
    done: { opts: { original: "part", part: "p1", names: {}, folder: null }, created: {}, filled: [], original_done: false } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  routes["GET /api/splitparts/H1"] = { status: 200, body: STARTED_DRAFT };
  run(`spPid = null; spDoneChoicePid = null; spDraft = null`);
  resetLog();
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  check("SPD reopening a started Done lands on the locked finish screen, not an editable form",
        /id="sp-finish-done"/.test(doneHtml()) && !/id="sp-confirm"/.test(doneHtml()) &&
        /removed from it/.test(doneHtml()), doneHtml());
  check("SPD ...naming the folder as top level when none was chosen",
        /— top level —/.test(doneHtml()), doneHtml());
  routes["POST /api/splitparts/H1/done"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "running", phase: "creating", done: 0, total: 1, message: null, summary: null } };
  resetLog();
  run(`$("sp-finish-done").onclick()`);
  await tick();
  const finishBody = JSON.parse(log.find((l) => l.path === "/api/splitparts/H1/done").body);
  check("SPD its button posts exactly the stored opts",
        JSON.stringify(finishBody) === JSON.stringify(STARTED_DRAFT.done.opts), JSON.stringify(finishBody));
  run(`spStopPolling()`);

  // reopening a home whose Done is actively RUNNING right now must show the
  // progress panel (never the parts, and never the finish screen either —
  // there is nothing to click until it's done), using the kind the server
  // recorded so the finish is dispatched to spDoneFinished, not treated as
  // a fresh Suggest.
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "running", phase: "creating", done: 1, total: 2, message: null, summary: null, kind: "done" } };
  run(`spPid = null; spDoneChoicePid = null; spDraft = null`);
  resetLog();
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  check("SPD reopening a home with Done running shows the progress panel, not the parts or finish screen",
        $$("sp-progress").hidden === false && $$("sp-parts").hidden === true && $$("sp-done").hidden === true,
        `progress.hidden=${$$("sp-progress").hidden} parts.hidden=${$$("sp-parts").hidden} done.hidden=${$$("sp-done").hidden}`);
  check("SPD ...and never even asked for the draft while it was running",
        !log.some((l) => l.method === "GET" && l.path === "/api/splitparts/H1"), "");
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "done", phase: "done", done: 2, total: 2, message: null,
    summary: { created: [{ id: "N3", name: "rock", count: 2 }], original: "part", empty: [], local: [] } } };
  await run(`spPoll("done")`);
  check("SPD ...and ends in the summary once the running Done completes",
        /id="sp-finish"/.test(doneHtml()) && $$("sp-done").hidden === false, doneHtml());
  run(`spStopPolling()`);

  // an error on a Done that already wrote something lands on the locked
  // finish screen (with the error on top), not the editable "Done again"
  // form, which could now disagree with the stored choice.
  run(`spPid = "H1"; spDoneChoicePid = "H1"; spDraft = ${JSON.stringify(STARTED_DRAFT)}`);
  routes["GET /api/splitparts/H1"] = { status: 200, body: STARTED_DRAFT };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "error", phase: "", done: 0, total: 1,
    message: "Spotify API 429: rate limited — your split is saved; press Done again to continue", summary: null } };
  resetLog();
  await run(`spPoll("done")`);
  check("SPD an error on a started Done lands on the locked finish screen, with the error on top",
        /id="sp-finish-done"/.test(doneHtml()) && /rate limited/.test(doneHtml()) && !/id="sp-retry"/.test(doneHtml()),
        doneHtml());

  // the draft can vanish (Done finished and dropped it) while the job's own
  // last progress record for this pid still holds the summary; reopening
  // the home must show that summary, not a blank "pick a count" screen as
  // if nothing had ever been split here.
  routes["GET /api/splitparts/H1"] = { status: 404, body: { detail: "no split in progress for this playlist" } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "done", phase: "done", done: 1, total: 1, message: null,
    summary: { created: [{ id: "N9", name: "rock", count: 2 }], original: "subset", empty: [], local: [] } } };
  run(`spPid = null; spDoneChoicePid = null; spDraft = null`);
  resetLog();
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  check("SPD a vanished draft with a held summary shows the summary on reopen, not the setup screen",
        $$("sp-done").hidden === false && /id="sp-finish"/.test(doneHtml()) && $$("sp-setup").hidden === true,
        doneHtml());

  // ---- fix round 1, item 2: a stale /plan answer never wins --------------

  run(`spPid = "H1"; spDoneChoicePid = "H1"; spDraft = ${JSON.stringify(DRAFT)}; spPlan = null`);
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 6, creates: 2, empty: [], local: [], removals: 0,
    folders: [{ path: "ROOT / Mix", blocked: null }], folder: "ROOT / Mix" } };
  await run(`spRefreshPlan()`);   // a normal first load, so the next ones are "pending", not spinner-replaced
  let resolvePart, resolveSubset;
  routes["GET /api/splitparts/H1/plan?original=part&part=p1"] = () => new Promise((resolve) => { resolvePart = resolve; });
  run(`spDoneChoice.original = "part"; spDoneChoice.part = "p1"`);
  run(`spRefreshPlan()`);   // request 1, in flight
  await tick();
  check("SPD Confirm is disabled and the cost line says 'pricing…' while a plan is pending",
        $$("sp-confirm").disabled === true && /pricing…/.test(doneHtml()), doneHtml());
  routes["GET /api/splitparts/H1/plan?original=subset"] = () => new Promise((resolve) => { resolveSubset = resolve; });
  run(`spDoneChoice.original = "subset"; spDoneChoice.part = null`);
  run(`spRefreshPlan()`);   // request 2 (the latest), in flight too
  await tick();
  // resolve the STALE one (request 1, "part") first — out of order — then
  // the fresh one (request 2, "subset").
  resolvePart({ status: 200, body: { calls: 4, creates: 1, empty: [], local: [], removals: 1,
    folders: [{ path: "ROOT / Mix", blocked: null }], folder: "ROOT / Mix" } });
  await tick();
  resolveSubset({ status: 200, body: { calls: 6, creates: 2, empty: [], local: [], removals: 0,
    folders: [{ path: "ROOT / Mix", blocked: null }], folder: "ROOT / Mix" } });
  await tick();
  check("SPD out-of-order plan answers leave the cost line matching the LAST choice, not the stale one",
        /2 new playlists/.test(doneHtml()) && /about 6 calls/.test(doneHtml()) &&
        /renames the original with the subset mark/.test(doneHtml()) &&
        $$("sp-confirm").disabled === false, doneHtml());

  // ---- fix round 1, item 4a: Back → parts → Done… keeps what was typed ---

  run(`spPid = "H1"; spDoneChoicePid = "H1"; spDraft = ${JSON.stringify(DRAFT)}; spPlan = null`);
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 6, creates: 2, empty: [], local: [], removals: 0,
    folders: [{ path: "ROOT / Mix", blocked: null }], folder: "ROOT / Mix" } };
  await run(`spOpenDone()`);
  $$("sp-name-p1").value = "Rocky";
  run(`spDoneChoice.folder = "ROOT / Mix"`);
  run(`$("sp-done-back").onclick()`);   // Back → parts, reading the typed name first
  check("SPD Back reads the typed name before leaving",
        run("spDoneChoice.names.p1") === "Rocky", run("spDoneChoice.names.p1"));
  await run(`spOpenDone()`);   // Done… again, same pid
  check("SPD reopening Done for the SAME pid keeps the typed name and choice",
        /value="Rocky"/.test(doneHtml()) && $$("sp-folder").value === "ROOT / Mix", doneHtml());

  const DRAFT_H3 = { playlist_id: "H3", name: "Third Home", snapshot_id: "s3", n: 2,
    parts: [{ key: "q1", name: "folk", uris: ["v1"] }, { key: "q2", name: "ambient", uris: ["v2"] }],
    unknown: [], hint: null, scores: {}, songs: { v1: { name: "Six", artists: "F", local: false },
    v2: { name: "Seven", artists: "G", local: false } }, tracks: [], done: null };
  routes["GET /api/splitparts/H3/plan?original=subset"] = { status: 200, body: {
    calls: 3, creates: 2, empty: [], local: [], removals: 0, folders: [], folder: "" } };
  run(`spPid = "H3"; spDraft = ${JSON.stringify(DRAFT_H3)}`);
  // spRefreshPlan() now only ever reads names from a form that is genuinely
  // the one on screen (`hadPlan`) — opening Done for this different pid
  // resets `spPlan` to null first, so the stale "Rocky" sitting in the
  // stub's sticky "sp-name-p1" element (there is no "sp-name-p1" in THIS
  // draft anyway; it would have been "sp-name-q1") is never read at all,
  // matching a real DOM where that node would already be gone.
  await run(`spOpenDone()`);
  check("SPD opening Done for a DIFFERENT pid resets the choice, not the other split's typed name",
        !/value="Rocky"/.test(doneHtml()) && /value="folk"/.test(doneHtml()), doneHtml());
  run(`spStopPolling()`);

  // ---- fix round 1, item 4d: a refused /plan keeps the form up -----------

  run(`spPid = "H1"; spDoneChoicePid = "H1"; spDraft = ${JSON.stringify(DRAFT)}; spPlan = null`);
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 6, creates: 2, empty: [], local: [], removals: 0,
    folders: [{ path: "ROOT / Mix", blocked: null }], folder: "ROOT / Mix" } };
  await run(`spOpenDone()`);
  routes["GET /api/splitparts/H1/plan?original=part&part=p2"] = { status: 400, body: { detail: "at least 2 parts need songs in them" } };
  run(`spDoneChoice.original = "part"; spDoneChoice.part = "p2"`);
  await run(`spRefreshPlan()`);
  check("SPD a refused plan keeps the form up, the message in the cost line, Confirm disabled",
        /at least 2 parts need songs/.test(doneHtml()) && $$("sp-confirm").disabled === true &&
        /id="sp-orig-part-key"/.test(doneHtml()) && /id="sp-folder"/.test(doneHtml()), doneHtml());
  run(`$("sp-orig-subset").onchange()`);
  await tick();
  check("SPD ...and the user can switch the choice back to one that prices fine",
        /2 new playlists/.test(doneHtml()) && $$("sp-confirm").disabled === false, doneHtml());

  run(`spStopPolling(); show("lists")`);
}

// ============================================================================
// SPD2 — Done fix round 2: the typed choice invalidates when the part SET
// changes shape, a held summary never blocks re-splitting, the locked
// finish screen matches what the server will actually do, and smaller
// robustness fixes (a stale plan answer, a draft re-fetch failure, a race
// between two openSpHome calls).
// ============================================================================
{
  resetLog();
  const doneHtml2 = () => $$("sp-done").innerHTML;
  const songs3 = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                   u3: { name: "Three", artists: "C", local: true } };
  const DRAFT3 = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 3,
    parts: [{ key: "p1", name: "rock", uris: ["u1"] }, { key: "p2", name: "jazz", uris: ["u2"] },
            { key: "p3", name: "blues", uris: ["u3"] }],
    unknown: [], hint: null, scores: {}, songs: songs3, tracks: [], done: null };
  run(`show("split"); spPid = "H1"; spDoneChoicePid = null; spPlan = null;
       spDraft = ${JSON.stringify(DRAFT3)}; spShown = "p1"; spSection("sp-work"); renderSpParts()`);

  // ---- item 1: a structural change invalidates the typed choice ---------

  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 7, creates: 3, empty: [], local: [], removals: 0, folders: [], folder: "" } };
  await run(`spOpenDone()`);
  $$("sp-name-p2").value = "JazzyX";
  $$("sp-name-p1").value = "RockyKeep";
  run(`spReadNames()`);
  check("SPD2 the typed name lands on p2 (by key) before any structural change",
        run("spDoneChoice.names.p2") === "JazzyX", run("spDoneChoice.names.p2"));

  let removeCall = 0;
  const REMOVED_DRAFT = { ...DRAFT3, parts: [{ key: "p1", name: "rock", uris: ["u1", "u2"] },
                                              { key: "p3", name: "blues", uris: ["u3"] }] };
  routes["POST /api/splitparts/H1/parts/p2/remove"] = () => {
    removeCall++;
    // The dry run (moves preview) and the real removal share this one
    // route in the stub (keyed on method+path, not body) — the dry run
    // comes first, in source order.
    return removeCall === 1 ? { status: 200, body: { moves: { p1: 1 } } } : { status: 200, body: REMOVED_DRAFT };
  };
  await run(`spRemovePart("p2")`);
  // The parts screen wiped #sp-done, so in a real DOM the old name fields
  // are gone and getElementById answers null for them. The stub keeps every
  // id it ever saw and makes up a blank one for any other, either of which
  // spReadNames would read back as if a field were on screen — so, for
  // these checks only, a name field exists only while markup holds it.
  const realGetById = document.getElementById;
  document.getElementById = (id) => (id.startsWith("sp-name-") ? (reg[id] || null) : realGetById(id));
  const dropNameFields = () => { for (const k of Object.keys(reg)) if (k.startsWith("sp-name-")) delete reg[k]; };
  dropNameFields();
  check("SPD2 removing p2 leaves p3 with its OWN key, not renumbered to p2",
        run(`spDraft.parts.map((p) => p.key).join(",")`) === "p1,p3",
        run(`spDraft.parts.map((p) => p.key).join(",")`));

  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 5, creates: 2, empty: [], local: [], removals: 0, folders: [], folder: "" } };
  await run(`spOpenDone()`);
  check("SPD2 a remove invalidates the choice — p3 gets its OWN suggested name, never p2's leaked one",
        /id="sp-name-p3"[^>]*value="blues"/.test(doneHtml2()) && !/JazzyX/.test(doneHtml2()), doneHtml2());
  // Surgical, not a reset: the untouched part keeps what was typed for it,
  // and only the removed part's typed name is gone.
  check("SPD2 ...while p1's typed name survives the removal, and p2's typed name is gone",
        /id="sp-name-p1"[^>]*value="RockyKeep"/.test(doneHtml2()) && run("spDoneChoice.names.p2") === undefined,
        doneHtml2());
  routes["POST /api/splitparts/H1/parts"] = { status: 200, body: { ...REMOVED_DRAFT,
    parts: [...REMOVED_DRAFT.parts, { key: "p4", name: "New part", uris: [] }] } };
  run(`renderSpParts()`);
  dropNameFields();
  await run(`spAddPart()`);
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 5, creates: 2, empty: ["New part"], local: [], removals: 0, folders: [], folder: "" } };
  await run(`spOpenDone()`);
  check("SPD2 ...and survives adding a part, the new part showing its own suggested name",
        /id="sp-name-p1"[^>]*value="RockyKeep"/.test(doneHtml2()) &&
        /id="sp-name-p4"[^>]*value="New part"/.test(doneHtml2()) && !/JazzyX/.test(doneHtml2()), doneHtml2());
  document.getElementById = realGetById;
  run(`spStopPolling()`);

  // Start over (same key scheme, entirely different songs) invalidates too.
  run(`spDoneChoice.names = { p1: "OldStaleName" }; spDoneChoicePid = spPid;
       spPlan = { folders: [], local: [], empty: [], calls: 1, creates: 1, folder: "" };`);
  routes["POST /api/splitparts/H1/suggest"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "done", phase: "done", done: 0, total: 0, message: null, summary: null } };
  const RESUGGESTED = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "metal", uris: ["u5"] }, { key: "p2", name: "folk", uris: ["u6"] }],
    unknown: [], hint: null, scores: {}, songs: { u5: { name: "Five", artists: "E", local: false },
    u6: { name: "Six", artists: "F", local: false } }, tracks: [], done: null };
  routes["GET /api/splitparts/H1"] = { status: 200, body: RESUGGESTED };
  await run(`spResuggest(2)`);
  check("SPD2 Start over bumps the draft generation, so the stale choice no longer matches",
        run("spDoneChoiceGen !== spDraftGen") === true,
        `gen=${run("spDraftGen")} choiceGen=${run("spDoneChoiceGen")}`);
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 3, creates: 2, empty: [], local: [], removals: 0, folders: [], folder: "" } };
  await run(`spOpenDone()`);
  check("SPD2 ...so Done… shows fresh defaults for the NEW parts, not the old typed name",
        /id="sp-name-p1"[^>]*value="metal"/.test(doneHtml2()) && !/OldStaleName/.test(doneHtml2()), doneHtml2());
  run(`spStopPolling()`);

  // A stale part key (the choice pointed at a part that no longer exists)
  // falls back to the first part instead of leaving a dead key behind.
  routes["GET /api/splitparts/H1/plan?original=part&part=p1"] = { status: 200, body: {
    calls: 2, creates: 1, empty: [], local: [], removals: 1, folders: [], folder: "" } };
  run(`spDoneChoice = { original: "part", part: "DEAD", folder: "", names: {} }; spDoneChoicePid = spPid;
       spPlan = { folders: [], local: [], empty: [], calls: 1, creates: 1, folder: "" };`);
  await run(`spRefreshPlan()`);
  check("SPD2 a stale part key is replaced with the first part",
        run("spDoneChoice.part") === "p1", run("spDoneChoice.part"));
  run(`spStopPolling()`);

  // ---- item 2: a held summary never blocks re-splitting ------------------

  const SUMMARY = { created: [{ id: "N1", name: "metal", count: 1 }], original: "subset", empty: [], local: [] };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "done", phase: "done", done: 1, total: 1, message: null, summary: SUMMARY } };
  routes["GET /api/splitparts/H1"] = { status: 404, body: { detail: "no split in progress for this playlist" } };
  run(`spPid = null; spDoneChoicePid = null; spDraft = null; spShownSummaryKey = null;`);
  resetLog();
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  check("SPD2 the first reopen after Done shows the held summary",
        /id="sp-finish"/.test(doneHtml2()) && $$("sp-done").hidden === false, doneHtml2());
  check("SPD2 ...and offers no Split this home again in SUBSET mode — the original is no longer a home",
        !/id="sp-split-again"/.test(doneHtml2()), doneHtml2());

  run(`spPid = null`);   // left, then reopened — the job record for H1 is unchanged
  resetLog();
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  check("SPD2 reopening AGAIN after the summary was shown goes to the counter, not the same summary",
        $$("sp-setup").hidden === false && $$("sp-done").hidden === true, "");

  const SUMMARY2 = { created: [{ id: "N2", name: "folk", count: 1 }], original: "part", empty: [], local: [] };
  run(`spPid = "H1"; renderSpSummary(${JSON.stringify(SUMMARY2)})`);
  check("SPD2 the summary offers Split this home again in PART mode — the original stayed a home",
        /id="sp-split-again"/.test(doneHtml2()), doneHtml2());
  run(`$("sp-split-again").onclick()`);
  check("SPD2 ...and it goes straight to the counter",
        $$("sp-setup").hidden === false, "");

  // ---- item 4: the locked finish screen matches the server ---------------

  const STARTED3 = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 3,
    parts: [{ key: "p1", name: "rock", uris: ["u1"] }, { key: "p2", name: "jazz", uris: ["u2"] },
            { key: "p3", name: "blues", uris: ["u3"] }],
    unknown: [], hint: null, scores: {}, songs: songs3, tracks: [],
    done: { opts: { original: "part", part: "p1", names: {}, folder: "ROOT / Mix" },
            created: {}, filled: [], original_done: false } };
  routes["GET /api/splitparts/H1/plan?original=part&part=p1"] = { status: 200, body: {
    calls: 3, creates: 1, empty: ["blues"], local: ["Three"], removals: 1, folders: [], folder: "" } };
  routes["GET /api/splitparts/H1"] = { status: 200, body: STARTED3 };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  run(`spPid = null; spDoneChoicePid = null; spDraft = null`);
  resetLog();
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  check("SPD2 the finish screen lists only the part the server will actually create (blues is empty/local-only)",
        /jazz/.test(doneHtml2()) && !/blues/.test(doneHtml2()), doneHtml2());
  check("SPD2 ...names which part the original becomes, and that the rest is removed from it",
        /becomes the part “rock”/.test(doneHtml2()) && /removed from it/.test(doneHtml2()), doneHtml2());
  check("SPD2 ...and shows the folder, the local-file note and the price, straight from a real /plan",
        /ROOT \/ Mix/.test(doneHtml2()) && /Three/.test(doneHtml2()) && /local file/.test(doneHtml2()) &&
        /1 new playlist/.test(doneHtml2()) && /about 3 calls/.test(doneHtml2()), doneHtml2());

  const STARTED_SUBSET = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "rock", uris: ["u1"] }, { key: "p2", name: "jazz", uris: ["u2"] }],
    unknown: [], hint: null, scores: {}, songs: songs3, tracks: [],
    done: { opts: { original: "subset", part: null, names: {}, folder: null },
            created: {}, filled: [], original_done: false } };
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 5, creates: 2, empty: [], local: [], removals: 0, folders: [], folder: "" } };
  routes["GET /api/splitparts/H1"] = { status: 200, body: STARTED_SUBSET };
  run(`spPid = null; spDoneChoicePid = null; spDraft = null`);
  resetLog();
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  check("SPD2 subset mode's finish screen also says it stops being a home",
        /renamed with the subset mark/.test(doneHtml2()) && /stops being a home/.test(doneHtml2()), doneHtml2());

  // ---- item 5: a draft re-fetch failure offers Try again, not a guess ----

  routes["GET /api/splitparts/H1"] = { status: 500, body: { detail: "boom" } };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "error", phase: "", done: 0, total: 1,
    message: "Spotify API 429: rate limited — your split is saved; press Done again to continue", summary: null } };
  run(`spPid = "H1"; spDraft = ${JSON.stringify(STARTED_SUBSET)}`);
  resetLog();
  await run(`spPoll("done")`);
  check("SPD2 a draft re-fetch failure shows the message with Try again, not the stale editable form",
        /rate limited/.test(doneHtml2()) && /id="sp-try-again"/.test(doneHtml2()) &&
        !/id="sp-retry"/.test(doneHtml2()) && !/id="sp-finish-done"/.test(doneHtml2()), doneHtml2());
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  routes["GET /api/splitparts/H1"] = { status: 200, body: STARTED_SUBSET };
  await run(`$("sp-try-again").onclick()`);
  check("SPD2 ...and Try again reopens the home, landing on the (now working) finish screen",
        /id="sp-finish-done"/.test(doneHtml2()), doneHtml2());

  // ---- item 6: typed-while-pending survives; a stale warning never carries over; null-guards ----

  run(`spPid = "H1"; spDoneChoicePid = "H1"; spDraft = ${JSON.stringify(DRAFT3)};
       spPlan = { folders: [], local: [], empty: [], calls: 1, creates: 1, folder: "" };`);
  let resolvePending;
  routes["GET /api/splitparts/H1/plan?original=part&part=p1"] = () => new Promise((resolve) => { resolvePending = resolve; });
  run(`spDoneChoice.original = "part"; spDoneChoice.part = "p1"`);
  run(`spRefreshPlan()`);   // in flight ("pricing…")
  await tick();
  $$("sp-name-p2").value = "TypedDuringPending";
  resolvePending({ status: 200, body: { calls: 2, creates: 2, empty: [], local: [], removals: 1, folders: [], folder: "" } });
  await tick();
  check("SPD2 a name typed WHILE a plan was pending survives into the final render",
        run("spDoneChoice.names.p2") === "TypedDuringPending", run("spDoneChoice.names.p2"));
  run(`spStopPolling()`);

  run(`spDoneChoice.original = "subset"; spDoneChoice.part = null; spPlan = null;`);
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 7, creates: 3, empty: [], local: ["StaleLocalFile"], removals: 0, folders: [], folder: "" } };
  await run(`spRefreshPlan()`);   // seed a plan WITH a local warning
  check("SPD2 the good plan shows its own local warning", /StaleLocalFile/.test(doneHtml2()), doneHtml2());
  routes["GET /api/splitparts/H1/plan?original=part&part=p1"] = { status: 400, body: { detail: "at least 2 parts need songs in them" } };
  run(`spDoneChoice.original = "part"; spDoneChoice.part = "p1"`);
  await run(`spRefreshPlan()`);
  check("SPD2 the error render does not carry over the PREVIOUS plan's local warning",
        !/StaleLocalFile/.test(doneHtml2()) && /at least 2 parts need songs/.test(doneHtml2()), doneHtml2());
  run(`spDoneChoice.original = "subset"; spDoneChoice.part = null; spStopPolling();`);

  let threw = null;
  run(`spDraft = null`);
  try { run(`renderSpDone()`); } catch (e) { threw = e; }
  check("SPD2 renderSpDone with spDraft === null does not throw", threw === null, threw ? String(threw) : "");

  run(`spDraft = ${JSON.stringify(DRAFT3)}; $("sp-work").hidden = true;`);
  threw = null;
  try { run(`renderSpDone()`); } catch (e) { threw = e; }
  check("SPD2 renderSpDone while #sp-work is hidden does not throw", threw === null, threw ? String(threw) : "");
  run(`$("sp-work").hidden = false;`);

  // ---- item 7: a second openSpHome call wins over a slow first one -------

  let resolveH1Progress;
  routes["GET /api/splitparts/H1/progress"] = () => new Promise((resolve) => { resolveH1Progress = resolve; });
  run(`spPid = null; spDraft = null`);
  run(`openSpHome({ id: "H1", name: "Big Home" })`);   // fire-and-forget: in flight on its own /progress
  await tick();
  routes["GET /api/splitparts/H4/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  const DRAFT4 = { playlist_id: "H4", name: "Fourth Home", snapshot_id: "s4", n: 2,
    parts: [{ key: "r1", name: "chill", uris: ["w1"] }, { key: "r2", name: "dance", uris: ["w2"] }],
    unknown: [], hint: null, scores: {}, songs: { w1: { name: "W1", artists: "X", local: false },
    w2: { name: "W2", artists: "Y", local: false } }, tracks: [], done: null };
  routes["GET /api/splitparts/H4"] = { status: 200, body: DRAFT4 };
  await run(`openSpHome({ id: "H4", name: "Fourth Home" })`);
  check("SPD2 opening a second home while the first's /progress is in flight shows the second",
        /chill/.test($$("sp-chips").innerHTML), $$("sp-chips").innerHTML);
  resolveH1Progress({ status: 200, body: { state: "running", phase: "x", done: 0, total: 1, message: null, summary: null, kind: "suggest" } });
  await tick();
  check("SPD2 ...and H1's stale /progress answer landing late does not clobber it",
        $$("sp-progress").hidden === true && /chill/.test($$("sp-chips").innerHTML), "");

  run(`spStopPolling(); show("lists")`);
}

// ============================================================================
// SPD3 — Done fix round 3: stale state made structurally impossible.
// #sp-done is wiped the moment it is not the panel on screen; a draft
// generation (spDraftGen) lets every awaited answer tell a fresh load
// from a stale one; add/remove edit spDoneChoice surgically instead of a
// blunt reset.
// ============================================================================
{
  resetLog();
  const doneHtml3 = () => $$("sp-done").innerHTML;

  // ---- the structural fix itself ------------------------------------------
  run(`show("split"); spPid = "H1"; spSection("sp-work"); $("sp-done").innerHTML = "<p>stale</p>"; spPanel("sp-parts");`);
  check("SPD3 spPanel wipes a stale #sp-done form when switching to another panel",
        doneHtml3() === "", doneHtml3());

  // ---- scenario A: type, Back, Start over, leave via header Back while it
  // runs, the suggestion lands, reopen → Done… shows the NEW defaults ------

  const songsA = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false } };
  const DRAFT_A = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "rock", uris: ["u1"] }, { key: "p2", name: "jazz", uris: ["u2"] }],
    unknown: [], hint: null, scores: {}, songs: songsA, tracks: [], done: null };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  routes["GET /api/splitparts/H1"] = { status: 200, body: DRAFT_A };
  run(`spPid = null; spDraft = null;`);
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 4, creates: 2, empty: [], local: [], removals: 0, folders: [], folder: "" } };
  await run(`spOpenDone()`);
  $$("sp-name-p1").value = "RockyTyped";
  run(`$("sp-done-back").onclick()`);   // Back → parts, reading the typed name
  check("SPD3 the typed name is captured before Start over",
        run("spDoneChoice.names.p1") === "RockyTyped", run("spDoneChoice.names.p1"));

  let resolveProgressA;
  routes["POST /api/splitparts/H1/suggest"] = { status: 200, body: { started: true } };
  routes["GET /api/splitparts/H1/progress"] = () => new Promise((resolve) => { resolveProgressA = resolve; });
  run(`spResuggest(2)`);   // fire-and-forget: confirm() always answers yes
  await tick();

  // leave via the HEADER Back while the suggestion is still running — this
  // never touches spPid, only the view.
  routes["GET /api/splitparts/homes"] = { status: 200, body: { homes: [] } };
  await run(`$("sp-back").onclick()`);
  check("SPD3 leaving via header Back does not touch spPid",
        run("spPid") === "H1", run("spPid"));

  // the suggestion lands while we're away
  const RESUGGESTED_A = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "folk", uris: ["u3"] }, { key: "p2", name: "blues", uris: ["u4"] }],
    unknown: [], hint: null, scores: {}, songs: { u3: { name: "Three", artists: "C", local: false },
    u4: { name: "Four", artists: "D", local: false } }, tracks: [], done: null };
  routes["GET /api/splitparts/H1"] = { status: 200, body: RESUGGESTED_A };
  resolveProgressA({ status: 200, body: { state: "done", phase: "done", done: 0, total: 0, message: null, summary: null } });
  await tick();

  // reopen the home and open Done… again
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 4, creates: 2, empty: [], local: [], removals: 0, folders: [], folder: "" } };
  await run(`spOpenDone()`);
  check("SPD3 reopening Done… after the suggestion landed shows the NEW draft's suggested names",
        /id="sp-name-p1"[^>]*value="folk"/.test(doneHtml3()) && !/RockyTyped/.test(doneHtml3()), doneHtml3());
  run(`spStopPolling()`);

  // ---- scenario B1: a pending EDITABLE /plan answer must not replace the
  // locked finish screen once it is drawn ----------------------------------

  const STARTED_B = { ...DRAFT_A, done: { opts: { original: "subset", part: null, names: {}, folder: null },
                                           created: {}, filled: [], original_done: false } };
  run(`spPid = "H1"; spDraft = ${JSON.stringify(STARTED_B)};`);   // draft.done already set; generation unchanged
  let resolveEditablePlanB;
  routes["GET /api/splitparts/H1/plan?original=subset"] = () => new Promise((resolve) => { resolveEditablePlanB = resolve; });
  run(`spDoneChoice = { original: "subset", part: null, folder: "", names: {} }; spPlan = null;`);
  run(`spRefreshPlan()`);   // a lingering editable request, fire-and-forget
  await tick();

  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 4, creates: 2, empty: [], local: [], removals: 0, folders: [], folder: "" } };
  await run(`renderSpFinish()`);   // the screen moves to the locked view, same generation
  check("SPD3 the locked finish screen is up",
        /id="sp-finish-done"/.test(doneHtml3()), doneHtml3());
  resolveEditablePlanB({ status: 200, body: { calls: 9, creates: 9, empty: [], local: [], removals: 0, folders: [], folder: "" } });
  await tick();
  check("SPD3 ...and the lingering editable /plan answer landing after it does not redraw over it",
        /id="sp-finish-done"/.test(doneHtml3()) && !/about 9 calls/.test(doneHtml3()), doneHtml3());

  // ---- scenario B2: a /plan answer for H1 landing while H2 is open draws
  // nothing on H2 ------------------------------------------------------------

  run(`spPid = "H1"; spDraft = ${JSON.stringify(DRAFT_A)}; spDoneChoicePid = null; spPlan = null;`);
  let resolveH1Plan;
  routes["GET /api/splitparts/H1/plan?original=subset"] = () => new Promise((resolve) => { resolveH1Plan = resolve; });
  run(`spOpenDone()`);   // fire-and-forget: in flight on its own /plan
  await tick();

  const DRAFT_H2 = { playlist_id: "H2", name: "Second Home", snapshot_id: "s2", n: 2,
    parts: [{ key: "p1", name: "pop", uris: ["w1"] }, { key: "p2", name: "soul", uris: ["w2"] }],
    unknown: [], hint: null, scores: {}, songs: { w1: { name: "W1", artists: "X", local: false },
    w2: { name: "W2", artists: "Y", local: false } }, tracks: [], done: null };
  routes["GET /api/splitparts/H2/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  routes["GET /api/splitparts/H2"] = { status: 200, body: DRAFT_H2 };
  await run(`openSpHome({ id: "H2", name: "Second Home" })`);
  check("SPD3 H2 is open on its own parts screen",
        /pop/.test($$("sp-chips").innerHTML) && $$("sp-parts").hidden === false, $$("sp-chips").innerHTML);

  resolveH1Plan({ status: 200, body: { calls: 4, creates: 2, empty: [], local: [], removals: 0, folders: [], folder: "" } });
  await tick();
  check("SPD3 H1's stale /plan answer landing while H2 is open draws nothing on H2",
        $$("sp-parts").hidden === false && /pop/.test($$("sp-chips").innerHTML) && doneHtml3() === "",
        `parts.hidden=${$$("sp-parts").hidden} done=${doneHtml3()}`);
  run(`spStopPolling()`);

  // ---- scenario C: H1's draft landing late while H2 opens changes nothing

  let resolveH1Draft;
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  routes["GET /api/splitparts/H1"] = () => new Promise((resolve) => { resolveH1Draft = resolve; });
  run(`spPid = null; spDraft = null;`);
  run(`openSpHome({ id: "H1", name: "Big Home" })`);   // fire-and-forget: in flight on its own draft fetch
  await tick();

  await run(`openSpHome({ id: "H2", name: "Second Home" })`);
  check("SPD3 opening H2 while H1's draft fetch is still in flight shows H2",
        /pop/.test($$("sp-chips").innerHTML), $$("sp-chips").innerHTML);

  const draftBefore = run(`JSON.stringify(spDraft)`);
  resolveH1Draft({ status: 200, body: DRAFT_A });
  await tick();
  check("SPD3 H1's late draft does not change spDraft or the screen",
        run(`JSON.stringify(spDraft)`) === draftBefore && /pop/.test($$("sp-chips").innerHTML),
        run(`JSON.stringify(spDraft)`));
  run(`spStopPolling()`);

  // ---- item D: "which parts get created" is decided by key/content, never
  // by matching a possibly-renamed display name against /plan's own
  // suggested-name-based `empty` list ---------------------------------------

  const songsD = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: true } };
  const DRAFT_D = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
    parts: [{ key: "p1", name: "rock", uris: ["u1"] }, { key: "p2", name: "jazz", uris: ["u2"] }],
    unknown: [], hint: null, scores: {}, songs: songsD, tracks: [], done: null };
  run(`spPid = "H1"; spDraft = ${JSON.stringify(DRAFT_D)}; spDoneChoicePid = null; spPlan = null;`);
  routes["GET /api/splitparts/H1/plan?original=subset"] = { status: 200, body: {
    calls: 2, creates: 1, empty: ["jazz"], local: ["Two"], removals: 0, folders: [], folder: "" } };
  await run(`spOpenDone()`);
  run(`spDoneChoice.names.p2 = "RenamedJazz"`);   // a custom name for the local-only (never-created) part
  run(`renderSpDone()`);
  check("SPD3 the editable screen's 'Not created' line matches by key/content, surviving a rename",
        /Not created, no songs: RenamedJazz/.test(doneHtml3()), doneHtml3());
  run(`spStopPolling()`);

  const STARTED_D = { ...DRAFT_D, done: { opts: { original: "subset", part: null, names: { p2: "RenamedJazz" }, folder: null },
                                           created: {}, filled: [], original_done: false } };
  routes["GET /api/splitparts/H1"] = { status: 200, body: STARTED_D };
  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  run(`spPid = null; spDraft = null;`);
  resetLog();
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  check("SPD3 the locked finish screen excludes the renamed local-only part too, by key",
        !/RenamedJazz/.test(doneHtml3()) && /rock/.test(doneHtml3()), doneHtml3());

  // ---- item F: a non-404 draft-fetch failure in openSpHome offers Try
  // again, not a silent fall-through to the counter screen -----------------

  routes["GET /api/splitparts/H1/progress"] = { status: 200, body: { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null } };
  routes["GET /api/splitparts/H1"] = { status: 500, body: { detail: "boom" } };
  run(`spPid = null; spDraft = null;`);
  resetLog();
  await run(`openSpHome({ id: "H1", name: "Big Home" })`);
  check("SPD3 a non-404 draft-fetch failure shows the message with Try again, not the counter",
        /boom/.test(doneHtml3()) && /id="sp-try-again"/.test(doneHtml3()) && $$("sp-setup").hidden === true,
        doneHtml3());
  routes["GET /api/splitparts/H1"] = { status: 200, body: DRAFT_A };
  await run(`$("sp-try-again").onclick()`);
  check("SPD3 ...and Try again reopens the home successfully",
        $$("sp-parts").hidden === false, "");

  run(`spStopPolling(); show("lists")`);
}

// ============================================================================
// SPF — final review fixes. Start over opens the counter (asking only when
// the draft holds manual edits), Discard, folder filing shown per part in
// the summary, a poll token that a left view's in-flight answer cannot get
// past, the last finished Done found on entering the tab, Abandon on the
// locked finish screen, and the "becomes the part" picker naming the parts
// as typed.
// ============================================================================
{
  resetLog();
  const savedConfirm = ctx.confirm;
  const savedNow = run("JSON.stringify(nowState)");
  const doneF = () => $$("sp-done").innerHTML;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const IDLE = { state: "idle", phase: "", done: 0, total: 0, message: null, summary: null, kind: null };
  const songsF = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                   u3: { name: "Three", artists: "C", local: false } };
  const DRAFT_F = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 3,
    parts: [{ key: "p1", name: "rock", uris: ["u1"] }, { key: "p2", name: "jazz", uris: ["u2"] },
            { key: "p3", name: "blues", uris: ["u3"] }],
    unknown: [], hint: 2, scores: {}, songs: songsF, done: null, edited: false };
  let asked = null;
  try {
    routes["GET /api/splitparts/progress"] = { pid: null, kind: null, state: "idle", summary: null };
    routes["GET /api/splitparts/homes"] = { homes: [] };
    routes["GET /api/splitparts/H1/progress"] = IDLE;
    routes["GET /api/splitparts/H1"] = DRAFT_F;
    run(`show("split"); spPid = null; spDraft = null;`);
    await run(`openSpHome({ id: "H1", name: "Big Home" })`);

    // ---- 1: Start over opens the counter at the current count ------------
    ctx.confirm = (m) => { asked = m; return true; };
    resetLog();
    run(`$("sp-start-over").onclick()`);
    check("SPF Start over opens the counter, prefilled with the draft's count",
          $$("sp-setup").hidden === false && $$("sp-n").textContent === "3" && posts("/api/splitparts/H1/suggest") === 0,
          `setup=${$$("sp-setup").hidden} n=${$$("sp-n").textContent}`);
    check("SPF ...with a way back to the parts it came from",
          $$("sp-setup-keep").hidden === false, "");
    run(`$("sp-setup-keep").onclick()`);
    check("SPF ...which lands on the same draft's parts",
          $$("sp-parts").hidden === false && /blues/.test($$("sp-chips").innerHTML), "");

    routes["POST /api/splitparts/H1/suggest"] = { started: true };
    routes["GET /api/splitparts/H1/progress"] = { ...IDLE, state: "done", kind: "suggest" };
    run(`$("sp-start-over").onclick(); $("sp-minus").onclick()`);
    asked = null;
    await run(`$("sp-suggest").onclick()`);
    await tick();
    const sentF = bodies("/api/splitparts/H1/suggest").filter(Boolean);
    check("SPF Suggest from there, on an unedited draft, re-suggests with the new count and asks nothing",
          asked === null && sentF.length === 1 && sentF[0].n === 2, `asked=${asked} ${JSON.stringify(sentF)}`);

    routes["GET /api/splitparts/H1"] = { ...DRAFT_F, edited: true };
    await run(`openSpHome({ id: "H1", name: "Big Home" })`);
    resetLog();
    ctx.confirm = (m) => { asked = m; return false; };
    run(`$("sp-start-over").onclick()`);
    await run(`$("sp-suggest").onclick()`);
    check("SPF ...on an edited draft Suggest asks first, and a no posts nothing",
          /lost/.test(asked || "") && posts("/api/splitparts/H1/suggest") === 0, asked);
    run(`$("sp-setup-keep").onclick()`);

    // the hint keeps re-suggesting directly — asking only when edited
    asked = null;
    await run(`$("sp-hint-go").onclick()`);
    check("SPF the hint on an edited draft asks first too",
          /2 parts/.test(asked || "") && posts("/api/splitparts/H1/suggest") === 0, asked);
    run(`spDraft.edited = false; renderSpParts()`);
    asked = null;
    ctx.confirm = (m) => { asked = m; return true; };
    await run(`$("sp-hint-go").onclick()`);
    await tick();
    check("SPF ...and on an unedited one re-suggests with N straight away",
          asked === null && bodies("/api/splitparts/H1/suggest").filter(Boolean).pop()?.n === 2, `asked=${asked}`);
    run(`spStopPolling()`);

    // ---- 1: Discard ----------------------------------------------------------
    routes["GET /api/splitparts/H1"] = { ...DRAFT_F, edited: true };
    routes["GET /api/splitparts/H1/progress"] = IDLE;
    await run(`openSpHome({ id: "H1", name: "Big Home" })`);
    resetLog();
    ctx.confirm = (m) => { asked = m; return false; };
    check("SPF the parts screen offers Discard this split", /id="sp-discard"/.test($$("sp-actions").innerHTML),
          $$("sp-actions").innerHTML);
    await run(`$("sp-discard").onclick()`);
    check("SPF ...which asks first on an edited draft, and a no deletes nothing",
          /Discard/.test(asked || "") && log.filter((l) => l.method === "DELETE").length === 0, asked);
    routes["DELETE /api/splitparts/H1"] = { ok: true };
    run(`spDraft.edited = false`);
    asked = null;
    await run(`$("sp-discard").onclick()`);
    await tick();
    check("SPF ...an unedited one is deleted without a question, back on the homes list",
          asked === null && log.filter((l) => l.method === "DELETE" && l.path === "/api/splitparts/H1").length === 1 &&
          $$("sp-pick").hidden === false && $$("sp-work").hidden === true && run("spDraft") === null,
          `asked=${asked} pick=${$$("sp-pick").hidden}`);
    ctx.confirm = () => true;

    // ---- 2: filing is watched per part in the summary --------------------
    run(`SP_FILING_POLL_MS = 5`);
    let n1 = "filing";
    routes["GET /api/playlists/filing/N1"] = () => ({ state: n1, folder: n1 === "filed" ? "ROOT / Mix" : "ROOT / Mix", error: null });
    routes["GET /api/playlists/filing/N2"] = { state: "failed", folder: null, error: "another playlist is also called 'Jazzy'" };
    routes["GET /api/playlists/filing/N3"] = { state: "filing", folder: "ROOT / Mix", error: null };
    const SUM_F = { created: [{ id: "N1", name: "Rocky", count: 1 }, { id: "N2", name: "Jazzy", count: 1 },
                              { id: "N3", name: "Bluesy", count: 1 }],
                    original: "part", empty: [], local: [], folder: "ROOT / Mix", name: "Big Home" };
    resetLog();
    run(`spPid = "H1"; spSection("sp-work"); renderSpSummary(${JSON.stringify(SUM_F)})`);
    await tick();
    const line = (id) => $$(`sp-filing-${id}`).textContent;
    check("SPF the summary shows each part filing…",
          /filing…/.test(line("N1")) && /filing…/.test(line("N3")), `${line("N1")} | ${line("N3")}`);
    check("SPF ...and a refused one as left at the top level, with why",
          /left at the top level — another playlist is also called 'Jazzy'/.test(line("N2")), line("N2"));
    n1 = "filed";
    await wait(30); await tick();
    check("SPF a landed move reads filed in its folder",
          /filed in ROOT \/ Mix/.test(line("N1")), line("N1"));
    check("SPF ...and a final part is not asked about again",
          gets("/api/playlists/filing/N2") === 1, `${gets("/api/playlists/filing/N2")} GETs`);
    run(`show("lists")`);
    const n3Before = gets("/api/playlists/filing/N3");
    await wait(30); await tick();
    check("SPF leaving the view stops the filing watch",
          gets("/api/playlists/filing/N3") === n3Before, `${n3Before} -> ${gets("/api/playlists/filing/N3")}`);

    routes["GET /api/playlists/filing/N4"] = { state: "idle" };
    run(`show("split"); spPid = "H1"; spSection("sp-work"); renderSpSummary(${JSON.stringify({ ...SUM_F, created: [{ id: "N4", name: "Stuck", count: 1 }] })})`);
    await wait(40); await tick();
    check("SPF a filing job that never starts ends as left at the top level, not watched forever",
          /left at the top level — the move never started/.test(line("N4")), line("N4"));
    resetLog();
    run(`renderSpSummary(${JSON.stringify({ ...SUM_F, folder: null })})`);
    await tick();
    check("SPF with no folder chosen nothing is watched",
          log.filter((l) => l.path.startsWith("/api/playlists/filing/")).length === 0, "");
    run(`spStopPolling()`);

    // ---- 3: a poll in flight when the view is left draws nothing ----------
    let resolveDone;
    const SUM_SUBSET = { ...SUM_F, original: "subset", folder: null,
                         created: [{ id: "N7", name: "Seven", count: 1 }, { id: "N8", name: "Eight", count: 1 }] };
    routes["GET /api/splitparts/H1/progress"] = () => new Promise((r) => { resolveDone = r; });
    run(`spPid = "H1"; spSection("sp-work"); spPanel("sp-progress"); spShownSummaryKey = null;`);
    run(`spPoll("done")`);
    await tick();
    routes["GET /api/splitparts/progress"] = { pid: "H1", kind: "done", state: "running", summary: null };
    await run(`$("sp-back").onclick()`);
    await tick();
    resetLog();
    resolveDone({ status: 200, body: { state: "done", phase: "done", done: 2, total: 2, message: null, summary: SUM_SUBSET, kind: "done" } });
    await tick();
    check("SPF an in-flight poll that lands after leaving draws nothing and marks nothing shown",
          run("spShownSummaryKey") === null && $$("sp-pick").hidden === false && !/Seven/.test(doneF()),
          `key=${run("spShownSummaryKey")}`);
    await wait(1100); await tick();
    check("SPF ...and does not re-arm itself",
          gets("/api/splitparts/H1/progress") === 0, `${gets("/api/splitparts/H1/progress")} GETs`);

    // ...so entering the tab finds the finished Done and shows its summary
    routes["GET /api/splitparts/progress"] = { pid: "H1", kind: "done", state: "done", summary: SUM_SUBSET };
    run(`spPid = null`);
    await run(`showSplit()`);
    await tick();
    check("SPF entering the Split tab shows a finished Done's unseen summary (subset mode: no home to reopen)",
          $$("sp-work").hidden === false && /Seven/.test(doneF()) && $$("sp-title").textContent === "Big Home" &&
          run("spPid") === "H1", `title=${$$("sp-title").textContent}`);
    await run(`showSplit()`);
    await tick();
    check("SPF ...once only: the next entry lands on the homes list",
          $$("sp-pick").hidden === false && $$("sp-work").hidden === true, "");

    // ---- 4: Abandon on the locked finish screen -------------------------
    const STARTED_F = { ...DRAFT_F, done: { opts: { original: "subset", part: null, names: { p1: "Rocky" }, folder: null },
                                            created: { p1: "N1" }, filled: ["p1"], original_done: false } };
    routes["GET /api/splitparts/H1/progress"] = IDLE;
    routes["GET /api/splitparts/H1"] = STARTED_F;
    routes["GET /api/splitparts/H1/plan?original=subset"] = { calls: 5, creates: 3, empty: [], local: [], removals: 0, folders: [], folder: "" };
    await run(`openSpHome({ id: "H1", name: "Big Home" })`);
    check("SPF the locked finish screen offers Abandon — keep what was made",
          /id="sp-finish-done"/.test(doneF()) && /id="sp-abandon"[^>]*>Abandon — keep what was made/.test(doneF()), doneF());
    resetLog();
    ctx.confirm = (m) => { asked = m; return false; };
    await run(`$("sp-abandon").onclick()`);
    check("SPF ...it asks first, saying what stays, and a no posts nothing",
          /stay/.test(asked || "") && /original/.test(asked || "") && posts("/api/splitparts/H1/abandon") === 0, asked);
    ctx.confirm = () => true;
    routes["POST /api/splitparts/H1/abandon"] = { created: [{ id: "N1", name: "Rocky" }], original_done: false };
    await run(`$("sp-abandon").onclick()`);
    check("SPF ...a yes abandons and lists what was already made",
          posts("/api/splitparts/H1/abandon") === 1 && /Rocky/.test(doneF()) && /id="sp-abandon-back"/.test(doneF()) &&
          run("spDraft") === null, doneF());
    await run(`$("sp-abandon-back").onclick()`);
    check("SPF ...and returns to the homes list", $$("sp-pick").hidden === false, "");

    // ---- minors -------------------------------------------------------------
    routes["GET /api/splitparts/H1"] = DRAFT_F;
    await run(`openSpHome({ id: "H1", name: "Big Home" })`);
    run(`spDoneChoicePid = null; spPlan = null;`);
    routes["GET /api/splitparts/H1/plan?original=part&part=p1"] = { calls: 4, creates: 2, empty: [], local: [], removals: 2, folders: [], folder: "" };
    await run(`spOpenDone()`);
    $$("sp-name-p2").value = "Jazz Typed";
    run(`$("sp-name-p2").oninput()`);
    check("SPF the becomes-the-part picker names the parts as typed above",
          /Jazz Typed/.test($$("sp-orig-part-key").innerHTML) && /rock/.test($$("sp-orig-part-key").innerHTML),
          $$("sp-orig-part-key").innerHTML);
    run(`$("sp-orig-part").onchange()`);
    await tick();
    check("SPF ...and still does after a re-render",
          /Jazz Typed/.test($$("sp-orig-part-key").innerHTML), $$("sp-orig-part-key").innerHTML);
  } finally {
    ctx.confirm = savedConfirm;
    run(`spStopPolling(); previewHold.stop(true); nowState = ${savedNow}; show("lists")`);
  }
}

// ============================================================================
// SPP — a tap on a split song opens the HOUSE player (previewHold) on that
// song: its first page comes from the part's own preview endpoint, starting
// at the tapped uri; "next" walks on down the part; leaving the tab closes it.
// ============================================================================
{
  const savedNow = run("JSON.stringify(nowState)");
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    const songs = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                    u3: { name: "Three", artists: "C", local: false }, u4: { name: "Four", artists: "D", local: false } };
    const D = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
      parts: [{ key: "p1", name: "rock", uris: ["u1", "u2", "u3"] }, { key: "p2", name: "jazz", uris: ["u4"] }],
      unknown: [], hint: null, scores: {}, songs, done: null, edited: false };
    // Not playing: no resume is owed, so no repoll lands in a later block.
    run(`show("split"); nowState = { is_playing: false }; previewHold.stop(true)`);
    run(`spDraft = ${JSON.stringify(D)}; spPid = "H1"; spShown = "p1"; spSection("sp-work"); renderSpParts()`);
    const clip = (u, n) => ({ name: songs[u].name, artist: songs[u].artists, url: `https://cdn/spp-${u}-${n}.mp3`, uri: u, deezer_id: n });
    routes["GET /api/splitparts/H1/preview?key=p1&start=u2"] = { clips: [clip("u2", 1)], next_offset: 2, tracks: [], total: 3 };
    routes["GET /api/splitparts/H1/preview?key=p1&offset=2"] = { clips: [clip("u3", 2)], next_offset: null, tracks: [], total: 3 };
    routes["GET /api/splitparts/H1/preview?key=p1&start=u1"] = { clips: [clip("u1", 3)], next_offset: 1, tracks: [], total: 3 };
    const previewGets = () => log.filter((l) => l.method === "GET" && l.path.includes("/preview")).map((l) => l.path);

    run(`spPointerDown("u2", 10, 10, 7)`);
    await run(`spPointerUp(11, 11, 7)`);
    await tick();
    check("SPP a tap on a song opens the house player",
          $$("preview-pop").hidden === false && /Two/.test($$("preview-pop").innerHTML) && /pv-next/.test($$("preview-pop").innerHTML),
          `hidden=${$$("preview-pop").hidden} ${$$("preview-pop").innerHTML.slice(0, 160)}`);
    check("SPP ...whose first request is the part's preview, keyed by part and starting at the tapped song",
          previewGets()[0] === "/api/splitparts/H1/preview?key=p1&start=u2", JSON.stringify(previewGets()));
    check("SPP ...and it plays that song's clip", AudioStub.made.at(-1)?.src === "https://cdn/spp-u2-1.mp3" &&
          !AudioStub.made.at(-1).paused, `src=${AudioStub.made.at(-1)?.src}`);
    check("SPP ...and moves nothing", !log.some((l) => l.path.endsWith("/move")), "");
    check("SPP ...and nothing asks the deleted single-song endpoint",
          !log.some((l) => l.path.startsWith("/api/track_preview")), "");

    const first = AudioStub.made.at(-1);
    run(`$("pv-next").onclick()`);
    await tick();
    check("SPP the player's next fetches the part's next page and plays the next song",
          previewGets().includes("/api/splitparts/H1/preview?key=p1&offset=2") &&
          AudioStub.made.at(-1)?.src === "https://cdn/spp-u3-2.mp3" && first.paused === true,
          `${JSON.stringify(previewGets())} src=${AudioStub.made.at(-1)?.src}`);

    // A tap on another song starts there, never where the last walk stood.
    resetLog();
    run(`previewHold.stop(true)`);
    run(`spPointerDown("u1", 10, 10, 8)`);
    await run(`spPointerUp(10, 10, 8)`);
    await tick();
    check("SPP a later tap on a different song starts at that song, not the old position",
          previewGets()[0] === "/api/splitparts/H1/preview?key=p1&start=u1" &&
          AudioStub.made.at(-1)?.src === "https://cdn/spp-u1-3.mp3", JSON.stringify(previewGets()));

    const live = AudioStub.made.at(-1);
    run(`show("now")`);
    await new Promise((r) => setTimeout(r, 300));   // the player's soft landing is a 180ms fade
    check("SPP leaving the Split tab closes the player and stops its clip",
          $$("preview-pop").hidden === true && live.paused === true, `hidden=${$$("preview-pop").hidden}`);

    // Starting a fresh Suggest closes it too.
    run(`show("split"); spDraft = ${JSON.stringify(D)}; spPid = "H1"; spShown = "p1"; spSection("sp-work"); renderSpParts()`);
    run(`spPointerDown("u1", 10, 10, 9)`);
    await run(`spPointerUp(10, 10, 9)`);
    await tick();
    routes["POST /api/splitparts/H1/suggest"] = { started: true };
    routes["GET /api/splitparts/H1/progress"] = { state: "running", phase: "grouping", done: 0, total: 0, message: null, summary: null };
    const before = $$("preview-pop").hidden;
    await run(`spSuggest(2)`);
    run(`spStopPolling()`);
    check("SPP starting a fresh Suggest closes the player",
          before === false && $$("preview-pop").hidden === true, `before=${before} after=${$$("preview-pop").hidden}`);
  } finally {
    run(`spStopPolling(); previewHold.stop(true); nowState = ${savedNow}; show("lists")`);
  }
}

// ============================================================================
// SPE — "move to another playlist instead": each song row carries an icon
// button. Pressed and moved, it drags like the rest of the row; tapped, it
// opens the picker on the split's move targets; a pick moves the song at once.
// ============================================================================
{
  const savedNow = run("JSON.stringify(nowState)");
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    const songs = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                    u3: { name: "Three", artists: "C", local: false }, u4: { name: "Four", artists: "D", local: false } };
    const D = (parts, extra = {}) => ({ playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2, parts,
      unknown: [], hint: null, scores: {}, songs, done: null, edited: false, ...extra });
    const two = () => [{ key: "p1", name: "rock", uris: ["u1", "u2", "u3"] }, { key: "p2", name: "jazz", uris: ["u4"] }];
    run(`show("split"); nowState = { is_playing: false }; previewHold.stop(true); $("picker").hidden = true`);
    run(`spDraft = ${JSON.stringify(D(two()))}; spPid = "H1"; spShown = "p1"; spSection("sp-work"); renderSpParts()`);
    // A real pointerdown on the icon lands on its svg; closest() walks up to
    // the button and to the row. Scripted here: the stub DOM has no closest.
    const press = (uri, onIcon, x, y, id) => run(`spListPointerDown({
      target: { closest: (sel) => sel === ".sp-song" ? { dataset: { uri: ${JSON.stringify(uri)} } }
                                : sel === ".sp-elsewhere" ? (${onIcon} ? {} : null) : null },
      clientX: ${x}, clientY: ${y}, pointerId: ${id}, preventDefault() {} })`);
    const listHtml = () => $$("sp-list").innerHTML;
    check("SPE every song row ends with a labelled move-elsewhere icon button",
          /data-uri="u1"[\s\S]*?<button[^>]*class="[^"]*sp-elsewhere[^"]*"[^>]*aria-label="Move to another home"[^>]*>\s*<svg/.test(listHtml()) &&
          (listHtml().match(/sp-elsewhere/g) || []).length === 3, listHtml().slice(0, 400));

    routes["GET /api/splitparts/H1/targets"] = { targets: [
      { id: "A1", name: "alpha", folder: "ROOT / Mix" }, { id: "X1", name: "[Inbox]", folder: null }] };
    // 1: press on the icon and move: it lifts and drags like the row does
    routes["POST /api/splitparts/H1/move"] = D([{ key: "p1", name: "rock", uris: ["u2", "u3"] },
                                               { key: "p2", name: "jazz", uris: ["u4", "u1"] }]);
    document.elementFromPoint = () => ({ closest: () => ({ dataset: { key: "p2" } }) });
    press("u1", true, 10, 10, 21);
    run(`spPointerMove(10, 40, 21)`);
    check("SPE a press on the icon then a move lifts the song",
          document.body.children.some((c) => c.className === "sp-ghost") && run("spDropKey") === "p2",
          `drop=${run("spDropKey")}`);
    await run(`spPointerUp(10, 40, 21)`);
    await tick();
    document.elementFromPoint = () => null;
    const mvE = bodies("/api/splitparts/H1/move").filter(Boolean);
    check("SPE ...and a drop on a chip moves it, with no picker and no targets asked",
          mvE.length === 1 && mvE[0].uri === "u1" && mvE[0].to === "p2" && $$("picker").hidden === true &&
          gets("/api/splitparts/H1/targets") === 0, JSON.stringify(mvE));

    // 2: a tap on the icon opens the picker on the targets — no preview
    run(`spDraft = ${JSON.stringify(D(two()))}; spShown = "p1"; renderSpParts()`);
    resetLog();
    press("u1", true, 10, 10, 22);
    await run(`spPointerUp(11, 12, 22)`);
    await tick();
    const rows = $$("picker-list").children;
    check("SPE a tap on the icon opens the picker listing the move targets",
          $$("picker").hidden === false && gets("/api/splitparts/H1/targets") === 1 &&
          rows.length === 2 && /alpha/.test(rows[0].innerHTML) && /ROOT \/ Mix/.test(rows[0].innerHTML) &&
          /\[Inbox\]/.test(rows[1].innerHTML), rows.map((r) => r.innerHTML).join(" | "));
    check("SPE ...and asks for no preview", !log.some((l) => l.path.includes("/preview")) &&
          $$("preview-pop").hidden === true, JSON.stringify(log.map((l) => l.path)));

    // 3: picking one moves the song there at once
    routes["POST /api/splitparts/H1/elsewhere"] = { ...D([{ key: "p1", name: "rock", uris: ["u2", "u3"] },
                                                         { key: "p2", name: "jazz", uris: ["u4"] }], { edited: true }),
                                                    moved_to: { id: "A1", name: "alpha" } };
    rows[0].onclick();
    await tick();
    const sentE = bodies("/api/splitparts/H1/elsewhere").filter(Boolean);
    check("SPE picking one posts the song and the playlist to /elsewhere",
          sentE.length === 1 && sentE[0].uri === "u1" && sentE[0].to_id === "A1" && $$("picker").hidden === true,
          JSON.stringify(sentE));
    check("SPE ...the song leaves the list, the part's count drops, and a toast says where it went",
          !/data-uri="u1"/.test(listHtml()) && /data-uri="u2"/.test(listHtml()) &&
          /data-key="p1"[\s\S]*?sp-chip-n">2</.test($$("sp-chips").innerHTML) &&
          $$("toast").textContent === "moved to alpha", `${$$("toast").textContent} | ${$$("sp-chips").innerHTML}`);
    check("SPE ...and the draft held keeps no moved_to of its own", run("spDraft.moved_to") === undefined, "");

    // 4: a removal that failed after the add says so and changes nothing here
    run(`spDraft = ${JSON.stringify(D(two()))}; spShown = "p1"; renderSpParts()`);
    resetLog();
    routes["POST /api/splitparts/H1/elsewhere"] = { status: 502, body: { detail:
      "added to “alpha”, but removing it from “Big Home” failed (boom) — the song is now in both playlists" } };
    press("u2", true, 10, 10, 23);
    await run(`spPointerUp(10, 10, 23)`);
    await tick();
    const draftBefore = run("JSON.stringify(spDraft)");
    $$("picker-list").children[0].onclick();
    await tick();
    check("SPE a move that landed in both playlists says so, and the song stays listed",
          /now in both playlists/.test($$("toast").textContent) && /data-uri="u2"/.test(listHtml()) &&
          run("JSON.stringify(spDraft)") === draftBefore,
          `${$$("toast").textContent} | draft same=${run("JSON.stringify(spDraft)") === draftBefore}`);

    // 5: a plain tap on the row (not the icon) still previews, no picker
    resetLog();
    routes["GET /api/splitparts/H1/preview?key=p1&start=u3"] = { clips: [], next_offset: null, tracks: [], total: 3 };
    press("u3", false, 10, 10, 24);
    await run(`spPointerUp(10, 10, 24)`);
    await tick();
    check("SPE a tap on the row itself still opens the player, not the picker",
          gets("/api/splitparts/H1/preview?key=p1&start=u3") === 1 && gets("/api/splitparts/H1/targets") === 0 &&
          $$("picker").hidden === true, JSON.stringify(log.map((l) => l.path)));
  } finally {
    document.elementFromPoint = () => null;
    run(`spStopPolling(); previewHold.stop(true); $("picker").hidden = true; nowState = ${savedNow}; show("lists")`);
  }
}

// ============================================================================
// SPG — fix round 1: the click a phone synthesizes after a tap must not land
// on the player's Close button just drawn under the finger; and the open
// player marks the body so the song list keeps its last rows clear of it.
// ============================================================================
{
  const savedNow = run("JSON.stringify(nowState)");
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    const songs = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false } };
    const D = { playlist_id: "H1", name: "Big Home", snapshot_id: "s1", n: 2,
      parts: [{ key: "p1", name: "rock", uris: ["u1", "u2"] }, { key: "p2", name: "jazz", uris: [] }],
      unknown: [], hint: null, scores: {}, songs, done: null, edited: false };
    run(`show("split"); nowState = { is_playing: false }; previewHold.stop(true); $("picker").hidden = true`);
    run(`spDraft = ${JSON.stringify(D)}; spPid = "H1"; spShown = "p1"; spSection("sp-work"); renderSpParts()`);
    routes["GET /api/splitparts/H1/preview?key=p1&start=u1"] = {
      clips: [{ name: "One", artist: "A", url: "https://cdn/spg-u1.mp3", uri: "u1", deezer_id: 1 }],
      next_offset: null, tracks: [], total: 2 };

    run(`spPointerDown("u1", 10, 10, 31)`);
    await run(`spPointerUp(10, 10, 31)`);
    await tick();
    check("SPG a tap opens the player and marks the body as previewing",
          $$("preview-pop").hidden === false && run(`document.body.classList.contains("previewing")`) === true, "");
    const ghost = dispatchClick($$("pv-close"));
    check("SPG the tap's own synthesized click, landing on Close at once, is swallowed",
          $$("preview-pop").hidden === false && ghost.defaultPrevented === true, `hidden=${$$("preview-pop").hidden}`);
    await wait(450);
    dispatchClick($$("pv-close"));
    check("SPG a real tap on Close after the window closes the player",
          $$("preview-pop").hidden === true && run(`document.body.classList.contains("previewing")`) === false, "");
    check("SPG ...and the swallower is gone, not left on the document",
          (handlers.click || []).every((h) => !captureFns.has(h)), `${[...captureFns].length} capture listener(s)`);

    // A click that never comes still lets the swallower go on its timeout.
    run(`spPointerDown("u1", 10, 10, 32)`);
    await run(`spPointerUp(10, 10, 32)`);
    await tick();
    check("SPG while the window is open one capture listener waits", [...captureFns].length === 1, `${[...captureFns].length}`);
    await wait(450);
    check("SPG ...and the timeout removes it", [...captureFns].length === 0, `${[...captureFns].length}`);

    // A drag is not a tap: no click to swallow, nothing installed.
    run(`previewHold.stop(true)`);
    run(`spPointerDown("u1", 10, 10, 33); spPointerMove(10, 60, 33)`);
    await run(`spPointerUp(10, 60, 33)`);
    check("SPG a drag installs no swallower", [...captureFns].length === 0, `${[...captureFns].length}`);
  } finally {
    run(`spStopPolling(); previewHold.stop(true); nowState = ${savedNow}; show("lists")`);
  }
}

// ============================================================================
// SPM — 2026-10-21, from the phone: a pick with the preview player open never
// posted (the player sits above the picker), nothing said a move was under
// way, and the icon is the Now card's magnifier, opening ready to type.
// ============================================================================
{
  const savedNow = run("JSON.stringify(nowState)");
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    const songs = { u1: { name: "Ronda", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                    u3: { name: "Three", artists: "C", local: false } };
    const D = (parts, extra = {}) => ({ playlist_id: "H9", name: "Big Home", snapshot_id: "s1", n: 2, parts,
      unknown: [], hint: null, scores: {}, songs, done: null, edited: false, ...extra });
    const two = () => [{ key: "p1", name: "rock", uris: ["u1", "u2"] }, { key: "p2", name: "jazz", uris: ["u3"] }];
    run(`show("split"); nowState = { is_playing: false }; previewHold.stop(true); $("picker").hidden = true; spTargets = null`);
    run(`spDraft = ${JSON.stringify(D(two()))}; spPid = "H9"; spShown = "p1"; spSection("sp-work"); renderSpParts()`);
    const press = (uri, onIcon, id) => run(`spListPointerDown({
      target: { closest: (sel) => sel === ".sp-song" ? { dataset: { uri: ${JSON.stringify(uri)} } }
                                : sel === ".sp-elsewhere" ? (${onIcon} ? {} : null) : null },
      clientX: 10, clientY: 10, pointerId: ${id}, preventDefault() {} })`);
    const listHtml = () => $$("sp-list").innerHTML;
    check("SPM the row's icon is the Now card's magnifier",
          listHtml().includes(run("ICON_SEARCH_SM")) && !/ICON_ELSEWHERE/.test(listHtml()), listHtml().slice(0, 300));

    routes["GET /api/splitparts/H9/targets"] = { targets: [
      { id: "T1", name: "Throttle Back", folder: "ROOT" }, { id: "A1", name: "alpha", folder: null }] };
    routes["GET /api/splitparts/H9/preview?key=p1&start=u1"] = {
      clips: [{ uri: "u1", name: "Ronda", artists: "A", url: "/c1.mp3" }], next_offset: null,
      tracks: [{ uri: "u1", name: "Ronda", artists: "A" }], total: 2 };
    // 1: open the player on a song, then tap that song's icon
    press("u1", false, 31);
    await run(`spPointerUp(10, 10, 31)`);
    await tick(); await tick();
    const playerWasOpen = $$("preview-pop").hidden === false;
    run(`$("picker-filter").focused = 0; $("picker-filter").focus = function () { this.focused++; };`);
    press("u1", true, 32);
    await run(`spPointerUp(10, 10, 32)`);
    await tick();
    check("SPM a tap on the icon closes the open player before the picker shows",
          playerWasOpen && $$("preview-pop").hidden === true && $$("picker").hidden === false,
          `wasOpen=${playerWasOpen} pop=${$$("preview-pop").hidden} picker=${$$("picker").hidden}`);
    check("SPM ...and opens it ready to type, like the Now card's magnifier",
          run(`$("picker-filter").focused`) === 1, `focused=${run(`$("picker-filter").focused`)}`);

    // 2: a pick says "moving…" before the answer, then "moved to …"
    // The toast is read before any await can settle: the pick's own task.
    routes["POST /api/splitparts/H9/elsewhere"] = { ...D([{ key: "p1", name: "rock", uris: ["u2"] },
      { key: "p2", name: "jazz", uris: ["u3"] }], { edited: true }), moved_to: { id: "T1", name: "Throttle Back" } };
    const pick = $$("picker-list").children.find((r) => /Throttle Back/.test(r.innerHTML));
    pick.onclick();
    const early = $$("toast").textContent;
    await tick(); await tick();
    check("SPM a pick says it is moving at once, by the home's name",
          early === "moving to Throttle Back…", early);
    check("SPM ...then posts it and says where it went",
          bodies("/api/splitparts/H9/elsewhere").filter(Boolean).length === 1 &&
          $$("toast").textContent === "moved to Throttle Back" && !/data-uri="u1"/.test(listHtml()),
          $$("toast").textContent);

    // 3: the targets are kept for the split — a second tap asks again for nothing
    resetLog();
    press("u2", true, 33);
    await run(`spPointerUp(10, 10, 33)`);
    check("SPM a second tap opens the picker at once, without asking for the targets again",
          $$("picker").hidden === false && gets("/api/splitparts/H9/targets") === 0,
          `picker=${$$("picker").hidden} gets=${gets("/api/splitparts/H9/targets")}`);
  } finally {
    run(`spStopPolling(); previewHold.stop(true); $("picker").hidden = true; spTargets = null; nowState = ${savedNow}; show("lists")`);
  }
}

// ============================================================================
// SPH — 2026-10-21: the song the preview player is on is marked in the list,
// moves with "next", and the mark goes when the player closes.
// ============================================================================
{
  const savedNow = run("JSON.stringify(nowState)");
  try {
    resetLog();
    AudioStub.made.length = 0; AudioStub.refuse = false;
    const songs = { u1: { name: "One", artists: "A", local: false }, u2: { name: "Two", artists: "B", local: false },
                    u3: { name: "Three", artists: "C", local: false } };
    const D = { playlist_id: "H7", name: "Big Home", snapshot_id: "s1", n: 2,
      parts: [{ key: "p1", name: "rock", uris: ["u1", "u2"] }, { key: "p2", name: "jazz", uris: ["u3"] }],
      unknown: [], hint: null, scores: {}, songs, done: null, edited: false };
    run(`show("split"); nowState = { is_playing: false }; previewHold.stop(true)`);
    run(`spDraft = ${JSON.stringify(D)}; spPid = "H7"; spShown = "p1"; spSection("sp-work"); renderSpParts()`);
    const clip = (u, n) => ({ name: songs[u].name, artist: songs[u].artists, url: `https://cdn/sph-${u}-${n}.mp3`, uri: u, deezer_id: n });
    routes["GET /api/splitparts/H7/preview?key=p1&start=u1"] = { clips: [clip("u1", 1), clip("u2", 2)], next_offset: null, tracks: [], total: 2 };
    // The row the app re-classes in place: the stub hands back one element
    // per selector, the same one each time, as a real DOM would for a row.
    const row = (u) => $$("sp-list").querySelector(`.sp-song[data-uri="${u}"]`);
    const marked = (u) => row(u).classList.contains("playing");

    run(`spPointerDown("u1", 10, 10, 41)`);
    await run(`spPointerUp(10, 10, 41)`);
    await tick(); await tick();
    check("SPH the song the player is on is marked in the list",
          marked("u1") && !marked("u2") && run("spPlaying") === "u1", `spPlaying=${run("spPlaying")}`);
    run(`$("pv-next").onclick()`);
    await tick();
    check("SPH ...the mark moves with next",
          marked("u2") && !marked("u1") && run("spPlaying") === "u2", `spPlaying=${run("spPlaying")}`);
    run(`renderSpList()`);
    check("SPH ...and a redraw of the list keeps it on the right row",
          /class="sp-song playing" data-uri="u2"/.test($$("sp-list").innerHTML) &&
          ($$("sp-list").innerHTML.match(/sp-song playing/g) || []).length === 1, $$("sp-list").innerHTML.slice(0, 200));
    run(`previewHold.stop(true)`);
    check("SPH ...and goes when the player closes",
          run("spPlaying") === null && !marked("u2"), `spPlaying=${run("spPlaying")}`);
  } finally {
    run(`spStopPolling(); previewHold.stop(true); nowState = ${savedNow}; show("lists")`);
  }
}

// ---- summary ---------------------------------------------------------------
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
