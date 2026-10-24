// Recording stage: listens to the server's SSE `trace` events and shows each pipeline step next to the phone.
// demo/record.py drives it through window.stage (captions, taps, scenes, outro).

const $ = (id) => document.getElementById(id);
const SERVICE = { brightdata: "Bright Data", claude: "Claude", falkordb: "FalkorDB", wingman: "Wingman" };
const firstWord = (s) => String(s || "").split(" ")[0];
const MAX_ROWS = 16;
const CHECK = '<svg class="check" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5 6.5 12 13 4.5"/></svg>';

const state = {
  group: false,
  rows: new Map(),      // key -> { el, ids:Set, running:Set, last }
  bd: 0,
  bdByProduct: {},
  t: {},                // performance.now() marks: badge, quick, full
  flags: {},
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------- trace rows ----------

function rowParts(d) {
  const chip = d.service === "llm" ? firstWord(d.product) : SERVICE[d.service] || d.service;
  if (d.service === "brightdata") return { chip, bold: d.product, rest: d.action };
  if (d.service === "claude" || d.service === "llm") return { chip, bold: d.action, rest: d.product.split(" ").slice(1).join(" ") };
  return { chip, bold: d.action, rest: d.product !== chip && d.product !== "Resolver" ? d.product : "" };
}

function statusHTML(r, d) {
  if (r.running.size) return `<span class="result"><span class="spin"></span>running</span>`;
  const count = r.ids.size > 1 ? `${r.ids.size}× ` : "";
  if (d.state === "error") return `<span class="result">failed</span>`;
  const res = d.result ? `${CHECK}${esc(count + d.result)}` : `${CHECK}${count ? esc(count.trim()) : "done"}`;
  return `<span class="result">${res}</span>${d.ms != null ? `<span class="ms">${fmtMs(d.ms)}</span>` : ""}`;
}

const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`);

function render(r, d) {
  const p = rowParts(d);
  const keep = ["in", "out"].filter((c) => r.el.classList.contains(c)).map((c) => ` ${c}`).join("");
  r.el.className = `row ${d.service}${r.running.size ? " running" : ""}${d.state === "error" ? " error" : ""}${keep}`;
  r.el.innerHTML = `<div class="inner">
      <span class="chip ${d.service}">${esc(p.chip)}</span>
      <div class="body"><div class="what"><b>${esc(p.bold)}</b>${p.rest ? `<span class="rest">${esc(p.rest)}</span>` : ""}</div>
        ${d.detail ? `<div class="detail">${esc(d.detail)}</div>` : ""}</div>
      <div class="status">${statusHTML(r, d)}</div>
    </div>`;
}

function append(el) {
  const list = $("trace-list");
  list.appendChild(el);
  trim(el);
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("in")));
  while (list.children.length > MAX_ROWS) list.firstElementChild.remove();
}

// Keep the log top-down: when the next row won't fit, collapse the oldest rows as it expands.
function trim(newEl) {
  const list = $("trace-list");
  const cap = list.clientHeight - 12;
  const rows = [...list.children].filter((el) => !el.classList.contains("out"));
  let total = rows.reduce((h, el) => h + el.firstElementChild.scrollHeight, 0);
  for (const el of rows) {
    if (total <= cap || el === newEl) break;
    total -= el.firstElementChild.scrollHeight;
    el.classList.add("out");
    setTimeout(() => el.remove(), 650);
  }
}

function onTrace(d) {
  if (d.service === "brightdata" && d.state === "start") {
    state.bd += 1;
    state.bdByProduct[d.product] = (state.bdByProduct[d.product] || 0) + 1;
    if (state.t.badge) state.bdLive = (state.bdLive || 0) + 1;
    $("bd-count").innerHTML = `<b>${state.bd}</b> Bright Data call${state.bd === 1 ? "" : "s"}`;
  }
  if (d.action === "Read badge photo" && d.state === "start") state.t.badge = performance.now();
  const key = state.group ? `${d.service}|${d.product}|${d.action}` : d.id;
  let r = state.rows.get(key);
  if (!r) {
    r = { el: document.createElement("li"), ids: new Set(), running: new Set() };
    state.rows.set(key, r);
    render(r, d);
    append(r.el);
  }
  r.ids.add(d.id);
  if (d.state === "start") r.running.add(d.id); else r.running.delete(d.id);
  render(r, d);
}

function separator(label) {
  const el = document.createElement("li");
  el.className = "row sep";
  el.innerHTML = `<div class="inner"><span class="label">${esc(label)}</span></div>`;
  append(el);
}

// ---------- live updates ----------

function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => { state.flags.sse = true; };
  es.addEventListener("trace", (ev) => onTrace(JSON.parse(ev.data)));
  es.addEventListener("card", (ev) => {
    const d = JSON.parse(ev.data);
    const st = d.card && d.card.status;
    if (st === "quick" && !state.t.quick) state.t.quick = performance.now();
    if (st === "full" && !state.t.full) { state.t.full = performance.now(); state.flags.cardFull = true; }
  });
  es.addEventListener("demo", (ev) => { if (JSON.parse(ev.data).state === "done") state.flags.demoDone = true; });
  es.addEventListener("enrich", (ev) => { if (JSON.parse(ev.data).status === "failed") state.flags.enrichFailed = true; });
  es.addEventListener("nudge", () => { state.flags.nudge = true; });
}

// ---------- outro ----------

const PRODUCT_LINES = {
  "Business Search": "found <b>Maya</b> by name and company in Bright Data's people index",
  "Scraper API": "pulled her <b>LinkedIn profile and posts</b>, and every speaker's profile the night before",
  "SERP API": "searched <b>Google News and the web</b> for timely things to talk about",
  "Web Unlocker": "fetched <b>her blog post</b> as clean markdown",
};

// lines: optional {product: html} written by the recorder for this run (trusted, it's our own script)
function outro({ lines } = {}) {
  const n = state.bdLive || state.bd;
  const text = lines || PRODUCT_LINES;
  const products = Object.keys(text).filter((p) => state.bdByProduct[p]);
  $("outro-h").textContent = `${n} Bright Data calls turned one badge photo into a card.`;
  $("outro-list").innerHTML = products
    .map((p) => `<li><span class="chip brightdata">${esc(p)}</span><span>${text[p]}</span></li>`).join("");
  const words = ["", "One", "Two", "Three", "Four", "Five"];
  $("outro-times").textContent = `${words[products.length] || products.length} Bright Data products, one API key.`;
  $("stage").querySelector(".info-col").classList.add("closing");
  const o = $("outro");
  o.classList.add("enter");
  o.hidden = false;
  requestAnimationFrame(() => requestAnimationFrame(() => o.classList.remove("enter")));
}

// ---------- API for the recorder ----------

window.stage = {
  flags: state.flags,
  times: state.t,
  start() {
    $("title").classList.add("gone");
    $("stage").classList.add("on");
  },
  caption(h, p) {
    const c = $("caption");
    c.classList.add("swap");
    const nb = (s) => (s || "").replace(/Bright Data/g, "Bright\u00a0Data").replace(/Wispr Flow/g, "Wispr\u00a0Flow");
    setTimeout(() => {
      $("cap-h").textContent = nb(h);
      $("cap-p").textContent = nb(p);
      c.classList.remove("swap");
    }, 260);
  },
  scene(label, { group = false } = {}) {
    state.group = group;
    if (group) state.rows.clear();
    if (label) separator(label);
  },
  tap(x, y) {
    const t = document.createElement("span");
    t.className = "tap";
    t.style.left = `${x}px`;
    t.style.top = `${y}px`;
    $("taps").appendChild(t);
    setTimeout(() => t.remove(), 800);
  },
  dictation(on) { $("dictation").hidden = !on; },
  ff(on, factor = 4) { $("ff-label").textContent = `Sped up ${factor}×`; $("ff").hidden = !on; },
  outro,
};

// Say what's real: replayed fixtures, or live Bright Data + model calls.
fetch("/api/demo/info").then((r) => r.json()).then((info) => {
  const model = info.llm.startsWith("Claude") ? "Claude" : info.llm.split(" ").slice(1).join(" ");
  $("title-sub").textContent = `Bright Data finds the people you meet. FalkorDB connects them to you. ${model} writes the card.`;
  document.querySelector(".outro-foot").textContent = `Wingman · built on Bright Data, FalkorDB and ${firstWord(info.llm)}`;
  if (!info.fixtures) {
    const provider = firstWord(info.llm);
    $("disclaimer").textContent = `Live Bright Data and ${provider} calls. Long waits are sped up and marked.`;
  }
  state.flags.info = true;
}).catch(() => { state.flags.info = true; });

connect();
document.fonts.ready.then(() => { state.flags.fonts = true; });
