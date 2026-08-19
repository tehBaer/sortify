"use strict";

const $ = (id) => document.getElementById(id);
const views = ["setup", "lists", "triage", "now", "split"];

let statusData = null;
let playlistData = [];   // lists view
let roles = {};          // id -> "input" | "home" | null
let hintTexts = {};      // id -> "ambient, piano" — per-home matching hints
let triage = null;       // {id, name, homes:Map, tracks, idx, sorted, skipped, history}
let split = null;        // {id, name, piles, decided, active_sitting} — the open split view
// {splitId, sittingId, pileId, pileName, uris, decided} — a UI convenience,
// NOT the source of truth. The Split view's own display reads split.active_sitting
// directly (fresh on every open); the Now view's decide card reads
// nowState.sitting (fresh on every poll, from the server). This global only
// drives the persistent "a sitting is active" bar when neither of those is
// in view, and remembers which split/pile to finish from that bar. Because
// the backend allows one active sitting *per split*, this single pointer can
// only ever reflect one of them at a time if more than one is live — see the
// report.
let sitting = null;

// ---- plumbing --------------------------------------------------------------

async function api(path, body, method) {
  // `method` is the escape hatch for verbs that carry no body — DELETE
  // (cancelQueue) — everything else still just says "POST if there's a
  // body, GET otherwise" the way every existing call site already relies on.
  const opts = (body || method)
    ? { method: method || "POST", headers: { "Content-Type": "application/json" },
        ...(body ? { body: JSON.stringify(body) } : {}) }
    : {};
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (_) {}
  if (resp.status === 401 && data.needs_auth) { show("setup"); throw new Error("auth needed"); }
  if (!resp.ok) {
    // .status lets a caller tell "not split yet" (404) apart from a
    // transient failure worth retrying instead of re-offering a paid action.
    const err = new Error(data.detail || `${resp.status} error`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function show(view) {
  for (const v of views) $("view-" + v).hidden = v !== view;
  // Triage and split are reached from (and exit back to) the Playlists view,
  // so they keep that link lit rather than leaving the nav dark.
  $("nav-now").classList.toggle("active", view === "now");
  $("nav-lists").classList.toggle("active", view === "lists" || view === "triage" || view === "split");
  $("nav-reconnect").classList.toggle("active", view === "setup");
}

let toastTimer = null;
function toast(msg, ms = 2600) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

function esc(s) {
  return (s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- boot & setup ----------------------------------------------------------

async function boot() {
  statusData = await api("/api/status");
  $("whoami").textContent = statusData.me ? statusData.me.name : "";
  if (!statusData.authed) { show("setup"); return; }
  showNow();
}

$("btn-auth-start").onclick = async () => {
  // Blank is allowed when reconnecting — the server falls back to the stored
  // Client ID and answers with a readable 400 if there isn't one.
  const clientId = $("client-id").value.trim();
  try {
    const { auth_url } = await api("/api/auth/start", { client_id: clientId });
    const a = $("auth-link");
    a.href = auth_url;
    $("step-authlink").hidden = false;
    a.scrollIntoView({ behavior: "smooth" });
  } catch (e) { toast(e.message); }
};

$("btn-auth-finish").onclick = async () => {
  try {
    const { me } = await api("/api/auth/finish", { redirect_url: $("redirect-url").value });
    toast(`hello, ${me.name}`);
    await boot();
  } catch (e) { toast(e.message); }
};

// ---- playlist roles --------------------------------------------------------

function ageText(fetchedAt) {
  if (!fetchedAt) return "";
  const mins = Math.max(0, Math.round((Date.now() / 1000 - fetchedAt) / 60));
  if (mins < 60) return `list read ${mins} min ago`;
  const hrs = Math.round(mins / 60);
  return hrs < 48 ? `list read ${hrs}h ago` : `list read ${Math.round(hrs / 24)} days ago`;
}

async function loadLists() {
  show("lists");
  $("playlists").innerHTML = '<p class="hint">Loading playlists…</p>';
  try {
    const data = await api("/api/playlists");
    playlistData = data.playlists;
    roles = Object.fromEntries(playlistData.map((p) => [p.id, p.role]));
    hintTexts = Object.fromEntries(
      playlistData.filter((p) => p.hints).map((p) => [p.id, p.hints]));
    // The list is cached until refreshed by hand, so say how old it is rather
    // than present a stale list as current.
    $("pl-age").textContent = ageText(data.fetched_at);
    renderOrphans(data.sitting_orphans || []);
    renderLists();
  } catch (e) {
    if (e.message === "auth needed") return;
    $("playlists").innerHTML =
      `<p class="hint">Couldn't load playlists: ${esc(e.message)}</p>
       <button id="btn-retry-lists">Retry</button>`;
    $("btn-retry-lists").onclick = loadLists;
  }
}

// A non-owned playlist can never be split, full stop: the Feb-2026 dev-mode
// API 403s reading /playlists/{id}/items for anything this account doesn't
// own (on the read's first page — the wasted call is one, not the whole
// paginated read), so clicking through here would pay a call to discover
// what `editable` (already in the cached listing, zero calls) says for
// free — see create_split's pre-flight guard, which refuses the same thing
// server-side. A pure function so the gating logic itself is unit-testable
// without the DOM (see ui_harness.mjs).
function splitDisabledReason(p) {
  return p.editable ? null : "Not yours to split — make your own copy in Spotify first, then split that copy";
}

function renderLists() {
  const wrap = $("playlists");
  wrap.innerHTML = "";
  const q = $("pl-filter").value.trim().toLowerCase();
  const marked = playlistData.filter((p) => roles[p.id] || p.id === "liked");
  const rest = playlistData.filter((p) => !roles[p.id] && p.id !== "liked");
  let shown = [...marked, ...rest];
  if (q) shown = shown.filter((p) => (p.name + " " + (p.folder || "")).toLowerCase().includes(q));
  const CAP = 200;
  const overflow = Math.max(shown.length - CAP, 0);
  for (const p of shown.slice(0, CAP)) {
    const row = document.createElement("div");
    row.className = "pl-row";
    const img = p.image
      ? `<img src="${esc(p.image)}" alt="" loading="lazy">`
      : '<div class="noimg"></div>';
    const splitNote = p.split ? `split into ${p.split.piles} pile${p.split.piles === 1 ? "" : "s"}, ${p.split.remaining} left` : null;
    const sub = [p.folder, p.total != null ? `${p.total} tracks` : null, p.editable ? null : p.id === "liked" ? "library" : "not yours", splitNote]
      .filter(Boolean).join(" · ");
    row.innerHTML = `${img}
      <div class="pl-meta"><div class="name">${esc(p.name)}</div><div class="sub">${esc(sub)}</div></div>
      <div class="pl-roles">
        <button class="chip r-input">In</button>
        <button class="chip r-home">Home</button>
        <button class="pl-sort" title="Sort this input">▶</button>
        <button class="pl-split" title="Split into piles">⑃</button>
      </div>
      <input class="pl-hints" placeholder="matching hints, e.g. ambient, piano"
             title="Your own words about what belongs here — they join this home's tag profile (docs/matching.md)">`;
    const [bIn, bHome, bSort, bSplit] = row.querySelectorAll("button");
    const hintsEl = row.querySelector(".pl-hints");
    hintsEl.value = hintTexts[p.id] || "";
    hintsEl.oninput = () => {
      if (hintsEl.value.trim()) hintTexts[p.id] = hintsEl.value;
      else delete hintTexts[p.id];
    };
    const paint = () => {
      bIn.classList.toggle("on-input", roles[p.id] === "input");
      bHome.classList.toggle("on-home", roles[p.id] === "home");
      bHome.hidden = p.id === "liked" || !p.editable;
      bSort.hidden = roles[p.id] !== "input";
      // The hints field only makes sense for a home — it feeds that home's
      // matching profile. Kept visible if it has leftover text so the user
      // can still see (and clear) hints on a demoted playlist.
      hintsEl.hidden = roles[p.id] !== "home" && !hintsEl.value.trim();
      // Splitting only earns its keep on a playlist too long to work through
      // as-is — a 30-track input doesn't need piles.
      bSplit.hidden = p.id === "liked" || (p.total ?? 0) < 100;
      // Disabled rather than hidden when it's not owned: hiding leaves a
      // 1372-track playlist with no visible split button and no
      // explanation, where disabling teaches the actual fix on hover
      // instead of just hiding the dead end.
      const reason = splitDisabledReason(p);
      bSplit.disabled = !!reason;
      bSplit.title = reason || "Split into piles";
    };
    bIn.onclick = () => { roles[p.id] = roles[p.id] === "input" ? null : "input"; paint(); };
    bHome.onclick = () => { roles[p.id] = roles[p.id] === "home" ? null : "home"; paint(); };
    bSort.onclick = () => { saveConfig().then(() => startTriage(p.id, p.name)); };
    bSplit.onclick = () => openSplit(p.id, p.name);
    paint();
    wrap.appendChild(row);
  }
  if (overflow) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = `…and ${overflow} more — type in the filter to find them.`;
    wrap.appendChild(p);
  }
}

$("pl-filter").oninput = renderLists;

// Leftover sitting playlists: ones sortify created and never managed to
// remove — a lost response to the create call, or a crash before the record
// was saved. They are found by reading the cached listing (zero Spotify
// calls) and matching the marker every sitting playlist carries, so they can
// only appear for a listing the user has actually refreshed. That is why this
// lives next to Refresh: the button that reveals them is right here.
let orphanCount = 0;
function renderOrphans(orphans) {
  orphanCount = orphans.length;
  const bar = $("pl-orphan-bar");
  bar.hidden = orphanCount === 0;
  if (!orphanCount) return;
  const names = orphans.slice(0, 3).map((o) => o.name).join(", ");
  $("pl-orphan-status").textContent =
    `${orphanCount} leftover sitting playlist${orphanCount === 1 ? "" : "s"} — ` +
    `${names}${orphanCount > 3 ? "…" : ""}`;
  // The price is on the button, the same way every other spending control in
  // this app states its cost before it is pressed.
  $("btn-clean-sittings").textContent =
    `Remove (${Math.min(orphanCount, 10)} call${Math.min(orphanCount, 10) === 1 ? "" : "s"})`;
}

$("btn-clean-sittings").onclick = async () => {
  const btn = $("btn-clean-sittings");
  btn.disabled = true;
  try {
    const res = await api("/api/sittings/cleanup", {});
    if (res.deferred) {
      toast("a sitting is starting right now — try again once it has");
    } else {
      const n = res.removed.length;
      toast(n
        ? `removed ${n} leftover playlist${n === 1 ? "" : "s"}` +
          (res.remaining ? ` — ${res.remaining} still to go, press Remove again` : "")
        : "nothing could be removed — try again in a moment");
    }
    await loadLists();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
};

// The only thing that re-reads the listing from Spotify. It is slow on
// purpose — ~21 paginated calls paced by the rolling-window throttle — so the
// button says so and stays disabled until it lands.
$("btn-refresh-lists").onclick = async () => {
  const btn = $("btn-refresh-lists");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  $("pl-age").textContent = "re-reading from Spotify — this takes about a minute";
  try {
    const res = await api("/api/refresh", {});
    await loadLists();
    toast(`refreshed — ${res.calls_spent} Spotify calls spent`, 4000);
  } catch (e) {
    toast(e.message);
    $("pl-age").textContent = "";
  } finally {
    btn.disabled = false;
    btn.textContent = "Refresh";
  }
};

async function saveConfig() {
  const input_ids = Object.keys(roles).filter((k) => roles[k] === "input");
  const home_ids = Object.keys(roles).filter((k) => roles[k] === "home");
  await api("/api/config", { input_ids, home_ids, home_hints: hintTexts });
}

$("btn-save-config").onclick = async () => {
  try { await saveConfig(); toast("saved"); } catch (e) { toast(e.message); }
};

// ---- triage ----------------------------------------------------------------

async function startTriage(id, name) {
  show("triage");
  $("card").innerHTML = "";
  $("triage-progress").textContent = name;
  $("loading-msg").textContent =
    "Building playlist profiles… the first run fetches every home playlist and every artist " +
    "(slowly on purpose — Spotify's rate cooldowns are brutal), so give it up to ~10 minutes. " +
    "After that it's cached and quick.";
  $("triage-loading").hidden = false;
  try {
    const data = await api(`/api/triage/${id}`);
    triage = {
      id, name: data.playlist.name,
      homes: new Map(data.homes.map((h) => [h.id, h])),
      tracks: data.tracks, idx: 0, sorted: 0, skipped: 0, history: [],
    };
    $("triage-loading").hidden = true;
    renderCard();
  } catch (e) {
    $("triage-loading").hidden = true;
    if (e.message !== "auth needed") {
      $("card").innerHTML = `<div class="done-msg"><p>${esc(e.message)}</p></div>`;
    }
  }
}

function renderCard() {
  const t = triage;
  $("btn-undo").disabled = t.history.length === 0;
  if (t.idx >= t.tracks.length) {
    $("triage-progress").textContent = t.name;
    $("card").innerHTML = `<div class="done-msg">
      <p><b>${esc(t.name)}</b> is triaged 🎉</p>
      <p>${t.sorted} sorted · ${t.skipped} skipped</p></div>`;
    return;
  }
  const tr = t.tracks[t.idx];
  $("triage-progress").textContent = `${t.idx + 1} / ${t.tracks.length}`;
  const img = tr.image ? `<img src="${esc(tr.image)}" alt="">` : '<div class="noimg"></div>';
  const artists = tr.artists.map((a) => a.name).join(", ");

  let suggHtml = "";
  tr.suggestions.forEach((s, i) => {
    const home = t.homes.get(s.playlist_id);
    if (!home) return;
    suggHtml += `<button class="sugg${s.already ? " already" : ""}" data-to="${esc(s.playlist_id)}">
      <span class="s-pct">${s.already ? "" : s.pct + "%"}</span>
      <span class="s-name"><kbd>${i + 1}</kbd> ${esc(home.name)}</span>
      <span class="s-why">${esc([home.folder, ...s.reasons].filter(Boolean).join(" · "))}</span>
    </button>`;
  });
  if (!tr.suggestions.length) {
    suggHtml = `<p class="hint">${tr.sortable ? "No confident match — pick one:" : "Can't be sorted via the API (local file or episode) — remove or skip."}</p>`;
  }

  $("card").innerHTML = `<div class="track-card">
    ${img}
    <div class="t-name">${esc(tr.name)}</div>
    <div class="t-artist">${esc(artists)}${tr.album ? " — " + esc(tr.album) : ""}</div>
    ${suggHtml}
    <div class="minor-actions">
      ${tr.sortable ? `<button id="btn-more"><kbd>m</kbd> More…</button>` : ""}
      <button id="btn-remove" class="danger"><kbd>r</kbd> Remove only</button>
      <button id="btn-skip"><kbd>s</kbd> Skip</button>
    </div>
  </div>`;

  $("card").querySelectorAll(".sugg").forEach((b) => {
    b.onclick = () => moveTo(b.dataset.to);
  });
  const more = $("btn-more");
  if (more) more.onclick = () => openPicker(triage.homes, moveTo);
  $("btn-remove").onclick = removeOnly;
  $("btn-skip").onclick = () => { triage.skipped++; triage.idx++; renderCard(); };
}

async function moveTo(toId) {
  const t = triage, tr = t.tracks[t.idx];
  try {
    const res = await api("/api/act", { action: "move", uri: tr.uri, from_id: t.id, to_id: toId });
    t.history.push({ track: tr, idx: t.idx });
    t.sorted++;
    toast(res.note || `→ ${t.homes.get(toId)?.name ?? "moved"}`);
    t.tracks.splice(t.idx, 1);
    renderCard();
  } catch (e) { toast(e.message); }
}

async function removeOnly() {
  const t = triage, tr = t.tracks[t.idx];
  try {
    await api("/api/act", { action: "remove", uri: tr.uri, from_id: t.id });
    t.history.push({ track: tr, idx: t.idx });
    t.sorted++;
    toast("removed from input");
    t.tracks.splice(t.idx, 1);
    renderCard();
  } catch (e) { toast(e.message); }
}

$("btn-undo").onclick = async () => {
  const t = triage;
  if (!t || !t.history.length) return;
  try {
    await api("/api/undo");
    const last = t.history.pop();
    t.tracks.splice(Math.min(last.idx, t.tracks.length), 0, last.track);
    t.idx = Math.min(last.idx, t.tracks.length - 1);
    t.sorted--;
    toast("undone");
    renderCard();
  } catch (e) { toast(e.message); }
};

$("btn-back").onclick = () => { triage = null; loadLists(); };
$("home-link").onclick = () => { triage = null; boot(); };
// The setup view used to be reachable only when logged out, which meant the
// one person who needed it — someone already logged in with a token issued
// before a scope was added — was the one person who could not get to it.
$("nav-reconnect").onclick = () => {
  stopNowPolling();
  triage = null;
  $("reconnect-hint").hidden = !statusData?.authed;
  $("client-id").value = "";
  show("setup");
};

$("nav-now").onclick = showNow;
$("nav-lists").onclick = () => { stopNowPolling(); stopNowTicker(); triage = null; loadLists(); };

// ---- now playing -----------------------------------------------------------

let nowState = null;   // last /api/now payload + client flags
// True exactly when the card on screen is an error/cooldown message rather
// than a live track. `nowState` is deliberately NOT cleared on a failed poll
// (the sitting bar and the toasts still want the last known truth), so it
// alone cannot answer "is what the user is looking at current?" — and the
// keyboard must not fire an irreversible keep against a track the screen
// stopped showing. A keep spends a call and removes the track from every
// future sitting; unlike /api/act there is no undo.
let nowProblem = false;
let nowTimer = null;
let nowActions = 0;    // enables Undo
let filedUris = {};    // uri -> home name we filed it to this session

function stopNowPolling() { clearTimeout(nowTimer); nowTimer = null; }

// Manual mode ("data saver"): no automatic polling at all — the refresh
// button (and opening the view) are the only fetches. Client-side by
// design: the server's pacing already guarantees auto mode costs ~1 call
// per track, so this is for people who want zero background traffic, not a
// budget-safety mechanism. Persisted per browser.
let nowManual = false;
try { nowManual = localStorage.getItem("sortify-manual") === "1"; } catch (_) {}

function paintManualChip() {
  const b = $("btn-now-manual");
  b.textContent = nowManual ? "manual" : "auto";
  b.classList.toggle("on", nowManual);
  b.title = nowManual
    ? "Manual: nothing is fetched until you press refresh. Tap to switch back to auto."
    : "Auto: the server paces polling (~1 request per track). Tap for manual mode.";
}

$("btn-now-manual").onclick = () => {
  nowManual = !nowManual;
  try { localStorage.setItem("sortify-manual", nowManual ? "1" : "0"); } catch (_) {}
  paintManualChip();
  // Back to auto = "show me current truth, then resume the server's pace".
  if (nowManual) stopNowPolling(); else pollNow(true);
};

$("btn-now-refresh").onclick = async () => {
  const b = $("btn-now-refresh");
  b.disabled = true;
  b.classList.add("spinning");
  try { await pollNow(true); } finally { b.disabled = false; b.classList.remove("spinning"); }
};

function showNow() {
  show("now");
  stopNowPolling();
  pollNow(true);  // opening the view is a request for current truth
}

// The server sets the pace: it knows when the playing track ends, which is the
// only moment the answer can change by itself. Choosing an interval here too
// is what used to make every poll a guaranteed cache miss.
function scheduleNext(ms) {
  stopNowPolling();
  if (nowManual) return;  // manual mode: the refresh button is the schedule
  nowTimer = setTimeout(() => pollNow(), ms);
}

async function pollNow(force = false) {
  if ($("view-now").hidden) return;
  if (document.hidden) { scheduleNext(15000); return; }
  try {
    const data = await api("/api/now" + (force ? "?force=1" : ""));
    nowState = { ...data, homes: new Map((data.homes || []).map((h) => [h.id, h])) };
    renderNow();
    scheduleNext(data.poll_after_ms || 60000);
  } catch (e) {
    if (e.message === "auth needed") { stopNowPolling(); return; }
    renderNowProblem(e.message);
    scheduleNext(90000);
  }
}

// A sitting stays visible here no matter what's playing or whether Spotify
// itself is in a cooldown — it costs a real Spotify call to finish, so its
// status must never depend on the currently-playing poll succeeding. Prefers
// the server's own answer (nowState.sitting, fresh every poll) when there is
// one; falls back to the last-known client copy otherwise — e.g. while the
// user is listening to something else and the poll no longer reports it.
function paintSittingBar() {
  const bar = $("now-sitting-bar");
  const live = nowState?.sitting;
  const s = live
    ? { pileName: live.pile_name, uris: live.uris, decided: live.decided }
    : sitting;
  if (!s) { bar.hidden = true; return; }
  const uris = s.uris || [];
  const left = uris.filter((u) => !s.decided[u]).length;
  // A reservation whose create_playlist failed carries no tracks at all —
  // "0 of 0 left" would read as "done", so say what it actually is. Either
  // way the Finish button beside this text is the escape.
  $("now-sitting-status").innerHTML = uris.length
    ? `Sitting: <b>${esc(s.pileName)}</b> · ${left} of ${uris.length} left
       <span class="sb-bar"><span class="sb-fill" style="width:${Math.round(((uris.length - left) / uris.length) * 100)}%"></span></span>`
    : `Sitting: <b>${esc(s.pileName)}</b> — reserved, no tracks added (finish it to release the split)`;
  bar.hidden = false;
}
$("btn-now-finish-sitting").onclick = () => finishSitting(nowState?.sitting?.split_id || sitting?.splitId);

// Cooldown countdown: ticks locally every second between polls — display
// only, no extra requests of any kind. The total sizes the progress bar and
// survives re-polls of the same cooldown (the server re-reports shrinking
// minutes every ~90s; treat anything within tolerance as the same event).
let nowCooldownTimer = null;
let nowCooldownUntil = 0;   // epoch ms
let nowCooldownTotal = 0;   // ms at first sighting

function stopCooldownTicker() { clearInterval(nowCooldownTimer); nowCooldownTimer = null; }

function fmtCountdown(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const pad = (n) => String(n).padStart(2, "0");
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${pad(m)}m ${pad(sec)}s` : m ? `${m}m ${pad(sec)}s` : `${sec}s`;
}

function renderNowProblem(msg) {
  nowProblem = true;
  paintSittingBar();
  $("now-context").textContent = "";
  $("now-controls").hidden = true;
  stopCooldownTicker();
  stopNowTicker();
  const cd = msg.match(/cooldown — try again in ~(\d+) min/);
  if (!cd) {
    nowCooldownUntil = nowCooldownTotal = 0;
    $("now-card").innerHTML = `<p class="done-msg">${esc(msg)}</p>`;
    return;
  }
  // One clock read for the whole first paint. Deriving `until` from Date.now()
  // and then formatting `until - Date.now()` read the clock twice, so the
  // opening frame depended on how many milliseconds fell between the two: on a
  // cooldown that is a whole number of minutes — every cooldown the server
  // reports — a single millisecond rendered "2h 57m 59s" for what had just
  // been computed as 2h 58m. The ticker below still reads the clock live,
  // which is correct; it is only this first frame that must agree with the
  // deadline it was derived from.
  const now = Date.now();
  const until = now + Number(cd[1]) * 60000;
  if (Math.abs(until - nowCooldownUntil) > 120000) nowCooldownTotal = until - now;
  nowCooldownUntil = until;
  const at = new Date(until);
  const pad = (n) => String(n).padStart(2, "0");
  const pct = () => Math.max(0, Math.min(100,
    Math.round(((nowCooldownTotal - (until - Date.now())) / (nowCooldownTotal || 1)) * 100)));
  $("now-card").innerHTML = `<div class="cooldown-card">
    <div class="cb-head"><span class="cb-title">Spotify has rate-limited the app</span><span class="cb-chip">paused</span></div>
    <div class="cb-time" id="cb-time">${fmtCountdown(until - now)}</div>
    <div class="cb-at">back around <b>${pad(at.getHours())}:${pad(at.getMinutes())}</b> — nothing to do until then;
      your music and playlists are unaffected</div>
    <div class="cb-bar"><div class="cb-fill" id="cb-fill" style="width:${pct()}%"></div></div>
  </div>`;
  nowCooldownTimer = setInterval(() => {
    const rem = until - Date.now();
    if (rem <= 0) {
      stopCooldownTicker();
      $("cb-time").textContent = "any moment now";
      $("cb-fill").style.width = "100%";
      return;  // the normal poll schedule repaints with the real answer
    }
    $("cb-time").textContent = fmtCountdown(rem);
    $("cb-fill").style.width = pct() + "%";
  }, 1000);
}

// The input switcher used to hide whenever nothing was playing — which is
// exactly the moment "start an input" is the one useful action on the page.
// It now stays up as the empty state's call to action; only the error-ish
// states (cooldown, reauth) hide it, since starting playback there would
// just bounce off the same wall.
function paintNowControls(d) {
  const playable = (d.inputs || []).filter((l) => l.id !== "liked");
  if (!playable.length || d.needs_reauth || d.cooldown) { $("now-controls").hidden = true; return; }
  const sel = $("now-input-switch");
  const current = d.playing ? d.context?.id : null;
  sel.innerHTML =
    `<option value="">${d.playing ? "— switch to another input —" : "▶ start an input…"}</option>` +
    playable.map((l) =>
      `<option value="${esc(l.id)}"${l.id === current ? " selected" : ""}>${esc(l.name)}</option>`
    ).join("");
  $("now-controls").hidden = false;
}

// Spotify needs a moment to settle after a skip before it reports the new
// track; the server has already dropped its cached answer, so this poll is
// guaranteed to go upstream rather than repeat what we just replaced.
function repollAfterPlaybackChange() {
  setTimeout(() => pollNow(true), 900);
}

// Card-internal control (re-created by every renderNow), so it's wired per
// render rather than once at load like the static controls below.
async function playerNext() {
  const btn = $("btn-now-next");
  if (btn) btn.disabled = true;
  try {
    await api("/api/player/next", {});
    repollAfterPlaybackChange();
  } catch (e) {
    toast(e.message);
  } finally {
    const b = $("btn-now-next");
    if (b) b.disabled = false;
  }
}

// Optimistic flip, deliberately without a forced repoll: pausing doesn't
// change the track, the server has already dropped its now-cache (see
// _playback_call), and the next scheduled poll fetches the truth anyway —
// so a repoll here would spend one extra call per press to learn what we
// just did ourselves. The displayed progress is frozen at its current
// ticked value on pause so resume continues from what the user sees.
async function playerToggle() {
  const d = nowState;
  if (!d?.playing) return;
  const wasPlaying = d.is_playing;
  try {
    await api(wasPlaying ? "/api/player/pause" : "/api/player/resume", {});
    if (wasPlaying && nowTickBase && d.track?.duration_ms) {
      d.progress_ms = Math.min(
        d.track.duration_ms, nowTickBase.p0 + (Date.now() - nowTickBase.t0));
    }
    d.is_playing = !wasPlaying;
    renderNow();
  } catch (e) { toast(e.message); }
}

// Progress ticker: display only, between polls, zero requests. One global
// interval; renderNow restarts it for each card and every non-track state
// stops it.
let nowTickTimer = null;
let nowTickBase = null;  // {t0: Date.now() at poll, p0: progress_ms then}

function stopNowTicker() { clearInterval(nowTickTimer); nowTickTimer = null; nowTickBase = null; }

function fmtTime(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function startNowTicker(d, tr) {
  stopNowTicker();
  if (!tr.duration_ms) return;
  nowTickBase = { t0: Date.now(), p0: d.progress_ms || 0 };
  if (!d.is_playing) return;  // base still recorded so pause math works
  nowTickTimer = setInterval(() => {
    const fill = $("np-fill"), elapsed = $("np-elapsed");
    if (!fill || !elapsed) { stopNowTicker(); return; }
    const p = Math.min(tr.duration_ms, nowTickBase.p0 + (Date.now() - nowTickBase.t0));
    elapsed.textContent = fmtTime(p);
    fill.style.width = ((p / tr.duration_ms) * 100).toFixed(2) + "%";
    // At track end just stop — the poll schedule (whose TTL is exactly the
    // track's remaining runtime) repaints with the real next track.
    if (p >= tr.duration_ms) stopNowTicker();
  }, 1000);
}

const ICON_PLAY = '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M7 5h3.6v14H7zM13.4 5H17v14h-3.6z"/></svg>';
const ICON_NEXT = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><path d="M6 5.5v13l8.5-6.5zM16.5 5.5h2v13h-2z"/></svg>';

// The strip under the title: elapsed / bar / total, then play-pause + next.
// Local files and episodes carry no duration; they get the buttons only.
function playbackStrip(d, tr) {
  let bar = "";
  if (tr.duration_ms) {
    const pct = Math.min(100, ((d.progress_ms || 0) / tr.duration_ms) * 100).toFixed(2);
    bar = `<div class="np-progress">
      <span id="np-elapsed" class="np-time">${fmtTime(d.progress_ms || 0)}</span>
      <span class="np-bar"><span id="np-fill" style="width:${pct}%"></span></span>
      <span class="np-time">${fmtTime(tr.duration_ms)}</span>
    </div>`;
  }
  return `${bar}<div class="np-buttons">
    <button id="btn-now-toggle" class="np-round" title="${d.is_playing ? "Pause" : "Play"}">${d.is_playing ? ICON_PAUSE : ICON_PLAY}</button>
    <button id="btn-now-next" class="np-round np-small" title="Skip to the next track">${ICON_NEXT}</button>
  </div>`;
}

$("now-input-switch").onchange = async (e) => {
  const id = e.target.value;
  if (!id) return;
  try {
    await api("/api/player/play", { input_id: id });
    toast("starting…");
    repollAfterPlaybackChange();
  } catch (err) {
    toast(err.message);
    e.target.value = nowState?.context?.id || "";
  }
};

// Keeps the client-side `sitting` convenience global roughly in step with
// what the server just confirmed, so the persistent bar/Finish button have
// something recent to show even after the user tabs away from the sitting's
// track. Not load-bearing for correctness — decide() calls always go through
// nowState.sitting directly while it's available (see decideKeep etc.).
function syncSittingFromNow(d) {
  if (!d.sitting) return;
  sitting = {
    splitId: d.sitting.split_id, sittingId: d.context?.id, pileId: d.sitting.pile_id,
    pileName: d.sitting.pile_name, uris: d.sitting.uris, decided: { ...d.sitting.decided },
  };
}

function renderNow() {
  const d = nowState;
  // Callers that are not the poll — finishSitting's success and 404 paths,
  // most of all — can reach this before any poll has ever succeeded, because
  // paintSittingBar falls back to the `sitting` global and so a Finish
  // button exists with nowState still null. Dereferencing d below then threw
  // a TypeError that ate the success toast on one path and escaped as an
  // unhandled rejection (from inside the catch, with nothing shown at all)
  // on the other. The bar is the part that must still update here: the
  // sitting it described is exactly what just went away.
  if (!d) { paintSittingBar(); return; }
  // The server is authoritative here (see /api/now's `sitting` field): if it
  // says this poll's context is a sitting, it is one, regardless of what
  // this client remembers from before a reload. `/api/undo` knows nothing
  // about split decisions, so it must not stay live just because an earlier,
  // unrelated ordinary filing left nowActions > 0.
  const inSitting = !!d.sitting;
  // Hidden, not just disabled: a permanently greyed-out button at top-right
  // is noise for the 95% of the time nothing is undoable.
  const undo = $("btn-undo-now");
  undo.disabled = nowActions === 0 || inSitting;
  undo.hidden = undo.disabled;
  if (!d.cooldown) { stopCooldownTicker(); nowCooldownUntil = nowCooldownTotal = 0; }
  paintNowControls(d);
  syncSittingFromNow(d);
  paintSittingBar();
  if (d.needs_reauth) {
    nowProblem = true;
    stopNowTicker();
    $("now-context").textContent = "";
    $("now-card").innerHTML =
      `<div class="state-card">
        <p>Spotify needs one more permission<br>(reading what's currently playing).</p>
        <button id="btn-reauth-go" class="primary">Reconnect Spotify</button>
        <p class="hint">Same login-link + paste-back dance as the first time.</p>
      </div>`;
    // Same handler as the nav link — the card just puts it where the
    // problem is instead of making the user find it top-right.
    $("btn-reauth-go").onclick = $("nav-reconnect").onclick;
    return;
  }
  if (d.cooldown) { renderNowProblem(d.cooldown); return; }
  if (!d.playing) {
    nowProblem = false;
    stopNowTicker();
    $("now-context").textContent = "";
    const hasInputs = (d.inputs || []).some((l) => l.id !== "liked");
    $("now-card").innerHTML =
      `<div class="state-card">
        <div class="state-icon">${ICON_PLAY}</div>
        <p>Nothing playing.</p>
        <p class="hint">${hasInputs
          ? "Start one of your inputs above, or put something on in Spotify."
          : "Put something on in Spotify and it shows up here."}</p>
      </div>`;
    return;
  }

  const tr = d.track;
  const ctx = d.context;

  $("now-context").textContent = inSitting
    ? ""  // the sitting banner directly below already names the pile
    : ctx?.name
      ? (ctx.is_input ? `playing from ${ctx.name}` : `playing from ${ctx.name} (not an input)`)
      : "not playing from a playlist";

  const img = tr.image ? `<img src="${esc(tr.image)}" alt="">` : '<div class="noimg"></div>';
  const artists = tr.artists.map((a) => a.name).join(", ");

  const body = inSitting ? sittingCardBody(tr, d.sitting) : ordinaryCardBody(d, tr, ctx);

  nowProblem = false;  // a real card for a real track is about to go up
  $("now-card").innerHTML = `<div class="track-card${d.is_playing ? "" : " is-paused"}">
    <div class="art">${img}${d.is_playing ? "" : '<span class="paused-chip">paused</span>'}</div>
    <div class="t-name">${esc(tr.name)}</div>
    <div class="t-artist">${esc(artists)}${tr.album ? " — " + esc(tr.album) : ""}</div>
    ${tr.bpm ? `<div class="t-meta">${Math.round(tr.bpm)} BPM</div>` : ""}
    ${playbackStrip(d, tr)}
    ${body}
  </div>`;
  const tog = $("btn-now-toggle");
  if (tog) tog.onclick = playerToggle;
  const nxt = $("btn-now-next");
  if (nxt) nxt.onclick = playerNext;
  startNowTicker(d, tr);

  if (inSitting) {
    wireSittingCard();
  } else {
    $("now-card").querySelectorAll(".sugg").forEach((b) => {
      b.onclick = () => nowFile(b.dataset.to);
    });
    const more = $("btn-now-more");
    if (more) more.onclick = () => openPicker(nowState.homes, nowFile);
    const rem = $("btn-now-remove");
    if (rem) rem.onclick = nowRemove;
    $("now-card").querySelectorAll(".in-chip").forEach((b) => {
      b.onclick = () => nowCapture(b.dataset.in);
    });
  }
}

function ordinaryCardBody(d, tr, ctx) {
  const filedTo = filedUris[tr.uri];
  if (filedTo) return `<p class="done-msg">✓ filed to <b>${esc(filedTo)}</b></p>`;
  if (!tr.sortable) return '<p class="hint">Can\'t be sorted via the API (local file or episode).</p>';

  let body = "";
  d.suggestions.forEach((s, i) => {
    const home = nowState.homes.get(s.playlist_id);
    if (!home) return;
    body += `<button class="sugg${s.already ? " already" : ""}" data-to="${esc(s.playlist_id)}" style="--pct:${s.already ? 100 : s.pct}%">
      <span class="s-pct">${s.already ? '<span class="s-badge">already there</span>' : s.pct + "%"}</span>
      <span class="s-name"><kbd>${i + 1}</kbd> ${esc(home.name)}</span>
      <span class="s-why">${esc([home.folder, ...s.reasons].filter(Boolean).join(" · "))}</span>
    </button>`;
  });
  if (!d.suggestions.length) body += '<p class="hint">No confident match — use Add to…</p>';
  body += `<div class="minor-actions">
    <button id="btn-now-more"><kbd>m</kbd> Add to…</button>
    ${ctx?.is_input ? '<button id="btn-now-remove" class="danger"><kbd>r</kbd> Remove from input</button>' : ""}
  </div>`;
  const chips = (d.inputs || []).map((l) =>
    `<button class="chip in-chip${l.has_track ? " has" : ""}" data-in="${esc(l.id)}"${l.has_track ? " disabled" : ""}>${l.has_track ? "✓" : "+"} ${esc(l.name)}</button>`
  ).join("");
  if (chips) body += `<div class="capture"><span class="hint">capture to input:</span>${chips}</div>`;
  return body;
}

async function nowCapture(inId) {
  const d = nowState, tr = d.track;
  try {
    const res = await api("/api/act", { action: "move", uri: tr.uri, from_id: null, to_id: inId });
    nowActions++;
    const entry = d.inputs.find((l) => l.id === inId);
    if (entry) entry.has_track = true;
    toast(res.note || `+ ${entry?.name || "input"}`);
    renderNow();
  } catch (e) { toast(e.message); }
}

async function nowFile(toId) {
  const d = nowState, tr = d.track;
  const fromId = d.context?.is_input ? d.context.id : null;
  try {
    const res = await api("/api/act", { action: "move", uri: tr.uri, from_id: fromId, to_id: toId });
    nowActions++;
    filedUris[tr.uri] = d.homes.get(toId)?.name || "home";
    toast(res.note || `→ ${filedUris[tr.uri]}${fromId ? " (removed from input)" : ""}`);
    renderNow();
  } catch (e) { toast(e.message); }
}

async function nowRemove() {
  const d = nowState, tr = d.track;
  if (!d.context?.is_input) return;
  try {
    await api("/api/act", { action: "remove", uri: tr.uri, from_id: d.context.id });
    nowActions++;
    filedUris[tr.uri] = "nowhere (removed from input)";
    toast("removed from input");
    renderNow();
  } catch (e) { toast(e.message); }
}

$("btn-undo-now").onclick = async () => {
  if (!nowActions) return;
  try {
    const res = await api("/api/undo");
    nowActions--;
    const uri = Object.keys(filedUris).pop();
    if (uri) delete filedUris[uri];
    toast(res.restored_to ? "undone — restored to input" : "undone — removed from home again");
    renderNow();
  } catch (e) { toast(e.message); }
};

// ---- picker ----------------------------------------------------------------

function openPicker(homesMap, onPick) {
  const list = $("picker-list");
  const paint = (filter) => {
    list.innerHTML = "";
    const homes = [...homesMap.values()].sort((a, b) =>
      (a.folder || "").localeCompare(b.folder || "") || a.name.localeCompare(b.name));
    for (const h of homes) {
      if (filter && !(h.name + " " + (h.folder || "")).toLowerCase().includes(filter)) continue;
      const b = document.createElement("button");
      b.className = "picker-row";
      // Name first and bold; the folder path demoted to a small second line —
      // the full "folder / name (n)" string was unscannable on a phone.
      const sub = [h.folder, h.total != null ? `${h.total} tracks` : ""].filter(Boolean).join(" · ");
      b.innerHTML = `<span class="p-name">${esc(h.name)}</span>` +
        (sub ? `<span class="p-sub">${esc(sub)}</span>` : "");
      b.onclick = () => { closePicker(); onPick(h.id); };
      list.appendChild(b);
    }
  };
  paint("");
  $("picker-filter").value = "";
  $("picker-filter").oninput = (e) => paint(e.target.value.trim().toLowerCase());
  $("picker").hidden = false;
  $("picker-filter").focus();
}
function closePicker() { $("picker").hidden = true; }
$("picker-close").onclick = closePicker;
$("picker").onclick = (e) => { if (e.target.id === "picker") closePicker(); };

// ---- splitting ---------------------------------------------------------------
//
// A split turns one long, incoherent input into piles by musical character,
// and a sitting materialises one pile as a disposable ~2h playlist to
// actually listen through. Two different costs are in play here on purpose:
//   - reading tracks + tagging (POST /api/split) spends real Spotify budget
//     (~15 calls warm, ~35 cold) — never fired without the user asking for it.
//   - reclustering is pure local math over what's already cached — 0 calls —
//     so the UI says so every time, to make retuning feel free (because it
//     is) while splitting still feels like the deliberate, costly step it is.
// Keep/reject decisions live in the Now view, not here — see decideKeep below
// for why.

async function openSplit(id, name) {
  stopQueuePolling();
  queueStatus = null;
  $("queue-panel").hidden = true;
  $("queue-panel").innerHTML = "";
  split = { id, name, piles: [], decided: {}, active_sitting: null };
  show("split");
  $("split-title").textContent = name;
  $("split-empty").innerHTML = "";
  $("piles").innerHTML = "";
  $("split-params").hidden = true;
  try {
    const data = await api(`/api/split/${id}`);
    applySplitData(data);
    pollQueueStatus();
  } catch (e) {
    if (e.message === "auth needed") return;
    if (e.status !== 404) {
      // 404 ("no split for that playlist") is the one expected failure here
      // and means genuinely "offer to split it". Anything else — a
      // transient 5xx, a network hiccup — must NOT fall into that same
      // branch: this playlist may already be split, and the offer below
      // spends ~15-35 real Spotify calls if clicked.
      $("split-empty").innerHTML =
        `<p class="hint">Couldn't load this split: ${esc(e.message)}</p>
         <button id="btn-retry-split">Retry</button>`;
      $("btn-retry-split").onclick = () => openSplit(id, name);
      return;
    }
    // Estimate follows the same pagination math as the read itself (~100
    // tracks/call). A snapshot that hasn't moved since the last read costs
    // 0 — only a cold or changed cache pays the full amount.
    const total = playlistData.find((p) => p.id === id)?.total ?? 0;
    const warm = Math.ceil(total / 100) + 1;
    $("split-empty").innerHTML =
      `<p class="hint">Not split yet. Reading ${total} tracks costs 0 Spotify
       calls if nothing's changed since the last read, otherwise about
       ${warm} — tagging them via Last.fm afterwards costs no Spotify calls
       at all.</p>
       <p class="hint">Splitting works from the playlist listing as of the last
       <b>Refresh</b> on the Playlists view. If you edited or created this
       playlist in the Spotify client since then, Refresh first — otherwise
       this reads the old track list, or says it doesn't know the playlist.</p>
       <button id="btn-do-split" class="primary">Split it (0–${warm} calls)</button>`;
    $("btn-do-split").onclick = doSplit;
  }
}

// `#split-loading` is an in-flow spinner, not an overlay, and the paid offer
// above it is only cleared once the POST resolves — so without this the
// button stays live for the whole ~15-call read and every extra click buys
// another one (3 clicks measured at 42 Spotify calls against a button
// labelled "0–15"). Same disable-and-relabel pattern as btn-refresh-lists.
// `create_split` is the one split-family endpoint with no server-side
// in-flight guard of its own (start_sitting reserves under _split_lock,
// decide uses _pending_keeps), so this is the only thing standing between a
// double-click and a doubled spend.
let splitInFlight = false;

// ---- split progress --------------------------------------------------------
//
// GET /api/split/{id}/progress reads one module-level dict on the server and
// cannot reach Spotify (pinned server-side by
// test_split_progress_spends_no_api_calls), so none of the /api/now
// call-budget rules apply to polling it — the same reasoning that lets the
// queue panel poll. What DOES apply is stopping. This runs about once a second
// for the ~3 minutes the Last.fm phase takes, and a timer left armed after the
// split ends is the exact shape of the bug that once cost ~600 Spotify calls
// an hour from a single open tab. Two independent gates hold it closed:
// `splitInFlight` (the POST is still running) and the server's own
// poll_after_ms, which is 0 the moment the run reaches a terminal state.

// Last.fm is paced at one artist per MIN_INTERVAL (0.25s, tags.py), which is
// what makes a count of remaining artists convertible into a time at all.
const LASTFM_SECONDS_PER_ARTIST = 0.25;

let splitProgress = null;
let splitProgressTimer = null;

function stopSplitProgressPolling() {
  clearTimeout(splitProgressTimer);
  splitProgressTimer = null;
}

function etaText(seconds) {
  // Rounded coarsely on purpose: the estimate is only as good as Last.fm's
  // latency, and "about 2 min left" ages better than a false "1:47".
  if (seconds >= 90) return `about ${Math.round(seconds / 60)} min left`;
  return `about ${Math.max(5, Math.round(seconds / 5) * 5)} sec left`;
}

// Pure — takes the exact shape GET /api/split/{id}/progress returns and gives
// back markup, so ui_harness.mjs can exercise it without a DOM. Same shape as
// renderQueuePanel. "" means there is nothing to show, which is also what
// hides the bar.
function renderSplitProgress(p) {
  if (!p || p.state !== "running") return "";
  if (p.phase === "reading") {
    return `<p class="hint">Reading the playlist's tracks from Spotify…</p>`;
  }
  if (p.phase === "clustering") {
    return `<p class="hint">Tagged. Sorting tracks into piles — local, no calls.</p>`;
  }
  if (p.phase !== "tagging" || !p.total) {
    return `<p class="hint">Starting…</p>`;
  }
  const done = p.done || 0;
  const pct = Math.min(100, Math.round((done / p.total) * 100));
  const eta = etaText((p.total - done) * LASTFM_SECONDS_PER_ARTIST);
  return `<div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
     <p class="hint">Tagging artists via Last.fm — ${done} of ${p.total} (${pct}%),
     ${eta}. This phase costs no Spotify calls.</p>`;
}

function paintSplitProgress() {
  const el = $("split-progress");
  const html = renderSplitProgress(splitProgress);
  el.innerHTML = html;
  el.hidden = !html;
}

async function pollSplitProgress(splitId) {
  try {
    const p = await api(`/api/split/${splitId}/progress`);
    if (!split || split.id !== splitId) { stopSplitProgressPolling(); return; }
    splitProgress = p;
  } catch (_) {
    // Local and free — a failed poll leaves the last-known state on screen
    // rather than blanking the bar mid-run.
  }
  paintSplitProgress();
  stopSplitProgressPolling();
  const ms = splitProgress ? splitProgress.poll_after_ms : 0;
  if (splitInFlight && ms > 0) {
    splitProgressTimer = setTimeout(() => pollSplitProgress(splitId), ms);
  }
}

async function doSplit() {
  if (splitInFlight) return;
  splitInFlight = true;
  const btn = $("btn-do-split");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Splitting… (already spending)"; }
  $("split-loading").hidden = false;
  $("split-msg").textContent = "Reading tracks, then tagging artists via Last.fm…";
  // Kicked off before the POST is awaited, so the first answer lands while the
  // track read is still running rather than after everything is over.
  splitProgress = null;
  paintSplitProgress();
  pollSplitProgress(split.id);
  try {
    const data = await api(`/api/split/${split.id}`, splitParams());
    toast(`${data.tagged} tagged, ${data.untagged} untagged`);
    // The split now exists — clear the paid "Split it" offer immediately,
    // before risking a second request, so a failure below can never leave
    // stale UI still inviting a spend that already happened.
    $("split-empty").innerHTML = "";
    try {
      // The POST response is raw piles with no progress fields at all — and
      // create_split PRESERVES any decisions from a previous split of this
      // same playlist, so synthesising decided:0 here would show every pile
      // as fully undecided even when it isn't. GET is a free, local read;
      // it's also what seeds the resolution/pile-size inputs from what the
      // server actually used.
      const fresh = await api(`/api/split/${split.id}`);
      applySplitData(fresh);
    } catch (e2) {
      // The split happened; only the follow-up read failed. Offering
      // "Split it" again here would be confusing (it already exists) even
      // though it'd actually be free and safe (create_split resumes rather
      // than re-tagging) — so offer a plain retry of the read instead.
      $("split-empty").innerHTML =
        `<p class="hint">Split, but couldn't load the piles: ${esc(e2.message)}</p>
         <button id="btn-retry-split-load">Retry</button>`;
      $("btn-retry-split-load").onclick = () => openSplit(split.id, split.name);
    }
  } catch (e) {
    if (e.status === 404) {
      // 404 here is not "no split yet" (that's the GET) — create_split
      // validates the id against the cached listing, so it means the playlist
      // isn't in the listing sortify last read. The fix is a Refresh, which
      // is not something the raw "unknown playlist" detail says.
      toast("Spotify's listing here doesn't have this playlist — go back and press " +
            "Refresh on the Playlists view (it re-reads the listing), then try again", 6000);
    } else if (e.status === 403) {
      // Not ours, and no retry can change that: the Feb-2026 dev-mode API will
      // not read another user's playlist tracks at all. This is deliberately a
      // different status from the two 502s so it can be told apart from
      // something transient — re-offering the paid button here would invite a
      // spend guaranteed to fail. A persistent card, because the fix ("make
      // your own copy") is an instruction to follow, not a flash to catch.
      $("split-empty").innerHTML = `<p class="hint">${esc(e.message)}</p>`;
    } else if (e.status === 502 && /progress was saved/i.test(e.message)) {
      // Partial Last.fm failure. The server has already persisted every artist
      // it got an answer for, so this is resumable — but a 2600ms toast
      // vanished long before a user could learn that, and "stopped after 431
      // of 712 artists" reads like several hundred lost answers.
      $("split-empty").innerHTML =
        `<p class="hint">${esc(e.message)}</p>
         <p class="hint">Nothing already fetched is lost. <b>Resume</b> picks up
          where it stopped: it re-reads the track list (0 Spotify calls unless
          the playlist changed since the last read) and asks Last.fm only about
          the artists still missing.</p>
         <button id="btn-resume-split" class="primary">Resume tagging</button>`;
      $("btn-resume-split").onclick = doSplit;
    } else {
      toast(e.message);
    }
  } finally {
    // Cleared before the timer is, so a poll already awaiting a response
    // cannot re-arm itself on the way out.
    splitInFlight = false;
    stopSplitProgressPolling();
    splitProgress = null;
    paintSplitProgress();
    $("split-loading").hidden = true;
    // Gone from the DOM on the success path (split-empty was cleared above);
    // still there after a failure, where re-offering the spend is correct.
    const again = $("btn-do-split");
    if (again) { again.disabled = false; again.textContent = label; }
  }
}

function splitParams() {
  return {
    resolution: Number($("split-resolution").value) || 1,
    min_pile: Number($("split-minpile").value) || 15,
  };
}

function applySplitData(data) {
  split.piles = data.piles;
  split.decided = data.decided || {};
  split.active_sitting = data.active_sitting || null;
  // Seed the tuning inputs from what the server actually used — otherwise a
  // split made at resolution 1.4 displays 1.0, and pressing Re-cluster to
  // "keep the same shape" silently reshapes every pile.
  if (data.params) {
    $("split-resolution").value = data.params.resolution ?? 1;
    $("split-minpile").value = data.params.min_pile ?? 15;
  }
  syncSitting(split.id, split.piles, split.decided, split.active_sitting);
  renderPiles();
}

// Rebuilds the `sitting` convenience global from a split's own (piles,
// decided, active_sitting) — used both when the split view is (re)opened
// (recovering `sitting` after e.g. a page reload) and by finishSitting's
// cleared:false path below. Always refreshes `decided` even for a sitting
// already known: without that, revisiting this same split's view — the one
// free resync path (GET /api/split, local read) the design relies on — did
// nothing for the sitting it already recognised, and two tabs on one
// sitting would drift permanently.
//
// Gated on the reservation merely EXISTING, exactly like the backend
// (start_sitting/recluster/create_split all refuse on `active_sitting`
// truthiness alone). A reservation is written with `playlist_id: None`
// BEFORE the first Spotify call, so a 429 landing on create_playlist —
// this project's documented failure mode — leaves one standing with no id.
// Also requiring an id here used to hide that reservation from every
// surface: no bar, no Finish button, Start buttons still enabled, and every
// one of them 409ing with "finish it first" against a Finish button that
// existed nowhere. The server-side escape is free; it just needs a click to
// reach it.
function syncSitting(splitId, piles, decided, activeSitting) {
  if (!activeSitting) {
    if (sitting && sitting.splitId === splitId) sitting = null;
    return;
  }
  const uris = activeSitting.uris || [];
  const decidedHere = {};
  for (const u of uris) {
    if (decided[u]) decidedHere[u] = decided[u];
  }
  const pile = piles.find((p) => p.id === activeSitting.pile_id);
  sitting = {
    splitId, sittingId: activeSitting.playlist_id, pileId: activeSitting.pile_id,
    pileName: pile ? pile.name : activeSitting.pile_id, uris, decided: decidedHere,
  };
}

function renderPiles() {
  $("split-params").hidden = false;
  renderSaveAll();
  const wrap = $("piles");
  wrap.innerHTML = "";
  // Reads split.active_sitting directly, not the `sitting` global — the
  // backend allows one active sitting PER split, so a sitting started on a
  // different split must not disable or hide this one's own, correctly
  // live, active_sitting (which GET /api/split refreshes every time this
  // view opens, independent of whatever else the global currently points
  // at).
  //
  // Existence alone, no playlist_id requirement — same gate the backend
  // uses (see syncSitting). A reservation with no id still 409s every Start
  // here, so leaving these buttons enabled only promises a spend the server
  // will refuse.
  const sittingActive = !!split.active_sitting;
  for (const p of split.piles) {
    const left = p.total - p.decided;
    const row = document.createElement("div");
    row.className = "pile-row";
    const tags = p.tags && p.tags.length ? p.tags.join(" · ") : "";
    const save = saveOffer(p);
    const busy = saveAllBusy || pileSaveBusy.has(p.id);
    row.innerHTML = `
      <div class="pl-meta">
        <div class="name">${esc(p.name)}</div>
        <div class="sub">${left} of ${p.total} left${tags ? " · " + esc(tags) : ""}${
          save.note ? " · " + esc(save.note) : ""}</div>
      </div>
      <button class="pile-save" ${save.disabled || busy ? "disabled" : ""} title="${esc(save.title)}">${
        busy ? "Queuing… (already spending)" : esc(save.label)}</button>
      <button class="pile-sitting primary" ${left === 0 || sittingActive ? "disabled" : ""}>Start ~2h sitting (~24 calls)</button>`;
    row.querySelector(".pile-sitting").onclick = () => startSitting(p.id, p.name);
    const saveBtn = row.querySelector(".pile-save");
    saveBtn.onclick = () => queuePiles([p.id], save.calls);
    wrap.appendChild(row);
  }
  renderSplitSittingBar();
}

// One header button that queues every pile with something left to save in
// one go (`queuePiles(null, …)` — null is the server's "everything" sigil,
// same contract as a single pile). The total is computed here, once, and
// baked into the closure the button's click reads — never recomputed at
// click time — so what gets echoed back to the server is guaranteed to be
// the number that was actually on screen, the same misclick contract every
// other priced button in this file keeps.
let saveAllTotal = 0;

function renderSaveAll() {
  const wrap = $("split-save-all");
  const btn = $("btn-save-all");
  saveAllTotal = split.piles.reduce(
    (sum, p) => sum + (Number.isInteger(p.materialise_calls) ? p.materialise_calls : 0), 0);
  if (saveAllTotal <= 0) { wrap.hidden = true; return; }
  btn.textContent = saveAllBusy ? "Queuing… (already spending)" : `Save all piles (${floorPrice(saveAllTotal)})`;
  btn.title = renderSaveAllLabel(saveAllTotal);
  btn.disabled = saveAllBusy;
  wrap.hidden = false;
}

// Finding I2: every price this app shows is a FLOOR, not a ceiling —
// request() retries a transient 429 up to 3x, and each attempt is a real
// Spotify call the retry pays for, so the true spend can run over what was
// quoted. Every displayed price says so: `≥ N calls` on the button itself,
// and the disclosure spelled out in its title tooltip. One shared pair of
// helpers so the wording can't drift between the per-pile buttons and the
// save-all header button.
function floorPrice(calls) {
  return `≥ ${calls} call${calls === 1 ? "" : "s"}`;
}

function floorDisclosure(calls) {
  return `at least ${calls} Spotify call${calls === 1 ? "" : "s"} — a retried ` +
         `429 is charged per attempt.`;
}

// The save-all header button's price-floor disclosure — also unit-tested
// directly (ui_harness.mjs) since a wrong number here is exactly the kind of
// thing that would otherwise only surface after a misclick.
function renderSaveAllLabel(calls) {
  return `Save all piles (${floorPrice(calls)}) — ${floorDisclosure(calls)}`;
}

// What the "save this pile" button says, and what it will hand back as
// `expected_calls`. The cost is NEVER computed here: the server sends
// `materialise_calls` with every pile (a free, local read) and refuses the
// enqueue unless the number comes back exactly, so a figure invented on this
// side could only ever buy a refusal. A pile row from a server that didn't
// send one is therefore offered as un-clickable rather than guessed at.
//
// One call per track — the Feb-2026 API has no batch add — queued and run by
// a server-side worker, not this tab. The governor starts at 1.8 calls/min
// and climbs (after clean stretches) to a 7.0/min ceiling, so the honest
// estimate is a RANGE: ~calls/7.0 minutes if it's already at ceiling, up to
// calls/1.8 if it's still at (or has just been knocked back to) the start
// rate. For the 309-track pile that's ~45 min at ceiling, several hours at
// the start rate.
const QUEUE_CEILING_RATE = 7.0;  // calls/min, sortify/pacing.py CEILING_RATE
const QUEUE_START_RATE = 1.8;    // calls/min, sortify/pacing.py START_RATE

// Minutes -> a short human string, switching to hours once it's unwieldy.
function formatQueueDuration(mins) {
  if (mins < 60) return `${Math.max(1, Math.round(mins))} min`;
  const hours = mins / 60;
  return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)} hr`;
}

function saveOffer(p) {
  const calls = p.materialise_calls;
  const m = p.materialised;
  if (!Number.isInteger(calls)) {
    return { calls: null, disabled: true, label: "Save as playlist",
             note: "", title: "Reopen this split to see what saving it would cost." };
  }
  const fast = formatQueueDuration(calls / QUEUE_CEILING_RATE);
  const slow = formatQueueDuration(calls / QUEUE_START_RATE);
  const price = `${floorPrice(calls)} — one per track, paced by a server-side worker ` +
    `(about ${fast} at sortify's fastest pace, up to ${slow} if it's still ramping up) ` +
    `(${floorDisclosure(calls)}). This job runs on the server, not in this tab — closing ` +
    `it doesn't stop it. If it's interrupted (a rate-limit cooldown, say) nothing is ` +
    `lost — pressing it again, or letting it resume on its own, adds only the tracks ` +
    `that are still missing.`;
  if (calls === 0) {
    return { calls: 0, disabled: true, label: "Saved as a playlist",
             note: `saved as a playlist (${m && m.added} tracks)`,
             title: "This pile is already a real playlist in your Spotify account. " +
                    "sortify never deletes it — remove it there if you don't want it." };
  }
  if (m && m.stale) {
    return { calls, disabled: false, label: `Save as a new playlist (${floorPrice(calls)})`,
             note: "saved earlier, but the pile has changed since",
             title: "This pile was saved before, but re-clustering has changed which " +
                    "tracks are in it, so the old playlist is no longer this pile. " +
                    "Saving now makes a separate one. " + price };
  }
  if (m && m.playlist_id) {
    return { calls, disabled: false, label: `Resume saving (${floorPrice(calls)})`,
             note: `${m.added} of ${p.total} saved so far`,
             title: "Adds only the tracks still missing from the playlist this pile " +
                    "already has. " + price };
  }
  return { calls, disabled: false, label: `Save as playlist (${floorPrice(calls)})`,
           note: "", title: "Makes a permanent Spotify playlist named after this pile " +
                            "and adds every track in it. " + price };
}

// One entry per (split, pile-set) signature currently mid-`queuePiles()` in
// this tab — "save all" and "save this one pile" are different signatures
// on purpose, so a save-all in flight doesn't lock out a single-pile retry
// (or vice versa) even though the server ultimately allows only one queue at
// a time; that invariant is enforced server-side (409) and handled below
// like any other refusal. A save can run for a long time, so "disable the
// button" alone is not enough — the button is re-rendered on every free
// re-read of the split, and a re-render would hand a second click a fresh,
// enabled one.
const queueActionInFlight = new Set();
let saveAllBusy = false;
const pileSaveBusy = new Set();

function queueActionKey(pileIds) {
  return pileIds === null ? "*" : [...pileIds].sort().join(",");
}

// Replaces the old one-shot materialisePile (Task 7 removed its endpoint):
// this posts to the queue instead of spending synchronously, so a save that
// used to tie up the tab for up to ~26 minutes now returns almost instantly
// and the worker drains it in the background — see renderQueuePanel below
// for how that progress is shown. Same misclick contract as the one-shot
// had: the POST echoes back exactly the number the button displayed
// (`expectedCalls`), and `pileIds: null` is the server's "every pile" sigil
// for the save-all button, never invented client-side.
async function queuePiles(pileIds, expectedCalls) {
  if (!split) return;
  const splitId = split.id;
  if (expectedCalls === null) return;
  const key = splitId + " " + queueActionKey(pileIds);
  if (queueActionInFlight.has(key)) return;
  queueActionInFlight.add(key);
  const targetIds = pileIds === null ? split.piles.map((p) => p.id) : pileIds;
  if (pileIds === null) saveAllBusy = true;
  for (const id of targetIds) pileSaveBusy.add(id);
  renderPiles();
  try {
    const data = await api(`/api/split/${splitId}/queue`,
                           { pile_ids: pileIds, expected_calls: expectedCalls });
    toast(data.total_calls === 0
      ? "already saved — nothing queued"
      : `queued ${data.queued.length} pile${data.queued.length === 1 ? "" : "s"} — ` +
        `${data.total_calls} Spotify calls, running in the background`, 5000);
  } catch (e) {
    if (e.message === "auth needed") return;
    // A 409 here is either the cost guard (a stale row) or "a queue is
    // already running" — neither is a conflict to retry blindly. The free
    // re-read below puts the current truth back on screen either way.
    toast(e.message, 6000);
  } finally {
    queueActionInFlight.delete(key);
    if (pileIds === null) saveAllBusy = false;
    for (const id of targetIds) pileSaveBusy.delete(id);
  }
  // Always re-read, on success and on failure alike — same behaviour the
  // one-shot materialisePile had: a stale price or an already-landed queue
  // needs the fresh row, not what was true before this click. Free and
  // local — GET /api/split spends nothing.
  try {
    const fresh = await api(`/api/split/${splitId}`);
    if (split && split.id === splitId) applySplitData(fresh);
  } catch (_) {
    // Leave the original message on screen; a failed re-read has nothing
    // better to say than the failure that preceded it.
  }
  pollQueueStatus();
}

// Shown whenever a reservation exists at all — `playlist_id` only decides
// the wording, never whether the Finish button is reachable. A reservation
// with no id (create_playlist failed, e.g. a 429 on the sitting's very first
// call) is precisely the state that needs this bar most: nothing else in the
// UI can release it, and finishing it costs 0 Spotify calls.
function renderSplitSittingBar() {
  const bar = $("split-sitting-bar");
  const a = split.active_sitting;
  if (!a) { bar.hidden = true; return; }
  const pile = split.piles.find((p) => p.id === a.pile_id);
  const pileName = pile ? pile.name : a.pile_id;
  const uris = a.uris || [];
  const left = uris.filter((u) => !split.decided[u]).length;
  $("split-sitting-status").textContent = a.playlist_id
    ? `Listening: ${pileName} — ${left} of ${uris.length} left. ` +
      `Open Spotify and the Now view to keep/reject, or finish here.`
    : `A sitting on ${pileName} is reserved but its Spotify playlist was never ` +
      `created — the call failed. Nothing was spent and nothing else can start ` +
      `until it's cleared. Finish it here (0 Spotify calls).`;
  bar.hidden = false;
}

// ---- the queued materialiser's status panel --------------------------------
//
// GET /api/split/{id}/queue never touches Spotify (it reads queue.json and
// pacing.json, both local), so none of the /api/now call-budget rules apply
// here — it's polled purely for UI freshness, gated on the panel actually
// being on screen AND the queue actually being able to change on its own
// (running/sleeping/quiet). A paused/stopped/done queue doesn't move without
// a click, so polling it is pure waste.
const QUEUE_ACTIVE_STATES = new Set(["running", "sleeping", "quiet"]);
let queueStatus = null;     // last known {queue, pacing} for the open split
let queuePanelTimer = null;

function stopQueuePolling() { clearTimeout(queuePanelTimer); queuePanelTimer = null; }

// Pure — takes the exact shape GET /api/split/{id}/queue returns and renders
// it to a markup string, so it's testable without a DOM (ui_harness.mjs).
function renderQueuePanel(status) {
  const q = (status && status.queue) || {};
  const p = (status && status.pacing) || {};
  const prog = q.progress || {};
  const state = q.state || "stopped";
  const badge = `<span class="queue-state">${esc(state)}</span>`;
  const bits = [];
  if (prog.pile_count) {
    bits.push(`pile ${prog.pile_index ?? 0}/${prog.pile_count} · ` +
               `track ${prog.track ?? 0}/${prog.track_total ?? 0}`);
  }
  if (p.rate_per_min != null) {
    bits.push(`rate ${p.rate_per_min}/min (ceiling ${p.ceiling ?? 7.0})`);
  }
  if (p.max_clean_rate != null) {
    bits.push(`max clean rate ${p.max_clean_rate}/min`);
  }
  // M-3: spend vs. cap+reserve — the same numbers _queue_progress already
  // sends with every GET (a free, local read), just not previously shown.
  if (prog.daily_cap != null && prog.reserve != null) {
    bits.push(`spend ${prog.spent_today ?? 0}/${prog.daily_cap} today ` +
               `(bulk ${prog.bulk_today ?? 0}, reserve ${prog.reserve})`);
  }
  const summary = bits.length
    ? `<div class="queue-summary">${badge}<span>${esc(bits.join(" · "))}</span></div>`
    : `<div class="queue-summary">${badge}</div>`;
  // stop_reason carries both permanent quota trips and the last 429's
  // reason string (see app.py's classify_429/note_429) — surfaced verbatim
  // rather than re-worded, since the exact reason is what tells a user
  // whether Resume is worth clicking yet. A quota trip is the severe case
  // (Development Mode's daily allowance, gone until the window resets — see
  // CLAUDE.md's lockout history) and must not read like a routine 429 note,
  // so it gets its own class and explicit wording rather than the raw
  // "quota" string.
  const stopLine = q.stop_reason === "quota"
    ? `<p class="hint queue-stop-quota">daily quota tripped — resume is manual</p>`
    : q.stop_reason
      ? `<p class="hint">last stop: ${esc(q.stop_reason)}</p>` : "";
  // M-3: the pacing side's own record of the last 429, whether or not it
  // stopped the worker (a rate 429 the governor halved for and kept going
  // through leaves no stop_reason at all, so this is the only place that
  // history is visible in the UI).
  const hist429 = Array.isArray(p.history_429) ? p.history_429 : [];
  const last429 = hist429.length ? hist429[hist429.length - 1] : null;
  const last429Line = last429
    ? `<p class="hint queue-last-429">last 429: ${esc(last429.kind)} at ` +
      `${last429.rate}/min (${esc(new Date(last429.when * 1000).toLocaleTimeString())})</p>`
    : "";
  const canPause = QUEUE_ACTIVE_STATES.has(state);
  // Resume 409s ("nothing queued") whenever pending/current are both empty
  // — cancel leaves the queue in exactly that state (stopped, pending: [],
  // current: null), so gating on state alone left Resume permanently
  // enabled-but-dead after a cancel. Both fields ride along in the same GET.
  const hasWork = (Array.isArray(q.pending) && q.pending.length > 0) || !!q.current;
  const canResume = (state === "paused" || state === "stopped") && hasWork;
  const canCancel = state !== "done" && state !== "stopped";
  return `${summary}${stopLine}${last429Line}
    <div class="queue-controls">
      <button id="btn-queue-pause" ${canPause ? "" : "disabled"}>Pause</button>
      <button id="btn-queue-resume" ${canResume ? "" : "disabled"}>Resume</button>
      <button id="btn-queue-cancel" ${canCancel ? "" : "disabled"}>Cancel</button>
    </div>`;
}

function paintQueuePanel() {
  const el = $("queue-panel");
  // Only ever shown for the split that actually owns the current/last queue
  // — GET stays global (M4, Task 9) so a split that never queued anything
  // would otherwise render someone else's progress as its own.
  if (!queueStatus || !queueStatus.queue || !split ||
      queueStatus.queue.playlist_id !== split.id) {
    el.hidden = true; el.innerHTML = "";
    return;
  }
  el.innerHTML = renderQueuePanel(queueStatus);
  el.hidden = false;
  const pause = $("btn-queue-pause");
  const resume = $("btn-queue-resume");
  const cancel = $("btn-queue-cancel");
  if (pause) pause.onclick = pauseQueue;
  if (resume) resume.onclick = resumeQueue;
  if (cancel) cancel.onclick = cancelQueue;
}

async function pollQueueStatus() {
  if (!split) { stopQueuePolling(); return; }
  const splitId = split.id;
  try {
    const status = await api(`/api/split/${splitId}/queue`);
    if (!split || split.id !== splitId) return;   // navigated away mid-flight
    queueStatus = status;
  } catch (_) {
    // Local and free — a failed poll leaves the last-known status on screen
    // rather than erasing it.
  }
  paintQueuePanel();
  const active = queueStatus && queueStatus.queue &&
    queueStatus.queue.playlist_id === splitId &&
    QUEUE_ACTIVE_STATES.has(queueStatus.queue.state);
  stopQueuePolling();
  if (!$("queue-panel").hidden && active) {
    queuePanelTimer = setTimeout(pollQueueStatus, 10000);
  }
}

// Same in-flight guard queuePiles uses (cheap consistency): the queue
// control buttons are static ids, re-rendered whenever the panel repaints,
// so a raw boolean would survive a repaint fine but a per-action key on the
// same Set costs nothing extra and keeps every "is this already in flight"
// question answered the same way in one place.
function queueControlKey(action) {
  return split ? split.id + " control:" + action : null;
}

// On success, the response IS the truth — no need to spend a second round
// trip to confirm it. On failure (most commonly a 409 from a panel that was
// already stale — the queue finished, or someone else cancelled it) the
// on-screen state was never true to begin with, so re-polling is what
// reconciles the UI immediately instead of leaving a dead Pause button
// enabled until the next scheduled poll (or none, if polling had already
// stopped). Same behaviour resumeQueue/cancelQueue already have below.
async function pauseQueue() {
  if (!split) return;
  const key = queueControlKey("pause");
  if (queueActionInFlight.has(key)) return;
  queueActionInFlight.add(key);
  try {
    await api(`/api/split/${split.id}/queue/pause`, {});
    if (queueStatus && queueStatus.queue) queueStatus.queue.state = "paused";
    stopQueuePolling();
    paintQueuePanel();
  } catch (e) {
    toast(e.message);
    pollQueueStatus();
  } finally {
    queueActionInFlight.delete(key);
  }
}

async function resumeQueue() {
  if (!split) return;
  const key = queueControlKey("resume");
  if (queueActionInFlight.has(key)) return;
  queueActionInFlight.add(key);
  try {
    await api(`/api/split/${split.id}/queue/resume`, {});
  } catch (e) { toast(e.message); }
  queueActionInFlight.delete(key);
  pollQueueStatus();
}

async function cancelQueue() {
  if (!split) return;
  const key = queueControlKey("cancel");
  if (queueActionInFlight.has(key)) return;
  queueActionInFlight.add(key);
  try {
    await api(`/api/split/${split.id}/queue`, undefined, "DELETE");
    toast("queue cancelled");
  } catch (e) { toast(e.message); }
  queueActionInFlight.delete(key);
  pollQueueStatus();
}

async function startSitting(pileId, pileName) {
  const splitId = split.id;
  try {
    const data = await api(`/api/split/${splitId}/sitting`, { pile_id: pileId, target_minutes: 120 });
    split.active_sitting = { playlist_id: data.sitting_id, pile_id: pileId, uris: data.uris };
    syncSitting(split.id, split.piles, split.decided, split.active_sitting);
    toast(`${data.uris.length} tracks (~${data.minutes} min) — open it in Spotify, ` +
          `then switch to Now to keep or reject as you go`, 4500);
    renderPiles();
  } catch (e) {
    toast(e.message);
    // A failure here does NOT mean nothing happened. start_sitting reserves
    // the slot under the lock before spending anything and fills in the
    // playlist id as soon as create_playlist returns, so a 429 on add #5 of
    // 22 leaves a live reservation AND a real ▶ playlist in the account.
    // Toasting and leaving `split.active_sitting` null would show no sitting
    // and enabled Start buttons over both. Re-read the split — a local,
    // 0-call read — and let the server's answer stand.
    try {
      const fresh = await api(`/api/split/${splitId}`);
      if (split && split.id === splitId) applySplitData(fresh);
    } catch (_) {
      // Leave the original failure on screen; a resync that fails too has
      // nothing better to say.
    }
  }
}

// Finishing is a real Spotify call (one unfollow) and it is not idempotent
// from the user's side: a second click while the first is in flight spends a
// second unfollow, loses the compare-and-swap in finish_sitting, and comes
// back cleared:false. Both Finish buttons are static in the DOM, so
// disabling them by id here is enough — nothing re-renders them away.
let finishInFlight = false;

function setFinishBusy(busy) {
  for (const id of ["btn-now-finish-sitting", "btn-split-finish-sitting"]) {
    const b = $(id);
    if (!b) continue;
    b.disabled = busy;
    b.textContent = busy ? "Finishing…" : "Finish sitting";
  }
}

// `targetSplitId` lets a caller finish a SPECIFIC split's sitting (the Split
// view's own Finish button always means "this split", regardless of what the
// `sitting` global last pointed at — see the note by its declaration). Falls
// back to the global for the Now view's bar, which has no "current split" of
// its own to hand over explicitly.
async function finishSitting(targetSplitId) {
  const finishedSplitId = targetSplitId || sitting?.splitId;
  if (!finishedSplitId) return;
  if (finishInFlight) return;
  finishInFlight = true;
  setFinishBusy(true);
  try {
    const data = await api(`/api/split/${finishedSplitId}/sitting/finish`, {});
    if (data.cleared) {
      toast("sitting finished — disposable playlist removed");
      if (sitting && sitting.splitId === finishedSplitId) sitting = null;
      if (split && split.id === finishedSplitId) { split.active_sitting = null; renderPiles(); }
    } else {
      // cleared:false means the reservation this call observed was NOT the
      // one it cleared — most likely a newer sitting now occupies the slot.
      // finish_sitting still unfollowed whatever it read, but treating this
      // as "already finished" and wiping local state would make a real,
      // live sitting invisible to every surface with no pointer left
      // anywhere. Re-sync from the server (free, local) instead of guessing.
      const fresh = await api(`/api/split/${finishedSplitId}`);
      syncSitting(finishedSplitId, fresh.piles, fresh.decided || {}, fresh.active_sitting || null);
      if (split && split.id === finishedSplitId) {
        split.piles = fresh.piles;
        split.decided = fresh.decided || {};
        split.active_sitting = fresh.active_sitting || null;
        renderPiles();
      }
      // Said only after the resync, and from what the resync found. The old
      // wording ("finish it again") was unconditional, so the overwhelmingly
      // common cause — a double-click, where the first click already
      // finished everything — told the user to go spend a third unfollow on
      // a split with nothing active on it at all.
      toast(fresh.active_sitting
        ? "a newer sitting is active on this split — press Finish again to end that one"
        : "that sitting was already finished — nothing left to clear");
    }
    if (!$("view-now").hidden) renderNow();
  } catch (e) {
    if (e.status === 404) {
      // No active sitting for this split at all — this client's pointer to
      // one is stale (finished elsewhere, or left over from a session that
      // never synced). Clearing it here is what stops every further Finish
      // click from 404ing on the same stale pointer forever.
      toast("no active sitting to finish — clearing it here too");
      if (sitting && sitting.splitId === finishedSplitId) sitting = null;
      if (split && split.id === finishedSplitId) { split.active_sitting = null; renderPiles(); }
      if (!$("view-now").hidden) renderNow();
    } else {
      toast(e.message);
    }
  } finally {
    finishInFlight = false;
    setFinishBusy(false);
  }
}

$("btn-split-back").onclick = () => { stopQueuePolling(); split = null; loadLists(); };
$("btn-split-finish-sitting").onclick = () => finishSitting(split?.id);
$("btn-save-all").onclick = () => queuePiles(null, saveAllTotal);
$("btn-recluster").onclick = async () => {
  try {
    const data = await api(`/api/split/${split.id}/recluster`, splitParams());
    split.piles = data.piles;
    renderPiles();
    toast("re-clustered — 0 Spotify calls");
  } catch (e) { toast(e.message); }
};

// ---- split decisions (decided from the Now view) ---------------------------
//
// Keep/reject controls live on the Now view's card rather than a card of
// their own here, for one concrete reason: this app has no cheap way to
// display a track's name/artist/art for an arbitrary Spotify uri — the
// dev-mode API has no batch lookup, so getting that for up to 40 sitting
// tracks up front would cost 40 extra calls, on top of the ~24 the sitting
// itself already spent. /api/now already carries full track metadata for
// free, as a side effect of the polling that already exists to show what's
// playing. Piggybacking on it is the only way to show "what track is this"
// without paying for it twice.
//
// It also keeps the two decision models honest instead of tangled: /api/act
// (ordinary triage/Now filing, undo-able, 1–2 calls) and /api/split/.../decide
// (a keep is final and costs 1 call, a reject touches nothing and costs 0)
// have different guarantees. Reusing nowFile/nowRemove here would silently
// route a "keep" through /api/act instead — no charge to the split's
// `decided` bookkeeping, no pile-progress update, and the track could be
// resurfaced by a future sitting from the same pile as if nothing happened.
// Keeping them as separate code paths, gated on whether the currently-playing
// context is the active sitting's playlist, is what makes that impossible.

// `srvSitting` is always d.sitting from the most recent /api/now poll — the
// server's own answer for "is the currently-playing context a sitting, and
// what's already decided in it." Never the client-side `sitting` global:
// that one only mirrors this for the cross-view convenience bar, and reading
// it here would let a stale local guess override a fresh server answer (the
// two-tab-drift and reload-gap bugs a review round caught).
function sittingCardBody(tr, srvSitting) {
  const dec = srvSitting.decided[tr.uri];
  if (dec?.action === "keep") {
    const homeName = dec.to_id === "liked" ? "Liked Songs" : (nowState.homes.get(dec.to_id)?.name || dec.to_id);
    return `<p class="done-msg">✓ kept to <b>${esc(homeName)}</b><br>
      <span class="hint">final — edit it from the home playlist if that was wrong</span></p>`;
  }
  if (dec?.action === "reject") {
    return `<p class="hint">✗ rejected.</p>
      <div class="minor-actions"><button id="btn-decide-unreject">Undo reject (free)</button></div>`;
  }
  let html = "";
  if (!tr.sortable) {
    html += '<p class="hint">Can\'t be kept via the API (local file or episode) — reject it instead.</p>';
  } else {
    (nowState.suggestions || []).forEach((s, i) => {
      const home = nowState.homes.get(s.playlist_id);
      if (!home) return;
      html += `<button class="sugg${s.already ? " already" : ""}" data-keep="${esc(s.playlist_id)}" style="--pct:${s.already ? 100 : s.pct}%">
        <span class="s-pct">${s.already ? '<span class="s-badge">already there</span>' : s.pct + "%"}</span>
        <span class="s-name"><kbd>${i + 1}</kbd> Keep → ${esc(home.name)}</span>
        <span class="s-why">${esc([home.folder, ...s.reasons].filter(Boolean).join(" · "))}</span>
      </button>`;
    });
    if (!nowState.suggestions.length) html += '<p class="hint">No confident match — use Keep to… below.</p>';
  }
  html += `<div class="minor-actions">
    ${tr.sortable ? `<button id="btn-decide-more"><kbd>m</kbd> Keep to…</button>` : ""}
    <button id="btn-decide-reject" class="danger"><kbd>r</kbd> Reject (free)</button>
  </div>`;
  return html;
}

function wireSittingCard() {
  $("now-card").querySelectorAll("[data-keep]").forEach((b) => {
    b.onclick = () => decideKeep(b.dataset.keep);
  });
  const more = $("btn-decide-more");
  if (more) more.onclick = () => openPicker(nowState.homes, decideKeep);
  const rej = $("btn-decide-reject");
  if (rej) rej.onclick = decideReject;
  const un = $("btn-decide-unreject");
  if (un) un.onclick = decideUndecide;
}

// Applies whatever the server says actually stands for `uri` — `decision` is
// res.decision from a decide() response: {action, to_id} if something is
// decided, or null if not (a successful undecide, or nothing ever landed).
// NEVER the action this client just attempted: decide() reports
// `changed: false` exactly when a different outcome already won (a
// throttled add still in flight when a second click landed, for instance),
// and recording the attempt instead of the true outcome is how a card ends
// up reading "kept to Jazz" for a track that is actually in Rock.
function applyDecision(uri, decision) {
  if (nowState?.sitting) {
    if (decision) nowState.sitting.decided[uri] = decision;
    else delete nowState.sitting.decided[uri];
  }
  if (sitting) {
    if (decision) sitting.decided[uri] = decision;
    else delete sitting.decided[uri];
  }
}

function homeNameFor(id) {
  if (id === "liked") return "Liked Songs";
  return nowState.homes.get(id)?.name || id;
}

// Describes res.decision — the standing decision a no-op left untouched —
// for a toast. Shared so a hardcoded "already rejected"/"nothing to undo"
// can never say something the card itself contradicts (e.g. the card
// correctly showing "kept to Jazz" while a hardcoded toast said "already
// rejected").
function describeStanding(decision) {
  if (!decision) return "no change";
  return decision.action === "keep" ? `kept to ${homeNameFor(decision.to_id)}` : decision.action;
}

async function decideKeep(homeId) {
  if (!nowState?.sitting) return;
  const splitId = nowState.sitting.split_id;
  const tr = nowState.track;
  try {
    const res = await api(`/api/split/${splitId}/decide`,
                          { uri: tr.uri, action: "keep", to_id: homeId });
    applyDecision(tr.uri, res.decision);
    // decide() reports `changed: false` for a no-op (e.g. re-keeping an
    // already-kept track, or losing a race to another click) — that must
    // read as "already handled", never as a fresh success.
    toast(res.changed
      ? `kept → ${homeNameFor(homeId)} (${res.remaining} left in the split)`
      : `already decided — ${describeStanding(res.decision)}`);
    renderNow();
  } catch (e) { toast(e.message); }
}

async function decideReject() {
  if (!nowState?.sitting) return;
  const splitId = nowState.sitting.split_id;
  const tr = nowState.track;
  try {
    const res = await api(`/api/split/${splitId}/decide`,
                          { uri: tr.uri, action: "reject" });
    applyDecision(tr.uri, res.decision);
    toast(res.changed
      ? `rejected — free (${res.remaining} left in the split)`
      : `already decided — ${describeStanding(res.decision)}`);
    renderNow();
  } catch (e) { toast(e.message); }
}

async function decideUndecide() {
  if (!nowState?.sitting) return;
  const splitId = nowState.sitting.split_id;
  const tr = nowState.track;
  try {
    const res = await api(`/api/split/${splitId}/decide`,
                          { uri: tr.uri, action: "undecide" });
    applyDecision(tr.uri, res.decision);  // null on success — clears it
    toast(res.changed
      ? `un-rejected — free (${res.remaining} left in the split)`
      : res.decision ? `already decided — ${describeStanding(res.decision)}` : "nothing to undo");
    renderNow();
  } catch (e) { toast(e.message); }
}

// ---- keyboard --------------------------------------------------------------

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (!$("picker").hidden) { if (e.key === "Escape") closePicker(); return; }

  if (!$("view-triage").hidden && triage) {
    const tr = triage.tracks[triage.idx];
    if (!tr) return;
    if (["1", "2", "3"].includes(e.key)) {
      const s = tr.suggestions[Number(e.key) - 1];
      if (s) moveTo(s.playlist_id);
    } else if (e.key === "m" && tr.sortable) openPicker(triage.homes, moveTo);
    else if (e.key === "r") removeOnly();
    else if (e.key === "s") { triage.skipped++; triage.idx++; renderCard(); }
    else if (e.key === "u") $("btn-undo").click();
    return;
  }

  // `nowProblem` and not just `nowState?.playing`: a failed poll leaves
  // nowState untouched on purpose, so after e.g. a 500 from /api/now the
  // screen shows an error card while `playing` still reads true — and `1`
  // would fire a keep, which is final and costs a call. The card has to be
  // showing the track a keystroke is about to decide on.
  if (!$("view-now").hidden && !nowProblem && nowState?.playing) {
    if (nowState.sitting) {
      const dec = nowState.sitting.decided[nowState.track.uri];
      if (dec?.action === "keep") return;  // final — nothing left to press
      if (dec?.action === "reject") { if (e.key === "u") decideUndecide(); return; }
      if (["1", "2", "3"].includes(e.key)) {
        const s = nowState.suggestions[Number(e.key) - 1];
        if (s) decideKeep(s.playlist_id);
      } else if (e.key === "m" && nowState.track.sortable) openPicker(nowState.homes, decideKeep);
      else if (e.key === "r") decideReject();
      return;
    }
    if (filedUris[nowState.track.uri]) return;
    if (["1", "2", "3"].includes(e.key)) {
      const s = nowState.suggestions[Number(e.key) - 1];
      if (s) nowFile(s.playlist_id);
    } else if (e.key === "m" && nowState.track.sortable) openPicker(nowState.homes, nowFile);
    else if (e.key === "r") nowRemove();
    else if (e.key === "u") $("btn-undo-now").click();
  }
});

// Coming back to the tab is the moment a skip is most likely to have happened
// behind our back, so this one bypasses the predicted TTL.
document.addEventListener("visibilitychange", () => {
  // Manual mode means exactly that — not even the refocus poll fires; the
  // refresh button is the only trigger the user hasn't pressed themselves.
  if (!document.hidden && !$("view-now").hidden && !nowManual) pollNow(true);
});

paintManualChip();

boot().catch((e) => { if (e.message !== "auth needed") toast(e.message); });
