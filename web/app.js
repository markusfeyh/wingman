import {
  api, AuthError, NetError, onUnauthorized, html, raw, setHTML, safeUrl, avatar, roleLine,
  fmtTime, fmtDayTime, fmtDay, localDateStr, debounce, downscaleImage, urlBase64ToUint8Array, icon,
} from "./lib.js";
import { Listener } from "./listen.js";

const $ = (id) => document.getElementById(id);
const TABS = ["now", "card", "people", "recap", "settings"];
const NOTE_DRAFT_KEY = "wm.noteDraft";

const state = {
  me: null,
  tab: "now",
  prevTab: "now",
  encounter: null,      // current Encounter
  read: null,           // what the badge/QR said
  nowCard: null,        // Card for encounter.person
  capturing: null,      // { label, thumb }
  captureError: null,   // { message, retry }
  cardView: { personId: null, encounterId: null, card: null, error: null },
  transcript: [],       // final lines
  interim: "",
  noteBind: null,       // { encounterId, name }
  sseOk: false,
  pollTimer: null,
  enriching: new Set(), // person ids with a Bright Data lookup in flight (full card coming)
  demoListen: false,    // fixture demo: Listen plays a scripted conversation from the server
};

// ---------------------------------------------------------------- toasts

function toast(text, { kind = "info", action = null, timeout = 6000, iconName = null } = {}) {
  const box = $("toasts");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  if (iconName) setHTML(el, icon(iconName, "icon toast-ico"));
  const span = document.createElement("span");
  span.className = "grow";
  span.textContent = text;
  el.appendChild(span);
  if (action) {
    const b = document.createElement("button");
    b.className = "btn";
    b.textContent = action.label;
    b.addEventListener("click", (e) => { e.stopPropagation(); el.remove(); action.fn(); });
    el.appendChild(b);
  }
  el.addEventListener("click", () => el.remove());
  box.prepend(el);
  while (box.children.length > 3) box.lastChild.remove();
  if (timeout) setTimeout(() => el.remove(), timeout);
}

function errText(e) {
  if (e instanceof NetError) return e.message;
  if (e instanceof AuthError) return "Please sign in.";
  return (e && e.message) || "Something went wrong.";
}

// ---------------------------------------------------------------- login

function showLogin(message) {
  $("login").hidden = false;
  const err = $("login-error");
  err.hidden = !message;
  err.textContent = message || "";
  setTimeout(() => $("login-token").focus(), 50);
}

async function login(token) {
  await api("/api/login", { method: "POST", body: { token }, authRedirect: false });
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const token = $("login-token").value.trim();
  if (!token) return;
  const btn = e.submitter || $("login-form").querySelector("button");
  btn.disabled = true;
  try {
    await login(token);
    $("login").hidden = true;
    $("login-token").value = "";
    await loadAll();
  } catch (err) {
    showLogin(err instanceof AuthError ? "That token didn't work." : errText(err));
  } finally {
    btn.disabled = false;
  }
});

onUnauthorized(() => showLogin());

// ---------------------------------------------------------------- tabs / routing

function showTab(tab, { updateHash = true } = {}) {
  if (!TABS.includes(tab)) tab = "now";
  if (tab !== state.tab) state.prevTab = state.tab;
  state.tab = tab;
  document.querySelectorAll(".screen").forEach((s) => { s.hidden = s.dataset.screen !== tab; });
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  if (updateHash) {
    const hash = tab === "card" && state.cardView.personId ? `#card/${encodeURIComponent(state.cardView.personId)}` : `#${tab}`;
    if (location.hash !== hash) history.replaceState(null, "", hash);
  }
  window.scrollTo(0, 0);
  if (tab === "people") loadPeople();
  else if (tab === "recap") loadRecap();
  else if (tab === "settings") loadSettings();
  else if (tab === "card") renderCardView();
}

function routeFromHash() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  const [tab, id] = h.split("/");
  if (tab === "card" && id) { openCard(id, null); return; }
  showTab(TABS.includes(tab) ? tab : "now", { updateHash: false });
}

$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) showTab(b.dataset.tab);
});
window.addEventListener("hashchange", routeFromHash);

// ---------------------------------------------------------------- card rendering

function factList(items) {
  return html`<ul>${items.map((f) => {
    const url = safeUrl(f.url);
    const label = f.source === "graph" ? "your graph" : f.source;
    const src = label ? (url ? html` <span class="src">· <a href="${url}" target="_blank" rel="noopener">${label}</a></span>` : html` <span class="src">· ${label}</span>`) : "";
    const via = f.via ? html` <span class="via" title="Retrieved with Bright Data ${f.via}">Bright Data</span>` : "";
    return html`<li>${f.text}${src}${via}</li>`;
  })}</ul>`;
}

// What Bright Data retrieved for this card, so every claim can be traced to a source.
function retrievedList(items) {
  if (!items || !items.length) return "";
  return html`<div class="sec sources"><div class="sec-t">Retrieved with Bright Data</div>
    <ul>${items.map((r) => {
      const url = safeUrl(r.url);
      return html`<li><span class="bd-product">${r.product}</span>${url ? html`<a href="${url}" target="_blank" rel="noopener">${r.label}</a>` : r.label}</li>`;
    })}</ul></div>`;
}

function section(title, items) {
  if (!items || !items.length) return "";
  return html`<div class="sec"><div class="sec-t">${title}</div>${factList(items)}</div>`;
}

function statusPill(status) {
  const label = { pending: "Building…", quick: "Quick", full: "Full" }[status] || status;
  return html`<span class="pill ${status}">${label}</span>`;
}

