"use strict";

const $ = (id) => document.getElementById(id);
const views = ["setup", "lists", "triage", "now", "split", "splitpick"];

let statusData = null;
let playlistData = [];   // lists view
let roles = {};          // id -> "input" | "home" | null
let hintTexts = {};      // id -> "ambient, piano" — per-home matching hints
// Subset ids as they were on load, before any chip taps this view visit —
// what "newly marked" means for the Save button's pending-cost label. A
// subset opted in earlier (and so already cached) costs nothing to re-save.
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
  if (resp.status === 401 && data.needs_auth) { show("setup"); loadSetup(); throw new Error("auth needed"); }
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
  // Preview audio must not outlive the card it belongs to: navigating away
  // used to leave clips playing with no visible control to stop them, and
  // held the auto-resume back until the user found their way back here.
  previewHold.stop();
  for (const v of views) $("view-" + v).hidden = v !== view;
  // Triage is reached from (and exits back to) the Playlists view, so it
  // keeps that link lit. Split has its own top-level tab now, and the
  // split view itself counts as part of it (opened from the picker or, still,
  // the per-row button).
  // The input switcher and now-actions live in the merged header; this class
  // is what shows them on the Now view and hides them everywhere else.
  document.body.classList.toggle("on-now", view === "now");
  $("nav-now").classList.toggle("active", view === "now");
  $("nav-lists").classList.toggle("active", view === "lists" || view === "triage");
  $("nav-split").classList.toggle("active", view === "split" || view === "splitpick");
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

// Folder paths run as deep as "archive / other / Previous / Old / Diverse /
// Røde Runde / Anbefalte" — wider than a phone on their own. Under a
// suggestion only the leaf is shown, the folder the playlist actually sits
// in; the picker keeps the full path, having the width for it.
function folderLeaf(f) {
  return (f || "").split(" / ").pop();
}

