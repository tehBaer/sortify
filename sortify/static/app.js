"use strict";

const $ = (id) => document.getElementById(id);
const views = ["setup", "lists", "triage", "now"];

let statusData = null;
let playlistData = [];   // lists view
let roles = {};          // id -> "input" | "home" | null
let triage = null;       // {id, name, homes:Map, tracks, idx, sorted, skipped, history}

// ---- plumbing --------------------------------------------------------------

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const resp = await fetch(path, opts);
  let data = {};
  try { data = await resp.json(); } catch (_) {}
  if (resp.status === 401 && data.needs_auth) { show("setup"); throw new Error("auth needed"); }
  if (!resp.ok) throw new Error(data.detail || `${resp.status} error`);
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
  const clientId = $("client-id").value.trim();
  if (!clientId) return toast("paste the Client ID first");
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

async function loadLists() {
  show("lists");
  $("playlists").innerHTML = '<p class="hint">Loading playlists…</p>';
  try {
    const data = await api("/api/playlists");
    playlistData = data.playlists;
    roles = Object.fromEntries(playlistData.map((p) => [p.id, p.role]));
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
    const sub = [p.folder, p.total != null ? `${p.total} tracks` : null, p.editable ? null : p.id === "liked" ? "library" : "not yours"]
      .filter(Boolean).join(" · ");
    row.innerHTML = `${img}
      <div class="pl-meta"><div class="name">${esc(p.name)}</div><div class="sub">${esc(sub)}</div></div>
      <div class="pl-roles">
        <button class="chip r-input">In</button>
        <button class="chip r-home">Home</button>
        <button class="pl-sort" title="Sort this input">▶</button>
      </div>`;
    const [bIn, bHome, bSort] = row.querySelectorAll("button");
    const paint = () => {
      bIn.classList.toggle("on-input", roles[p.id] === "input");
      bHome.classList.toggle("on-home", roles[p.id] === "home");
      bHome.hidden = p.id === "liked" || !p.editable;
      bSort.hidden = roles[p.id] !== "input";
    };
    bIn.onclick = () => { roles[p.id] = roles[p.id] === "input" ? null : "input"; paint(); };
    bHome.onclick = () => { roles[p.id] = roles[p.id] === "home" ? null : "home"; paint(); };
    bSort.onclick = () => { saveConfig().then(() => startTriage(p.id, p.name)); };
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

function renderNowProblem(msg) {
  $("now-context").textContent = "";
  const cd = msg.match(/cooldown — try again in ~(\d+) min/);
  $("now-card").innerHTML = cd
    ? `<p class="done-msg">Spotify has rate-limited the app.<br>
       Back in about <b>${Math.round(Number(cd[1]) / 60)} hours</b> — nothing to do until then,
       your music and playlists are unaffected.</p>`
    : `<p class="done-msg">${esc(msg)}</p>`;
}

function renderNow() {
  const d = nowState;
  $("btn-undo-now").disabled = nowActions === 0;
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
  $("now-context").textContent = ctx?.name
    ? (ctx.is_input ? `playing from ${ctx.name}` : `playing from ${ctx.name} (not an input)`)
    : "not playing from a playlist";

  const filedTo = filedUris[tr.uri];
  const img = tr.image ? `<img src="${esc(tr.image)}" alt="">` : '<div class="noimg"></div>';
  const artists = tr.artists.map((a) => a.name).join(", ");

  let body = "";
  if (filedTo) {
    body = `<p class="done-msg">✓ filed to <b>${esc(filedTo)}</b></p>`;
  } else if (!tr.sortable) {
    body = '<p class="hint">Can\'t be sorted via the API (local file or episode).</p>';
  } else {
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
      <button id="btn-now-more"><kbd>m</kbd> More…</button>
      ${ctx?.is_input ? '<button id="btn-now-remove" class="danger"><kbd>r</kbd> Remove from input</button>' : ""}
    </div>`;
    const chips = (d.inputs || []).map((l) =>
      `<button class="chip in-chip${l.has_track ? " has" : ""}" data-in="${esc(l.id)}"${l.has_track ? " disabled" : ""}>${l.has_track ? "✓" : "+"} ${esc(l.name)}</button>`
    ).join("");
    if (chips) body += `<div class="capture"><span class="hint">capture to input:</span>${chips}</div>`;
  }

  $("now-card").innerHTML = `<div class="track-card">
    ${img}
    <div class="t-name">${esc(tr.name)}</div>
    <div class="t-artist">${esc(artists)}${tr.album ? " — " + esc(tr.album) : ""}</div>
    ${body}
  </div>`;

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

  if (!$("view-now").hidden && nowState?.playing && !filedUris[nowState.track.uri]) {
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
