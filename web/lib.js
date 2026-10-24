// Small helpers: safe HTML templating, API calls, formatting.

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
export const raw = (s) => new Raw(s);

function part(v) {
  if (v == null || v === false || v === true) return "";
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(part).join("");
  return esc(v);
}

// Tagged template: interpolated values are escaped unless wrapped in raw() or html``.
export function html(strings, ...vals) {
  let out = "";
  strings.forEach((s, i) => { out += s; if (i < vals.length) out += part(vals[i]); });
  return new Raw(out);
}

export function setHTML(el, content) {
  if (typeof el === "string") el = document.getElementById(el);
  if (el) el.innerHTML = part(content);
}

// Only allow http(s) URLs in href/src.
export function safeUrl(u) {
  if (!u || typeof u !== "string") return "";
  try {
    const url = new URL(u, location.href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch { return ""; }
}

export class AuthError extends Error {}
export class NetError extends Error {}

let unauthorizedHandler = null;
export function onUnauthorized(fn) { unauthorizedHandler = fn; }

export async function api(path, { method = "GET", body, form, timeout = 20000, authRedirect = true } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, {
      method,
      credentials: "same-origin",
      headers,
      body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
      signal: ctrl.signal,
    });
  } catch (e) {
    throw new NetError(e && e.name === "AbortError" ? "The server took too long to answer." : "No connection to the server.");
  } finally {
    clearTimeout(timer);
  }
  if (res.status === 401) {
    if (authRedirect && unauthorizedHandler) unauthorizedHandler();
    let detail = "Not signed in";
    try { const d = await res.json(); if (d && d.detail) detail = String(d.detail); } catch {}
    throw new AuthError(detail);
  }
  const ct = res.headers.get("content-type") || "";
  let data = null;
  if (ct.includes("application/json")) data = await res.json().catch(() => null);
  else data = await res.text().catch(() => null);
  if (!res.ok) {
    let msg = `Server error (${res.status})`;
    if (data && typeof data === "object" && data.detail) msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

export function initials(name) {
  return String(name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
}

export function avatar(person, cls = "") {
  const src = safeUrl(person && person.photo_url);
  if (src) return html`<img class="avatar ${cls}" src="${src}" alt="" loading="lazy" referrerpolicy="no-referrer" data-initials="${initials(person && person.name)}">`;
  return html`<span class="avatar ${cls}">${initials(person && person.name)}</span>`;
}

// Broken avatar photos fall back to initials (no inline handlers, CSP-friendly).
document.addEventListener("error", (e) => {
  const img = e.target;
  if (img && img.tagName === "IMG" && img.classList.contains("avatar")) {
    const span = document.createElement("span");
    span.className = img.className;
    span.textContent = img.dataset.initials || "?";
    img.replaceWith(span);
  }
}, true);

export function roleLine(p) {
  if (!p) return "";
  const parts = [];
  if (p.title) parts.push(p.title);
  if (p.company) parts.push(p.company);
  if (parts.length) return parts.join(" · ");
  return p.headline || "";
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const dayTimeFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });

function parseDate(s) {
  if (!s) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s); // date-only: treat as a local day, not UTC midnight
  const d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(s);
  return isNaN(d) ? null : d;
}
export const fmtTime = (s) => { const d = parseDate(s); return d ? timeFmt.format(d) : ""; };
export const fmtDayTime = (s) => { const d = parseDate(s); return d ? dayTimeFmt.format(d) : ""; };
export const fmtDay = (s) => { const d = parseDate(s); return d ? dayFmt.format(d) : ""; };

export function localDateStr(d = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// Downscale an image file to a JPEG blob no larger than maxDim on its long side.
export async function downscaleImage(file, maxDim = 1600, quality = 0.85) {
  let source, w, h;
  try {
    source = await createImageBitmap(file, { imageOrientation: "from-image" });
    w = source.width; h = source.height;
  } catch {
    source = await new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(file);
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Could not read that photo.")); };
      img.src = url;
    });
    w = source.naturalWidth; h = source.naturalHeight;
  }
  const scale = Math.min(1, maxDim / Math.max(w, h));
  const cw = Math.max(1, Math.round(w * scale));
  const ch = Math.max(1, Math.round(h * scale));
  const canvas = document.createElement("canvas");
  canvas.width = cw; canvas.height = ch;
  canvas.getContext("2d").drawImage(source, 0, 0, cw, ch);
  if (source.close) source.close();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
  if (!blob) throw new Error("Could not process that photo.");
  return blob;
}

export function urlBase64ToUint8Array(base64) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// Line icons (24px grid, drawn with currentColor) for places that need an icon in rendered HTML.
const ICON_PATHS = {
  alert: '<path d="M12 4 2.8 19.5h18.4L12 4z"/><path d="M12 10v4.2M12 17h.01"/>',
  goal: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none"/>',
  intro: '<circle cx="9" cy="9" r="3.2"/><path d="M3.5 19.5c.8-3 2.9-4.6 5.5-4.6s4.7 1.6 5.5 4.6M18 8v6M15 11h6"/>',
  topic: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.6 10.8c.6.5 1 1.2 1 2v.2h5.2v-.2c0-.8.4-1.5 1-2A6 6 0 0 0 12 3z"/>',
  reminder: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
};

export function icon(name, cls = "icon") {
  const d = ICON_PATHS[name];
  return d ? raw(`<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`) : "";
}
