# Wingman API contract (v1)

The backend is FastAPI and serves the web app from `/` (static files in `web/`) and the API under `/api`.
All JSON. Times are ISO 8601 strings in UTC (`2026-10-28T17:05:00Z`). IDs are strings.

## Auth

Single user. `WINGMAN_TOKEN` in `.env`.

- `POST /api/login` `{token}` → `200 {ok:true}` and sets an HttpOnly cookie `wm` (1 year). `401` on a bad token.
- Every other `/api/*` route and `/ws/*` requires the cookie. Unauthenticated → `401 {detail}`.
- `GET /api/me` → `{name, headline, goals: [str], event: {id, name, dates} | null, listen_mode: "deepgram" | "browser", push_public_key: str | null}`

## Shared shapes

```ts
Person = {
  id: string, name: string, headline: string | null, company: string | null, title: string | null,
  photo_url: string | null, linkedin_url: string | null, location: string | null,
  is_speaker: boolean, enriched: "none" | "quick" | "full"
}
Candidate = Person & { score: number /* 0..1 */, why: string /* "on speaker list · company matches" */ }
Fact = { text: string, source: string /* short label: "LinkedIn post", "speaker bio", "graph" */, url: string | null }
Card = {
  person: Person,
  status: "pending" | "quick" | "full",
  common_ground: Fact[],     // 0-3
  ask_about: Fact[],         // 0-2
  goal_fit: Fact[],          // 0-2
  heads_up: string[],        // low confidence, stale data, "you met on Tue and promised X"
  sessions: Session[],       // their talks at this event
  previous_encounters: { id: string, at: string, summary: string | null }[],
  generated_at: string
}
Session = { id: string, title: string, start: string | null, end: string | null, room: string | null }
Encounter = {
  id: string, at: string, ended_at: string | null, place: string | null,
  person: Person | null, status: "resolving" | "needs_pick" | "confirmed" | "unknown",
  candidates: Candidate[], summary: string | null
}
Resolution = { encounter: Encounter, read: { name: string | null, company: string | null, title: string | null, raw_text: string | null } | null }
ActionItem = { id: string, text: string, owner: "me" | "them" | "unknown", due: string | null, quote: string | null, status: "open" | "done", person_id: string | null, encounter_id: string }
```

## Capture (during the event)

All capture calls create or reuse the **current encounter** and return a `Resolution` synchronously (typically 2–10 s).
If the top candidate is clearly ahead, `encounter.status == "confirmed"` and `encounter.person` is set; otherwise `"needs_pick"` with up to 3 candidates.
Card generation then runs in the background and arrives over SSE (`card` event).

- `POST /api/capture/photo` multipart `file` (jpeg/png/heic converted client-side to jpeg if possible) — QR code (LinkedIn URL) or badge.
- `POST /api/capture/link` `{url}` — a LinkedIn URL (pasted, or scanned with the iOS camera).
- `POST /api/capture/name` `{name, company?}` — manual lookup.
- `POST /api/capture/note` `{text, encounter_id?}` → `{note_id, encounter_id, resolution: Resolution | null}`. Wispr Flow types into this box. If the note names someone new ("just met Priya from Stripe"), a new encounter is resolved and returned in `resolution`; otherwise the note attaches to the current/given encounter.
- `POST /api/encounters/{id}/confirm` `{person_id}` → `Encounter` (status confirmed; card job starts).
- `POST /api/encounters/{id}/reject` `{person_id}` → `Encounter` (removes candidate; status `needs_pick` or `unknown`).
- `POST /api/encounters/{id}/end` → `Encounter`.
- `GET /api/encounters/current` → `Encounter | null` (open encounter from the last 20 minutes).
- `GET /api/encounters?date=YYYY-MM-DD` → `Encounter[]` (local event day; default today).

## People and cards

- `GET /api/people/{id}` → `Person`
- `GET /api/people/{id}/card` → `Card` (status `pending` while generating)
- `POST /api/people/{id}/card/refresh` → `Card` (status pending; regenerates)
- `GET /api/people?q=text&limit=20` → `Person[]` (full-text name/company search)
- `GET /api/shortlist?limit=30` → `{ person: Person, score: number, why: string, sessions: Session[] }[]` (worth meeting, ranked)

## Listen mode

- `GET /api/me` → `listen_mode` tells the client which path to use.
- **`deepgram` mode:** `WS /ws/listen`. Client sends binary frames of **PCM16 little-endian, mono, 16 kHz** (~100 ms per frame). Client may send text `{"type":"stop"}`. Server sends text JSON messages:
  - `{type:"transcript", text, speaker: number | null, is_final: boolean, at}`
  - `{type:"status", state: "connected" | "upstream_error" | "closed", detail?}`
- **`browser` mode** (no Deepgram key; use `webkitSpeechRecognition`/`SpeechRecognition` with `continuous=true, interimResults=true`, restart on `end`): `POST /api/listen/text` `{text, is_final, at}` for final results only → `{ok:true}`.
- In both modes the server runs detection on the rolling transcript: names → resolution (`resolution` SSE), topics/requests → `nudge` SSE.

## Server-sent events

`GET /api/events` (EventSource, cookie auth). Event names and `data` JSON:

- `resolution` → `Resolution` (from live listening or background work)
- `card` → `{ person_id, encounter_id | null, card: Card }`
- `nudge` → `{ encounter_id | null, text: string, kind: "intro" | "goal" | "topic" | "reminder" }`
- `enrich` → `{ person_id, status: "quick" | "full" | "failed" }`
- `ping` every 25 s (ignore)

## Recap (after)

- `POST /api/recap/run` `{date?: "YYYY-MM-DD"}` → `{ok:true, encounters: number}` (runs the recap job; can take a minute)
- `GET /api/recap?date=YYYY-MM-DD` → `{ date, encounters: { encounter: Encounter, summary: string | null, notes: {id, text, at}[], action_items: ActionItem[], follow_up: { channel: "email" | "linkedin", text: string } | null }[], open_items: ActionItem[] }`
- `PATCH /api/action-items/{id}` `{status: "open" | "done"}` → `ActionItem`
- `POST /api/ask` `{question}` → `{answer: string}` ("who did I meet who works on robotics?")

## Settings

- `GET /api/settings` → `{ goals: string[], me: { name, linkedin_url, about } }`
- `PUT /api/settings` `{ goals?: string[] }` → same shape

## Push (optional)

- `POST /api/push/subscribe` `{subscription}` (PushSubscription JSON) → `{ok:true}`. Server sends a push when a full card is ready and the user has no open SSE connection. `push_public_key` from `/api/me` is the VAPID key (null → push disabled).