// ctx: { encounterId, mode: "now" | "view" }
function renderCard(card, person, ctx) {
  const p = (card && card.person) || person || {};
  const status = card ? card.status : "pending";
  const pending = status === "pending";
  const empty = card && !pending && !card.common_ground.length && !card.ask_about.length && !card.goal_fit.length;
  const sessions = (card && card.sessions) || [];
  const prev = (card && card.previous_encounters) || [];
  const heads = (card && card.heads_up) || [];
  const li = safeUrl(p.linkedin_url);

  let actions;
  if (ctx.encounterId) {
    actions = html`<div class="card-actions">
      <button class="btn danger" data-action="not-them" data-enc="${ctx.encounterId}" data-person="${p.id}">Not them</button>
      <button class="btn" data-action="add-note" data-enc="${ctx.encounterId}" data-name="${p.name}">Add note</button>
      <button class="btn" data-action="end" data-enc="${ctx.encounterId}">End</button>
    </div>`;
  } else {
    actions = html`<div class="card-actions two">
      <button class="btn primary" data-action="start-convo" data-person="${p.id}">Talking to them</button>
      <button class="btn" data-action="refresh-card" data-person="${p.id}">Refresh card</button>
    </div>`;
  }

  return html`<article class="card">
    <div class="card-head">
      ${avatar(p, "lg")}
      <div class="meta">
        <div class="card-name">${p.name || "Unknown"}</div>
        <div class="card-role">${roleLine(p)}</div>
        <div class="row" style="margin-top:6px;gap:6px;flex-wrap:wrap">
          ${statusPill(status)}
          ${p.is_speaker ? html`<span class="pill speaker">Speaker</span>` : ""}
          ${li ? html`<a class="pill" href="${li}" target="_blank" rel="noopener">LinkedIn</a>` : ""}
        </div>
      </div>
    </div>
    ${pending ? html`<div class="sec"><div class="sec-t">Building card</div><div class="skeleton" style="width:90%"></div><div class="skeleton" style="width:75%"></div><div class="skeleton" style="width:60%"></div></div>` : ""}
    ${section("Common ground", card && card.common_ground)}
    ${section("Ask about", card && card.ask_about)}
    ${section("Fits your goals", card && card.goal_fit)}
    ${empty ? html`<div class="sec muted small">Nothing in common found yet. ${p.headline ? html`Headline: ${p.headline}` : ""}</div>` : ""}
    ${state.enriching.has(p.id) && status !== "full" ? html`<div class="enriching"><span class="spinner sm" aria-hidden="true"></span>Getting their LinkedIn, news and own site via Bright Data…</div>` : ""}
    ${heads.length ? html`<div class="heads">${heads.map((h) => html`<div>${icon("alert")}<span>${h}</span></div>`)}</div>` : ""}
    ${sessions.length ? html`<div class="sec"><div class="sec-t">Their sessions</div><ul>${sessions.map((s) => html`<li>${s.title}${s.start ? html` <span class="src">· ${fmtDayTime(s.start)}${s.room ? ` · ${s.room}` : ""}</span>` : ""}</li>`)}</ul></div>` : ""}
    ${prev.length ? html`<div class="sec"><div class="sec-t">Met before</div><ul>${prev.map((m) => html`<li>${fmtDay(m.at)}${m.summary ? html` · ${m.summary}` : ""}</li>`)}</ul></div>` : ""}
    ${pending ? "" : retrievedList(card && card.retrieved)}
    ${actions}
  </article>`;
}

// ---------------------------------------------------------------- NOW: encounter

function nowPersonId() {
  return state.encounter && state.encounter.status === "confirmed" && state.encounter.person ? state.encounter.person.id : null;
}

function renderNow() {
  const el = $("now-encounter");
  if (state.capturing) {
    setHTML(el, html`<div class="enc-status">
      ${state.capturing.thumb ? html`<img class="thumb" src="${state.capturing.thumb}" alt="">` : ""}
      <div class="spinner"></div><div>${state.capturing.label}</div>
    </div>`);
    return;
  }
  if (state.captureError) {
    setHTML(el, html`<div class="panel">
      <div class="error">${state.captureError.message}</div>
      <div class="row"><button class="btn ghost grow" data-action="capture-dismiss">Dismiss</button>
      ${state.captureError.retry ? html`<button class="btn primary grow" data-action="capture-retry">Retry</button>` : ""}</div>
    </div>`);
    return;
  }
  const enc = state.encounter;
  if (!enc) {
    setHTML(el, html`<div class="hint">Snap a badge or QR code, look someone up, or turn on Listen.<br><span class="small">Cards show here.</span></div>`);
    return;
  }
  const read = state.read && (state.read.name || state.read.company)
    ? html`<div class="read-line">Read: <strong>${state.read.name || "?"}</strong>${state.read.company ? html` · ${state.read.company}` : ""}${state.read.title ? html` · ${state.read.title}` : ""}</div>`
    : "";
  if (enc.status === "resolving") {
    setHTML(el, html`${read}<div class="enc-status"><div class="spinner"></div><div>Figuring out who this is…</div></div>`);
    return;
  }
  if (enc.status === "needs_pick") {
    setHTML(el, html`${read}<h2>Who is this?</h2>
      <div class="cand-list">${(enc.candidates || []).map((c) => html`
        <button class="cand" data-action="pick" data-enc="${enc.id}" data-person="${c.id}">
          ${avatar(c)}
          <span class="meta"><div class="name">${c.name}</div><div class="sub">${roleLine(c) || c.headline || ""}</div>${c.why ? html`<div class="why">${c.why}</div>` : ""}</span>
          <span class="score">${Math.round((c.score || 0) * 100)}%</span>
        </button>`)}
      </div>
      <div class="row" style="margin-top:8px">
        <button class="btn ghost grow" data-action="none-of-these" data-enc="${enc.id}">None of these</button>
        <button class="btn ghost grow" data-action="toggle-lookup">Look up</button>
      </div>`);
    return;
  }
  if (enc.status === "unknown" || !enc.person) {
    setHTML(el, html`${read}<div class="panel">
      <div>Couldn't find a match${state.read && state.read.name ? html` for <strong>${state.read.name}</strong>` : ""}.</div>
      <div class="row">
        <button class="btn grow" data-action="lookup-prefill">Look up</button>
        <button class="btn ghost grow" data-action="add-note" data-enc="${enc.id}" data-name="">Add note</button>
        <button class="btn ghost grow" data-action="end" data-enc="${enc.id}">End</button>
      </div></div>`);
    return;
  }
  // confirmed
  const card = state.nowCard && state.nowCard.person && state.nowCard.person.id === enc.person.id ? state.nowCard : null;
  setHTML(el, html`${read}${renderCard(card, enc.person, { encounterId: enc.id, mode: "now" })}`);
}

