# Wingman web app: notes for the backend

Plain HTML/CSS/ES modules, no build step. Files: `index.html`, `styles.css`, `app.js` (screens, SSE, actions), `lib.js` (escaping template, `api()`, formatting, image downscale), `listen.js` (listen mode), `sw.js`, `manifest.webmanifest`, `icons/`.

## What the backend must do

- Serve `web/` at `/` with SPA fallback to `index.html` for unknown non-`/api` paths (deep links like `/#card/<id>` only need `/`).
- MIME types: `.webmanifest` → `application/manifest+json`, `.js` → `text/javascript` (ES modules fail with a wrong type).
- Serve `/sw.js` from the root (scope `/`). Send `Cache-Control: no-cache` for `sw.js`, `index.html`, `*.js`, `*.css` so updates land; icons can be cached.
- Cookie auth: `POST /api/login` sets `wm` (HttpOnly, `SameSite=Lax`, `Secure` when on https). EventSource and WebSocket rely on it (same origin).
- SSE `/api/events`: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`; send `ping` every ~25 s. Reconnect is client-driven (closes on error, backs off 1→30 s, re-checks `/api/me` first so a 401 shows the login screen).
- WebSocket `/ws/listen`: unauthenticated → close with code **4401** (client then stops and asks to sign in). Other closes trigger reconnect with backoff; the client buffers ~5 s of audio while reconnecting.
- Audio frames: binary PCM16 LE, mono, 16 kHz, 100 ms (3200 bytes) each. Client sends `{"type":"stop"}` before closing.
- Error bodies: `{detail: string}`; the app shows `detail` to the user (e.g. capture failures get a Retry button).
- Image upload: field `file`, JPEG (client downscales to ≤1600 px; falls back to the original file if decoding fails, which may be HEIC).

## Contract assumptions

- `GET /api/encounters/current` may return JSON `null` when there's no open encounter.
- `GET /api/people/{id}/card` may return `status: "pending"`; the app then waits for the SSE `card` event and also polls every 4–8 s for up to 2 min as a fallback.
- "None of these" calls `POST /api/encounters/{id}/reject` once per candidate (no bulk endpoint).
- Starting a conversation with someone from People/Card ("Talking to them") reuses `POST /api/capture/link` (if `linkedin_url`) or `POST /api/capture/name`. A dedicated `POST /api/encounters {person_id}` would be cleaner but isn't required.
- Push payload (if implemented): JSON `{title, body, url, tag}`; `url` like `/#card/<person_id>` opens that card.
- `/api/settings` `me.linkedin_url` is shown in Settings if present.
- `GET /api/*` responses are cached by the service worker (network-first) so the last shortlist/cards show offline; `/api/events` and non-GET requests are never cached.

## iPhone behavior

- Listen works only while the app is open and the screen is on (a Screen Wake Lock is requested). When hidden it stops and restarts on return; if iOS blocks the restart without a tap, the next tap resumes it.
- Another app taking the mic (e.g. Wispr Flow dictation) shows "Mic unavailable … retrying" and retries with backoff.
- Push requires the app to be added to the Home Screen (iOS 16.4+); Settings explains this.