function esc(s) {
  return (s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- boot & setup ----------------------------------------------------------

async function boot() {
  statusData = await api("/api/status");
  $("whoami").textContent = statusData.me ? statusData.me.name : "";
  if (!statusData.authed) { show("setup"); loadSetup(); return; }
  showNow();
}

const CID_RE = /^[A-Za-z0-9]{32}$/;

async function loadSetup() {
  $("client-id").value = localStorage.getItem("spotifyClientId") || "";
  validateClientId();
  try {
    const { redirect_uri } = await api("/api/auth/redirect-uri");
    $("redirect-uri").textContent = redirect_uri;
  } catch (_) { $("redirect-uri").textContent = "(couldn't load — refresh the page)"; }
}

function validateClientId() {
  const v = $("client-id").value.trim();
  const ok = CID_RE.test(v);
  $("client-id-hint").textContent =
    v ? (ok ? "Looks right — 32 characters." : "A Client ID is 32 letters and numbers.") : "";
  return ok;
}
$("client-id").oninput = validateClientId;

$("btn-copy-redirect").onclick = async () => {
  try {
    await navigator.clipboard.writeText($("redirect-uri").textContent);
    $("btn-copy-redirect").textContent = "Copied";
    setTimeout(() => { $("btn-copy-redirect").textContent = "Copy"; }, 1500);
  } catch {
    const r = document.createRange(); r.selectNode($("redirect-uri"));
    const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); // fallback: select for manual copy
  }
};

$("btn-auth-start").onclick = async () => {
  // Blank is allowed when reconnecting — the server falls back to the stored
  // Client ID and answers with a readable 400 if there isn't one.
  const clientId = $("client-id").value.trim();
  if (clientId && !validateClientId()) { $("client-id").focus(); return; }
  try {
    const { auth_url } = await api("/api/auth/start", { client_id: clientId });
    if (clientId) localStorage.setItem("spotifyClientId", clientId);
    // Same tab: Spotify's consent page redirects back to /auth/callback,
    // which lands on / as a fresh, now-authed load of the app.
    location.href = auth_url;
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
    loadNaming();
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

// Whether a row may be marked as a subset is the server's answer, read from
// `subset_eligible`, not re-derived here — so the chip can never disagree
// with what a save would actually do. That mattered more when eligibility
// was a name pattern; it still holds now that it is simply "do you own it",
// because ownership is the server's fact too. A pure function, same
// reasoning as splitDisabledReason above: unit-testable without the DOM.
function subsetChipHidden(p) {
  return !p.subset_eligible;
}

function makeListRow(p) {
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
      <button class="chip r-input">Buffer</button>
      <button class="chip r-home">Home</button>
      <button class="chip r-subset">Subset</button>
      <button class="pl-sort" title="Sort this input">▶</button>
      <button class="pl-split" title="Split into piles">⑃</button>
    </div>
    <input class="pl-hints" placeholder="matching hints, e.g. ambient, piano"
           title="Your own words about what belongs here — they join this home's tag profile (docs/matching.md)">`;
  const [bIn, bHome, bSubset, bSort, bSplit] = row.querySelectorAll("button");
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
    bSubset.classList.toggle("on-subset", roles[p.id] === "subset");
    bSubset.hidden = subsetChipHidden(p);
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
  bSubset.onclick = () => {
    roles[p.id] = roles[p.id] === "subset" ? null : "subset";
    paint();
  };
  bSort.onclick = () => { saveConfig().then(() => startTriage(p.id, p.name)); };
  bSplit.onclick = () => openSplit(p.id, p.name);
  paint();
  return row;
}

// Which input sets the user has expanded in the Lists view. The buffer set
// is the day-to-day one and stays open; the rest fold, so 26 inputs don't
// bury the playlists below them.
let listSetsOpen = {};
try { listSetsOpen = JSON.parse(localStorage.getItem("sortify-listsets") || "{}"); } catch (_) {}

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
  shown = shown.slice(0, CAP);

  // Inputs group into their sets; everything else keeps the flat listing.
  const bySet = new Map();
  const flat = [];
  for (const p of shown) {
    if (roles[p.id] === "input") {
      const k = p.input_set || NOW_BUFFER_SET;
      if (!bySet.has(k)) bySet.set(k, []);
      bySet.get(k).push(p);
    } else flat.push(p);
  }

  for (const [key, ps] of bySet) {
    const d = document.createElement("details");
    d.className = "pl-set";
    // Buffer open by default; a filter that matched inside a folded set
    // opens it too, so a search never hides its own results.
    d.open = key === NOW_BUFFER_SET ? listSetsOpen[key] !== false
                                    : (!!listSetsOpen[key] || !!q);
    const sum = document.createElement("summary");
    sum.textContent = `${setLabel(key)} · ${ps.length}`;
    d.appendChild(sum);
    for (const p of ps) d.appendChild(makeListRow(p));
    d.ontoggle = () => {
      listSetsOpen[key] = d.open;
      try { localStorage.setItem("sortify-listsets", JSON.stringify(listSetsOpen)); } catch (_) {}
    };
    wrap.appendChild(d);
  }
  for (const p of flat) wrap.appendChild(makeListRow(p));

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

// Naming-convention violations: computed server-side from the cached
// listing (zero Spotify calls), so checking on every view open is free.
// Renames are approved one at a time — never bulk — and each states its
// price on the button, like every other spending control here.
let namingOpen = false;
async function loadNaming() {
  let rows = [];
  try {
    rows = (await api("/api/naming")).violations || [];
  } catch (_) { /* a broken check must not break the Playlists view */ }
  const bar = $("pl-naming-bar");
  bar.hidden = rows.length === 0;
  if (!rows.length) { $("pl-naming-list").hidden = true; namingOpen = false; return; }
  $("pl-naming-status").textContent =
    `${rows.length} naming issue${rows.length === 1 ? "" : "s"}`;
  $("btn-naming-toggle").textContent = namingOpen ? "Hide" : "Show";
  $("btn-naming-toggle").onclick = () => { namingOpen = !namingOpen; renderNaming(rows); };
  renderNaming(rows);
}

function renderNaming(rows) {
  const list = $("pl-naming-list");
  list.hidden = !namingOpen;
  $("btn-naming-toggle").textContent = namingOpen ? "Hide" : "Show";
  if (!namingOpen) return;
  list.innerHTML = "";
  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "pl-row";
    row.innerHTML = `
      <div class="pl-meta">
        <div class="name">${esc(r.current)} → ${esc(r.proposed)}</div>
        <div class="sub">${esc(r.rule)}</div>
      </div>
      <button class="rename-btn">Rename (1 call)</button>`;
    const btn = row.querySelector("button");
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const res = await api(`/api/naming/${r.playlist_id}/rename`, {});
        toast(`renamed to ${res.renamed.to}`);
        await loadLists();   // re-reads cache + naming; the fixed row disappears
      } catch (e) {
        toast(e.message);
        btn.disabled = false;
        // A 409 means the listing moved on — show the current state.
        if (e.status === 409) await loadLists();
      }
    };
    list.appendChild(row);
  }
}

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

// Folder tree re-import: the box's own Spotify client, run headless, is the
// source — no file to export, no other machine. Slow (the sync step runs
// the client for ~a minute) but free: zero Web API calls.
$("btn-folders-refresh").onclick = async () => {
  const btn = $("btn-folders-refresh");
  const status = $("folders-refresh-status");
  btn.disabled = true;
  btn.textContent = "Re-importing…";
  status.textContent = "syncing the client's cache — about a minute";
  try {
    const res = await api("/api/folders/refresh", {});
    await loadLists();
    const changes = [];
    if (res.added) changes.push(`${res.added} added`);
    if (res.moved) changes.push(`${res.moved} moved`);
    if (res.dropped) changes.push(`${res.dropped} dropped`);
    status.textContent =
      `tree as of ${res.tree_as_of || "?"} — ${changes.join(", ") || "no changes"}; ` +
      `${res.homes_marked} homes marked`;
  } catch (e) {
    toast(e.message);
    status.textContent = "";
  } finally {
    btn.disabled = false;
    btn.textContent = "Re-import folder tree";
  }
};

async function saveConfig() {
  const input_ids = Object.keys(roles).filter((k) => roles[k] === "input");
  const home_ids = Object.keys(roles).filter((k) => roles[k] === "home");
  const subset_ids = Object.keys(roles).filter((k) => roles[k] === "subset");
  await api("/api/config", { input_ids, home_ids, home_hints: hintTexts, subset_ids });
}

// Save used to price itself: marking a subset meant reading that playlist
// to build a profile, so the button showed the pending call cost. Subsets
// are not scored any more and nothing is read, so there is no cost to state
// — a price of "0 calls" would be noise, and any price at all would be a lie.

$("btn-save-config").onclick = async () => {
  try {
    await saveConfig();
    toast("saved");
  } catch (e) { toast(e.message); }
};

// Creating a home from here is 1 call; the row appears in place, already
// marked Home, with no Refresh. The folder path stays blank until the next
// desktop-client folder export — not an error, homes work without one.
async function createHome(name) {
  const res = await api("/api/playlists/create", { name, role: "home" });
  const p = res.playlist;
  playlistData.unshift(p);
  roles[p.id] = "home";
  return { p, note: res.note };
}

$("btn-new-home").onclick = async () => {
  const name = $("new-home-name").value.trim();
  if (!name) return;
  const btn = $("btn-new-home");
  btn.disabled = true;
  try {
    const { p, note } = await createHome(name);
    $("new-home-name").value = "";
    renderLists();
    toast(note ? `created home "${p.name}" — ${note}` : `created home "${p.name}"`, note ? 5000 : undefined);
  } catch (e) { toast(e.message); } finally { btn.disabled = false; }
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
  if (tr.suggestions.length && tr.suggestions[0].weak) {
    suggHtml += '<p class="hint">No confident match — closest guesses:</p>';
  }
  tr.suggestions.forEach((s, i) => {
    const home = t.homes.get(s.playlist_id);
    if (!home) return;
    suggHtml += `<button class="sugg${s.already ? " already" : ""}${s.weak ? " weak" : ""}" data-to="${esc(s.playlist_id)}">
      <span class="s-pct">${s.already ? "" : s.pct + "%"}</span>
      <span class="s-name"><kbd>${i + 1}</kbd> ${esc(home.name)}</span>
      <span class="s-why">${esc([folderLeaf(home.folder), ...s.reasons].filter(Boolean).join(" · "))}</span>
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
    b.onclick = () => { if (previewHold.consumeClick()) return; moveTo(b.dataset.to); };
    previewHold.attach(b, b.dataset.to, triage.homes.get(b.dataset.to)?.name,
      { label: "File here", run: () => moveTo(b.dataset.to) });
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
    toast((res.note || `→ ${t.homes.get(toId)?.name ?? "moved"}`) + sweptSuffix(res.swept));
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
    await api("/api/undo", {});
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
  closeNavPop();
  stopNowPolling();
  triage = null;
  $("reconnect-hint").hidden = !statusData?.authed;
  loadSetup();
  // Reconnect reuses the server's stored ID — a prefilled field would suggest
  // it needs retyping, so blank wins over the localStorage prefill here.
  if (statusData?.authed) { $("client-id").value = ""; validateClientId(); }
  show("setup");
};

// ---- the header menu -------------------------------------------------------
//
// Four destinations behind one button. The panel borrows the input switcher's
// language wholesale — same card, same dismissal rules — because two popovers
// in one app that behave differently is a worse cost than the duplication.
// Every exit closes it: choosing a destination, Escape, or a tap outside. A
// nav panel left open sits on top of the view it just navigated to.
function openNavPop() {
  $("nav-pop").hidden = false;
  $("btn-nav-menu").setAttribute("aria-expanded", "true");
  $("btn-nav-menu").classList.add("open");
}

function closeNavPop() {
  $("nav-pop").hidden = true;
  $("btn-nav-menu").setAttribute("aria-expanded", "false");
  $("btn-nav-menu").classList.remove("open");
}

// The markup ships the panel hidden, but the closed state is owned here too:
// the keydown handler treats "nav-pop is open" as a claim on the keyboard, so
// a panel that is merely *believed* open swallows every shortcut in the app.
// One line, and that failure mode cannot exist.
closeNavPop();

$("btn-nav-menu").onclick = (e) => {
  if (e && e.stopPropagation) e.stopPropagation();
  if ($("nav-pop").hidden) openNavPop(); else closeNavPop();
};

document.addEventListener("click", (e) => {
  if ($("nav-pop").hidden) return;
  const t = e && e.target;
  if (t && t.closest && t.closest("#nav-pop, #btn-nav-menu")) return;
  closeNavPop();
});

$("nav-now").onclick = () => { closeNavPop(); showNow(); };
$("nav-lists").onclick = () => {
  closeNavPop();
  stopNowPolling(); stopNowTicker(); triage = null; loadLists();
};
$("nav-split").onclick = () => { closeNavPop(); showSplitPicker(); };

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
// Every filing action this session, oldest first. The undo stack is the
// server's; this mirrors ONLY what the client must undo locally, which is
// why each entry carries its kind: a subset add writes no filed state, so
// undoing one must not clear a filed badge that belongs to another track.
//
// Invariant: every path that changes `nowActions` makes the matching change
// to `nowActionLog`, in the same order, or `btn-undo-now`'s pop stops lining
// up with the server's own undo stack. The six sites: nowCapture, nowFile,
// nowRemove and nowAddToSubset push on increment; undoRemoval and
// btn-undo-now's onclick pop on decrement.
let nowActionLog = [];

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
    ? "Manual: fetches only when you press refresh — or when a song plays out while the tool is open. Tap to switch back to auto."
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

// Last /api/now/suggest payload, keyed by the track it was computed for, so
// unchanged-track polls merge it locally instead of asking the server again.
let nowSuggestCache = null;   // {uri, data}

// Fold a /api/now/suggest payload into nowState. `track_uri`/`playing` are
// the suggest response's own envelope, not card state — strip them.
function applySuggest(data) {
  const { track_uri, playing, ...rest } = data;
  nowState = { ...nowState, ...rest,
    suggPending: false, suggError: null,
    homes: new Map((data.homes || []).map((h) => [h.id, h])),
    subsetTargets: new Map((data.subset_targets || [])
      .map((s) => [s.id, { id: s.id, name: s.name, total: s.total, folder: s.folder }])) };
}

async function pollNow(force = false) {
  if ($("view-now").hidden) return;
  if (document.hidden) { scheduleNext(15000); return; }
  try {
    // Two-phase card: `light=1` answers with just the track — no profile
    // build, no Last.fm, no tag-map parsing on the server — so the card
    // goes up the moment Spotify answers. The suggestion side follows from
    // /api/now/suggest and is merged in when it lands.
    const data = await api("/api/now?light=1" + (force ? "&force=1" : ""));
    // When the server's answer left its upstream fetch (it may have served
    // its cache) — the anchor for the bar's last-update marker: the track
    // position Spotify actually confirmed, as opposed to where the local
    // ticker has extrapolated to since.
    nowFetchedAt = Date.now() - (data.fetched_ago_ms || 0);
    nowFetchedProgress = data.track?.uri
      ? { uri: data.track.uri, ms: data.progress_ms || 0 } : null;
    // The light payload has no inputs while playing; carry the previous
    // poll's over so the input switcher doesn't flash "no inputs yet"
    // between phases. The suggest merge refreshes them (with this track's
    // own has_track flags) moments later.
    // subsetTargets is carried across the light phase for the same reason
    // inputs are: it answers "which playlists exist to file into", which
    // changes when config or the listing changes — never per track. Rebuilt
    // empty here, tapping Add to subset… during the light phase would open
    // an empty picker.
    // `homes` and `homeless_id` ride along on the same argument, and for the
    // same payoff one step further: they are what the card needs to draw Add
    // to… and Homeless, so carrying them is what lets the whole card go up in
    // phase 1 with only the suggested rows still to come (see
    // ordinaryCardBody). Neither is per-track — `_homes_payload` and
    // `_homeless_id` read the listing and the config, nothing about the song.
    nowState = { ...data,
                 homes: nowState?.homes || new Map(),
                 homeless_id: data.homeless_id ?? nowState?.homeless_id ?? null,
                 subsetTargets: nowState?.subsetTargets || new Map(),
                 suggestions: [],
                 inputs: data.inputs || nowState?.inputs || [] };
    // A genuinely new track re-arms the played-out refetch (declared below)
    // — and so does the SAME track started over, which is what Spotify's
    // repeat-one does. Matching on uri alone meant repeat-one armed the
    // guard once and manual mode never refetched that track again.
    const dur = data.track?.duration_ms || 0;
    const restarted = dur && (data.progress_ms || 0) < dur * PLAYED_OUT_RESTART_FRACTION;
    if (playedOutUri && (data.track?.uri !== playedOutUri || restarted)) playedOutUri = null;
    const uri = data.track?.uri;
    const cached = nowSuggestCache && nowSuggestCache.uri === uri ? nowSuggestCache.data : null;
    if (cached) applySuggest(cached);
    // A force may sharpen suggestions (it is what unlocks the Last.fm
    // fetch), so it refetches even with a cached payload — but the cached
    // one still paints first, so nothing visibly goes blank meanwhile.
    const needsSuggest = !!(data.playing && data.track?.sortable && (force || !cached));
    if (!cached) nowState.suggPending = needsSuggest;
    renderNow();
    scheduleNext(data.poll_after_ms || 60000);
    if (needsSuggest) fetchNowSuggest(uri, force);
  } catch (e) {
    if (e.message === "auth needed") { stopNowPolling(); return; }
    renderNowProblem(e.message);
    scheduleNext(90000);
  }
}

async function fetchNowSuggest(uri, force = false) {
  try {
    const s = await api("/api/now/suggest" + (force ? "?force=1" : ""));
    // The track may have moved on while this computed — a stale payload is
    // dropped, never painted over the wrong card.
    if (!s.playing || s.track_uri !== uri) return;
    nowSuggestCache = { uri, data: s };
    if (!nowState || nowState.track?.uri !== uri) return;
    applySuggest(s);
    renderNow();
  } catch (e) {
    if (e.message === "auth needed") { stopNowPolling(); return; }
    if (!nowState || nowState.track?.uri !== uri) return;
    nowState.suggPending = false;
    nowState.suggError = e.message;
    renderNow();
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
  parkInputSwitch(/cooldown/.test(msg) ? "Spotify is cooling down" : "waiting for Spotify");
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
// It now stays up always: as the empty state's call to action, and in the
// error-ish states (cooldown, reauth) as a disabled placeholder — starting
// playback there would bounce off the same wall, but hiding the button made
// the whole top row reflow (see parkInputSwitch).
// The buffer set is the day-to-day one and stays open; every other set
// folds, because 26 inputs in one flat <select> is a list you hunt through
// rather than scan. A folded set still shows any playlist that CONTAINS the
// playing track — "peeking" — so you always learn the track is already
// filed there without expanding anything.
const NOW_BUFFER_SET = "buffer";
let nowSetsExpanded = false;
try { nowSetsExpanded = localStorage.getItem("sortify-sets-open") === "1"; } catch (_) {}

function setLabel(key) { return (key || NOW_BUFFER_SET).replace(/-/g, " "); }

// The last `d` paintNowControls saw — the popover re-renders from it on
// open and on fold-toggle, without waiting for another poll.
let nowCtl = null;

// The switcher never leaves the bar. It used to hide in the unusable states,
// which meant the whole top row reflowed — every button jumped right — the
// moment the first poll recognized the song. A parked, disabled placeholder
// holds the layout still and says why it's inert.
function parkInputSwitch(label) {
  const b = $("btn-input-switch");
  b.disabled = true;
  b.classList.add("placeholder");
  $("input-switch-label").textContent = label;
  closeInputPop();
}

function paintNowControls(d) {
  const playable = (d.inputs || []).filter((l) => l.id !== "liked");
  if (d.needs_reauth) { parkInputSwitch("reconnect Spotify first"); return; }
  if (d.cooldown) { parkInputSwitch("Spotify is cooling down"); return; }
  if (!playable.length) { parkInputSwitch("no inputs yet"); return; }
  nowCtl = d;
  $("btn-input-switch").disabled = false;
  // The trigger's face doubles as the old "playing from …" context line:
  // whatever is true about the playing context is what the button says.
  const ctx = d.context;
  $("input-switch-label").textContent = d.playing
    ? (ctx?.name
        ? (ctx.is_input ? ctx.name : `${ctx.name} (not an input)`)
        : "not playing from a playlist")
    : "start an input…";
  $("btn-input-switch").classList.toggle("placeholder", !(d.playing && ctx?.is_input));
  if (!$("input-pop").hidden) renderInputPop();
}

// Which buffer list to put on when you don't care which — the drawn pick
// costs nothing but the one play call it ends in, because the whole pool
// arrives with the poll that already painted the switcher. `rand` is a
// parameter so the draw is testable rather than sampled.
//
// The playing list leaves the pool first: a "random" button that restarts
// what you are already hearing reads as broken, not as a coincidence. It
// comes back only when removing it would leave nothing to draw from — one
// buffer list is still an answer, just a foregone one.
function pickRandomBuffer(inputs, currentId, rand = Math.random) {
  const pool = (inputs || []).filter(
    (l) => l.id !== "liked" && (l.set || NOW_BUFFER_SET) === NOW_BUFFER_SET);
  const fresh = pool.filter((l) => l.id !== currentId);
  const from = fresh.length ? fresh : pool;
  return from.length ? from[Math.floor(rand() * from.length)] : null;
}

const ICON_DIE = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="3"/><circle cx="9" cy="9" r="1.2" fill="currentColor" stroke="none"/><circle cx="15" cy="15" r="1.2" fill="currentColor" stroke="none"/></svg>';

function renderInputPop() {
  if (!nowCtl) return;
  const d = nowCtl;
  const playable = (d.inputs || []).filter((l) => l.id !== "liked");
  const current = d.playing ? d.context?.id : null;

  const bySet = new Map();
  for (const l of playable) {
    const k = l.set || NOW_BUFFER_SET;
    if (!bySet.has(k)) bySet.set(k, []);
    bySet.get(k).push(l);
  }
  const row = (l) =>
    `<button id="ip-${esc(l.id)}" class="ip-row${l.id === current ? " current" : ""}">` +
    `<span class="ip-name">${esc(l.name)}</span>` +
    (l.has_track ? `<span class="ip-dot" title="already contains this track"></span>` : "") +
    `</button>`;

  let html = "";
  let hidden = 0;
  const shownAll = [];
  for (const [key, lists] of bySet) {
    const open = key === NOW_BUFFER_SET || nowSetsExpanded;
    // A folded set is never fully silent: the playing track's own lists,
    // and whatever is currently playing, still surface.
    const shown = open ? lists : lists.filter((l) => l.has_track || l.id === current);
    hidden += lists.length - shown.length;
    if (!shown.length) continue;
    shownAll.push(...shown);
    const note = open || shown.length === lists.length
      ? "" : ` · ${shown.length} of ${lists.length}`;
    if (!(key === NOW_BUFFER_SET && bySet.size === 1))
      html += `<div class="ip-set">${esc(setLabel(key) + note)}</div>`;
    // Heads the buffer set, because that is the set it draws from — and only
    // when there is an actual draw to make. With one buffer list the row
    // would be a second button for the list sitting right under it.
    if (key === NOW_BUFFER_SET && lists.length > 1)
      html += `<button id="ip-random" class="ip-row ip-random" ` +
        `title="Put on one of your buffer lists at random">${ICON_DIE}` +
        `<span class="ip-name">random buffer list</span></button>`;
    html += shown.map(row).join("");
  }
  if (hidden || nowSetsExpanded)
    html += `<button id="btn-now-sets" class="ip-more">${
      nowSetsExpanded ? "fewer sets" : `show all sets (+${hidden})`}</button>`;

  const pop = $("input-pop");
  pop.innerHTML = html;
  // innerHTML replaced the nodes, so the bindings go per render — the same
  // reason renderNow rewires its card-internal buttons every time.
  for (const l of shownAll) $("ip-" + l.id).onclick = () => pickInput(l.id);
  const die = $("ip-random");
  // Drawn at click, not at render: the pool the poll last painted is the
  // pool you meant, and a row that decided its answer minutes ago would
  // start a list you have since moved on from.
  if (die) die.onclick = () => {
    const pick = pickRandomBuffer(playable, current);
    if (pick) pickInput(pick.id, pick.name);
  };
  const more = $("btn-now-sets");
  if (more) more.onclick = () => {
    nowSetsExpanded = !nowSetsExpanded;
    try { localStorage.setItem("sortify-sets-open", nowSetsExpanded ? "1" : "0"); } catch (_) {}
    renderInputPop();
  };
}

function openInputPop() {
  renderInputPop();
  $("input-pop").hidden = false;
  $("btn-input-switch").classList.add("open");
}

function closeInputPop() {
  $("input-pop").hidden = true;
  $("btn-input-switch").classList.remove("open");
}

$("btn-input-switch").onclick = (e) => {
  if (e && e.stopPropagation) e.stopPropagation();
  if ($("input-pop").hidden) openInputPop(); else closeInputPop();
};

// Tapping anywhere else dismisses the panel; clicks inside it (rows, the
// fold toggle) are its own business.
document.addEventListener("click", (e) => {
  if ($("input-pop").hidden) return;
  const t = e && e.target;
  if (t && t.closest && t.closest("#input-pop, #btn-input-switch")) return;
  closeInputPop();
});

// `label` names the list in the toast. Picking a row by hand needs no such
// echo — you just read the name you tapped — but the random draw does, or
// the button is a black box you have to wait out the bar to decode.
async function pickInput(id, label = null) {
  closeInputPop();
  try {
    await api("/api/player/play", { input_id: id });
    toast(label ? `starting ${label}…` : "starting…");
    repollAfterPlaybackChange();
  } catch (err) {
    toast(err.message);
  }
}

// Spotify needs a moment to settle after a skip before it reports the new
// track; the server has already dropped its cached answer, so this poll is
// guaranteed to go upstream rather than repeat what we just replaced.
function repollAfterPlaybackChange(prevUri = null) {
  setTimeout(async () => {
    await pollNow(true);
    // If Spotify hadn't settled and the poll still got the pre-skip track:
    // in auto mode the server chases it by itself (an unsettled answer gets
    // only a short TTL, so poll_after_ms comes right back). Manual mode
    // schedules nothing, so the one follow-up poll happens here — timed
    // past that settle TTL, and still part of the same click.
    if (nowManual && prevUri && nowState?.playing && nowState.track?.uri === prevUri)
      setTimeout(() => pollNow(), 3600);
  }, 900);
}

// The press-to-effect gap on the two verbs, made visible.
//
// Both take a moment the card cannot hide: Next waits ~900ms for Spotify to
// settle before the repoll can show the new track, and Remove waits on
// /api/act. Until this existed, Next disabled its button for the length of
// the POST and re-enabled it immediately — so for about a second the card sat
// unchanged with a ready-looking button, which reads as "nothing happened",
// and a second press skipped a second track.
//
// Rendered state, not a DOM flag: every poll re-runs renderNow and rebuilds
// these buttons, so a `btn.disabled` set by hand survives only until the next
// poll lands. It carries the uri it was pressed on because that is how Next
// completes — see renderNow.
let npPending = null;        // {verb: "next"|"remove"|"both", uri} while one is in flight
let npPendingTimer = null;

function setNpPending(verb, uri) {
  npPending = { verb, uri };
  clearTimeout(npPendingTimer);
  // A stuck spinner is worse than no spinner. If the track never changes —
  // Spotify not advancing, an answer that never lands — the button has to
  // come back rather than stay dead until something else happens to render.
  npPendingTimer = setTimeout(() => {
    if (npPending?.verb === verb && npPending?.uri === uri) { npPending = null; renderNow(); }
  }, 6000);
  renderNow();
}

function clearNpPending() {
  if (!npPending) return;
  npPending = null;
  clearTimeout(npPendingTimer);
  npPendingTimer = null;
}

// Card-internal control (re-created by every renderNow), so it's wired per
// render rather than once at load like the static controls below.
async function playerNext() {
  // The guard is the rendered state, not the button's own disabled attribute:
  // the keyboard reaches this function directly, bypassing the button.
  if (npPending) return;
  const prevUri = nowState?.track?.uri || null;
  setNpPending("next", prevUri);
  try {
    await api("/api/player/next", {});
    // Deliberately NOT cleared here. The POST returning only means Spotify
    // accepted the skip; the press is not finished until the new track is on
    // screen, which is what the settle repoll brings (and renderNow clears on).
    repollAfterPlaybackChange(prevUri);
  } catch (e) {
    clearNpPending();
    renderNow();
    toast(e.message);
  }
}

// Same shape as playerNext: the track changes, so the settle repoll applies.
async function playerPrev() {
  const btn = $("btn-now-prev");
  if (btn) btn.disabled = true;
  const prevUri = nowState?.track?.uri || null;
  try {
    await api("/api/player/previous", {});
    repollAfterPlaybackChange(prevUri);
  } catch (e) {
    toast(e.message);
  } finally {
    const b = $("btn-now-prev");
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
    // At track end stop the ticker; in auto mode the poll schedule (whose
    // TTL is exactly the track's remaining runtime) repaints with the real
    // next track, and manual mode gets its one played-out exception below.
    if (p >= tr.duration_ms) { stopNowTicker(); refetchAtPlayedOut(tr.uri); }
  }, 1000);
}

// Tap or drag the progress bar to move within the song.
//
// The call goes on RELEASE, and only on release. A seek per pointermove would
// turn one gesture into dozens of them and walk straight into WINDOW_CAP
// (12/60s, shared with the polls) — so the drag is pure local paint and the
// gesture spends exactly one call, the same as Next.
//
// Rewired by every renderNow along with the rest of the strip, because the
// node it binds to is rebuilt every time.
function wireSeekBar(tr) {
  const bar = $("np-bar");
  // No geometry under the test harness's stub DOM, and nothing to seek within
  // a local file or an episode.
  if (!bar || !tr.duration_ms || typeof bar.getBoundingClientRect !== "function") return;
  let dragging = false;
  const at = (e) => {
    const r = bar.getBoundingClientRect();
    if (!r.width) return null;
    return Math.round(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * tr.duration_ms);
  };
  // Local paint only — the same two nodes the ticker writes to.
  const paint = (ms) => {
    const fill = $("np-fill"), elapsed = $("np-elapsed");
    if (fill) fill.style.width = ((ms / tr.duration_ms) * 100).toFixed(2) + "%";
    if (elapsed) elapsed.textContent = fmtTime(ms);
  };
  bar.onpointerdown = (e) => {
    const ms = at(e);
    if (ms === null) return;
    dragging = true;
    // The ticker would fight the finger, repainting the old position every
    // second while you drag. It comes back anchored to wherever you land.
    stopNowTicker();
    bar.setPointerCapture?.(e.pointerId);
    bar.classList?.add("seeking");
    paint(ms);
  };
  bar.onpointermove = (e) => { if (dragging) { const ms = at(e); if (ms !== null) paint(ms); } };
  bar.onpointercancel = () => {
    if (!dragging) return;
    dragging = false;
    bar.classList?.remove("seeking");
    startNowTicker(nowState, tr);   // put the clock back where it was
  };
  bar.onpointerup = (e) => {
    if (!dragging) return;
    dragging = false;
    bar.classList?.remove("seeking");
    const ms = at(e);
    if (ms === null) { startNowTicker(nowState, tr); return; }
    nowSeek(ms, tr);
  };
}

async function nowSeek(ms, tr) {
  try {
    await api("/api/player/seek", { position_ms: ms });
    // Re-anchor locally rather than poll: we know where we just put the head,
    // and the server patched its own cached answer with the same number (see
    // player_seek), so nothing has to be asked. A seek costs one call, total.
    if (nowState) {
      nowState.progress_ms = ms;
      // The bar's last-update mark described the old position and would sit
      // on the wrong side of the fill now. The next poll re-establishes it.
      nowFetchedProgress = null;
      startNowTicker(nowState, tr);
      renderNow();
    }
  } catch (e) {
    toast(e.message);
    // Whatever Spotify is actually doing, the local bar no longer knows —
    // repaint from the last poll and let the schedule sort it out.
    if (nowState) startNowTicker(nowState, tr);
  }
}

// The card believes the song just played out. In auto mode the server's
// schedule lands on this same moment, so only manual mode has to act — and
// this is manual mode's ONE exception to "no automatic fetches": the song is
// provably over and the tool is open in front of the user, so the fetch is
// their real listening, not background traffic. One fetch per played-out
// track — if Spotify comes back with the same uri (stopped at the end, or
// not yet advanced), we do not chase it. A hidden tab remembers instead and
// fetches on return (see visibilitychange).
let playedOutUri = null;
let playedOutWhileHidden = false;
// A FRACTION of the runtime, not a fixed number of seconds: "back near the
// beginning" is what distinguishes a track that started again (repeat-one)
// from one still sitting at the end we already refetched for — and a fixed
// threshold reads a short track's ending as a restart.
const PLAYED_OUT_RESTART_FRACTION = 0.25;

function refetchAtPlayedOut(uri) {
  if (!nowManual || $("view-now").hidden || playedOutUri === uri) return;
  if (document.hidden) { playedOutWhileHidden = true; return; }
  playedOutUri = uri;
  pollNow();
}

// Freshness lives in the bar: a thin yellow marker inside the green fill at
// the track position Spotify last confirmed. The fill running ahead of it is
// the local ticker's extrapolation, so the gap between marker and fill tip
// IS the age — visible at a glance, no number. The server's cache can
// legitimately serve for a track's whole runtime, and in blind mode the card
// itself is blurred, so the marker is the one visible proof the app actually
// knows what's playing. Zero requests.
let nowFetchedAt = null;       // Date.now()-anchored time of the upstream fetch
let nowFetchedProgress = null; // {uri, ms}: the progress that fetch confirmed

const ICON_PLAY = '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M7 5h3.6v14H7zM13.4 5H17v14h-3.6z"/></svg>';
const ICON_NEXT = '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M6 5.5v13l8.5-6.5zM16.5 5.5h2v13h-2z"/></svg>';
const ICON_PREV = '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M18 5.5v13L9.5 12zM5.5 5.5h2v13h-2z"/></svg>';
// Feather's `repeat`: two arrows chasing each other round the list.
const ICON_LOOP = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>';

// A list losing a line — "take this out of the input", not "delete the song".
const ICON_REMOVE = '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M3 6h12v2H3zM3 11h12v2H3zM3 16h8v2H3zM14.5 15h7v2h-7z"/></svg>';
// The magnifier the card uses wherever a control opens a searchable list —
// Add to… wears the 18px one inline; this is the chip-sized twin.
const ICON_SEARCH_SM = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
const ICON_UNDO = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 9h10a5 5 0 0 1 0 10H9"/><path d="M8 5 4 9l4 4"/></svg>';

// The input you were last playing from. Persisted, because the moment you
// most want it named is a reload in the middle of an autoplay tail — the
// context is gone from the poll by then, and the session's memory of it with
// it. Id AND name: the name is what the banner says, the id is what the
// button plays.
let lastInput = null;
try { lastInput = JSON.parse(localStorage.getItem("sortify-lastinput") || "null"); } catch (_) {}

function rememberInput(ctx) {
  if (!ctx?.is_input || !ctx.id) return;
  if (lastInput?.id === ctx.id && lastInput?.name === ctx.name) return;
  lastInput = { id: ctx.id, name: ctx.name || "" };
  try { localStorage.setItem("sortify-lastinput", JSON.stringify(lastInput)); } catch (_) {}
}

// "The list ran out and Spotify carried on by itself."
//
// Deliberately narrow, because a banner that cries wolf gets ignored. All
// four conditions are about being sure the app has nothing to do with what is
// playing: you are not playing from an input, and the song is in none of your
// inputs and none of your homes. Put a home on deliberately, or play anything
// you have already filed, and this stays silent — the case it catches is the
// one where the tool is running but no longer sorting anything.
//
// The suggest phase gates it because membership is exactly what that phase
// answers: during phase 1 `suggestions` is empty, and reading that as "in no
// home" would flash the banner onto every fresh card.
function unfiled(d) {
  if (!d.playing || d.suggPending || d.suggError) return false;
  if (d.context?.is_input) return false;
  if ((d.inputs || []).some((l) => l.has_track)) return false;
  if ((d.suggestions || []).some((s) => s.already)) return false;
  return true;
}

// The banner is strictly narrower than the rows, and the two must not be
// confused again. Being unfiled is a fact about the SONG — it is in no inbox
// and no home — and the inbox rows answer it wherever it occurs, including on
// a playlist you deliberately put on. The banner is about PLAYBACK having
// drifted out of an inbox on its own: the list ran out and Spotify carried
// on. Choosing to play Discover Weekly is not drift, and telling you it is
// "not one of your inputs" is a complaint about a decision you just made.
//
// No playlist context at all is the signature. (It also covers an album or a
// single track played from search, which is why the head line hedges.)
function adrift(d) { return unfiled(d) && !d.context; }

// A list running dry, drawn as one: the rows stop and the arrow carries on
// past where they ended.
const ICON_ADRIFT = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 6h10M4 11h7M4 16h4"/><path d="M14 19h6m-3-3 3 3-3 3"/></svg>';

function adriftBanner(d) {
  if (!adrift(d)) return "";
  const name = lastInput?.name;
  // Only the autoplay tail reaches here now (see adrift), so the banner names
  // the list that ran out when it knows it and stays vague when it does not.
  // The old third branch — "Playing <X> — not one of your inputs" — is gone
  // with the condition that produced it: it fired on any playlist that was
  // not an input, which meant deliberately putting on Discover Weekly got a
  // banner scolding you for it. Worse, it could rarely name the playlist,
  // because a Spotify-owned one is absent from the cached listing both
  // `_light_context` and the suggest payload read, so it usually read
  // "Playing something else" — a complaint that could not even say about
  // what. Naming those costs an API call per unknown playlist; not
  // complaining costs nothing.
  const head = name ? `${esc(name)} ran out — autoplay took over`
                    : "Not playing from an input — autoplay took over";
  return `<div class="np-adrift">
    <span class="ad-head">${ICON_ADRIFT}<b>${head}</b></span>
    <span class="ad-sub">not in any of your inboxes — pick one below</span>
    ${name ? `<button id="btn-now-back" class="ad-back">Play ${esc(name)} again</button>` : ""}
  </div>`;
}

// The strip under the title: the progress line (elapsed / bar / total) with
// the two quiet controls tucked at its right end, then the verb row.
// Local files and episodes carry no duration; their progress line is just
// the quiet controls, right-aligned where they always live.
function playbackStrip(d, tr) {
  // Previous and pause are rare moves — previous is oops-recovery after a
  // too-eager Next, pause is hardly part of the filing loop at all — so they
  // live as small ghost discs flanking the bar line, out of the verb row
  // entirely: previous at the left end (back = left, guessable without
  // reading it), pause at the right.
  const prevBtn = `<button id="btn-now-prev" class="np-mini"
        title="Back to the previous track" aria-label="Back to the previous track">${ICON_PREV}</button>`;
  const pauseBtn = `<button id="btn-now-toggle" class="np-mini"
        title="${d.is_playing ? "Pause" : "Play"}" aria-label="${d.is_playing ? "Pause" : "Play"}">${d.is_playing ? ICON_PAUSE : ICON_PLAY}</button>`;
  const loopBtn = loopButton(d);
  // The last-update marker: pinned where the server's answer put the track,
  // regardless of what a local re-render (pause toggle, filing) has done to
  // d.progress_ms since. Only rendered for the track it was measured on.
  const markPct = nowFetchedProgress && nowFetchedProgress.uri === tr.uri && tr.duration_ms
    ? Math.min(100, (nowFetchedProgress.ms / tr.duration_ms) * 100).toFixed(2)
    : null;
  const bar = tr.duration_ms
    ? `<div class="np-progress">
      ${prevBtn}
      <span id="np-elapsed" class="np-time">${fmtTime(d.progress_ms || 0)}</span>
      <span id="np-bar" class="np-bar" role="slider" tabindex="-1"
            aria-label="Position in the track" aria-valuemin="0"
            aria-valuemax="${tr.duration_ms}" aria-valuenow="${d.progress_ms || 0}"
            title="Tap or drag to move within the song"><span id="np-fill" style="width:${Math.min(100, ((d.progress_ms || 0) / tr.duration_ms) * 100).toFixed(2)}%"></span>${
        markPct !== null ? `<span id="np-fill-mark" style="left:${markPct}%"></span>` : ""
      }</span>
      <span class="np-time">${fmtTime(tr.duration_ms)}</span>
      ${pauseBtn}${loopBtn}
    </div>`
    : `<div class="np-progress np-progress-bare">${prevBtn}${pauseBtn}${loopBtn}</div>`;
  // Once this track has been removed — or filed, or captured, or added to a
  // subset: any action of its own sitting on top of the undo stack — the
  // slot offers the way back instead. The undo belongs where the hand
  // already is, not in the top bar, and it lasts exactly as long as the
  // track does (the log's top entry stops matching the moment the next
  // track starts; removedUri has its own expiry in renderNow). Sittings are
  // excluded: their decisions have no /api/undo.
  const lastAct = nowActionLog[nowActionLog.length - 1];
  // While a combined press is settling, the trio below must hold: the remove
  // leg has already landed (removedUri is set), and without this suppression
  // the strip would flash its Undo swap for the second the skip leg takes.
  const bothBusy = npPending?.verb === "both" && npPending.uri === tr.uri;
  const undoable = !bothBusy && ((removedUri && removedUri === tr.uri) ||
    (!d.sitting && lastAct && lastAct.uri === tr.uri));
  const nextBusy = npPending?.verb === "next";
  const nextBtn = (shape) => `<button id="btn-now-next" class="${shape}${
    nextBusy ? " np-busy" : ""}"${nextBusy ? " disabled" : ""} title="${
    nextBusy ? "Skipping…" : "Skip to the next track"}">${ICON_NEXT}<span class="np-verb-label">${
    nextBusy ? "Skipping…" : "Next"}</span></button>`;
  // The verb row is the notched trio: Remove and Next extended toward each
  // other, each ending in a concave cradle, with the combined Remove+Next
  // circle in the notch — the moat around the circle IS the mis-tap buffer
  // the old column gap used to be. It renders exactly when the left slot
  // holds a Remove; the circle mirrors that button's live/dead state. With
  // an Undo on offer, or in a sitting, combining is moot and the row falls
  // back to the two-slot layout below, so Next keeps its place either way.
  if (!undoable && !d.sitting) {
    // np-buttons-trio: the wrapper's grid is for the slot pair — a trio left
    // inside it lands in the first 1fr column, ~25px left of centre.
    return `${bar}<div class="np-buttons np-buttons-trio"><div class="np-trio">
      ${removeButton(d)}${bothButton(d)}${nextBtn("np-nshape np-nnext")}
    </div></div>`;
  }
  // A sitting renders this strip too, and an input context never occurs
  // there — an always-drawn Remove could only ever be dead weight, so the
  // one place the slot still empties is the one place the verb can never
  // apply.
  const removeBtn = undoable
    ? `<button id="btn-now-undo-remove" class="np-round np-wide np-undo"
               title="Undo the last action for this track (u)" aria-label="Undo the last action for this track">${ICON_UNDO}<span class="np-verb-label">Undo</span></button>`
    : "";
  return `${bar}<div class="np-buttons">
    <span class="np-slot np-remove-slot">${removeBtn}</span>
    <span class="np-slot np-next-slot">
      ${nextBtn("np-round np-wide np-next")}
    </span>
  </div>`;
}

// The loop toggle, and the one case where it is not drawn at all.
//
// `repeat` is null when the answer is unknown — a token minted before the
// read-playback-state scope existed, or an account where /me/player turned
// out to be unusable (see Spotify.currently_playing). A toggle that cannot
// read the state can only show what it last SET, which is a button that lies
// after any change made in Spotify itself. So it stays away until the answer
// is real: log in again and it appears.
//
// Two states to press between, off and looping-the-list, because that is the
// question being asked here — "does this inbox run out or come round again".
// Spotify's third mode, repeat-one, is shown honestly if something else set
// it (the 1 badge) and pressing turns it off, but nothing here offers it.
function loopButton(d) {
  if (d.repeat == null) return "";
  const on = d.repeat === "context" || d.repeat === "track";
  const one = d.repeat === "track";
  const title = one ? "Repeating this track — press to stop looping"
              : on ? "Looping the list — press to stop"
                   : "Loop the list";
  return `<button id="btn-now-loop" class="np-mini np-loop${on ? " on" : ""}"
        title="${title}" aria-label="${title}" aria-pressed="${on}">${ICON_LOOP}${
    one ? '<span class="loop-one">1</span>' : ""}</button>`;
}

async function nowLoop() {
  const d = nowState;
  if (!d || d.repeat == null) return;
  const before = d.repeat;
  const next = before === "off" ? "context" : "off";
  // Optimistic: the button is the kind of control that must answer the press
  // immediately, and the server patches its cached answer with the same value
  // rather than re-reading it (see player_repeat), so there is nothing to
  // wait for that we do not already know.
  d.repeat = next;
  renderNow();
  try {
    await api("/api/player/repeat", { state: next });
  } catch (e) {
    d.repeat = before;
    renderNow();
    toast(e.message);
  }
}

// Whether Remove can act right now, and what to say when it can't.
//
// Two ways it can't. You are not playing from an input at all — there is no
// list for the verb to remove from. Or you are, but the song has already left
// it: file a track and reload, and the strip used to keep offering to remove
// a song the input no longer held. Only the membership flag knows that, which
// is why the payload's `inputs` row is consulted and not just the context.
//
// Phase 1 is the subtlety. The light poll carries `context.is_input` but no
// membership — `inputs` arrives with the suggest phase a second later — so a
// missing row means "not known yet", never "the song left". Treating the two
// alike flashed a grey button on every fresh card.
function removeState(d) {
  if (!d?.context?.is_input)
    return { live: false, why: "Not playing from an input list — nothing to remove from" };
  const row = (d.inputs || []).find((l) => l.id === d.context.id);
  if (row && !row.has_track)
    return { live: false, why: `No longer in ${d.context.name || "that list"}` };
  return { live: true, why: null };
}

// Drawn even when it cannot act. An empty slot taught the eye that the verb
// comes and goes and answered nothing when it was missing; greyed, it stays
// where the thumb expects it and can say why.
//
// aria-disabled rather than the `disabled` attribute, deliberately: a
// disabled button swallows the tap, and being pressable — so `nowRemove` can
// toast the reason — is the entire point of the dead state.
// The outline that clip-path cannot draw: clipping a bordered button cuts
// the border away along the cradle, so the notched Remove paints its own
// edge as a stroked path. Geometry shared with the clip-paths in style.css
// (keep the two in step): a 56px circle (r 28) plus an 11px moat cuts a
// 39px-radius cradle out of each 164px pill end; the cut crosses the pill's
// straight edges 27.1px from the trio's 150px centre (sqrt(39² − 28²)).
const NP_REMOVE_EDGE = '<svg class="np-edge" viewBox="0 0 164 56" aria-hidden="true"><path d="M28 0 L122.9 0 A39 39 0 0 0 122.9 56 L28 56 A28 28 0 0 1 28 0 Z"/></svg>';

function removeButton(d) {
  const { live, why } = removeState(d);
  const busy = npPending?.verb === "remove";
  const title = busy ? "Removing…" : live ? "Remove from input (r)" : why;
  return `<button id="btn-now-remove" class="np-nshape np-nremove ${
    live ? "np-danger" : "np-dead"}${busy ? " np-busy" : ""}"${
    live ? "" : ' aria-disabled="true"'} title="${esc(title)}" aria-label="${esc(title)}">${
    NP_REMOVE_EDGE}${ICON_REMOVE}<span class="np-verb-label">${busy ? "Removing…" : "Remove"}</span></button>`;
}

// The circle in the notch: the Remove verb plus a skip, one press. It
// mirrors Remove's live/dead matrix — and like Remove it is aria-disabled
// rather than disabled when dead, because being pressable is the point: the
// tap reaches nowBoth, which says why it cannot act.
function bothButton(d) {
  const { live, why } = removeState(d);
  const busy = npPending?.verb === "both";
  const title = busy ? "Removing…"
              : live ? "Remove from input and skip to the next track" : why;
  return `<button id="btn-now-both" class="np-both${live ? "" : " np-dead"}${
    busy ? " np-busy" : ""}"${live ? "" : ' aria-disabled="true"'} title="${
    esc(title)}" aria-label="${esc(title)}">${ICON_REMOVE}${ICON_NEXT}</button>`;
}

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
  // A blind-mode peek lasts exactly one track: the reveal falls away the
  // moment the playing uri is no longer the one that was peeked.
  if (document.body.classList.contains("peeked") && d.track?.uri !== peekedUri) {
    document.body.classList.remove("peeked");
    peekedUri = null;
  }
  // Same expiry, same reason: the strip's undo offer belongs to one track.
  if (removedUri && d.track?.uri !== removedUri) removedUri = null;
  // Next's completion signal. What the press was FOR is a different track, so
  // its arrival is the finish line — no callback to thread through the settle
  // repoll, and it works whichever poll happens to bring the new song.
  if (npPending && d.track?.uri && d.track.uri !== npPending.uri) clearNpPending();
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

  const img = tr.image ? `<img src="${esc(tr.image)}" alt="">` : '<div class="noimg"></div>';
  const artists = tr.artists.map((a) => a.name).join(", ");

  const body = inSitting ? sittingCardBody(tr, d.sitting) : ordinaryCardBody(d, tr, ctx);

  // Share needs a real track on screen; local files and episodes have no
  // spotify:track: uri and the tablet flow can't find them by title anyway.
  // It rides the card's corner beside the cover — the whitespace flanking
  // the art is exactly where a once-in-a-while control can sit without
  // costing the layout anything.
  const shareBtn = tr.uri?.startsWith("spotify:track:")
    ? `<button id="btn-share" class="icon-btn np-share" title="Send this song to a friend — Spotify Messages, via the tablet">
        <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/></svg>
      </button>`
    : "";

  rememberInput(d.context);
  nowProblem = false;  // a real card for a real track is about to go up
  // Fresh only for a track this card has not drawn yet: a re-render (a poll,
  // a pause toggle, the suggestions arriving) is not an arrival.
  const cardFresh = enterOnce("card", tr.uri) ? " card-fresh" : "";
  $("now-card").innerHTML = `<div class="track-card${cardFresh}${d.is_playing ? "" : " is-paused"}">
    ${shareBtn}
    ${adriftBanner(d)}
    <div class="art">${img}${d.is_playing ? "" : '<span class="paused-chip">paused</span>'}</div>
    <div class="t-name">${esc(tr.name)}</div>
    <div class="t-artist">${esc(artists)}${tr.album ? " — " + esc(tr.album) : ""}</div>
    ${playbackStrip(d, tr)}
    ${body}
  </div>`;
  const tog = $("btn-now-toggle");
  if (tog) tog.onclick = playerToggle;
  const nxt = $("btn-now-next");
  if (nxt) nxt.onclick = playerNext;
  const prv = $("btn-now-prev");
  if (prv) prv.onclick = playerPrev;
  const loop = $("btn-now-loop");
  if (loop) loop.onclick = nowLoop;
  // Wired with the other strip controls rather than in the ordinary-card
  // branch below: the button is part of the strip now, and the strip renders
  // for the sitting card too (where an input context — and so this button —
  // simply never occurs).
  const rem = $("btn-now-remove");
  if (rem) rem.onclick = nowRemove;
  const both = $("btn-now-both");
  if (both) both.onclick = nowBoth;
  const undoRem = $("btn-now-undo-remove");
  if (undoRem) undoRem.onclick = undoStripAction;
  const sh = $("btn-share");
  if (sh) sh.onclick = openSharePop;
  const back = $("btn-now-back");
  if (back) back.onclick = () => pickInput(lastInput.id, lastInput.name);
  startNowTicker(d, tr);
  wireSeekBar(tr);

  if (inSitting) {
    wireSittingCard();
  } else {
    // [data-to]: the Add to… row is a .sugg for layout's sake but has no
    // destination, and a preview hold on it would have nothing to play.
    $("now-card").querySelectorAll(".sugg[data-to]").forEach((b) => {
      b.onclick = () => { if (previewHold.consumeClick()) return; nowFile(b.dataset.to); };
      previewHold.attach(b, b.dataset.to, nowState.homes.get(b.dataset.to)?.name,
        { label: "File here", run: () => nowFile(b.dataset.to) });
    });
    // The adrift card's inbox rows. No previewHold: these are inboxes, and
    // hearing what is already in one tells you nothing about whether a song
    // you have not judged belongs there — the hold is for homes, where the
    // pile IS the answer to "does this fit".
    $("now-card").querySelectorAll(".sugg[data-cap]").forEach((b) => {
      b.onclick = () => nowCapture(b.dataset.cap);
    });
    const more = $("btn-now-more");
    if (more) more.onclick = openNowPicker;
    const sub = $("btn-now-subset");
    if (sub) sub.onclick = () => openPicker(nowState.subsetTargets, nowAddToSubset);
    const nh = $("btn-now-homeless");
    if (nh) nh.onclick = nowHomeless;
    capSuggScroll();
    // The chips are inert markers now — the only control in that row is the
    // one that opens the picker.
    const cap = $("btn-now-capture");
    if (cap) cap.onclick = openCapturePicker;
  }
}

// Three rows tall, always — the same height whether the card has one
// suggestion or six. Sizing it to where the fourth row happened to start meant
// a short list drew a short box, so the card's whole lower half moved every
// time a track changed; a fixed frame with room to spare below two rows is
// worth more than the space it wastes. Still measured rather than declared in
// CSS, because a row's real height depends on the rendered font.
const SUGG_VISIBLE = 3;

function suggRowPitch(height, marginBottom) {
  return height + marginBottom;
}

function suggScrollHeight(pitch, visible = SUGG_VISIBLE) {
  return pitch * visible;
}

function capSuggScroll() {
  const box = $("now-card").querySelector(".sugg-scroll");
  const rows = box && box.querySelectorAll ? [...box.querySelectorAll(".sugg")] : [];
  // No layout under the test harness's stub DOM — the arithmetic above is
  // pinned there instead. Measure an ordinary suggestion in preference to the
  // Add to… row, whose dashed border makes it a couple of pixels taller.
  if (!rows.length || rows[0].offsetHeight === undefined) return;
  if (typeof getComputedStyle !== "function") return;
  const row = rows.find((r) => !r.className.includes("sugg-more")) || rows[0];
  const pitch = suggRowPitch(row.offsetHeight,
                             parseFloat(getComputedStyle(row).marginBottom) || 0);
  box.style.height = `${suggScrollHeight(pitch)}px`;
}

// Entry animations, granted once each.
//
// Every poll rebuilds the Now card's innerHTML from scratch, so any CSS
// animation on a fresh node replays on each of them: the card slid in again
// every poll, and a filed track's check redrew itself whenever anything
// touched the card — pressing Next after filing most visibly, which is the
// one moment nothing new has happened. So the animating class is granted, not
// declared: `enterOnce(key, token)` answers true exactly once per token
// (normally the track uri), and passing null arms it again for next time.
const entered = {};
function enterOnce(key, token) {
  if (entered[key] === token) return false;
  entered[key] = token;
  return token !== null;
}

// The big grey check a card earns when its track is resolved — Feather's
// `check` in the house stroke style, drawn on by CSS (.done-mark). pathLength
// pins the polyline to 24 units so the dash animation needs no measuring.
const DONE_MARK = `<svg class="done-mark" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="2.5" stroke-linecap="round"
  stroke-linejoin="round" aria-hidden="true">
  <polyline points="4 12 9 17 20 6" pathLength="24"></polyline></svg>`;

// Its opposite, for a track that left an input instead of finding a home.
// Same stroke, same size, same draw-on; red, and a cross. A removal ends the
// card the way filing does, but it is not the same outcome and should not
// wear the same mark. Two polylines so the animation draws both strokes.
const GONE_MARK = `<svg class="gone-mark" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="2.5" stroke-linecap="round"
  stroke-linejoin="round" aria-hidden="true">
  <polyline points="6 6 18 18" pathLength="24"></polyline>
  <polyline points="18 6 6 18" pathLength="24"></polyline></svg>`;

function subsetButtonRow() {
  return `<div class="minor-actions">
    <button id="btn-now-subset">Add to subset…</button>
  </div>`;
}

function ordinaryCardBody(d, tr, ctx) {
  const filedTo = filedUris[tr.uri];
  if (filedTo) {
    // The filed card keeps Add to subset… — a song that just found its home
    // is exactly when you might also want it in a selection — but nothing
    // proposes one: subsets are destinations you choose, not suggestions.
    // `removedUri` is already the strip's "this track was taken out and can
    // be put back" flag, so the card reads the removal off the same state
    // rather than sniffing the label for it.
    const removed = removedUri === tr.uri;
    // Drawn on by the render that first puts it up, and only that one.
    const fresh = enterOnce("mark", `${tr.uri}:${removed ? "gone" : "done"}`) ? " mark-in" : "";
    return `<div class="done-msg${removed ? " gone-msg" : ""}${fresh}">` +
           `${removed ? GONE_MARK : DONE_MARK}` +
           `<p>${removed ? "removed from" : "filed to"} <b>${esc(filedTo)}</b></p></div>` +
           subsetButtonRow();
  }
  // Nothing resolved on screen — arm the draw for whatever this card resolves
  // to next, including a re-filing of this same track after an undo.
  enterOnce("mark", null);
  if (!tr.sortable) return '<p class="hint">Can\'t be sorted via the API (local file or episode).</p>';

  // What phase 2 of the two-phase card actually adds is the SUGGESTED ROWS,
  // and from here down only they wait for it. Everything around them —
  // Add to…, Add to subset…, Homeless, the capture chips — is answerable from
  // what the light poll already carries (homes, inputs and homeless_id ride
  // across it; none of the three is per-track — see pollNow), so the card goes
  // up whole and one box inside it says it is still thinking. It used to
  // return here with a lone "finding a home…" line and nothing else, and the
  // entire lower half of the card dropped in a second later.
  let body = "";
  let rows = "";
  // A new song is a different question, and the card asks that one instead.
  // `unfiled`, not `adrift`: the rows follow the song, not the playback. A
  // song you meet on Discover Weekly is exactly the case this is for, and
  // that has a playlist context, which is what keeps the banner off it.
  // Computed once — three places below turn on it, and unfiled() is cheap but
  // not free (it walks inputs and suggestions).
  const isUnfiled = unfiled(d);
  // Phase 1 can already tell a new song from a filing, and does not have to
  // wait to say so. The rows need the list of inboxes — not per-track, and
  // carried across the phase boundary by pollNow — plus the one question
  // "am I playing out of an inbox", which the light payload's own
  // context.is_input answers. Membership (in a home, in an inbox) is the only
  // thing phase 2 adds here, and that is what the BANNER turns on, not these.
  //
  // The bet this makes: playing from something that is not an inbox is
  // overwhelmingly a new song. When it is not — playing a home playlist
  // directly — phase 2 replaces the rows with that home marked "already
  // there", which is a correction rather than a wrong answer left standing.
  const provisional = !!(d.suggPending && !d.suggError && d.playing &&
                         !d.context?.is_input);
  const showingInboxes = isUnfiled || provisional;
  if (d.suggError) {
    body += `<p class="hint">suggestions failed: ${esc(d.suggError)} — refresh to retry.</p>`;
  } else if (provisional) {
    rows += captureRows(d, true);
  } else if (d.suggPending) {
    // Inside the scrolling box, where the rows themselves will appear: the
    // wait is shown where the thing being waited for goes.
    rows += '<p class="hint sugg-loading">finding a home…</p>';
  } else if (isUnfiled) {
    // Deliberately instead of the suggestions, not above them. The app has
    // established this song is in no inbox and no home, so the home list is
    // answering a question that has not been reached yet — and a weak guess
    // offered to a song that was never triaged is worse than no guess, because
    // it invites filing something you have not decided to keep. The homes stay
    // reachable through the Add to… row that follows, for when you do know.
    rows += captureRows(d);
  } else {
    if (d.suggestions.length && d.suggestions[0].weak) {
      body += '<p class="hint">No confident match — closest guesses:</p>';
    }
    // The rows go in a scrolling box of their own: the list is six long now
    // and six full-height rows are more card than a phone screen wants at
    // once. About three show, the fourth is cut off at the edge — which is
    // the only honest affordance that there is more, since a touch device
    // renders no scrollbar at rest. The lead-in hint above stays OUTSIDE the
    // box: it says what the whole list is, so scrolling it away would be
    // losing the label.
    d.suggestions.forEach((s, i) => {
      const home = nowState.homes.get(s.playlist_id);
      if (!home) return;
      rows += `<button class="sugg${s.already ? " already" : ""}${s.weak ? " weak" : ""}" data-to="${esc(s.playlist_id)}" style="--pct:${s.already ? 100 : s.pct}%">
        <span class="s-pct">${s.already ? '<span class="s-badge">already there</span>' : s.pct + "%"}</span>
        <span class="s-name"><kbd>${i + 1}</kbd> ${esc(home.name)}</span>
        <span class="s-why">${esc([folderLeaf(home.folder), ...s.reasons].filter(Boolean).join(" · "))}</span>
      </button>`;
    });
  }
  // Add to… ends the list rather than sitting under it as a button: reaching
  // past the suggestions is the same question the suggestions ask, so it is
  // the last and least confident answer to it, not a different control. It
  // keeps the rows' three-column shape so the list still scans as one column,
  // and takes a look of its own so it does not read as a seventh home. No
  // data-to and no --pct: nothing to file, nothing to be confident about.
  // Withheld in exactly one case: a first card of the session, still in phase
  // 1, where no suggest answer has ever landed and `homes` is genuinely empty
  // — the row would open a picker with nothing in it. Every later phase 1 has
  // the carried copy and draws it.
  if (!d.suggPending || nowState.homes.size) rows += `<button class="sugg sugg-more" id="btn-now-more">
    <span class="s-pct"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></span>
    <span class="s-name"><kbd>m</kbd> Add to…</span>
    <span class="s-why">any of your homes — search by name, or create one</span>
  </button>`;
  // Two columns for inboxes, one for homes. A home suggestion is a ranked
  // guess whose sub-line is the reason to trust it, so it earns the full
  // width; an inbox row is a name and a size, and what you want from that
  // list is to see all of it at once. capSuggScroll's arithmetic is untouched
  // by the grid — the rows keep their own margin and only a column gap is
  // added — so the box stays exactly as tall and simply holds twice as many.
  body += `<div class="sugg-scroll${showingInboxes ? " cap-grid" : ""}">${rows}</div>`;
  // Not on an adrift card: no home was proposed because none was asked for,
  // which is not the same fact as none fitting.
  if (!showingInboxes && !d.suggPending && !d.suggError && !d.suggestions.length) {
    body += '<p class="hint">No confident match — Add to… above.</p>';
  }
  // Remove from input lives in the playback strip now (see playbackStrip).
  body += `<div class="minor-actions">
    <button id="btn-now-subset">Add to subset…</button>
    ${homelessButton(d) || ""}
  </div>`;
  // Always drawn, even with no chips in it: the button is the row's reason to
  // exist, and a control that comes and goes with the song's membership would
  // be missing exactly when you reach for it.
  // Stood down while the main row is already this offer at full size, and the
  // chips are provably empty anyway — unfiled REQUIRES that no input holds the
  // song, so there is no membership left for them to report.
  if (!showingInboxes) {
    const chips = captureChips(d.inputs || []);
    body += `<div class="capture">${chips ? `<span class="hint">in:</span>${chips}` : ""}` +
      `<button id="btn-now-capture" class="chip cap-more" title="Put this song in one of your inputs — it stays where it is too">${
        ICON_SEARCH_SM} capture to…</button></div>`;
  }
  return body;
}

// The verdict "none of my homes fit this — it needs one of its own", as a
// move into the buffer that collects exactly those songs. A third destination
// rather than a fourth verb: it files like any suggestion does (out of the
// input, into somewhere), so it belongs beside Add to… and not next to the
// strip's Remove.
//
// Four conditions, and all four are about not offering a move that would be
// wrong rather than merely useless: no destination configured (see the
// server's `_homeless_id`), nothing to move out of, the destination IS the
// context, or the song is already there. Split from the button because the
// picker offers the same verdict and must agree about when it applies.
function homelessTarget(d) {
  if (!d?.homeless_id || !d.context?.is_input) return null;
  if (d.context.id === d.homeless_id) return null;
  if ((d.inputs || []).some((l) => l.id === d.homeless_id && l.has_track)) return null;
  return d.homeless_id;
}

function homelessButton(d) {
  return homelessTarget(d) ? '<button id="btn-now-homeless">Homeless</button>' : "";
}

async function nowHomeless() {
  const id = nowState.homeless_id;
  if (!id) return;
  // Deliberately nowFile, not a variant of nowCapture: the decision is spent
  // either way, so the card belongs in its ✓ filed state with the strip's
  // Undo — the same shape filing to a home leaves behind.
  await nowFile(id, "Homeless");
}

// Capture chips, grouped by input set. 26 chips in one wall is a thing you
// scan rather than use, so only the buffer set is always laid out; the rest
// collapse behind a per-set chip you tap to open.
//
// The exception that makes folding safe: a chip whose playlist ALREADY holds
// the playing track always shows, folded set or not. That is the whole point
// of these chips — telling you where the track already lives — and hiding
// that would make a folded set actively misleading rather than merely terse.


// A membership marker, not a button: this row answers "where is this song
// already", and the answer is not something you click.
function captureChip(l) {
  return `<span class="chip in-chip has" data-in="${esc(l.id)}">${esc(l.name)}</span>`;
}

// The lists this song is already in — and nothing else.
//
// This row used to render every buffer input, opted in or not, with the ones
// holding the song merely tinted: a wall of a dozen chips whose only real
// information was which two were highlighted, and which grew with the number
// of inboxes. Membership is the question the row answers, so membership is
// all it draws. Putting the song somewhere NEW is a different question and it
// goes through the picker beside them (see openCapturePicker) — the same
// shape the card already uses for homes, where a long list also belongs
// behind a search box rather than in front of one.
//
// Sets no longer group anything here: with only the matching lists left there
// is nothing to fold, and the per-set expanders went with the wall.
function captureChips(inputs) {
  return inputs.filter((l) => l.has_track).map(captureChip).join("");
}

// The rows an adrift card shows where the home suggestions normally go: the
// inboxes this song can be parked in. Same shape as a .sugg so the scroll
// box's measured row pitch (capSuggScroll) still works, and same two-column
// layout as the Add to… row, which is the other row here with nothing to be
// confident about — there is no score to show, because inboxes are not
// scored and nothing profiles them.
//
// The exclusions are openCapturePicker's, deliberately, so the two offers
// cannot drift apart: an inbox already holding the song is a no-op (and its
// holding the song is exactly what would stop the card being adrift), and
// Homeless stays out because it is a verdict you reach by filing, not
// somewhere you park a song you have not judged yet.
//
// Buffer set first: it is the day-to-day one, and the sets below it are
// older lists being reworked. Server order is kept within each group.
// `provisional` is the phase-1 draw. The light payload sends no inputs while
// playing, so the has_track flags the client carried over describe the
// PREVIOUS track — not evidence about this one, and acting on it would hide
// whichever inbox the last song happened to sit in, which on this app's main
// loop is the inbox you are filing out of. So phase 1 ignores membership
// entirely and phase 2 starts applying the real flags. Rows are only ever
// added by that correction, never pulled out from under a thumb: a song that
// is genuinely new is in no inbox, so the filter removes nothing.
function captureRows(d, provisional = false) {
  const inboxes = (d.inputs || []).filter(
    (l) => l.id !== d.homeless_id && (provisional || !l.has_track));
  const isBuffer = (l) => (l.set || NOW_BUFFER_SET) === NOW_BUFFER_SET;
  return [...inboxes.filter(isBuffer), ...inboxes.filter((l) => !isBuffer(l))].map((l) => {
    const sub = [setLabel(l.set || NOW_BUFFER_SET),
                 l.total == null ? "" : `${l.total} songs`].filter(Boolean).join(" · ");
    return `<button class="sugg sugg-capture" data-cap="${esc(l.id)}">
      <span class="s-name">${esc(l.name)}</span>
      <span class="s-why">${esc(sub)}</span>
    </button>`;
  }).join("");
}

async function nowCapture(inId) {
  const d = nowState, tr = d.track;
  try {
    const res = await api("/api/act", { action: "move", uri: tr.uri, from_id: null, to_id: inId });
    nowActions++;
    // Kind "input", not "home": capturing writes no filedUris key, so
    // undoing one must clear nothing — the existing kind === "home" check
    // in btn-undo-now already gets that right for free.
    nowActionLog.push({ uri: tr.uri, kind: "input" });
    const entry = d.inputs.find((l) => l.id === inId);
    if (entry) entry.has_track = true;
    toast(res.note || `+ ${entry?.name || "input"}`);
    renderNow();
  } catch (e) { toast(e.message); }
}

async function nowAddToSubset(id) {
  const tr = nowState.track;
  const name = nowState.subsetTargets?.get(id)?.name || "subset";
  try {
    // from_id stays null: a song in a best-of has not been sorted, so it
    // must not leave its input. The server refuses the other shape too.
    await api("/api/act", { action: "move", uri: tr.uri, from_id: null, to_id: id });
    nowActions++;
    nowActionLog.push({ uri: tr.uri, kind: "subset" });
    toast(`+ ${name}`);
    renderNow();
  } catch (e) { toast(e.message); }
}

// `label` names the destination on the done card and in the toast. It exists
// because not every filing destination is a home any more: the Homeless
// buffer is an input, so the `d.homes` lookup finds nothing and the card
// would read "filed to home".
// Names the other inboxes a sweep emptied. A delete from a playlist that was
// not on screen must never be silent — but the names are playlist names, so
// one is named outright and several are counted rather than listed.
function sweptSuffix(swept) {
  if (!swept || !swept.length) return "";
  return swept.length === 1 ? ` + ${swept[0]}` : ` + ${swept.length} more inputs`;
}


async function nowFile(toId, label) {
  const d = nowState, tr = d.track;
  const fromId = d.context?.is_input ? d.context.id : null;
  try {
    // sweep_inputs: filing answers the question for this song, so it leaves
    // every inbox holding it — not just the one being played. Without it a
    // copy in another buffer came round later and had to be decided twice.
    const res = await api("/api/act", { action: "move", uri: tr.uri, from_id: fromId,
                                        to_id: toId, sweep_inputs: true });
    nowActions++;
    filedUris[tr.uri] = label || d.homes.get(toId)?.name || "home";
    nowActionLog.push({ uri: tr.uri, kind: "home" });
    toast((res.note || `→ ${filedUris[tr.uri]}${fromId ? " (removed from input)" : ""}`) +
          sweptSuffix(res.swept));
    renderNow();
  } catch (e) { toast(e.message); }
}

// Picker's no-match create row, hoisted so both the "Add to…" button and the
// `m` keyboard shortcut get the create row (previously only the button did).
async function nowCreateAndFile(name) {
  try {
    const { p, note } = await createHome(name);
    // Stamped now: it just received this track, so the recency sort keeps it
    // on top until the next profile rebuild reports the real value.
    nowState.homes.set(p.id, { id: p.id, name: p.name, image: null, total: 0, folder: null,
      last_added_at: new Date().toISOString() });
    await nowFile(p.id);  // lands the card in its ordinary ✓ filed state; nowFile's
    // own toast covers that. The server's duplicate-name note is separate and
    // would otherwise be silently dropped, so surface it too.
    if (note) toast(note, 5000);
  } catch (e) { toast(e.message); }
}

// The remove leg shared by Remove and the combined circle: the /api/act call
// and every piece of bookkeeping a successful removal owes the card. Toasts
// its own success; a failure propagates — the caller decides what it aborts.
async function removeFromInput(d, tr) {
  // Same sweep as filing, same reason: rejecting a song is as final a
  // decision as giving it a home.
  const res = await api("/api/act", { action: "remove", uri: tr.uri,
                                      from_id: d.context.id, sweep_inputs: true });
  nowActions++;
  // The input's own name: the card pairs it with "removed from", so the
  // label is the place it left rather than a sentence about nowhere.
  filedUris[tr.uri] = d.context.name || "the input";
  nowActionLog.push({ uri: tr.uri, kind: "home" });
  // Blind mode blurred this track so the ear would decide, not the name.
  // That decision is spent the moment it leaves the input, so say what left
  // — otherwise the input quietly loses a track you never got to see. It is
  // the same peek a tap sets, so renderNow expires it when the next track
  // starts and the following one is blind again.
  if (blindMode) {
    peekedUri = tr.uri;
    document.body.classList.add("peeked");
  }
  removedUri = tr.uri;
  // Names the list. "removed from input" said only that something happened;
  // the card beside it already names the place, and the toast disagreeing
  // with it by being vaguer is a wasted line.
  toast(`removed from ${d.context.name || "input"}` + sweptSuffix(res.swept));
}

async function nowRemove() {
  const d = nowState, tr = d.track;
  // Says why instead of returning in silence. Reached from the greyed button
  // (aria-disabled, so the tap still lands here) and from the `r` key, which
  // has no button to consult at all.
  const { live, why } = removeState(d);
  if (!live) { toast(why); return; }
  if (npPending) return;
  setNpPending("remove", tr.uri);
  try {
    await removeFromInput(d, tr);
  } catch (e) { toast(e.message); }
  finally { clearNpPending(); renderNow(); }
}

// The circle's press: remove, then skip — in that order, and the skip only
// after the removal has landed. Skipping a track that was NOT removed would
// lose it un-decided, so a failed remove leg aborts the whole press.
async function nowBoth() {
  const d = nowState, tr = d.track;
  const { live, why } = removeState(d);
  if (!live) { toast(why); return; }
  if (npPending) return;
  setNpPending("both", tr.uri);
  try {
    await removeFromInput(d, tr);
  } catch (e) {
    toast(e.message);
    clearNpPending();
    renderNow();
    return;
  }
  try {
    await api("/api/player/next", {});
    // As for Next alone: the POST returning only means Spotify accepted the
    // skip. The press finishes when the new track is on screen, which is
    // what the settle repoll brings — renderNow clears the pending when the
    // track changes, and setNpPending's timeout backstops a skip that never
    // lands.
    repollAfterPlaybackChange(tr.uri);
  } catch (e) {
    toast(e.message);
    clearNpPending();
    renderNow();
  }
}

// The strip's own undo, offered only for the track it was removed from. The
// server's undo stack is authoritative; this just spends the top entry, which
// is necessarily this removal — the card is in its done state, so nothing else
// on this track can have acted since.
async function undoRemoval() {
  const uri = removedUri;
  if (!uri) return;
  try {
    const res = await api("/api/undo", {});
    nowActions = Math.max(0, nowActions - 1);
    // Targeted, unlike btn-undo-now's pop-the-last-key: we know exactly which
    // uri this undo restores.
    delete filedUris[uri];
    // Keep nowActionLog in step with nowActions (see the invariant at its
    // declaration). This undo is necessarily the log's own top entry in the
    // common case, so pop it there first; but if something reordered the
    // log, search for the matching uri instead of popping the wrong track's
    // entry, and if nothing matches leave the log alone rather than
    // corrupting it further — a missed pop is recoverable, a wrong one isn't.
    if (nowActionLog.length && nowActionLog[nowActionLog.length - 1].uri === uri) {
      nowActionLog.pop();
    } else {
      const idx = nowActionLog.map((e) => e.uri).lastIndexOf(uri);
      if (idx !== -1) nowActionLog.splice(idx, 1);
    }
    removedUri = null;
    toast(res.restored_to ? "undone — restored to input" : "undone");
    renderNow();
  } catch (e) { toast(e.message); }
}

async function undoLastNowAction() {
  if (!nowActions) return;
  try {
    const res = await api("/api/undo", {});
    nowActions--;
    // Pop the last ACTION, not the last filedUris key: a subset add adds an
    // entry here but no key there, so keying off the object undid the wrong
    // track's badge.
    const last = nowActionLog.pop();
    if (last && last.kind === "home") delete filedUris[last.uri];
    if (last && last.uri === removedUri) removedUri = null;
    toast(res.restored_to ? "undone — restored to input" : "undone");
    renderNow();
  } catch (e) { toast(e.message); }
}
$("btn-undo-now").onclick = undoLastNowAction;

// The strip's undo dispatch: a removal has its own targeted path (it knows
// the uri it restores); anything else — a filing, a capture, a subset add —
// is the generic pop of the stack's top entry, which the button only renders
// for when that entry belongs to the playing track.
async function undoStripAction() {
  if (removedUri && nowState?.track?.uri === removedUri) return undoRemoval();
  return undoLastNowAction();
}

// ---- blind mode ------------------------------------------------------------
//
// Hide what's playing so the ear decides, not the name: blurs title, artist,
// art, and the suggestion REASONS (they leak artist names) on the listening
// surfaces (#now-card — the now view and the sitting decide card both render
// there; triage keeps its labels). Pure client state, persisted locally.
// Tapping the blurred title, artist or art peeks the card without filing
// anything; the suggestion buttons stay live, so picking a home files it.

let blindMode = localStorage.getItem("blindMode") === "1";
// One tap on any blurred field reveals EVERYTHING for the remainder of that
// track — renderNow drops the reveal as soon as the playing uri changes.
let peekedUri = null;
// The track whose removal the strip is currently offering to undo. Expires
// with the track (renderNow), because an undo inherited by the next song
// would undo a decision made about a different one.
let removedUri = null;

function applyBlind() {
  // No emoji in the UI: the button is an inline SVG eye whose slash line is
  // shown by CSS when body.blind is set (same class the blurs key off).
  document.body.classList.toggle("blind", blindMode);
  $("btn-blind").classList.toggle("on", blindMode);
  if (!blindMode) { document.body.classList.remove("peeked"); peekedUri = null; }
}
$("btn-blind").onclick = () => {
  blindMode = !blindMode;
  localStorage.setItem("blindMode", blindMode ? "1" : "0");
  applyBlind();
  toast(blindMode ? "blind mode — tap a blurred field to peek" : "blind mode off");
};
applyBlind();

// ---- tablet share (Spotify Messages via the tablet — see /api/share) -------

let shareInFlight = false;

async function openSharePop() {
  const t = nowState?.track;
  if (!t?.uri?.startsWith("spotify:track:")) { toast("nothing shareable playing"); return; }
  const pop = $("share-pop");
  let targets = [];
  try { targets = (await api("/api/share/targets")).targets; } catch (e) { toast(e.message); return; }
  // The cache is empty until the first share has run; free-text still works
  // then — the share sheet's Search box is not driven yet, so an unknown
  // name fails fast server-side and the 502 names the real targets.
  const rows = targets.map((name, i) =>
    `<button id="share-t-${i}" class="share-target" data-name="${esc(name)}">${esc(name)}</button>`).join("");
  pop.innerHTML =
    `<div class="pv-head"><span class="pv-title">Send to…</span>
       <button id="share-close" class="icon-btn" title="Close">✕</button></div>
     <div class="share-targets">${rows || '<span class="hint">no targets cached yet — type a name</span>'}</div>
     <div class="pv-ctl"><input id="share-name" placeholder="friend's name, exactly as in Spotify">
       <button id="share-go">Send</button></div>`;
  pop.hidden = false;
  $("share-close").onclick = () => { pop.hidden = true; };
  const send = (friend) => doShare(t, friend);
  targets.forEach((name, i) => { $(`share-t-${i}`).onclick = () => send(name); });
  $("share-go").onclick = () => {
    const name = $("share-name").value.trim();
    if (name) send(name);
  };
}

async function doShare(track, friend) {
  if (shareInFlight) return;
  shareInFlight = true;
  // title+artist, no uri: the server drives the tablet via the search
  // deep link — a track link would autoplay there and steal playback.
  const artist = (track.artists || []).map((a) => a.name).join(" ");
  toast(`sending to ${friend}… (~40s, the tablet is doing the tapping)`, 45000);
  try {
    await api("/api/share/track", { title: track.name, artist, friend });
    $("share-pop").hidden = true;
    toast(`sent to ${friend}`);
  } catch (e) {
    toast(e.message, 6000);
  } finally {
    shareInFlight = false;
  }
}

// btn-share is card-internal now (rendered and wired by renderNow, like the
// other strip controls) — there is no static element left to wire here.

// Capture phase, so the peek happens before anything underneath reacts — but
// a blurred field inside a control is that control's, not the peek's: picking
// a playlist in blind mode files straight away rather than spending the click
// on a reveal. The suggestion reason (.s-why) lives inside the .sugg button,
// so it is only ever peeked as a side effect of tapping the title, artist or
// art — which lift every blur on the card at once.
$("now-card").addEventListener("click", (e) => {
  if (!blindMode || document.body.classList.contains("peeked")) return;
  const el = e.target.closest(".t-name, .t-artist, .art, .s-why");
  if (!el || el.closest("button")) return;
  e.stopPropagation();
  e.preventDefault();
  peekedUri = nowState?.track?.uri || null;
  document.body.classList.add("peeked");
}, true);

// ---- picker ----------------------------------------------------------------

// The Now card's picker, wired through the one place that knows whether the
// Homeless verdict applies right now. Both the button and `m` go through it,
// so the picker cannot end up offering a move the card itself withholds.
function openNowPicker() {
  openPicker(nowState.homes, nowFile, nowCreateAndFile,
             homelessTarget(nowState) ? nowHomeless : null);
}

// Capture: put the song in an input as well as wherever it already is. Every
// input the song is NOT already in — the ones it is in are the chips beside
// the button, and offering them here would be offering a no-op. `inputs`
// carries no folder path, so the set label stands in as the grey sub-line,
// which is what tells two similarly-named inboxes apart.
function openCapturePicker() {
  const map = new Map((nowState.inputs || [])
    // The Homeless buffer stays out, as it always has: its own button MOVES
    // the song there, and an offer to ADD it two centimetres away would give
    // one destination two meanings depending on which control you hit. The
    // membership chips no longer hide it, because a chip is a fact about
    // where the song is rather than an offer to put it somewhere.
    .filter((l) => !l.has_track && l.id !== nowState.homeless_id)
    .map((l) => [l.id, { id: l.id, name: l.name,
                         folder: setLabel(l.set || NOW_BUFFER_SET) }]));
  openPicker(map, nowCapture, null, null, "Capture here");
}

function openPicker(homesMap, onPick, onCreate, onHomeless, verb = "File here") {
  const list = $("picker-list");
  const paint = (filter) => {
    list.innerHTML = "";
    // Pinned above the homes and never filtered out: "none of these fit" is
    // the one answer a filter matching nothing does not rule out — it is the
    // case that makes it likeliest.
    if (onHomeless) {
      const b = document.createElement("button");
      b.className = "picker-row picker-homeless";
      b.innerHTML = '<span class="p-name">Homeless</span>' +
        '<span class="p-sub">no home fits — park it in the buffer</span>';
      b.onclick = () => { closePicker(); onHomeless(); };
      list.appendChild(b);
    }
    // Recency first: the home you filed into most recently is the likeliest
    // target again (ISO Zulu stamps compare fine as strings; homes never
    // added to sink to the bottom in the old folder → name order).
    const homes = [...homesMap.values()].sort((a, b) =>
      (b.last_added_at || "").localeCompare(a.last_added_at || "") ||
      (a.folder || "").localeCompare(b.folder || "") || a.name.localeCompare(b.name));
    let shown = 0;
    for (const h of homes) {
      if (filter && !(h.name + " " + (h.folder || "")).toLowerCase().includes(filter)) continue;
      shown++;
      const b = document.createElement("button");
      b.className = "picker-row";
      // Name first and bold; the folder path demoted to a small second line —
      // the full "folder / name (n)" string was unscannable on a phone.
      const sub = [h.folder, h.total != null ? `${h.total} tracks` : ""].filter(Boolean).join(" · ");
      b.innerHTML = `<span class="p-name">${esc(h.name)}</span>` +
        (sub ? `<span class="p-sub">${esc(sub)}</span>` : "");
      b.onclick = () => {
        // A completed hold-preview must not also file the track: the click
        // that follows pointerup is the same gesture, so it is consumed.
        if (previewHold.consumeClick()) return;
        closePicker(); onPick(h.id);
      };
      previewHold.attach(b, h.id, h.name,
        { label: verb, run: () => { closePicker(); onPick(h.id); } });
      list.appendChild(b);
    }
    // The moment of need: the right playlist doesn't exist yet. Create it
    // and file in one gesture — create + add, priced as such. (Spec §5.)
    if (!shown && filter && onCreate) {
      const typed = $("picker-filter").value.trim();
      // nowFile sends a remove too when filing from an input: create + add +
      // remove = 3 calls, not 2 — the label must state the true cost.
      const price = nowState.context?.is_input ? "3 calls" : "2 calls";
      const b = document.createElement("button");
      b.className = "picker-row picker-create";
      b.innerHTML = `<span class="p-name">Create home “${esc(typed)}” and file this track there</span>` +
        `<span class="p-sub">${price}</span>`;
      b.onclick = () => { closePicker(); onCreate(typed); };
      list.appendChild(b);
    }
  };
  paint("");
  $("picker-filter").value = "";
  $("picker-filter").oninput = (e) => paint(e.target.value.trim().toLowerCase());
  $("picker").hidden = false;
  $("picker-filter").focus();
}
// ---- hold-to-preview: hold opens a clip-player popup -----------------------
//
// Spotify's dev-mode API has no preview URLs, so the audio comes from
// /api/playlist_preview (Deezer 30s clips over the playlist's tracks, newest
// first — zero Spotify calls; see app.py). Hold on a picker row or a
// suggestion opens the player and eats the row's click; the popup then owns
// the audio: full 30s clips that fade in and out, auto-advance on clip end,
// prev/next controls, and an explicit close. The single budgeted
// auto-resume call fires on close OR when the playlist's clips run out
// (the phone OS pauses Spotify when preview audio takes focus and never
// un-pauses it on its own).
//
// Two things make the gesture honest, because the hold COMPETES with the
// row's own tap and wins by swallowing it:
//   - every holdable row wears a small waveform glyph, so the gesture is
//     discoverable at all rather than folklore;
//   - the press paints a filling bar along the row from 150ms in, and
//     buzzes when it fires, so a hold is something the user watches happen
//     and can abort — not a tap that silently failed to file.
// The popup also carries the row's own action ("File here"), so hearing the
// playlist and choosing it is one gesture instead of close-find-tap.
const previewHold = (() => {
  const HOLD_MS = 550;   // above the accidental-dwell range a tap can reach
  // Mirrors app.py's PREVIEW_RESUME_MIN_INTERVAL (5.0s), with a second of
  // slack for the round trip. Only used to tell "the server debounced MY
  // last resume" from "it debounced somebody else's" — never to gate a call.
  const PREVIEW_RESUME_DEBOUNCE_MS = 6000;
  const ICON = {
    close: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    prev: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>',
    next: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>',
    play: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>',
    // the affordance: bars, the shape of a clip waiting to be heard
    wave: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M2 6.5v3M5.5 3v10M9 5v6M12.5 6.5v3M15.5 7.5v1"/></svg>',
  };
  // `fired` marks a completed hold until its trailing click is consumed;
  // `holding` tracks finger-still-down after the popup opened, for the
  // scroll blocker only — the popup itself outlives the hold.
  let timer = null, fired = false, holding = false, seq = 0, audio = null;
  let wasPlaying = false, audioPlayed = false, needsTap = false;
  // Set when the end of the playlist resumed Spotify with the popup still
  // open: the next clip the user plays pauses that music again, so the
  // resume-owed flags re-arm from this (see playAt).
  let resumed = false;
  // When THIS client last fired a resume, so the server's 5s debounce can be
  // told apart from a genuine second resume of our own (see resumeSpotify).
  let lastResumeFiredAt = 0;
  // The playing uri the popup was opened for. The card's action files
  // whatever is playing WHEN TAPPED, so this is what proves the two are
  // still the same track (see the pv-act handler).
  let actUri = null;
  // Consecutive clips whose URL would not load. One is a stale CDN token;
  // a run of them means the whole cached page has expired.
  let deadClips = 0;
  const DEAD_CLIP_LIMIT = 4;
  // Player state: every clip resolved so far (pages accumulate), the next
  // page cursor, and the index of the clip being played.
  let clips = [], nextOffset = null, idx = -1, pid = null, title = "", total = 0, fallback = [];

  // Where each playlist's preview stood, so reopening one continues where it
  // left off instead of starting over. A preview serves one decision —
  // "where does the PLAYING track go" — so the memory lives exactly as long
  // as that track: a different playing uri wipes every stash. Restoring is
  // pure memory, so a resumed popup costs zero requests of any kind.
  let resumeTrack = null;       // the playing uri the stashes belong to
  const resumeAt = new Map();   // pid -> {clips, nextOffset, idx, total, fallback}

  function stashPosition() {
    if (pid && clips.length && idx >= 0)
      resumeAt.set(pid, { clips, nextOffset, idx, total, fallback });
  }

  // Nothing in this player starts or stops flat anymore: clips fade in, run
  // a fading tail into the next clip's rise, and land softly when the player
  // stops. All local Audio.volume ramps — zero requests of any kind.
  const FADE_MS = 450;       // a clip's fade-in, and the tail at its end
  const FADE_OUT_MS = 180;   // the quick landing when the player stops

  function fadeTo(a, target, ms, then) {
    if (!a) { if (then) then(); return; }
    clearInterval(a.__fade);
    const from = a.volume ?? 1, t0 = Date.now();
    a.__fade = setInterval(() => {
      const k = Math.min(1, (Date.now() - t0) / ms);
      a.volume = from + (target - from) * k;
      if (k >= 1) { clearInterval(a.__fade); if (then) then(); }
    }, 40);
  }

  // `hard` cuts instantly — for a skip (the user rejected the clip) and for
  // replacing the popup, where the next clip must own the audio NOW (two
  // live Audio objects is the "previews over each other" bug). The soft path
  // is detached: nothing waits on it, and nothing else is about to play.
  function stopAudio(hard) {
    const a = audio;
    audio = null;
    if (!a) return;
    clearInterval(a.__fade);
    // A retired clip reports nothing, ever. Clearing src below runs the media
    // element's load algorithm on an empty URL, which the browser answers with
    // an `error` — so the dead-clip handler read a NORMAL SKIP as a broken
    // clip and advanced again, and that advance retired another element and
    // fired the same error, until one press of next had run the whole
    // playlist. Detaching here, not at the call sites, because both stops end
    // in the same src="" and only the caller's own `seq` bump ever covered it.
    a.onerror = a.onended = a.ontimeupdate = null;
    if (hard) { a.pause(); a.src = ""; return; }
    fadeTo(a, 0, FADE_OUT_MS, () => { a.pause(); a.src = ""; });
  }

  // The single budgeted resume, shared by close() and the end of the
  // playlist. Fires at most once per stretch of preview audio: the owed
  // flags clear here and re-arm only when another clip takes focus again.
  function resumeSpotify() {
    if (!(wasPlaying && audioPlayed)) return;
    wasPlaying = audioPlayed = false;
    resumed = true;
    const prevFired = lastResumeFiredAt;
    lastResumeFiredAt = Date.now();
    api("/api/preview_resume", {}).then((r) => {
      if (r.ok) { repollAfterPlaybackChange(); return; }
      // "resume already sent" is the server's 5s debounce (app.py's
      // PREVIEW_RESUME_MIN_INTERVAL). Benign when some OTHER popup's resume
      // covered us — but NOT when the one it collided with was our own: that
      // resume landed before this clip re-paused the music, so the music is
      // still paused and no second call is coming. That is the one case the
      // old blanket swallow got wrong, and it is exactly the end-of-playlist
      // re-arm. Anything else is a plain failure and always speaks up.
      const oursCollided = r.error === "resume already sent"
        && prevFired && Date.now() - prevFired < PREVIEW_RESUME_DEBOUNCE_MS;
      if (r.error && (r.error !== "resume already sent" || oursCollided))
        toast("preview over — press play in Spotify");
    }).catch(() => {});   // a refused resume is a shrug, never an error card
  }

  // Preview audio has taken focus, which is what pauses Spotify on a phone.
  // Both play paths funnel through here so neither can forget to re-arm the
  // resume the end-of-playlist one already spent.
  function notePreviewAudioStarted() {
    audioPlayed = true;
    if (resumed) { wasPlaying = true; resumed = false; }
  }
  // What the held row would have done if tapped, offered inside the popup.
  let act = null;
  // The row currently painting a press — cleared on release, fire or close.
  let heldRow = null;

  const pop = () => $("preview-pop");

  function unpress() {
    if (heldRow) heldRow.classList.remove("holding");
    heldRow = null;
  }

  function paintProgress() {
    const fill = $("pv-fill");
    if (!fill || !audio || !audio.duration) return;
    fill.style.width = `${Math.min(100, (audio.currentTime / audio.duration) * 100)}%`;
  }

  function render(statusLine) {
    const c = clips[idx];
    const now = statusLine ? `<div class="pv-now">${esc(statusLine)}</div>` :
      c ? `<div class="pv-now">${esc(c.artist)} — ${esc(c.name)}</div>` : "";
    // The text list only fills in when there is no audio at all — with a
    // working player it is noise under the controls.
    const lines = clips.length ? "" : fallback.slice(0, 5).map((t) =>
      `<div class="pv-line">${esc(t.artist)} — ${esc(t.name)}</div>`).join("");
    const atStart = idx <= 0;
    const atEnd = idx >= clips.length - 1 && nextOffset == null;
    // How far into this 30s clip we are: `next` is otherwise a blind press.
    const prog = c ? '<div class="pv-prog"><i id="pv-fill"></i></div>' : "";
    // idx counts RESOLVED clips and misses are skipped, so it cannot be
    // "n of total" — the two numbers are not on the same scale.
    const pos = clips.length ? `clip ${idx + 1} · ${total} tracks` : `${total} tracks`;
    // Next is this player's main verb — skimming clips IS the previewing —
    // so it is the big center target, with the exit right beside it at the
    // same size. Prev stays small: going back is the rare move.
    pop().innerHTML = `
      <div class="pv-head"><span class="pv-title">${esc(title)}</span>
        <span class="pv-pos">${pos}</span></div>
      <div class="pv-live" aria-live="polite">${now}${lines}</div>
      ${prog}
      <div class="pv-ctl">
        <button id="pv-prev" class="icon-btn" aria-label="Previous clip"${atStart ? " disabled" : ""}>${ICON.prev}</button>
        ${needsTap ? `<button id="pv-play" class="icon-btn" aria-label="Play clip">${ICON.play}</button>` : ""}
        <button id="pv-next" class="icon-btn pv-big" aria-label="Next clip"${atEnd ? " disabled" : ""}>${ICON.next}</button>
        <button id="pv-close" class="icon-btn pv-big" aria-label="Close preview">${ICON.close}</button>
      </div>
      ${act ? `<button id="pv-act" class="pv-act primary">${esc(act.label)}</button>` : ""}`;
    // Arrow, not a bare reference: close() takes a `hard` argument, and an
    // onclick handler is called with the click event — which is truthy.
    $("pv-close").onclick = () => close();
    $("pv-prev").onclick = () => playAt(idx - 1);
    $("pv-next").onclick = () => playAt(idx + 1);
    // Autoplay can be refused when play() lands outside the gesture's call
    // stack (iOS). Silence plus a track name reads as a broken player, so
    // the card asks for the one tap that unblocks it instead.
    if (needsTap) $("pv-play").onclick = () => {
      if (!audio) return;
      // Same funnel as the main path: this tap also takes audio focus, and
      // skipping the re-arm here left the music paused after an
      // end-of-playlist resume — on iOS, the only platform needsTap exists
      // for, so the two features collided by construction.
      audio.play().then(() => { notePreviewAudioStarted(); needsTap = false; render(); })
        .catch(() => {});
    };
    // The whole point of previewing a home is deciding to use it — but the
    // action files WHATEVER IS PLAYING when it is tapped, while the decision
    // was made about the track playing when the popup opened. Those came
    // apart the moment the player learned to advance the music by itself:
    // the end-of-playlist resume restarts Spotify with the popup still up,
    // and manual mode's played-out refetch repaints nowState under it. Then
    // one tap files — and removes from the input — a song the user never
    // judged. The stash already honours this invariant; the action must too.
    if (act) $("pv-act").onclick = () => {
      const a = act;
      const playing = (nowState && nowState.track && nowState.track.uri) || null;
      close();
      if (actUri && playing !== actUri) {
        toast("the track moved on — nothing was filed");
        return;
      }
      a.run();
    };
    paintProgress();
  }

  async function playAt(i) {
    if (i < 0) return;
    const mySeq = seq;
    while (i >= clips.length) {
      if (nextOffset == null) {
        // Walked the whole playlist: stay on the last clip's card, and give
        // the user their own music back — the medley is over, and silence
        // until they find the close button serves nobody. The popup stays
        // up: prev still works, and playing a clip again re-arms the resume.
        if (clips.length) {
          idx = clips.length - 1;
          stopAudio();
          render("end of playlist — back to your music");
          resumeSpotify();
        }
        return;
      }
      render("loading…");
      try {
        const d = await api(`/api/playlist_preview/${pid}?offset=${nextOffset}`);
        if (mySeq !== seq) return;
        clips = clips.concat(d.clips);   // an all-miss page just loops for the next
        nextOffset = d.next_offset;
      } catch { if (mySeq === seq) render("couldn't load more clips"); return; }
    }
    idx = i;
    // The previous clip must stop BEFORE the next starts — two live Audio
    // objects is the "previews over each other" bug. Hard cut on purpose: a
    // skip is a rejection, and the incoming clip still rises from silence.
    stopAudio(true);
    const c = clips[idx];
    needsTap = false;
    const a = audio = new Audio(c.url);
    a.volume = 0;
    a.onended = () => { if (mySeq === seq) playAt(idx + 1); };  // full 30s, then advance
    a.ontimeupdate = () => {
      paintProgress();
      // The clip's last moments fade rather than cut: Deezer clips end hard,
      // and this tail into the next clip's rise is the medley's rhythm.
      if (a === audio && !a.__tail && a.duration
          && a.duration - a.currentTime <= FADE_MS / 1000) {
        a.__tail = true;
        fadeTo(a, 0, FADE_MS);
      }
    };
    // A clip URL carries a CDN token that EXPIRES (deezer.py says so), and
    // the position stash now holds those URLs for a whole track session — so
    // a dead clip is expected, not exotic. `error` fires instead of `ended`,
    // which used to strand the medley on a silent card with no auto-advance.
    // Skipping mirrors the server's own posture toward a miss; a whole page
    // of dead URLs stops rather than spinning through hundreds of them.
    a.onerror = () => {
      if (mySeq !== seq) return;
      if (++deadClips > DEAD_CLIP_LIMIT) { render("these clips have expired — reopen to refetch"); return; }
      playAt(idx + 1);
    };
    a.play().then(() => {
      if (mySeq !== seq) return;   // closed between play() and its microtask
      notePreviewAudioStarted();
      deadClips = 0;               // a clip that plays clears the run
    }).catch(() => { if (mySeq === seq) { needsTap = true; render(); } });
    fadeTo(a, 1, FADE_MS);
    render();
  }

  async function open(p, t, a) {
    seq++; const mySeq = seq;
    // Whatever was playing keeps its place first — replacing the popup with
    // another playlist's must not forget where this one stood.
    stashPosition();
    stopAudio(true);
    // The stashes belong to one playing track; a new one starts over.
    const uri = (nowState && nowState.track && nowState.track.uri) || null;
    if (uri !== resumeTrack) { resumeAt.clear(); resumeTrack = uri; }
    pid = p; title = t || "preview"; clips = []; nextOffset = null; idx = -1; fallback = [];
    total = 0; needsTap = false; act = a || null; resumed = false; deadClips = 0;
    // The track this popup's action belongs to, so a decision can't land on
    // a different one (see the pv-act handler).
    actUri = uri;
    // Captured at open: nowState still reflects pre-preview reality here.
    //
    // `playing` is NOT "music is coming out of the phone" — app.py sets it
    // whenever a track object exists at all, and reports the transport state
    // separately as `is_playing` (which is what playerToggle reads). Reading
    // `playing` here meant previewing while DELIBERATELY PAUSED spent a
    // budgeted call to start music the user had just stopped.
    //
    // nowState is null until the Now view has polled once, so a preview held
    // from Triage knows nothing. Unknown is treated as playing: previewing
    // is something you do while listening, and the failure that matters here
    // is the one this whole mechanism exists to prevent — preview audio
    // taking focus, the OS pausing Spotify, and nothing ever un-pausing it.
    // A needless resume costs one call and fails gracefully server-side;
    // silence costs the user their music with no clue why.
    wasPlaying = wasPlaying || (nowState ? !!nowState.is_playing : true);
    pop().hidden = false;
    // Lets the picker keep its last rows clear of the card (see style.css).
    document.body.classList.add("previewing");
    const saved = resumeAt.get(p);
    if (saved) {
      // Same playlist, same playing track: continue where it left off.
      ({ clips, nextOffset, total, fallback } = saved);
      playAt(saved.idx);
      return;
    }
    render("previewing…");
    try {
      const d = await api(`/api/playlist_preview/${pid}`);
      if (mySeq !== seq) return;   // closed (or replaced) before the answer
      clips = d.clips; nextOffset = d.next_offset; total = d.total; fallback = d.tracks;
      if (clips.length || nextOffset != null) playAt(0);
      else render("no audio preview");
    } catch (e) {
      // 404 carries real instruction ("open it once in sortify first"); a
      // transport or 500 message is machine noise in a body of card text.
      if (mySeq === seq) render(e.status === 404 ? e.message : "couldn't load the preview");
    }
  }

  // `hard` cuts the audio instead of fading it out. The hidden-tab path uses
  // it: a background tab clamps timers to ~1s, so the 180ms fade stretches
  // into seconds of clip that keep playing OVER the music resumeSpotify()
  // restarts on the very next line — audible, and invisible to the harness,
  // whose timers are never throttled.
  function close(hard) {
    seq++;
    clearTimeout(timer); timer = null; holding = false;
    unpress();
    stashPosition();
    stopAudio(hard);
    pop().hidden = true;
    document.body.classList.remove("previewing");
    needsTap = false; act = null;
    // One budgeted call per popup session, and only when preview audio
    // actually took focus from a playback that was running (resumeSpotify
    // checks exactly that; it is a no-op after the end-of-playlist resume).
    resumeSpotify();
    wasPlaying = audioPlayed = false; resumed = false;
  }

  // While the opening hold is still down, the finger must not scroll the
  // list. touch-action can't change mid-gesture, so this blocks touchmove at
  // the document (passive:false is required for preventDefault on touch).
  document.addEventListener("touchmove", (e) => { if (holding) e.preventDefault(); },
    { passive: false });
  // The hold ends wherever the pointer is released — the popup stays open.
  document.addEventListener("pointerup", () => { holding = false; unpress(); });
  document.addEventListener("pointercancel", () => { holding = false; unpress(); });
  // Escape closes the player before it closes anything underneath it.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !pop().hidden) { e.stopPropagation(); close(); }
  });

  function fire(pid_, name, act_) {
    fired = true; holding = true;
    unpress();
    // The gesture won the row's tap — say so in the hand, since the eyes
    // may be anywhere.
    if (typeof navigator !== "undefined" && navigator.vibrate) navigator.vibrate(12);
    open(pid_, name, act_);
  }

  return {
    attach(row, pid_, name, act_) {
      // Marks the row as holdable AND hangs the affordance on it — one place
      // for all four call sites, so no caller has to remember the glyph.
      if (!row.classList.contains("holdable")) {
        row.classList.add("holdable");
        const hint = document.createElement("span");
        hint.className = "hold-hint";
        hint.setAttribute("aria-hidden", "true");
        hint.innerHTML = ICON.wave;
        row.appendChild(hint);
      }
      let x0 = 0, y0 = 0;
      row.onpointerdown = (e) => {
        x0 = e.clientX; y0 = e.clientY; fired = false;
        unpress(); heldRow = row; row.classList.add("holding");
        clearTimeout(timer);
        timer = setTimeout(() => fire(pid_, name, act_), HOLD_MS);
      };
      row.onpointermove = (e) => {
        if (timer && Math.hypot(e.clientX - x0, e.clientY - y0) > 10) {
          clearTimeout(timer); timer = null; unpress();   // it's a scroll, not a hold
        }
      };
      // A released or drifted-off pointer only cancels a PENDING hold —
      // an open popup is closed by its own close button.
      row.onpointerup = row.onpointercancel = row.onpointerleave = () => {
        clearTimeout(timer); timer = null; holding = false; unpress();
      };
      row.oncontextmenu = (e) => e.preventDefault();  // long-press menu would steal the hold
    },
    consumeClick() { const f = fired; fired = false; return f; },
    stop: (hard) => close(hard),
  };
})();

function closePicker() { previewHold.stop(); $("picker").hidden = true; }
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

// The Split tab opens on a picker (design §2): same eligibility rule as the
// per-row button (not Liked, 100+ tracks — splitDisabledReason handles
// not-owned), any in-progress split pinned first. Both reads are free:
// /api/playlists serves the cached listing and GET queue reads queue.json.
async function showSplitPicker() {
  stopQueuePolling();
  stopNowPolling();
  stopNowTicker();
  split = null;
  show("splitpick");
  const wrap = $("splitpick-list");
  wrap.innerHTML = '<p class="hint">Loading playlists…</p>';
  try {
    const data = await api("/api/playlists");
    // "_" is fine: GET queue is deliberately global regardless of path id.
    const qs = await api("/api/split/_/queue").catch(() => null);
    const q = qs?.queue;
    const activeId = q && (q.pending?.length || q.current) ? q.playlist_id : null;
    const eligible = data.playlists.filter(
      (p) => p.id !== "liked" && (p.total ?? 0) >= 100);
    eligible.sort((a, b) => (b.id === activeId) - (a.id === activeId));
    wrap.innerHTML = "";
    if (!eligible.length) {
      wrap.innerHTML = '<p class="hint">No playlist here has 100+ tracks — nothing needs splitting.</p>';
      return;
    }
    for (const p of eligible) {
      const row = document.createElement("div");
      row.className = "pl-row";
      const reason = splitDisabledReason(p);
      const sub = [p.folder, `${p.total} tracks`,
                   p.id === activeId ? "split in progress" :
                     p.split ? `split into ${p.split.piles} pile${p.split.piles === 1 ? "" : "s"}` : null,
                   reason]
        .filter(Boolean).join(" · ");
      row.innerHTML = `<div class="pl-meta"><div class="name">${esc(p.name)}</div>
        <div class="sub">${esc(sub)}</div></div>`;
      if (!reason) row.onclick = () => openSplit(p.id, p.name);
      wrap.appendChild(row);
    }
  } catch (e) {
    if (e.message === "auth needed") return;
    wrap.innerHTML = `<p class="hint">Couldn't load playlists: ${esc(e.message)}</p>
       <button id="btn-retry-splitpick">Retry</button>`;
    $("btn-retry-splitpick").onclick = showSplitPicker;
  }
}

async function openSplit(id, name) {
  stopQueuePolling();
  queueStatus = null;
  $("queue-panel").hidden = true;
  $("queue-panel").innerHTML = "";
  // A previous split's rename offer must not survive into a view that then
  // fails to load below (404-that-isn't-"not split yet", 5xx, network) —
  // otherwise the button's onclick still points at the old split's id.
  $("btn-rename-outputs").hidden = true;
  split = { id, name, piles: [], decided: {}, active_sitting: null };
  show("split");
  $("split-title").textContent = name;
  $("split-empty").innerHTML = "";
  $("piles").innerHTML = "";
  $("split-params").hidden = true;
  try {
    const data = await api(`/api/split/${id}`);
    applySplitData(data);
    renderRenameOffer(data);
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

// Understated by design (design §2 judgement): shown only when this split
// has saved playlists still carrying bare pile names. The count is computed
// client-side from record names; the server re-derives it and 409s if they
// disagree — same echo contract as every other priced action.
function renderRenameOffer(data) {
  const btn = $("btn-rename-outputs");
  const prefix = split.name + " · ";
  const todo = (data.piles || []).filter(
    (p) => p.materialised?.playlist_id && p.materialised.name
           && !p.materialised.name.startsWith(prefix));
  btn.hidden = !todo.length;
  if (!todo.length) return;
  btn.textContent =
    `Rename ${todo.length} saved playlist${todo.length === 1 ? "" : "s"} to “${split.name} · …” (${todo.length} calls)`;
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      await api(`/api/split/${split.id}/rename_outputs`,
                { expected_calls: todo.length });
      toast("Renamed");
      btn.hidden = true;
    } catch (e) {
      if (e.message !== "auth needed") toast(e.message);
    } finally {
      btn.disabled = false;
    }
  };
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
                           { pile_ids: pileIds, expected_calls: expectedCalls,
                             spend_reserve: $("chk-spend-reserve").checked });
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

$("btn-split-back").onclick = () => { stopQueuePolling(); split = null; showSplitPicker(); };
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
    const fresh = enterOnce("mark", `${tr.uri}:keep`) ? " mark-in" : "";
    return `<div class="done-msg${fresh}">${DONE_MARK}<p>kept to <b>${esc(homeName)}</b><br>
      <span class="hint">final — edit it from the home playlist if that was wrong</span></p></div>`;
  }
  enterOnce("mark", null);
  if (dec?.action === "reject") {
    return `<p class="hint">✗ rejected.</p>
      <div class="minor-actions"><button id="btn-decide-unreject">Undo reject (free)</button></div>`;
  }
  let html = "";
  if (!tr.sortable) {
    html += '<p class="hint">Can\'t be kept via the API (local file or episode) — reject it instead.</p>';
  } else if (nowState.suggPending) {
    html += '<p class="hint sugg-loading">finding a home…</p>';
  } else {
    const sugg = nowState.suggestions || [];
    if (sugg.length && sugg[0].weak) {
      html += '<p class="hint">No confident match — closest guesses:</p>';
    }
    sugg.forEach((s, i) => {
      const home = nowState.homes.get(s.playlist_id);
      if (!home) return;
      html += `<button class="sugg${s.already ? " already" : ""}${s.weak ? " weak" : ""}" data-keep="${esc(s.playlist_id)}" style="--pct:${s.already ? 100 : s.pct}%">
        <span class="s-pct">${s.already ? '<span class="s-badge">already there</span>' : s.pct + "%"}</span>
        <span class="s-name"><kbd>${i + 1}</kbd> Keep → ${esc(home.name)}</span>
        <span class="s-why">${esc([folderLeaf(home.folder), ...s.reasons].filter(Boolean).join(" · "))}</span>
      </button>`;
    });
    if (!nowState.suggestions.length) html += '<p class="hint">No confident match — use Keep to… below.</p>';
  }
  html += `<div class="minor-actions">
    ${tr.sortable && (!nowState.suggPending || nowState.homes.size)
      ? `<button id="btn-decide-more"><kbd>m</kbd> Keep to…</button>` : ""}
    <button id="btn-decide-reject" class="danger"><kbd>r</kbd> Reject (free)</button>
  </div>`;
  return html;
}