async function loadNowCard() {
  const pid = nowPersonId();
  if (!pid) { state.nowCard = null; renderNow(); return; }
  try {
    const card = await api(`/api/people/${encodeURIComponent(pid)}/card`);
    if (nowPersonId() === pid) { state.nowCard = card; renderNow(); }
    if (card.status === "pending") schedulePoll();
  } catch (e) {
    if (!(e instanceof AuthError)) toast(`Couldn't load card: ${errText(e)}`, { kind: "warn" });
  }
}

// Fallback in case live updates (SSE) are down: poll pending cards for up to 2 min.
function schedulePoll(startedAt = Date.now()) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    const pending = [];
    if (state.nowCard && state.nowCard.status === "pending" && nowPersonId()) pending.push(["now", nowPersonId()]);
    if (state.cardView.card && state.cardView.card.status === "pending") pending.push(["view", state.cardView.personId]);
    if (!pending.length || Date.now() - startedAt > 120000 || document.hidden) return;
    for (const [where, pid] of pending) {
      try {
        const card = await api(`/api/people/${encodeURIComponent(pid)}/card`);
        applyCard(pid, card, where === "now" ? state.encounter && state.encounter.id : null, { quiet: true });
      } catch {}
    }
    schedulePoll(startedAt);
  }, state.sseOk ? 8000 : 4000);
}

function applyCard(personId, card, encounterId, { quiet = false } = {}) {
  let shown = false;
  if (nowPersonId() === personId) {
    const was = state.nowCard && state.nowCard.status;
    state.nowCard = card;
    renderNow();
    shown = state.tab === "now";
    if (!quiet && card.status === "full" && was !== "full" && state.tab !== "now") {
      toast(`Card ready: ${card.person.name}`, { kind: "ok", action: { label: "Open", fn: () => showTab("now") } });
    }
  }
  if (state.cardView.personId === personId) {
    state.cardView.card = card;
    if (state.tab === "card") { renderCardView(); shown = true; }
  }
  return shown;
}

function handleResolution(res, { fromLive = false } = {}) {
  if (!res || !res.encounter) return;
  const prevId = state.encounter && state.encounter.id;
  state.encounter = res.encounter;
  state.read = res.read || (prevId === res.encounter.id ? state.read : null);
  state.capturing = null;
  state.captureError = null;
  if (res.encounter.status === "confirmed") {
    if (!state.nowCard || !state.nowCard.person || state.nowCard.person.id !== res.encounter.person.id) state.nowCard = null;
    loadNowCard();
  } else {
    state.nowCard = null;
  }
  renderNow();
  if (fromLive || state.tab !== "now") {
    const enc = res.encounter;
    const who = enc.person ? enc.person.name : (res.read && res.read.name) || "someone new";
    const msg = enc.status === "needs_pick" ? `Heard a name: ${who}. Pick the right person.` : `Talking to ${who}?`;
    toast(msg, { action: state.tab !== "now" ? { label: "Open", fn: () => showTab("now") } : null });
  }
}

async function runCapture(label, thumb, fn) {
  state.capturing = { label, thumb };
  state.captureError = null;
  renderNow();
  if (state.tab !== "now") showTab("now");
  try {
    const res = await fn();
    handleResolution(res);
  } catch (e) {
    state.capturing = null;
    if (e instanceof AuthError) { state.captureError = null; renderNow(); return; }
    state.captureError = { message: errText(e), retry: () => runCapture(label, thumb, fn) };
    renderNow();
  }
}

async function loadCurrentEncounter() {
  try {
    const enc = await api("/api/encounters/current");
    if (!state.capturing) {
      const changed = (enc && enc.id) !== (state.encounter && state.encounter.id);
      state.encounter = enc || null;
      if (changed) state.read = null;
      if (enc && enc.status === "confirmed") loadNowCard();
      else state.nowCard = null;
      renderNow();
    }
  } catch (e) {
    if (e instanceof NetError) toast(`${e.message}`, { kind: "warn", action: { label: "Retry", fn: loadCurrentEncounter } });
  }
}

// ---------------------------------------------------------------- NOW: capture inputs

const photoInput = $("photo-input");
photoInput.addEventListener("change", async () => {
  const file = photoInput.files && photoInput.files[0];
  photoInput.value = "";
  if (!file) return;
  let blob;
  try {
    blob = await downscaleImage(file, 1600, 0.85);
  } catch (e) {
    blob = file; // let the server try the original
  }
  const thumb = URL.createObjectURL(blob);
  runCapture("Reading badge…", thumb, () => {
    const form = new FormData();
    form.append("file", blob, "capture.jpg");
    return api("/api/capture/photo", { method: "POST", form, timeout: 60000 });
  });
});

function setPanel(which) {
  const note = $("note-panel"), lookup = $("lookup-panel");
  note.hidden = which !== "note";
  lookup.hidden = which !== "lookup";
  document.querySelector('[data-action="toggle-note"]').classList.toggle("active", which === "note");
  document.querySelector('[data-action="toggle-lookup"]').classList.toggle("active", which === "lookup");
}

