# Demo videos

A recorded happy path with synthetic people: night before, badge photo → who is this →
quick card → full card, listen mode with a nudge, a Wispr voice note, and the evening recap. Next to the phone,
a "Behind the card" panel shows every pipeline step as it happens (each Bright Data call, model call and
FalkorDB query), streamed from the server's `trace` events.

| Take | Command | Data | Keys |
|---|---|---|---|
| Synthetic | `uv run --with playwright python demo/record.py` | Fictional people and companies; Bright Data and Claude responses replayed from `demo/fixtures/` with realistic, shortened latency | none |

Output goes to `demo/out/` (1920×1080, H.264). It needs FalkorDB running (`docker start wingman-falkordb`),
`ffmpeg`, and Google Chrome (Playwright drives your installed Chrome headless; no browser download).

## What's real in the take

Everything except the two network boundaries. The resolver, FalkorDB graph and vector search,
card building (including the "every line must cite a fact" check), live listening, recap and the phone app all
run for real. Fixture card answers cite facts by text, and `wingman/fixtures.py` maps them to the numbered facts
in the actual prompt, so a line whose fact isn't there is dropped just like an uncited claim.

## How the recorder works

`demo/record.py` starts the server on a separate graph (`wingman_demo`) with its own data
folder, seeds it (`POST /api/demo/seed`), renders a badge photo from `demo/stage/badge.html`, and opens
`demo/stage/` (served at `/stage/` in demo mode only). It drives the phone app inside the stage like a user,
including the file chooser for the badge, and pipes Chrome's screencast into ffmpeg at 30 fps.

Demo mode is on when `WINGMAN_FIXTURES` is set. In demo mode the
Listen switch plays the scripted conversation through the same path as the mic (`/api/demo/play`), because a
headless browser has no microphone.

## Bright Data in the demo

- **Business Search** (`POST /search/people`): finds the person by name and company in Bright Data's people
  index. It's early access; without it the resolver finds the LinkedIn profile with the SERP API.
- **Scraper API:** the LinkedIn profile, the employer's LinkedIn company page, and the speakers' profiles in one
  batch the night before.
- **SERP API:** Google News for the company, and web searches for the person and their own site.
- **Web Unlocker:** the person's own site or blog as markdown, with navigation stripped.

Privacy: results from people-search and data-broker sites, and Google redirect links that hide the real site,
are never fetched, sent to the model or shown. Phone numbers, emails and ages are stripped from snippets
(`wingman/enrich.py`, `_BLOCKED`, `_allowed`, `_clean`).

## Files

- `fixtures/brightdata.json`, `fixtures/claude.json`: synthetic responses for the fixture take.
- `me.yaml`, `world.yaml`: the synthetic user and speakers.
- `stage/`: the recording stage (Instrument Sans and JetBrains Mono, both SIL Open Font License, self-hosted).
- `record.py`: the recorder.
