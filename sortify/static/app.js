"use strict";

const $ = (id) => document.getElementById(id);
const views = ["setup", "lists", "triage", "now", "split"];

let statusData = null;
let playlistData = [];   // lists view
let roles = {};          // id -> "input" | "home" | null
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

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
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
    // The list is cached until refreshed by hand, so say how old it is rather
    // than present a stale list as current.
    $("pl-age").textContent = ageText(data.fetched_at);
    renderLists();
  } catch (e) {
    if (e.message === "auth needed") return;
    $("playlists").innerHTML =
      `<p class="hint">Couldn't load playlists: ${esc(e.message)}</p>
       <button id="btn-retry-lists">Retry</button>`;
    $("btn-retry-lists").onclick = loadLists;
  }
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
      </div>`;
    const [bIn, bHome, bSort, bSplit] = row.querySelectorAll("button");
    const paint = () => {
      bIn.classList.toggle("on-input", roles[p.id] === "input");
      bHome.classList.toggle("on-home", roles[p.id] === "home");
      bHome.hidden = p.id === "liked" || !p.editable;
      bSort.hidden = roles[p.id] !== "input";
      // Splitting only earns its keep on a playlist too long to work through
      // as-is — a 30-track input doesn't need piles.
      bSplit.hidden = p.id === "liked" || (p.total ?? 0) < 100;
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
  await api("/api/config", { input_ids, home_ids });
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
$("nav-lists").onclick = () => { stopNowPolling(); triage = null; loadLists(); };

// ---- now playing -----------------------------------------------------------

let nowState = null;   // last /api/now payload + client flags
let nowTimer = null;
let nowActions = 0;    // enables Undo
let filedUris = {};    // uri -> home name we filed it to this session

function stopNowPolling() { clearTimeout(nowTimer); nowTimer = null; }

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
  const left = s.uris.filter((u) => !s.decided[u]).length;
  $("now-sitting-status").textContent =
    `sitting: ${s.pileName} — ${left} of ${s.uris.length} left`;
  bar.hidden = false;
}
$("btn-now-finish-sitting").onclick = () => finishSitting(nowState?.sitting?.split_id || sitting?.splitId);

function renderNowProblem(msg) {
  paintSittingBar();
  $("now-context").textContent = "";
  $("now-controls").hidden = true;
  const cd = msg.match(/cooldown — try again in ~(\d+) min/);
  $("now-card").innerHTML = cd
    ? `<p class="done-msg">Spotify has rate-limited the app.<br>
       Back in about <b>${Math.round(Number(cd[1]) / 60)} hours</b> — nothing to do until then,
       your music and playlists are unaffected.</p>`
    : `<p class="done-msg">${esc(msg)}</p>`;
}

// Playback controls only make sense against something actually playing —
// Spotify 404s these calls when no device is active.
function paintNowControls(d) {
  const playable = (d.inputs || []).filter((l) => l.id !== "liked");
  if (!d.playing || !playable.length) { $("now-controls").hidden = true; return; }
  const sel = $("now-input-switch");
  const current = d.context?.id;
  sel.innerHTML =
    `<option value="">— play another input —</option>` +
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

$("btn-now-next").onclick = async () => {
  const btn = $("btn-now-next");
  btn.disabled = true;
  try {
    await api("/api/player/next", {});
    repollAfterPlaybackChange();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
};

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
  // The server is authoritative here (see /api/now's `sitting` field): if it
  // says this poll's context is a sitting, it is one, regardless of what
  // this client remembers from before a reload. `/api/undo` knows nothing
  // about split decisions, so it must not stay live just because an earlier,
  // unrelated ordinary filing left nowActions > 0.
  const inSitting = !!d.sitting;
  $("btn-undo-now").disabled = nowActions === 0 || inSitting;
  paintNowControls(d);
  syncSittingFromNow(d);
  paintSittingBar();
  if (d.needs_reauth) {
    $("now-context").textContent = "";
    $("now-card").innerHTML =
      `<p class="done-msg">Spotify needs one more permission (currently playing).<br>
       Ask for a new login link and redo the paste-back dance.</p>`;
    return;
  }
  if (d.cooldown) { renderNowProblem(d.cooldown); return; }
  if (!d.playing) {
    $("now-context").textContent = "";
    $("now-card").innerHTML =
      '<p class="done-msg">Nothing playing.<br>Put something on in Spotify and it shows up here.</p>';
    return;
  }

  const tr = d.track;
  const ctx = d.context;

  $("now-context").textContent = inSitting
    ? `sitting: ${d.sitting.pile_name}`
    : ctx?.name
      ? (ctx.is_input ? `playing from ${ctx.name}` : `playing from ${ctx.name} (not an input)`)
      : "not playing from a playlist";

  const img = tr.image ? `<img src="${esc(tr.image)}" alt="">` : '<div class="noimg"></div>';
  const artists = tr.artists.map((a) => a.name).join(", ");

  const body = inSitting ? sittingCardBody(tr, d.sitting) : ordinaryCardBody(d, tr, ctx);

  $("now-card").innerHTML = `<div class="track-card">
    ${img}
    <div class="t-name">${esc(tr.name)}</div>
    <div class="t-artist">${esc(artists)}${tr.album ? " — " + esc(tr.album) : ""}</div>
    ${body}
  </div>`;

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
    body += `<button class="sugg${s.already ? " already" : ""}" data-to="${esc(s.playlist_id)}">
      <span class="s-pct">${s.already ? "" : s.pct + "%"}</span>
      <span class="s-name"><kbd>${i + 1}</kbd> ${esc(home.name)}</span>
      <span class="s-why">${esc([home.folder, ...s.reasons].filter(Boolean).join(" · "))}</span>
    </button>`;
  });
  if (!d.suggestions.length) body += '<p class="hint">No confident match — use More…</p>';
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
      b.textContent = (h.folder ? h.folder + " / " : "") + h.name + (h.total != null ? ` (${h.total})` : "");
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
  split = { id, name, piles: [], decided: {}, active_sitting: null };
  show("split");
  $("split-title").textContent = name;
  $("split-empty").innerHTML = "";
  $("piles").innerHTML = "";
  $("split-params").hidden = true;
  try {
    const data = await api(`/api/split/${id}`);
    applySplitData(data);
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
       <button id="btn-do-split" class="primary">Split it (0–${warm} calls)</button>`;
    $("btn-do-split").onclick = doSplit;
  }
}