function renderNoteTarget() {
  const t = $("note-target");
  if (state.noteBind) {
    t.textContent = `Note for ${state.noteBind.name || "this conversation"}`;
    $("note-unbind").hidden = false;
  } else {
    const p = state.encounter && state.encounter.person;
    t.textContent = p ? `Note (goes to ${p.name} unless it names someone new)` : "New note. Name someone and we'll look them up.";
    $("note-unbind").hidden = true;
  }
}

function openNote(bind) {
  if (bind !== undefined) state.noteBind = bind;
  renderNoteTarget();
  setPanel("note");
  const ta = $("note-text");
  ta.focus({ preventScroll: true });
  ta.scrollIntoView({ block: "center", behavior: "smooth" });
}

const noteText = $("note-text");
noteText.value = localStorage.getItem(NOTE_DRAFT_KEY) || "";
noteText.addEventListener("input", () => localStorage.setItem(NOTE_DRAFT_KEY, noteText.value));
noteText.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $("note-panel").requestSubmit(); }
});

$("note-panel").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = noteText.value.trim();
  if (!text) { noteText.focus(); return; }
  const btn = $("note-panel").querySelector('button[type="submit"]');
  btn.disabled = true;
  const body = { text };
  if (state.noteBind && state.noteBind.encounterId) body.encounter_id = state.noteBind.encounterId;
  try {
    const res = await api("/api/capture/note", { method: "POST", body, timeout: 45000 });
    noteText.value = "";
    localStorage.removeItem(NOTE_DRAFT_KEY);
    state.noteBind = null;
    renderNoteTarget();
    toast("Note saved", { kind: "ok", timeout: 2500 });
    if (res && res.resolution) handleResolution(res.resolution);
    setPanel(null);
  } catch (err) {
    if (!(err instanceof AuthError)) toast(`Note not saved: ${errText(err)}. It's kept as a draft.`, { kind: "error", action: { label: "Retry", fn: () => $("note-panel").requestSubmit() } });
  } finally {
    btn.disabled = false;
  }
});

$("lookup-panel").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("lookup-name").value.trim();
  const company = $("lookup-company").value.trim();
  if (!name) return;
  const isUrl = /^(https?:\/\/)?([\w-]+\.)?linkedin\.com\//i.test(name) || /^https?:\/\//i.test(name);
  setPanel(null);
  $("lookup-name").blur(); $("lookup-company").blur();
  if (isUrl) {
    const url = /^https?:\/\//i.test(name) ? name : `https://${name}`;
    runCapture("Looking up profile…", null, () => api("/api/capture/link", { method: "POST", body: { url }, timeout: 60000 }));
  } else {
    runCapture(`Looking up ${name}…`, null, () => api("/api/capture/name", { method: "POST", body: company ? { name, company } : { name }, timeout: 60000 }));
  }
  $("lookup-name").value = ""; $("lookup-company").value = "";
});

// ---------------------------------------------------------------- listen

const listener = new Listener({
  onTranscript: (msg) => {
    if (msg.is_final) {
      state.transcript.push({ text: msg.text, speaker: msg.speaker });
      if (state.transcript.length > 3) state.transcript.splice(0, state.transcript.length - 3);
      state.interim = "";
    } else {
      state.interim = msg.text;
    }
    renderTranscript();
  },
  onStatus: ({ state: s, text }) => {
    $("listen-status").textContent = text || "";
    const on = listener.active;
    $("rec-dot").hidden = !(on && s === "listening");
    $("listen-card").classList.toggle("on", on);
    $("transcript").hidden = !on;
    if (s === "resumed") toast("Listening paused while the app was in the background. Resuming.", { kind: "warn", timeout: 4000 });
  },
  onStopped: (reason) => {
    $("listen-toggle").checked = false;
    $("rec-dot").hidden = true;
    $("listen-card").classList.remove("on");
    if (reason) toast(reason, { kind: "error", timeout: 8000 });
  },
  postText: async (payload) => {
    try { await api("/api/listen/text", { method: "POST", body: payload, timeout: 15000 }); } catch {}
  },
});

function renderTranscript() {
  const lines = state.transcript.map((l) => html`<div class="line final">${l.speaker != null ? html`<span class="spk">S${l.speaker + 1}</span>` : ""}${l.text}</div>`);
  if (state.interim) lines.push(html`<div class="line">${state.interim}</div>`);
  if (!lines.length) lines.push(html`<div class="line">Waiting for speech…</div>`);
  setHTML("transcript", lines);
}

// Fixture demo (listen_mode "demo"): no mic. The server plays a scripted conversation and its lines arrive
// as SSE `transcript` events through the same live pipeline.
function setDemoListen(on) {
  state.demoListen = on;
  $("listen-toggle").checked = on;
  $("listen-card").classList.toggle("on", on);
  $("rec-dot").hidden = !on;
  $("transcript").hidden = !on;
  $("listen-status").textContent = on ? "Listening" : "Off";
}

$("listen-toggle").addEventListener("change", async (e) => {
  if (e.target.checked) {
    if (!state.me) { e.target.checked = false; toast("Not connected to the server yet.", { kind: "warn" }); return; }
    state.transcript = []; state.interim = "";
    renderTranscript();
    $("transcript").hidden = false;
    if (state.me.listen_mode === "demo") {
      setDemoListen(true);
      try { await api("/api/demo/play", { method: "POST", body: {} }); } catch (err) { toast(errText(err), { kind: "error" }); setDemoListen(false); }
      return;
    }
    listener.start(state.me.listen_mode);
  } else if (state.demoListen) {
    setDemoListen(false);
  } else {
    listener.stop();
    $("transcript").hidden = true;
  }
});

// ---------------------------------------------------------------- CARD view