function wireSittingCard() {
  $("now-card").querySelectorAll("[data-keep]").forEach((b) => {
    b.onclick = () => { if (previewHold.consumeClick()) return; decideKeep(b.dataset.keep); };
    previewHold.attach(b, b.dataset.keep, nowState.homes.get(b.dataset.keep)?.name,
      { label: "Keep here", run: () => decideKeep(b.dataset.keep) });
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

// One key per suggestion row, and the list is TOP_N (6) long — the server's
// cap is what decides how many of these can ever fire, so this is the same
// number written down twice and the shared helper is where they stay in
// step. Read as "which suggestion did they press", not "is this a digit":
// a key past the end of a short list must resolve to nothing at all, never
// to suggestions[undefined].
const SUGG_KEYS = ["1", "2", "3", "4", "5", "6"];
const suggFor = (key, list) => (SUGG_KEYS.includes(key) ? (list || [])[Number(key) - 1] : null);

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (!$("picker").hidden) { if (e.key === "Escape") closePicker(); return; }
  if (!$("input-pop").hidden) { if (e.key === "Escape") closeInputPop(); return; }
  // Same rule as the other two overlays: while one is open it owns the
  // keyboard, so a shortcut cannot fire at the view hidden behind it.
  if (!$("nav-pop").hidden) { if (e.key === "Escape") closeNavPop(); return; }

  if (!$("view-triage").hidden && triage) {
    const tr = triage.tracks[triage.idx];
    if (!tr) return;
    const s = suggFor(e.key, tr.suggestions);
    if (s) moveTo(s.playlist_id);
    else if (SUGG_KEYS.includes(e.key)) return;
    else if (e.key === "m" && tr.sortable) openPicker(triage.homes, moveTo);
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
      const s = suggFor(e.key, nowState.suggestions);
      if (s) decideKeep(s.playlist_id);
      else if (SUGG_KEYS.includes(e.key)) return;
      else if (e.key === "m" && nowState.track.sortable) openPicker(nowState.homes, decideKeep);
      else if (e.key === "r") decideReject();
      return;
    }
    // Before the filed-guard below: once this track is filed or removed the
    // strip shows its Undo, and `u` must reach it — the guard used to eat
    // the key first, so the "(u)" the button advertised never worked.
    if (e.key === "u") {
      const b = $("btn-now-undo-remove");
      if (b) { b.click(); return; }
    }
    if (filedUris[nowState.track.uri]) return;
    const s = suggFor(e.key, nowState.suggestions);
    if (s) nowFile(s.playlist_id);
    else if (SUGG_KEYS.includes(e.key)) return;
    else if (e.key === "m" && nowState.track.sortable) openNowPicker();
    else if (e.key === "r") nowRemove();
    else if (e.key === "u") $("btn-undo-now").click();
  }
});

// Coming back to the tab is the moment a skip is most likely to have happened
// behind our back, so this one bypasses the predicted TTL.
document.addEventListener("visibilitychange", () => {
  // Same reasoning as show(): a backgrounded tab has no visible player. Hard
  // cut, not a fade — a hidden tab's timers are clamped, so a fade would go
  // on sounding over the music the close's resume just brought back.
  if (document.hidden) previewHold.stop(true);
  // Manual mode means almost exactly that — the refocus poll doesn't fire,
  // with one exception: a song that played out while the tab was hidden.
  // Coming back to the tool is "having it open", which is what the user
  // said that fetch is worth (see refetchAtPlayedOut).
  if (document.hidden || $("view-now").hidden) return;
  if (!nowManual) pollNow(true);
  else if (playedOutWhileHidden) { playedOutWhileHidden = false; pollNow(); }
});

paintManualChip();

boot().catch((e) => { if (e.message !== "auth needed") toast(e.message); });