async function doSplit() {
  $("split-loading").hidden = false;
  $("split-msg").textContent = "Reading tracks, then tagging artists via Last.fm…";
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
    toast(e.message);
  } finally {
    $("split-loading").hidden = true;
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
function syncSitting(splitId, piles, decided, activeSitting) {
  if (!activeSitting || !activeSitting.playlist_id) {
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
  const wrap = $("piles");
  wrap.innerHTML = "";
  // Reads split.active_sitting directly, not the `sitting` global — the
  // backend allows one active sitting PER split, so a sitting started on a
  // different split must not disable or hide this one's own, correctly
  // live, active_sitting (which GET /api/split refreshes every time this
  // view opens, independent of whatever else the global currently points
  // at).
  const sittingActive = !!(split.active_sitting && split.active_sitting.playlist_id);
  for (const p of split.piles) {
    const left = p.total - p.decided;
    const row = document.createElement("div");
    row.className = "pile-row";
    const tags = p.tags && p.tags.length ? p.tags.join(" · ") : "";
    row.innerHTML = `
      <div class="pl-meta">
        <div class="name">${esc(p.name)}</div>
        <div class="sub">${left} of ${p.total} left${tags ? " · " + esc(tags) : ""}</div>
      </div>
      <button class="primary" ${left === 0 || sittingActive ? "disabled" : ""}>Start ~2h sitting (~24 calls)</button>`;
    row.querySelector("button").onclick = () => startSitting(p.id, p.name);
    wrap.appendChild(row);
  }
  renderSplitSittingBar();
}

function renderSplitSittingBar() {
  const bar = $("split-sitting-bar");
  const a = split.active_sitting;
  if (!a || !a.playlist_id) { bar.hidden = true; return; }
  const pile = split.piles.find((p) => p.id === a.pile_id);
  const uris = a.uris || [];
  const left = uris.filter((u) => !split.decided[u]).length;
  $("split-sitting-status").textContent =
    `Listening: ${pile ? pile.name : a.pile_id} — ${left} of ${uris.length} left. ` +
    `Open Spotify and the Now view to keep/reject, or finish here.`;
  bar.hidden = false;
}

async function startSitting(pileId, pileName) {
  try {
    const data = await api(`/api/split/${split.id}/sitting`, { pile_id: pileId, target_minutes: 120 });
    split.active_sitting = { playlist_id: data.sitting_id, pile_id: pileId, uris: data.uris };
    syncSitting(split.id, split.piles, split.decided, split.active_sitting);
    toast(`${data.uris.length} tracks (~${data.minutes} min) — open it in Spotify, ` +
          `then switch to Now to keep or reject as you go`, 4500);
    renderPiles();
  } catch (e) { toast(e.message); }
}

// `targetSplitId` lets a caller finish a SPECIFIC split's sitting (the Split
// view's own Finish button always means "this split", regardless of what the
// `sitting` global last pointed at — see the note by its declaration). Falls
// back to the global for the Now view's bar, which has no "current split" of
// its own to hand over explicitly.
async function finishSitting(targetSplitId) {
  const finishedSplitId = targetSplitId || sitting?.splitId;
  if (!finishedSplitId) return;
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
      toast("another sitting is active on this split — finish it again");
      const fresh = await api(`/api/split/${finishedSplitId}`);
      syncSitting(finishedSplitId, fresh.piles, fresh.decided || {}, fresh.active_sitting || null);
      if (split && split.id === finishedSplitId) {
        split.piles = fresh.piles;
        split.decided = fresh.decided || {};
        split.active_sitting = fresh.active_sitting || null;
        renderPiles();
      }
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
  }
}

$("btn-split-back").onclick = () => { split = null; loadLists(); };
$("btn-split-finish-sitting").onclick = () => finishSitting(split?.id);
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
      html += `<button class="sugg${s.already ? " already" : ""}" data-keep="${esc(s.playlist_id)}">
        <span class="s-pct">${s.already ? "" : s.pct + "%"}</span>
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

  if (!$("view-now").hidden && nowState?.playing) {
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
  if (!document.hidden && !$("view-now").hidden) pollNow(true);
});

boot().catch((e) => { if (e.message !== "auth needed") toast(e.message); });