async function openCard(personId, encounterId) {
  state.cardView = { personId, encounterId: encounterId || null, card: null, error: null };
  showTab("card");
  try {
    const card = await api(`/api/people/${encodeURIComponent(personId)}/card`);
    if (state.cardView.personId === personId) {
      state.cardView.card = card;
      renderCardView();
      if (card.status === "pending") schedulePoll();
    }
  } catch (e) {
    if (state.cardView.personId === personId) {
      state.cardView.error = errText(e);
      renderCardView();
    }
  }
}

function renderCardView() {
  const el = $("card-view");
  const v = state.cardView;
  if (!v.personId) {
    const pid = nowPersonId();
    if (pid) {
      setHTML(el, renderCard(state.nowCard, state.encounter.person, { encounterId: state.encounter.id, mode: "view" }));
      return;
    }
    setHTML(el, html`<div class="hint">No card open. Snap a badge on <strong>Now</strong> or pick someone in <strong>People</strong>.</div>`);
    return;
  }
  if (v.error) {
    setHTML(el, html`<div class="panel"><div class="error">${v.error}</div><button class="btn" data-action="card-retry">Retry</button></div>`);
    return;
  }
  // If this person is the current conversation, show conversation actions.
  const encId = v.encounterId || (nowPersonId() === v.personId ? state.encounter.id : null);
  if (!v.card) {
    setHTML(el, html`<div class="enc-status"><div class="spinner"></div><div>Loading card…</div></div>`);
    return;
  }
  setHTML(el, renderCard(v.card, null, { encounterId: encId, mode: "view" }));
}

// ---------------------------------------------------------------- PEOPLE

function personItem(p, extra = "", encId = "") {
  return html`<button class="item" data-action="open-person" data-person="${p.id}" data-enc="${encId}">
    ${avatar(p)}
    <span class="meta"><div class="name">${p.name}${p.is_speaker ? html` <span class="pill speaker">Speaker</span>` : ""}</div>
    <div class="sub">${roleLine(p)}</div>${extra}</span>
  </button>`;
}

async function loadPeople() {
  setHTML("people-met", html`<div class="empty">Loading…</div>`);
  setHTML("people-shortlist", html`<div class="empty">Loading…</div>`);
  const [met, short] = await Promise.allSettled([
    api(`/api/encounters?date=${localDateStr()}`),
    api("/api/shortlist?limit=30"),
  ]);
  if (met.status === "fulfilled") {
    const encs = (met.value || []).filter((e) => e.person);
    setHTML("people-met", encs.length
      ? html`<div class="list">${encs.map((e) => personItem(e.person, html`<div class="sess">${fmtTime(e.at)}${e.summary ? html` · ${e.summary}` : ""}</div>`, e.id))}</div>`
      : html`<div class="empty">No one yet today.</div>`);
  } else {
    setHTML("people-met", html`<div class="empty error">${errText(met.reason)}</div>`);
  }
  if (short.status === "fulfilled") {
    const rows = short.value || [];
    setHTML("people-shortlist", rows.length
      ? html`<div class="list">${rows.map((r) => personItem(r.person, html`
          ${r.why ? html`<div class="why">${r.why}</div>` : ""}
          ${(r.sessions || []).slice(0, 2).map((s) => html`<div class="sess">${s.title}${s.start ? ` · ${fmtDayTime(s.start)}` : ""}${s.room ? ` · ${s.room}` : ""}</div>`)}`))}</div>`
      : html`<div class="empty">No shortlist yet. Import the event and set your goals.</div>`);
  } else {
    setHTML("people-shortlist", html`<div class="empty error">${errText(short.reason)}</div>`);
  }
}

const runSearch = debounce(async (q) => {
  if (!q) { setHTML("people-results", ""); return; }
  try {
    const rows = await api(`/api/people?q=${encodeURIComponent(q)}&limit=20`);
    if ($("people-search").value.trim() !== q) return;
    setHTML("people-results", rows && rows.length
      ? html`<div class="list" style="margin-bottom:8px">${rows.map((p) => personItem(p))}</div>`
      : html`<div class="empty">No matches.</div>`);
  } catch (e) {
    setHTML("people-results", html`<div class="empty error">${errText(e)}</div>`);
  }
}, 300);
$("people-search").addEventListener("input", (e) => runSearch(e.target.value.trim()));

// ---------------------------------------------------------------- RECAP

const recapDate = $("recap-date");
recapDate.value = localDateStr();
recapDate.addEventListener("change", () => loadRecap());

function actionItem(a) {
  return html`<div class="ai ${a.status === "done" ? "done" : ""}">
    <label>
      <input type="checkbox" data-action="ai-toggle" data-id="${a.id}" ${a.status === "done" ? raw("checked") : ""}>
      <span><span class="ai-text">${a.text}</span><span class="owner ${a.owner}">${a.owner === "me" ? "You" : a.owner === "them" ? "Them" : "?"}</span>
      ${a.due ? html` <span class="src">due ${fmtDay(a.due) || a.due}</span>` : ""}
      ${a.quote ? html`<div class="quote">“${a.quote}”</div>` : ""}</span>
    </label>
  </div>`;
}

let recapData = null;
async function loadRecap() {
  setHTML("recap-body", html`<div class="empty">Loading…</div>`);
  try {
    const d = await api(`/api/recap?date=${encodeURIComponent(recapDate.value || localDateStr())}`);
    recapData = d;
    renderRecap();
  } catch (e) {
    setHTML("recap-body", html`<div class="empty error">${errText(e)}</div>`);
  }
}

