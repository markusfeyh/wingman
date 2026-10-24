# Phase 0: Wingman with no app, just Claude Code

`.mcp.json` in this folder gives Claude Code two tools:
- **falkordb**: query the `wingman` graph (start FalkorDB first: `docker start wingman-falkordb`).
- **brightdata**: Google search, scrape any page as markdown, LinkedIn scrapers. Needs `BRIGHTDATA_API_KEY` exported in your shell.

Granola is already connected to your Claude account, so its meeting tools are available too.

Start Claude Code in this folder (`cd wingman && claude`) and try:

**Before the event**
- "In the FalkorDB graph `wingman`, find speakers who share topics or past employers with (:Person {id:'me'}). Rank the top 15 for my goals in config/me.yaml and tell me which session to catch each one at."
- "Research <name> at <company> with Bright Data (LinkedIn + Google). Give me 3 talking points and 2 questions, with sources."

**After the event**
- "List my Granola meetings from Oct 28–29. For each, find the matching person in the `wingman` graph (Encounter -> WITH -> Person, or by name), summarize it, and list every promise I made with the quote."
- "Who did I meet who works on AI agents? Draft a short follow-up to each."

The graph schema is documented at the top of `wingman/graph.py`.
