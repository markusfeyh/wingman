// Wingman service worker: app shell cache (network-first with a short timeout,
// so updates land quickly but the app still opens on bad conference Wi-Fi),
// network-first for GET /api reads, and push notifications.

const VERSION = "wingman-v1";
const SHELL = [
  "/",
  "/index.html",
  "/styles.css",
  "/app.js",
  "/lib.js",
  "/listen.js",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((c) => c.addAll(SHELL)).catch(() => {}).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function offlinePage() {
  const body = `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<body style="background:#0b0d10;color:#eceef1;font:16px -apple-system,system-ui;padding:40px 20px;text-align:center">
<h1 style="font-size:20px">Wingman can't reach the server</h1>
<p style="color:#9aa3ae">Check your connection, then try again.</p>
<button onclick="location.reload()" style="font:inherit;padding:12px 20px;border-radius:12px;border:0;background:#5b93ff;color:#fff">Try again</button></body>`;
  return new Response(body, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
}

function timeout(ms) {
  return new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms));
}

async function networkFirst(request, { waitMs, cacheKey } = {}) {
  const cache = await caches.open(VERSION);
  const net = fetch(request).then((res) => {
    if (res && res.ok && request.method === "GET") cache.put(cacheKey || request, res.clone()).catch(() => {});
    return res;
  });
  net.catch(() => {}); // avoid unhandled rejections when the cache answers first
  try {
    return await (waitMs ? Promise.race([net, timeout(waitMs)]) : net);
  } catch (err) {
    const hit = await cache.match(cacheKey || request);
    if (hit) return hit;
    return net; // slow network and nothing cached: keep waiting (rejects if truly offline)
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.method !== "GET") return; // never touch POST/PUT/PATCH
  if (url.pathname.startsWith("/ws/")) return;
  if (url.pathname === "/api/events") return; // SSE stream: leave alone
  if (url.pathname.startsWith("/api/")) {
    // Reads only; keeps last-known shortlist/cards available offline.
    event.respondWith(networkFirst(req).catch(() => new Response(JSON.stringify({ detail: "Offline" }), {
      status: 503, headers: { "Content-Type": "application/json" },
    })));
    return;
  }
  if (req.mode === "navigate") {
    event.respondWith(
      networkFirst(req, { waitMs: 3500, cacheKey: "/index.html" })
        .catch(async () => (await caches.match("/index.html")) || offlinePage())
    );
    return;
  }
  event.respondWith(networkFirst(req, { waitMs: 3500 }));
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data && event.data.text() }; }
  const title = data.title || "Wingman";
  const options = {
    body: data.body || "Your card is ready.",
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png",
    tag: data.tag || "wingman",
    data: { url: data.url || "/#now" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/#now";
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin) {
        c.postMessage({ type: "open", url: target });
        return c.focus();
      }
    }
    return self.clients.openWindow(target);
  })());
});