function renderRecap() {
  const d = recapData || { encounters: [], open_items: [] };
  const open = d.open_items || [];
  const encs = d.encounters || [];
  setHTML("recap-body", html`
    <h2>Open action items (${open.length})</h2>
    ${open.length ? html`<div class="panel">${open.map(actionItem)}</div>` : html`<div class="empty">Nothing open.</div>`}
    <h2>Conversations (${encs.length})</h2>
    ${encs.length ? encs.map((r, i) => {
      const enc = r.encounter || {};
      const p = enc.person;
      return html`<div class="recap-enc">
        <div class="head">${avatar(p || { name: "?" })}
          <div class="grow"><div class="name"><strong>${p ? p.name : "Unknown person"}</strong></div>
          <div class="time">${fmtTime(enc.at)}${enc.place ? ` · ${enc.place}` : ""}${p ? ` · ${roleLine(p)}` : ""}</div></div>
          ${p ? html`<button class="btn small-btn" data-action="open-person" data-person="${p.id}">Card</button>` : ""}
        </div>
        ${r.summary ? html`<p>${r.summary}</p>` : html`<p class="muted small">No summary yet. Run recap.</p>`}
        ${r.notes && r.notes.length ? html`<div class="sec"><div class="sec-t">Your notes</div><ul class="notes">${r.notes.map((n) => html`<li>${n.text} <span class="src">${fmtTime(n.at)}</span></li>`)}</ul></div>` : ""}
        ${r.action_items && r.action_items.length ? html`<div class="sec"><div class="sec-t">Action items</div>${r.action_items.map(actionItem)}</div>` : ""}
        ${r.follow_up ? html`<div class="sec"><div class="sec-t">Follow-up draft (${r.follow_up.channel === "linkedin" ? "LinkedIn" : "email"})</div>
          <div class="draft" id="draft-${i}">${r.follow_up.text}</div>
          <button class="btn block" style="margin-top:8px" data-action="copy-draft" data-idx="${i}">Copy</button></div>` : ""}
      </div>`;
    }) : html`<div class="empty">No conversations on this day.</div>`}
  `);
}

$("recap-run").addEventListener("click", async () => {
  const btn = $("recap-run");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "Running…";
  try {
    const r = await api("/api/recap/run", { method: "POST", body: { date: recapDate.value || localDateStr() }, timeout: 240000 });
    const n = r && r.encounters != null ? r.encounters : 0;
    toast(`Recap done: ${n} conversation${n === 1 ? "" : "s"}`, { kind: "ok" });
    await loadRecap();
  } catch (e) {
    if (!(e instanceof AuthError)) toast(`Recap failed: ${errText(e)}`, { kind: "error" });
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

$("ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("ask-text").value.trim();
  if (!q) return;
  const ans = $("ask-answer");
  ans.hidden = false;
  ans.textContent = "Thinking…";
  const btn = $("ask-form").querySelector("button");
  btn.disabled = true;
  try {
    const r = await api("/api/ask", { method: "POST", body: { question: q }, timeout: 120000 });
    ans.textContent = (r && r.answer) || "No answer.";
  } catch (err) {
    ans.textContent = errText(err);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- SETTINGS

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isStandalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

async function loadSettings() {
  renderMeInfo();
  try {
    const s = await api("/api/settings");
    $("goals-text").value = (s.goals || []).join("\n");
    if (s.me) $("me-info").textContent = `Signed in as ${s.me.name || "you"}${s.me.linkedin_url ? ` · ${s.me.linkedin_url}` : ""}`;
  } catch (e) {
    if (!(e instanceof AuthError)) toast(`Couldn't load settings: ${errText(e)}`, { kind: "warn" });
  }
  renderPush();
}

function renderMeInfo() {
  const me = state.me;
  $("listen-info").textContent = !me ? "Not connected." : me.listen_mode === "deepgram"
    ? "Server transcription (Deepgram) with speaker labels. Works while Wingman is open and the screen is on. iPhone pauses it when you lock the screen or switch apps."
    : "On-device speech recognition from your browser (no Deepgram key on the server). Works while Wingman is open and the screen is on; no speaker labels.";
  if (me && !$("me-info").textContent) $("me-info").textContent = `Signed in as ${me.name || "you"}`;
}

async function renderPush() {
  const info = $("push-info"), btn = $("push-enable");
  btn.hidden = true;
  const key = state.me && state.me.push_public_key;
  if (!key) { info.textContent = "Push notifications aren't set up on the server. Cards still update live while the app is open."; return; }
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    info.textContent = isIOS && !isStandalone
      ? "On iPhone, add Wingman to your Home Screen first (Share › Add to Home Screen), then open it from there to enable notifications."
      : "This browser doesn't support push notifications.";
    return;
  }
  if (isIOS && !isStandalone) {
    info.textContent = "On iPhone, add Wingman to your Home Screen first (Share › Add to Home Screen), then open it from there to enable notifications.";
    return;
  }
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub && Notification.permission === "granted") {
      info.textContent = "Notifications are on. You'll get one when a full card is ready and the app isn't open.";
      return;
    }
  } catch {}
  info.textContent = Notification.permission === "denied"
    ? "Notifications are blocked. Turn them on in iOS Settings › Notifications › Wingman."
    : "Get a notification when a full card is ready while the app is closed.";
  btn.hidden = Notification.permission === "denied";
}

async function enablePush() {
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { toast("Notifications not allowed.", { kind: "warn" }); renderPush(); return; }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(state.me.push_public_key) });
    }
    await api("/api/push/subscribe", { method: "POST", body: { subscription: sub.toJSON() } });
    toast("Notifications on", { kind: "ok" });
  } catch (e) {
    toast(`Couldn't enable notifications: ${errText(e)}`, { kind: "error" });
  }
  renderPush();
}

$("goals-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const goals = $("goals-text").value.split("\n").map((s) => s.trim()).filter(Boolean);
  try {
    const s = await api("/api/settings", { method: "PUT", body: { goals } });
    $("goals-text").value = (s.goals || goals).join("\n");
    if (state.me) state.me.goals = s.goals || goals;
    toast("Goals saved", { kind: "ok", timeout: 2500 });
  } catch (err) {
    if (!(err instanceof AuthError)) toast(`Not saved: ${errText(err)}`, { kind: "error" });
  }
});

