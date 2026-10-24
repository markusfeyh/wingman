# Wingman: a conference copilot built on FalkorDB and Bright Data

Plan, 2026-10-24.

## 1. Summary

Wingman has three jobs:

1. **Before the event.** It scrapes the conference site (speakers, sessions, sponsors) and your own public profile, then builds a graph of who will be there and how they connect to you. You get a short list of people worth meeting.
2. **During the event.** When you meet someone, it works out who they are (from a badge photo, a QR code, a voice note, or later from the live conversation) and sends a card to your phone. The card shows what you have in common, two good questions to ask, and how the person fits your goals for the event.
3. **After the event.** You get a timeline of everyone you met, what you talked about, what you promised, and draft follow-up messages.

FalkorDB stores everything as a graph, so "common interests" become short paths between you and the other person. Bright Data collects the public web data: LinkedIn, Google results, personal sites and company news.

## 2. Where Wispr Flow fits

Wispr Flow is push-to-talk dictation. You hold a button, speak, and it types clean text into whatever text box is open. It does that very well. But it doesn't record in the background and it doesn't capture the other person's voice, so it can't be the part that listens to your conversations.

Use it for **voice notes**. When someone walks away, tap the note box in Wingman and say: *"Priya Shah, Stripe, runs fraud ML, wants an intro to Dana, send her our eval deck."* These notes are the best input the system gets: names, promises, and your own read on the person. No integration is needed, because Wispr types into any text field. (Wispr seems to have an API, but I found no official public docs, so the plan doesn't depend on it.)

For the conversation itself, use a separate recorder:

| Option | Real-time? | Build effort | Notes |
|---|---|---|---|
| **Granola iPhone app** (Phase 1) | No. Transcript arrives after the note ends | None | Picks up in-person conversations. Granola is already connected in this workspace, and its MCP server returns transcripts. Good enough for the recap. |
| **Own iOS app → Deepgram streaming** (Phase 2) | Yes, with speaker labels | Medium (background audio on iOS is fiddly) | Allows live detection of names and topics. A clip-on mic helps a lot on a loud expo floor. |
| **Omi wearable → real-time webhook** | Yes | Low to medium | The webhook receives transcript segments as they arrive. Check the payload format before building on it. Adds hardware. |

**Test this on day one:** iOS usually lets only one app use the microphone at a time, so starting a Wispr dictation may pause Granola or our own recorder. Plan to dictate right after each conversation, and check that the recorder resumes.

## 3. How it works

### Before (the night before)

1. You enter the event URL and your goals, e.g. "hire 2 ML infra engineers", "find fintech design partners".
2. Bright Data scrapes the speaker, session and sponsor pages. Claude extracts the people, talks and companies, and they go into the graph.
3. Speakers (and any attendee list you have) are enriched in a batch with Bright Data's LinkedIn profile and post scrapers, plus one Google search per person. This uses the async `/trigger` endpoint, so it can run overnight.
4. Your own profile is enriched the same way, so the graph knows your employers, schools, cities and topics.
5. Output: a ranked list of people worth meeting, each with one line on why and the sessions where you can find them.

Pre-enrichment is the main thing that makes cards fast. A graph lookup takes milliseconds. A cold scrape takes up to a minute.

### During: a card in under 10 seconds

Any of these starts a lookup:

- **Badge photo** (the best option). Claude vision reads the name, company and title.
- **LinkedIn QR code.** This gives the exact profile URL, so no guessing is needed.
- **Voice note** through Wispr: "just met Priya from Stripe".
- **Name heard in conversation** (Phase 2): "Hi, I'm Priya, I'm at Stripe", picked up from the live transcript.

What happens next:

1. **Identify the person** (see section 7). If the match isn't certain, the app shows 2–3 candidates with photo and headline, and you tap the right one.
2. **Quick card**, built from the graph (if the person was pre-enriched) or from search snippets. Takes about 3–10 s.
3. **Full card**, after the LinkedIn profile, posts and pages are scraped. Takes about 30–60 s and arrives as a push notification.

The card should be readable in 5 seconds:

- Name, photo, and role at their company
- **Common ground:** 2–3 items, each with a source ("Both posted about LLM evals this month", "Your friend Dana worked with them at Plaid")
- **Ask about:** 2 questions about something recent, such as their talk today, a post, or their company's funding round
- **Fit with your goals:** "Their team is hiring", "Possible design partner"
- **Heads-up:** low confidence, stale data, or "You met on Tuesday and promised her the deck"

### After: who did I meet, and what do I owe them

- **Timeline** of conversations: when and where (session or booth), the card, a transcript summary, and your voice notes.
- **Action items** pulled from transcripts and notes. Each one has an owner (you or them), a due date and the line it came from. For example: *Send Priya the eval deck. You, by Friday. Quote: "I'll send you our deck."*
- **Draft follow-ups** for each person (email or LinkedIn), referring to what you discussed.
- **Ask anything:** "Who did I meet who works on robotics?" or "Who from Acme did I talk to?" Claude answers by writing Cypher queries through the FalkorDB MCP server.
- **Export** action items to Reminders or your task system, and people to your CRM (for example, Obsidian notes).

## 4. Architecture

```
iPhone: web app in Phase 1, native app in Phase 2
  badge camera · QR scan · note box (Wispr types here) · live cards (web push)
  Phase 2: background mic → Deepgram streaming
        │  HTTPS / WebSocket
        ▼
Wingman API (Python, FastAPI)
  capture:     /badge  /qr  /note  /transcript-segment
  resolver:    find candidates, score them, ask you if unsure
  enrichment:  Bright Data jobs → Claude extraction → graph writes
  recommender: Cypher queries → Claude writes the card
  after-event: import transcripts → match them to people → action items → digest
        │
        ├── FalkorDB      graph + vector + full-text indexes
        ├── Bright Data   SERP API · Web Unlocker · LinkedIn and other scrapers
        ├── Claude        Haiku 5.5 for live extraction and cards, Sonnet 5.5 for the recap
        ├── Embeddings    Voyage or similar, for topic similarity
        └── Transcripts   Granola MCP/API (Phase 1) · Deepgram (Phase 2)
```

- **Why Python:** the FalkorDB client, GraphRAG SDK and Graphiti are all Python.
- **Hosting:** one small VM (Fly.io or similar) running the API plus FalkorDB in Docker, or FalkorDB Cloud. It must be reachable from your phone over conference Wi-Fi or cellular.
- **Why a web app first:** web apps on iOS can use the camera and receive push notifications once added to the home screen. That covers all of Phase 1. Recording in the background needs a native app, which is why live listening waits for Phase 2.

## 5. Graph model (FalkorDB)

**Nodes**

| Label | Key properties |
|---|---|
| `Person` | id, name, headline, photo_url, linkedin_url, location, `is_me` |
| `Org` | name, domain, industry, funding_stage |
| `School`, `City` | name |
| `Topic` | name, embedding. Normalized interests ("LLM evaluation", "Rust", "climbing") |
| `Event`, `Session` | title, time, room |
| `Encounter` | at, place, summary, confidence. One per conversation |
| `Note` | text, at, source (`wispr` or `transcript`) |
| `Chunk` | text, at, speaker, embedding. Pieces of a transcript |
| `ActionItem` | text, owner, due, status, quote |
| `Post` | url, text, posted_at |

**Edges**

```
(Person)-[:WORKS_AT {title, from, to}]->(Org)
(Person)-[:STUDIED_AT]->(School)      (Person)-[:LIVES_IN]->(City)
(Person)-[:INTERESTED_IN {weight}]->(Topic)
(Person)-[:AUTHORED]->(Post)-[:ABOUT]->(Topic)
(Person)-[:SPEAKS_AT]->(Session)-[:PART_OF]->(Event)
(Org)-[:SPONSORS]->(Event)
(Person)-[:KNOWS]->(Person)            from your contacts, past encounters, co-speakers
(Encounter)-[:WITH]->(Person)   (Encounter)-[:AT]->(Event)
(Encounter)-[:DISCUSSED]->(Topic)   (Encounter)-[:HAS]->(Note|Chunk)
(ActionItem)-[:FROM]->(Encounter)   (ActionItem)-[:FOR]->(Person)
```

Every fact edge carries `source_url`, `fetched_at` and `confidence`. This lets each card show where a claim came from, and lets the app skip stale data.

**Indexes**

```cypher
CREATE INDEX FOR (p:Person) ON (p.linkedin_url)
CREATE INDEX FOR (o:Org) ON (o.domain)
CALL db.idx.fulltext.createNodeIndex('Person', 'name')   // fuzzy name match from badges
CREATE VECTOR INDEX FOR (t:Topic) ON (t.embedding) OPTIONS {dimension: 1024, similarityFunction: 'cosine'}
CREATE VECTOR INDEX FOR (c:Chunk) ON (c.embedding) OPTIONS {dimension: 1024, similarityFunction: 'cosine'}
```

Store embeddings with `vecf32([...])`. FalkorDB silently leaves plain lists out of the index. Set `dimension` to match your embedding model.

**Queries the card is built from**

Things you share directly:

```cypher
MATCH (me:Person {is_me: true}), (them:Person {id: $id})
MATCH (me)-[:WORKS_AT|STUDIED_AT|LIVES_IN|INTERESTED_IN]->(x)
      <-[:WORKS_AT|STUDIED_AT|LIVES_IN|INTERESTED_IN]-(them)
RETURN labels(x)[0] AS kind, x.name AS shared
```

Someone you know who worked with them:

```cypher
MATCH (me:Person {is_me: true})-[:KNOWS]-(friend:Person)-[:WORKS_AT]->(o:Org)
      <-[:WORKS_AT]-(them:Person {id: $id})
RETURN friend.name, o.name
```

Similar interests that aren't an exact match (vector search):

```cypher
MATCH (them:Person {id: $id})-[:INTERESTED_IN]->(t:Topic)
CALL db.idx.vector.queryNodes('Topic', 'embedding', 5, t.embedding) YIELD node AS near, score
MATCH (me:Person {is_me: true})-[:INTERESTED_IN]->(near)
RETURN t.name AS theirs, near.name AS yours, score
```

People you've already met who share their interests, i.e. intros you could make:

```cypher
MATCH (them:Person {id: $id})-[:INTERESTED_IN]->(t:Topic)<-[:INTERESTED_IN]-(other:Person)
      <-[:WITH]-(:Encounter)
WHERE other <> them
RETURN other.name, collect(t.name) AS shared_topics LIMIT 3
```

Recap of an event:

```cypher
MATCH (e:Encounter)-[:AT]->(:Event {id: $event}), (e)-[:WITH]->(p:Person)
OPTIONAL MATCH (a:ActionItem {status: 'open'})-[:FROM]->(e)
RETURN p.name, e.at, e.summary, collect(a.text) AS open_items
ORDER BY e.at
```

**Graphiti as an alternative.** FalkorDB can serve as the storage backend for Graphiti, which turns each transcript or note into an "episode" and maintains a time-aware graph automatically. That saves code, but its extraction is less predictable and makes more LLM calls. Recommendation: use the hand-written schema above for the MVP, and try Graphiti for transcripts later if the extraction code grows.

## 6. Enrichment with Bright Data

Steps for each person:

1. **Find them.** Use the SERP API with `"<name>" "<company>" site:linkedin.com/in` and a broader `"<name>" <company>` query. The results give LinkedIn URL candidates, a personal site, talks, GitHub and news.
2. **LinkedIn profile.** Use the Scraper API's sync `/datasets/v3/scrape` with dataset `gd_l1viktl72bvl7bjuj0`, one URL per call. It waits up to about a minute and returns a `snapshot_id` if the scrape takes longer.
3. **LinkedIn posts.** Use dataset `gd_lyy3tktm25m4avu764`. Recent posts are the best signal for what someone cares about right now.
4. **Other pages.** Fetch their site, talk pages and company blog as markdown through Web Unlocker.
5. **Company.** Look up the LinkedIn company profile and search the news for timely hooks: funding, launches, hiring.

```bash
curl -X POST "https://api.brightdata.com/datasets/v3/scrape?dataset_id=gd_l1viktl72bvl7bjuj0&format=json" \
  -H "Authorization: Bearer $BRIGHTDATA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '[{"url": "https://www.linkedin.com/in/<handle>"}]'
```

For the pre-event batch, send the same dataset to the async `/trigger` endpoint with many URLs, and collect results by webhook or by polling. Cache every response on disk, keyed by URL, so retries and re-runs cost nothing.

**Data caveat.** Since November 13, 2025, LinkedIn has restricted public access to the Position, Experience and Education fields. Bright Data reports that their fill rates fell to about 2% and that those fields now come from a cache. Claims like "you both worked at Square" will often need other sources: headline, about section, posts, personal sites, Google results and conference bios. Speaker bios on the event site are especially useful for this.

**MCP server.** Bright Data also offers these tools (search, scrape-as-markdown, LinkedIn scrapers) as a hosted MCP server. Use it in Phase 0 and for ad-hoc "dig deeper on this person" requests in the recap chat. Use the REST APIs in the pipeline, where latency and cost need to be predictable.

## 7. Identifying the right person

This is the riskiest part. Opening with "Congrats on the marathon!" to the wrong Priya Shah is worse than having no card at all.

- **Candidates:** a full-text name match in the graph (pre-enriched attendees) plus SERP results.
- **Scoring:** name similarity, company match, overlap between their title or topics and the event, location, and presence on the speaker or attendee list.
- **Auto-accept** only when the top candidate is clearly ahead, for example when they're on the speaker list and the company matches. Otherwise show 2–3 candidates to tap.
- **Shortcuts:** a QR code or a pasted LinkedIn URL skips all of this.
- **Undo:** if you tap "not them", their facts are detached and the lookup reruns.
- **No face recognition.** It creates privacy problems and legal exposure (for example under Illinois's biometric privacy law, BIPA), and badges are enough.

## 8. Recommendations

1. **Facts from the graph** (the Cypher queries above): shared companies, schools, cities and topics; friends who worked with them; people you met who share their interests; the session you're both in today.
2. **Semantic matches** (vector search): for example, your interest in Rust and their posts about systems programming.
3. **Goal fit:** their profile compared with the goals you entered.
4. **Timely hooks:** a recent post, their talk today, company news, a job change.
5. **Writing the card:** Claude Haiku 5.5 turns the ranked facts into the card. The prompt requires every bullet to cite a fact ID from the graph, and any bullet without one is dropped. This stops it from inventing things.

**Mid-conversation nudges (Phase 2).** About every 30 seconds of transcript, extract the topics. If one matches something useful ("we're hiring", "looking for a vendor for X", a person you know), send a one-line nudge. Allow at most one nudge per conversation unless it's high value, so your phone isn't buzzing constantly.

## 9. After-event pipeline

Runs every night, or when you ask:

1. Pull the day's transcripts (from Granola in Phase 1, from our own store in Phase 2).
2. Split them into conversations and match each to a person. The signals are the time of the nearest badge scan or voice note, plus names mentioned in the transcript. Anything that doesn't match goes on a "Who was this?" list for you to review.
3. Claude Sonnet 5.5 extracts a summary, topics and commitments (who promised what, by when), each with the supporting quote.
4. Write the results to the graph and draft the follow-ups.
5. Send a morning digest: who you met yesterday, open promises, and who to look for today.

## 10. Privacy, consent and data handling

- **Recording.** Some US states, California among them, require everyone's consent to record a private conversation, and the EU has GDPR. The simplest approach is to ask: "Mind if my notetaker listens?" Also: don't keep raw audio (transcribe it, then delete it), give recording a one-tap pause, and auto-delete transcripts after N days, keeping only the summary.
- **Scraping.** Collect public data only, at low volume, for your own use. LinkedIn's terms still forbid scraping. Bright Data does the collecting, but you are responsible for what you store.
- **No face recognition** (see section 7).
- **No sensitive inferences.** The extraction prompt filters out health, politics and religion.
- **Forgetting people.** A "forget this person" command deletes their node and all their facts.
- **Basics:** secrets in environment variables, the graph encrypted at rest, the API behind authentication.

## 11. Phases

| Phase | Scope | Size |
|---|---|---|
| **0: Agent-only prototype** | Claude plus the FalkorDB, Bright Data and Granola MCP servers. Before: "Research this speaker list into the graph." After: "Who did I meet, and what did I promise?" Tests the recap with almost no code. | 1 day |
| **1: MVP** | Web app with badge photo, QR, Wispr notes and live cards. Pre-event batch enrichment, resolver, recommender, and a Granola-based recap with action items. | about 2 weeks |
| **2: Live listening** | Native iOS app (Expo) recording in the background to Deepgram streaming with speaker labels. Live name and topic detection, mid-conversation nudges, Apple Watch notifications. | about 2–3 weeks |
| **3: Polish** | Follow-ups sent from your email, CRM and Reminders export, history across events ("You met her at NeurIPS last year"), a cost dashboard. | ongoing |

**Phase 1 build order**

1. FalkorDB in Docker, the schema, and your own profile as seed data.
2. Bright Data client: SERP, LinkedIn profiles and posts, Web Unlocker, and an on-disk cache.
3. Conference site scraper, extraction, and graph writes.
4. Badge reading and the resolver.
5. Recommender queries and card writing.
6. Web app: capture screen, card screen, push notifications.
7. Granola import, matching transcripts to people, action items, the digest.
8. Dry run with 20 people you know and a recorded mock conversation, then a local meetup.

**Dry-run targets**

- No card shown as confirmed for the wrong person. Uncertain matches always go through the pick step.
- Quick card in 10 s or less; full card in 60 s or less.
- At least 80% of the promises you made show up as action items.
- Bright Data and LLM cost per person met stays within a budget you set.

## 12. Rough costs

Example: one 3-day conference, 300 people pre-enriched, 50 actually met.

- **Bright Data:** about 3–5 requests per person, so roughly 1,000–1,500 in total. New accounts get 5,000 free credits a month; after that you pay per successful record. Check current prices.
- **Deepgram (Phase 2):** about $0.0048 per minute streaming, per a third-party pricing guide. 8 hours a day for 3 days is about $7.
- **Claude:** small. The frequent calls are short Haiku calls.
- **Hosting:** one small VM.

## 13. Risks

| Risk | Mitigation |
|---|---|
| Card for the wrong person | Confidence threshold, a pick step, QR shortcut, one-tap undo |
| LinkedIn career history restricted | Rely on bios, posts, personal sites and search results |
| Noisy expo floor | Clip-on mic; badge photo as the main trigger |
| Wispr and the recorder fighting over the mic | Dictate after the conversation; test on day one |
| Slow scrapes | Pre-enrich the night before; quick card first |
| Looking at your phone mid-conversation | Card readable in 5 s and checked before you walk up; watch notifications in Phase 2 |
| Consent and privacy | Section 10 |

## 14. Open questions

1. **Which conference, and when?** This decides whether Phase 2 can be ready in time.
2. **Is live help during the conversation a must?** Or is a card at hello plus a good recap enough? The second option is about 2 weeks less work.
3. **Phone only,** or would you wear an Omi or a clip-on mic?
4. **Attendee list:** do you have one (many conference apps export it), or only the speaker list?

## Sources

- FalkorDB MCP server: https://docs.falkordb.com/genai-tools/mcpserver.md
- FalkorDB vector index: https://docs.falkordb.com/cypher/indexing/vector-index.md
- FalkorDB indexing (full-text): https://docs.falkordb.com/cypher/indexing
- FalkorDB Graphiti MCP: https://docs.falkordb.com/agentic-memory/graphiti-mcp-server.md
- Bright Data LinkedIn scrapers (dataset IDs, sync/async, the Nov 2025 restriction): https://docs.brightdata.com/datasets/scrapers/linkedin/introduction.md
- Bright Data first LinkedIn request: https://docs.brightdata.com/datasets/scrapers/linkedin/send-first-request.md
- Bright Data MCP FAQ: https://docs.brightdata.com/mcp-server/faqs
- Granola iOS transcription: https://docs.granola.ai/help-center/ios/transcription
- Omi real-time transcript integrations: https://docs.omi.me/doc/developer/apps/Integrations
- Wispr Flow overview: https://en.wikipedia.org/wiki/Wispr_Flow
- Speech-to-text comparison (Deepgram pricing and latency): https://smallest.ai/blog/most-accurate-real-time-transcription-apis-in-2026
