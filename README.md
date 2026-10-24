# Wingman

A conference copilot. Before the event it loads the speakers and works out who is worth meeting. During the event, a badge photo, a LinkedIn QR code, a voice note, or a name heard in conversation brings up a card: what you have in common, what to ask, and how the person fits your goals. After the event it gives you who you met, what you promised, and draft follow-ups.

Built on **FalkorDB** (graph + vector search), **Bright Data** (Google search, LinkedIn profiles, page fetches), and **Claude** (badge reading, extraction, cards, recaps). The plan is in `docs/PLAN.md`; the API contract is in `docs/API.md`.

## Setup (once)

```bash
cd wingman
cp .env.example .env            # the local .env already exists with WINGMAN_TOKEN + push keys filled in
# fill in ANTHROPIC_API_KEY, BRIGHTDATA_API_KEY (+ BRIGHTDATA_SERP_ZONE), optionally DEEPGRAM_API_KEY
$EDITOR config/me.yaml          # your LinkedIn URL, a few lines about you, your goals for the event
docker start wingman-falkordb || docker run -d --name wingman-falkordb -p 6379:6379 -p 3000:3000 \
  -v "$PWD/data/falkordb:/var/lib/falkordb/data" falkordb/falkordb:latest
uv sync
```

Bright Data: create a **SERP API** zone in the control panel and put its name in `BRIGHTDATA_SERP_ZONE`. LinkedIn profiles use the Scraper API and need only the API key.

## The night before

```bash
uv run python -m wingman.cli prep
```

This does four things:
1. Imports the GitHub Universe speakers and sessions (183 speakers, 162 sessions).
2. Builds your own profile.
3. Scrapes the speakers' LinkedIn profiles as one Bright Data batch (about 170 records) and reads their GitHub profiles.
4. Extracts topics and talking points with Claude and ranks a "worth meeting" shortlist.

Re-running is cheap, because every Bright Data and GitHub response is cached in `data/cache/`.

Other commands: `uv run python -m wingman.cli --help`.

## Run it

```bash
uv run python -m wingman.cli serve --port 8000     # http://localhost:8000
```

Your phone needs HTTPS to reach it. There are two options:
- **Fly.io (recommended for the conference):** `fly auth login`, then `fly launch --copy-config --no-deploy`, `fly volumes create wingman_data --size 1`, `fly secrets import < .env`, and `fly deploy`. The container runs FalkorDB and the app together, with data on the volume.
- **This Mac:** `tailscale funnel 8000`. It only works while the Mac is awake and online.

On the iPhone, open the URL in Safari, choose Share → Add to Home Screen, open it from the home screen, and enter `WINGMAN_TOKEN` once.

## At the conference

- **Badge / QR:** snap the badge, or their LinkedIn QR code (exact match, no guessing). If the match is uncertain you pick from 2–3 people. Cards say where each claim comes from.
- **Note:** tap the box and dictate with Wispr Flow, e.g. "Priya, Northwind, send her the eval deck." A note that names someone new starts a new conversation.
- **Listen:** turn it on after asking ("mind if my notetaker listens?"). The app transcribes live, spots self-introductions ("Hi, I'm Priya from Stripe"), and nudges you when something matches your goals. It needs the screen on with Wingman open. No audio is stored, only transcript text. With `DEEPGRAM_API_KEY` set you get speaker labels; without it, the phone's built-in speech recognition is used.
- **Recap:** run it each evening for summaries, promises with quotes, and follow-up drafts. You can also ask questions like "who did I meet who works on agents?"

## Privacy

- California requires everyone's consent to record a confidential conversation. Ask first; the listen toggle shows a reminder.
- No raw audio is stored.
- Prompts tell Claude not to mention or infer sensitive traits. There is no face recognition.
- Data lives in your FalkorDB only.
- Bright Data collects public profile data only.

## Phase 0 (no app)

See `docs/PHASE0.md`. `.mcp.json` lets Claude Code query the graph and Bright Data directly.

## Tests

```bash
uv run pytest -q     # unit tests + graph queries against a scratch FalkorDB graph
```
