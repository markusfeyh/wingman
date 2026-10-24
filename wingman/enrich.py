"""Turn raw sources (speaker bio, LinkedIn, GitHub, search snippets) into graph facts.

enriched levels on Person: none -> quick (bio/snippets only) -> full (LinkedIn scraped or unavailable).
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import brightdata, embed, github, graph, llm
from .config import event_config, me_config, settings

log = logging.getLogger("wingman.enrich")


def _local(iso: str | None) -> str:
    if not iso:
        return "time TBA"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(settings().wingman_timezone))
    return dt.strftime("%a %-I:%M %p")


async def _sources(p: dict, linkedin: dict | None) -> list[str]:
    out = []
    if p.get("is_me") and p.get("about"):
        out.append(f"Self-description:\n{p['about']}")
    if p.get("bio"):
        out.append(f"Conference speaker bio:\n{p['bio']}")
    if p.get("headline"):
        out.append(f"Headline: {p['headline']}")
    if p.get("serp_snippet"):
        out.append(f"Search result snippet: {p['serp_snippet']}")
    sessions = await graph.person_sessions(p["id"])
    if sessions:
        out.append("Talks at this event:\n" + "\n".join(f"- {s['title']}" for s in sessions))
    if linkedin:
        out.append("LinkedIn profile JSON (job history and education may be cached/stale):\n"
                   + brightdata.trim_profile(linkedin))
    if p.get("github"):
        gh = await github.user_summary(p["github"])
        if gh:
            out.append(f"GitHub profile and top repos:\n{gh}")
    return out


async def apply_linkedin(pid: str, li: dict) -> None:
    upd = {"id": pid}
    if li.get("position") and not (await graph.get_person(pid) or {}).get("headline"):
        upd["headline"] = li["position"]
    if li.get("avatar") and not li.get("default_avatar"):
        upd["photo_url_li"] = li["avatar"]
    upd["location"] = li.get("city") or li.get("location")
    if li.get("name"):
        upd["li_name"] = li["name"]
    await graph.upsert_person(upd)


async def enrich_person(pid: str, *, scrape: bool = True, linkedin: dict | None = None) -> str:
    p = await graph.get_person(pid)
    if not p:
        return "missing"
    if linkedin is None and scrape and p.get("linkedin_url") and brightdata.enabled():
        try:
            linkedin = await brightdata.linkedin_profile(p["linkedin_url"])
        except brightdata.BrightDataError as e:
            log.warning("linkedin scrape failed for %s: %s", pid, e)
    if linkedin:
        await apply_linkedin(pid, linkedin)
        p = await graph.get_person(pid) or p
    sources = await _sources(p, linkedin)
    if not sources:
        return p.get("enriched") or "none"
    facts = await llm.extract_person(p.get("name") or pid, "\n\n".join(sources))
    if not facts:
        return p.get("enriched") or "none"

    await graph.clear_derived(pid, "extract")
    for r in facts.roles:
        await graph.set_works_at(pid, r.org, r.title, r.current, "extract")
    for s in facts.schools:
        await graph.link_simple(pid, "STUDIED_AT", "School", s, "extract")
    if facts.city:
        await graph.link_simple(pid, "LIVES_IN", "City", facts.city, "extract")
    await graph.set_topics(pid, facts.topics, "extract")
    for h in facts.hooks:
        await graph.add_fact(pid, "hook", h.text, h.source, h.url, h.date)
    ev = event_config()
    for s in await graph.person_sessions(pid):
        await graph.add_fact(pid, "talk", f"Speaking at {ev.get('name', 'the event')}: \"{s['title']}\" "
                             f"({_local(s['start'])}, {s['room'] or 'room TBA'})", "event schedule")

    level = "full" if (linkedin or not p.get("linkedin_url")) else "quick"
    if p.get("enriched") == "full":
        level = "full"
    upd = {"id": pid, "enriched": level, "enriched_at": graph.now_ms()}
    if not p.get("headline") and facts.headline:
        upd["headline"] = facts.headline
    if not p.get("title") and facts.current_title:
        upd["title"] = facts.current_title
    if not p.get("company") and facts.current_company:
        upd["company"] = facts.current_company
    await graph.upsert_person(upd)
    await embed.embed_missing_topics()
    return level


async def enrich_me() -> str:
    me = me_config()
    await graph.upsert_person({"id": graph.ME, "name": me.get("name"), "linkedin_url": me.get("linkedin_url") or None,
                               "about": me.get("about") or None, "is_me": True})
    for k in me.get("known_people") or []:
        kid = graph.person_id_for(k.get("linkedin_url"), fallback="k:" + graph.slug(k["name"]))
        await graph.upsert_person({"id": kid, "name": k["name"], "linkedin_url": k.get("linkedin_url")})
        for c in k.get("companies") or []:
            await graph.set_works_at(kid, c, None, False, "known")
        await graph.q("MATCH (me:Person {id:$me}), (k:Person {id:$kid}) MERGE (me)-[r:KNOWS]->(k) "
                      "SET r.source='known'", {"me": graph.ME, "kid": kid})
    return await enrich_person(graph.ME, scrape=True)


async def enrich_speakers(limit: int | None = None, scrape: bool = True, concurrency: int = 6) -> dict:
    ids = [r[0] for r in await graph.q(
        "MATCH (p:Person {is_speaker:true}) WHERE coalesce(p.enriched,'none') <> 'full' RETURN p.id, p.linkedin_url "
        "ORDER BY p.id")]
    rows = await graph.q("MATCH (p:Person) WHERE p.id IN $ids RETURN p.id, p.linkedin_url", {"ids": ids})
    if limit:
        rows = rows[:limit]
    profiles: dict[str, dict] = {}
    urls = [u for _, u in rows if u]
    if scrape and urls and brightdata.enabled():
        log.info("scraping %d LinkedIn profiles via Bright Data (batch)", len(urls))
        try:
            profiles = await brightdata.linkedin_profiles_batch(urls)
        except brightdata.BrightDataError as e:
            log.warning("batch scrape failed, continuing with bios only: %s", e)
    sem = asyncio.Semaphore(concurrency)
    done: dict[str, int] = {}

    async def one(pid: str, url: str | None):
        async with sem:
            try:
                # profiles missing from the batch stay "quick"; a live lookup retries the scrape
                level = await enrich_person(pid, scrape=False, linkedin=profiles.get(url or ""))
            except Exception as e:  # keep the batch going
                log.warning("enrich %s failed: %s", pid, e)
                level = "error"
            done[level] = done.get(level, 0) + 1

    await asyncio.gather(*(one(pid, url) for pid, url in rows))
    return done