$("token-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const token = $("token-text").value.trim();
  if (!token) return;
  try {
    await login(token);
    $("token-text").value = "";
    toast("Signed in", { kind: "ok", timeout: 2500 });
    await loadAll();
  } catch (err) {
    toast(err instanceof AuthError ? "That token didn't work." : errText(err), { kind: "error" });
  }
});

// ---------------------------------------------------------------- global actions

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const a = el.dataset.action;
  const enc = el.dataset.enc;
  const person = el.dataset.person;
  switch (a) {
    case "photo":
      photoInput.click();
      break;
    case "toggle-note":
      if ($("note-panel").hidden) openNote(); else setPanel(null);
      break;
    case "toggle-lookup":
      if ($("lookup-panel").hidden) { setPanel("lookup"); $("lookup-name").focus(); } else setPanel(null);
      break;
    case "lookup-prefill":
      setPanel("lookup");
      if (state.read) { $("lookup-name").value = state.read.name || ""; $("lookup-company").value = state.read.company || ""; }
      $("lookup-name").focus();
      break;
    case "note-clear":
      noteText.value = ""; localStorage.removeItem(NOTE_DRAFT_KEY); noteText.focus();
      break;
    case "note-unbind":
      state.noteBind = null; renderNoteTarget();
      break;
    case "add-note":
      if (state.tab !== "now") showTab("now");
      openNote(enc ? { encounterId: enc, name: el.dataset.name || "" } : null);
      break;
    case "capture-dismiss":
      state.captureError = null; renderNow();
      break;
    case "capture-retry":
      if (state.captureError && state.captureError.retry) state.captureError.retry();
      break;
    case "pick":
      el.disabled = true;
      try {
        const r = await api(`/api/encounters/${encodeURIComponent(enc)}/confirm`, { method: "POST", body: { person_id: person } });
        handleResolution({ encounter: r, read: state.read });
      } catch (err) { el.disabled = false; if (!(err instanceof AuthError)) toast(errText(err), { kind: "error" }); }
      break;
    case "none-of-these": {
      el.disabled = true;
      const cands = (state.encounter && state.encounter.candidates) || [];
      try {
        let r = state.encounter;
        for (const c of cands) {
          r = await api(`/api/encounters/${encodeURIComponent(enc)}/reject`, { method: "POST", body: { person_id: c.id } });
        }
        handleResolution({ encounter: r, read: state.read });
      } catch (err) { el.disabled = false; if (!(err instanceof AuthError)) toast(errText(err), { kind: "error" }); }
      break;
    }
    case "not-them":
      el.disabled = true;
      try {
        const r = await api(`/api/encounters/${encodeURIComponent(enc)}/reject`, { method: "POST", body: { person_id: person } });
        state.nowCard = null;
        handleResolution({ encounter: r, read: state.read });
        if (state.tab === "card") showTab("now");
      } catch (err) { el.disabled = false; if (!(err instanceof AuthError)) toast(errText(err), { kind: "error" }); }
      break;
    case "end":
      el.disabled = true;
      try {
        await api(`/api/encounters/${encodeURIComponent(enc)}/end`, { method: "POST" });
        state.encounter = null; state.nowCard = null; state.read = null;
        if (state.noteBind && state.noteBind.encounterId === enc) state.noteBind = null;
        renderNow(); renderNoteTarget();
        if (state.tab === "card") { state.cardView.encounterId = null; renderCardView(); }
        toast("Conversation ended. Dictate a quick note while it's fresh?", { kind: "ok", action: { label: "Note", fn: () => { showTab("now"); openNote({ encounterId: enc, name: "" }); } } });
      } catch (err) { el.disabled = false; if (!(err instanceof AuthError)) toast(errText(err), { kind: "error" }); }
      break;
    case "open-person":
      openCard(person, enc || null);
      break;
    case "card-retry":
      if (state.cardView.personId) openCard(state.cardView.personId, state.cardView.encounterId);
      break;
    case "back":
      showTab(state.prevTab && state.prevTab !== "card" ? state.prevTab : "now");
      break;
    case "start-convo": {
      const p = state.cardView.card && state.cardView.card.person;
      if (!p) break;
      if (p.linkedin_url) runCapture(`Starting with ${p.name}…`, null, () => api("/api/capture/link", { method: "POST", body: { url: p.linkedin_url }, timeout: 60000 }));
      else runCapture(`Starting with ${p.name}…`, null, () => api("/api/capture/name", { method: "POST", body: p.company ? { name: p.name, company: p.company } : { name: p.name }, timeout: 60000 }));
      break;
    }
    case "refresh-card":
      el.disabled = true;
      try {
        const card = await api(`/api/people/${encodeURIComponent(person)}/card/refresh`, { method: "POST", timeout: 30000 });
        applyCard(person, card, null, { quiet: true });
        schedulePoll();
      } catch (err) { if (!(err instanceof AuthError)) toast(errText(err), { kind: "error" }); }
      el.disabled = false;
      break;
    case "copy-draft": {
      const d = $(`draft-${el.dataset.idx}`);
      const text = d ? d.textContent : "";
      try { await navigator.clipboard.writeText(text); toast("Copied", { kind: "ok", timeout: 1500 }); }
      catch {
        const r = document.createRange(); r.selectNodeContents(d);
        const s = getSelection(); s.removeAllRanges(); s.addRange(r);
        toast("Selected. Tap Copy.", { timeout: 2500 });
      }
      break;
    }
    case "push-enable":
      enablePush();
      break;
    case "app-update":
      try {
        const reg = navigator.serviceWorker && await navigator.serviceWorker.getRegistration();
        if (reg) await reg.update();
      } catch {}
      location.reload();
      break;
    default:
      break;
  }
});

document.addEventListener("change", async (e) => {
  const el = e.target;
  if (el.dataset && el.dataset.action === "ai-toggle") {
    const id = el.dataset.id;
    const status = el.checked ? "done" : "open";
    try {
      await api(`/api/action-items/${encodeURIComponent(id)}`, { method: "PATCH", body: { status } });
      if (recapData) {
        const upd = (a) => { if (a.id === id) a.status = status; };
        (recapData.open_items || []).forEach(upd);
        (recapData.encounters || []).forEach((r) => (r.action_items || []).forEach(upd));
        if (status === "done") recapData.open_items = (recapData.open_items || []).filter((a) => a.id !== id);
        renderRecap();
      }
    } catch (err) {
      el.checked = !el.checked;
      if (!(err instanceof AuthError)) toast(errText(err), { kind: "error" });
    }
  }
});

// ---------------------------------------------------------------- live updates (SSE)

let es = null;
let sseBackoff = 1000;
let sseTimer = null;

function setSseDot(ok) {
  state.sseOk = ok;
  const d = $("sse-dot");
  d.classList.toggle("ok", ok);
  d.classList.toggle("bad", !ok);
  d.title = ok ? "Live updates on" : "Live updates reconnecting";
}

function closeSSE() {
  clearTimeout(sseTimer);
  if (es) { es.close(); es = null; }
}

function connectSSE() {
  closeSSE();
  if (document.hidden || !state.me) return;
  const src = new EventSource("/api/events", { withCredentials: true });
  es = src;
  src.onopen = () => { sseBackoff = 1000; setSseDot(true); };
  src.onerror = () => {
    if (es !== src) return;
    setSseDot(false);
    src.close();
    es = null;
    clearTimeout(sseTimer);
    const wait = sseBackoff;
    sseBackoff = Math.min(30000, sseBackoff * 2);
    sseTimer = setTimeout(async () => {
      try { await api("/api/me", { timeout: 10000 }); } catch (e) { if (e instanceof AuthError) return; }
      connectSSE();
      loadCurrentEncounter();
    }, wait);
  };
  const on = (name, fn) => src.addEventListener(name, (ev) => {
    let data; try { data = JSON.parse(ev.data); } catch { return; }
    fn(data);
  });
  on("resolution", (res) => handleResolution(res, { fromLive: true }));
  on("card", (d) => { if (d && d.card) applyCard(d.person_id, d.card, d.encounter_id); });
  on("nudge", (d) => {
    toast(d.text, { kind: "nudge", timeout: 9000, iconName: d.kind || "topic" });
    if (navigator.vibrate) navigator.vibrate(80);
  });
  on("transcript", (d) => {
    if (!d.demo || !d.is_final) return;
    if (!state.demoListen) setDemoListen(true);
    state.transcript.push({ text: d.text, speaker: d.speaker });
    if (state.transcript.length > 3) state.transcript.splice(0, state.transcript.length - 3);
    renderTranscript();
  });
  on("enrich", (d) => {
    if (d.status === "running") state.enriching.add(d.person_id);
    else state.enriching.delete(d.person_id);
    if (nowPersonId() === d.person_id) renderNow();
    if (state.tab === "card" && state.cardView.personId === d.person_id) renderCardView();
    if (d.status === "failed") {
      const name = (state.encounter && state.encounter.person && state.encounter.person.id === d.person_id && state.encounter.person.name)
        || (state.cardView.card && state.cardView.card.person.id === d.person_id && state.cardView.card.person.name);
      if (name) toast(`Couldn't fetch more about ${name}. Card uses what we have.`, { kind: "warn" });
    }
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) closeSSE();
  else if (state.me) { connectSSE(); loadCurrentEncounter(); }
});

// ---------------------------------------------------------------- boot

// "2026-10-28/2026-10-29" -> "Oct 28–29"
function fmtDates(range) {
  const [a, b] = String(range).split("/").map((d) => new Date(`${d}T12:00:00`));
  if (isNaN(a)) return range;
  const md = (d, opts) => d.toLocaleDateString(undefined, opts);
  if (!b || isNaN(b)) return md(a, { month: "short", day: "numeric" });
  return a.getMonth() === b.getMonth()
    ? `${md(a, { month: "short", day: "numeric" })}–${b.getDate()}`
    : `${md(a, { month: "short", day: "numeric" })} – ${md(b, { month: "short", day: "numeric" })}`;
}

function updateOnline() { $("offline-banner").hidden = navigator.onLine; }
window.addEventListener("online", () => { updateOnline(); if (state.me) { connectSSE(); loadCurrentEncounter(); } else loadAll(); });
window.addEventListener("offline", updateOnline);

async function loadAll() {
  try {
    const me = await api("/api/me");
    state.me = me;
    $("event-name").textContent = me.event ? `${me.event.name}${me.event.dates ? ` · ${fmtDates(me.event.dates)}` : ""}` : "No event loaded";
    renderMeInfo();
    renderNoteTarget();
    connectSSE();
    await loadCurrentEncounter();
    if (state.tab === "people") loadPeople();
    else if (state.tab === "recap") loadRecap();
    else if (state.tab === "settings") loadSettings();
  } catch (e) {
    if (e instanceof AuthError) return; // login overlay is showing
    setSseDot(false);
    toast(`Can't reach the Wingman server. ${errText(e)}`, { kind: "error", timeout: 0, action: { label: "Retry", fn: loadAll } });
  }
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data && e.data.type === "open" && e.data.url) {
      history.replaceState(null, "", e.data.url);
      routeFromHash();
    }
  });
}

updateOnline();
renderNow();
renderNoteTarget();
routeFromHash();
loadAll();
