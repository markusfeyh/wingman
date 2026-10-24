"""Turn raw sources (speaker bio, LinkedIn, GitHub, search snippets) into graph facts.

enriched levels on Person: none -> quick (bio/snippets only) -> full (LinkedIn scraped or unavailable).
"""

import asyncio
import json
import logging
import re
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

from . import brightdata, embed, github, graph, hub, llm
from .config import event_config, me_config, settings

log = logging.getLogger("wingman.enrich")


def _local(iso: str | None) -> str:
    if not iso:
        return "time TBA"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(settings().wingman_timezone))
    return dt.strftime("%a %-I:%M %p")


# Results from these sites aren't the person's own page; a personal site, blog or talk page is more useful.
_NOT_PERSONAL = ("linkedin.com", "x.com", "twitter.com", "facebook.com", "instagram.com", "youtube.com",
                 "crunchbase.com", "github.com", "theorg.com", "wikipedia.org")
# People-search and data-broker sites (phone numbers, ages, home addresses, donations). Never fetched, never
# sent to the model, never shown: PLAN.md section 10 (no sensitive data, public professional info only).
_BLOCKED = ("rocketreach.co", "fastpeoplesearch.com", "signalhire.com", "zoominfo.com", "spokeo.com",
            "whitepages.com", "truepeoplesearch.com", "beenverified.com", "peoplefinders.com", "radaris.com",
            "intelius.com", "mylife.com", "contactout.com", "lusha.com", "apollo.io", "nuwber.com",
            "clustrmaps.com", "peekyou.com", "thatsthem.com", "usphonebook.com", "familytreenow.com",
            "instantcheckmate.com", "truthfinder.com", "peoplelooker.com", "smartbackgroundchecks.com",
            "idcrawl.com", "theballotbook.com", "voterrecords.com", "fastbackgroundcheck.com", "cyberbackgroundchecks.com",
            "peoplesearchnow.com", "411.com", "addresses.com", "anywho.com", "advancedbackgroundchecks.com",
            "rocketreach.com", "datanyze.com", "leadiq.com", "seamless.ai", "adapt.io", "kaspr.io")
_PII = re.compile(r"\(?\b\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b|[\w.+-]+@[\w-]+\.[\w.]+|\(\d{2}\)")


# Titles data brokers use, for results whose link hides the real site (Google /goto redirects)
_BROKER_TITLE = re.compile(r"\b(e-?mail|phone|address|contact (info|details)|background check|people search|"
                           r"public records|age \d+|\(\d{2}\))", re.I)


def _allowed(r: dict) -> bool:
    d = _domain(r.get("link") or "")
    if not d or any(d == b or d.endswith("." + b) for b in _BLOCKED):
        return False
    if d.endswith("google.com"):  # /goto and /url redirects: the real site is unknown, so skip it
        return False
    return not _BROKER_TITLE.search(r.get("title") or "")


def _clean(text: str | None) -> str:
    """Drop phone numbers, email addresses and '(36)'-style ages that search snippets sometimes carry."""
    return re.sub(r"\s{2,}", " ", _PII.sub("", text or "")).strip()


def _owns(r: dict, name: str) -> int:
    """How plausibly a page is theirs: 2 = their name is the domain (mayaokafor.dev), 1 = full name in the
    title (a profile or talk page about them), 0 = no."""
    parts = graph.slug(name).split("-")
    if not parts or not parts[0]:
        return 0
    domain = _domain(r["link"]).replace("-", "")
    if "".join(parts) in domain or (len(parts[-1]) >= 4 and domain.startswith(parts[-1])):
        return 2
    return 1 if "-".join(parts) in graph.slug(r.get("title") or "") else 0


def _readable(md: str, limit: int = 3500) -> str:
    """Drop navigation (link-only lines, menus, images) so the excerpt is the page's actual text."""
    keep = []
    for line in md.splitlines():
        t = line.strip()
        bare = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", t).strip(" *-|#>")
        if not bare or (len(bare) < 25 and t != bare):  # empty once links are removed, or a short menu item
            continue
        keep.append(t)
    return "\n".join(keep)[:limit]


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _domain(url: str) -> str:
    return (urllib.parse.urlparse(url).netloc or "").lower().removeprefix("www.")


async def web_lookups(p: dict) -> list[dict]:
    """Live lookups beyond LinkedIn, all through Bright Data: recent company news (SERP API, Google News),
    a web search for the person, and their own site or blog fetched as markdown (Web Unlocker).
    Returns [{product, kind, label, url, text}]."""
    name, company = p.get("name"), p.get("company")
    if not (name and company and brightdata.enabled()):
        return []

    async def news():
        rs = [r for r in await brightdata.google(f'"{company}"', num=5, news=True) if r.get("link") and _allowed(r)]
        return [{"product": "SERP API", "kind": "news", "label": _clean(r["title"]), "url": r["link"],
                 "text": f"{_clean(r['title'])} ({', '.join(x for x in (r.get('source'), r.get('date')) if x)}): "
                         f"{_clean(r['description'])} [{r['link']}]"} for r in rs[:3]]

    async def site():
        # company-qualified results go to the model (fewer namesakes); a name-only search only looks for a
        # site on their own domain (mayaokafor.dev), which often doesn't mention the employer
        found, by_name = await asyncio.gather(
            brightdata.google(f'"{name}" "{company}" -site:linkedin.com', num=8),
            brightdata.google(f'"{name}" -site:linkedin.com', num=8))
        rs = [r for r in found if r.get("link") and _allowed(r)]
        out = [{"product": "SERP API", "kind": "web", "label": _clean(r["title"]), "url": r["link"],
                "text": f"{_clean(r['title'])}: {_clean(r['description'])} [{r['link']}]"} for r in rs[:3]]
        own_domain = [r for r in by_name if r.get("link") and _allowed(r) and _owns(r, name) == 2]
        pages = [(_owns(r, name), i, r["link"]) for i, r in enumerate(own_domain + rs)
                 if not any(d in _domain(r["link"]) for d in _NOT_PERSONAL)]
        # their own domain first; on it, an About page beats the home page, which beats Contact
        best = max((x for x in pages if x[0]), default=None,
                   key=lambda x: (x[0], "contact" not in x[2].lower(), "about" in x[2].lower(), -x[1]))
        if best:
            own = best[2]
            md = _readable(await brightdata.fetch_markdown(own))
            if md.strip():
                out.append({"product": "Web Unlocker", "kind": "page", "label": _domain(own) + urllib.parse.urlparse(own).path,
                            "url": own, "text": md})
        return out

    items: list[dict] = []
    for r in await asyncio.gather(news(), site(), return_exceptions=True):
        if isinstance(r, Exception):
            log.warning("web lookup failed for %s: %s", name, r)
        else:
            items += r
    return items


def _web_source_text(p: dict, web: list[dict]) -> list[str]:
    out = []
    news = [w["text"] for w in web if w["kind"] == "news"]
    if news:
        out.append(f"Recent news about {p.get('company')} (Google News):\n" + "\n".join(f"- {t}" for t in news))
    results = [w["text"] for w in web if w["kind"] == "web"]
    if results:
        out.append(f"Web search results for {p.get('name')}:\n" + "\n".join(f"- {t}" for t in results))
    for w in web:
        if w["kind"] == "page":
            out.append(f"Their page {w['url']} (markdown excerpt):\n{w['text']}")
        elif w["kind"] == "company":
            out.append(f"Their employer's LinkedIn company page ({w['url']}):\n{w['text']}")
    return out


def _bd_sources(p: dict, linkedin: dict | None, web: list[dict]) -> list[dict]:
    """What Bright Data retrieved about this person, for the card's Sources line."""
    out = []
    if p.get("found_via") == "business_search":
        out.append({"product": "Business Search", "label": "Matched name + company in the people index",
                    "url": None})
    elif p.get("found_via") == "serp":
        out.append({"product": "SERP API", "label": "Google search found their LinkedIn", "url": None})
    if linkedin:
        n = len(linkedin.get("posts") or []) + len(linkedin.get("activity") or [])
        out.append({"product": "Scraper API", "label": "LinkedIn profile" + (f" + {n} posts" if n else ""),
                    "url": p.get("linkedin_url")})
    news = [w for w in web if w["kind"] == "news"]
    if news:
        out.append({"product": "SERP API", "label": f"{len(news)} news articles about {p.get('company')}",
                    "url": news[0]["url"]})
    for w in web:
        if w["kind"] in ("page", "company"):
            out.append({"product": w["product"], "label": w["label"], "url": w["url"]})
    return out


def _via(url: str | None, source: str, linkedin: dict | None, web: list[dict]) -> str | None:
    """Which Bright Data product a hook came from, if any."""
    for w in sorted(web, key=lambda w: w["kind"] != "page"):  # a fetched page beats the search hit linking to it
        if url and url == w["url"]:
            return w["product"]
    if linkedin and ("linkedin.com" in (url or "") or "linkedin" in (source or "").lower()):
        return "Scraper API"
    return None


def _source_labels(p: dict, linkedin: dict | None, web: list[dict]) -> str:
    kinds = {w["kind"] for w in web}
    labels = [label for label, present in (
        ("speaker bio", p.get("bio")), ("people index record", p.get("found_via") == "business_search"),
        ("search snippet", p.get("found_via") == "serp"), ("LinkedIn profile", linkedin), ("news", "news" in kinds),
        ("web results", "web" in kinds), ("their site", "page" in kinds), ("company page", "company" in kinds),
        ("GitHub", p.get("github"))) if present]
    return ", ".join(labels) or "profile"


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


async def _company_page(linkedin: dict | None) -> list[dict]:
    """Their current employer's LinkedIn page (Scraper API), as one more source."""
    link = ((linkedin or {}).get("current_company") or {}).get("link") or ""
    if "linkedin.com/company/" not in link:
        return []
    try:
        c = await brightdata.linkedin_company(link)
    except brightdata.BrightDataError as e:
        log.warning("company page failed: %s", e)
        return []
    if not c:
        return []
    url = link.split("?")[0].rstrip("/")
    return [{"product": "Scraper API", "kind": "company", "label": f"LinkedIn company page: {c.get('name') or ''}".strip(),
             "url": url, "text": brightdata.trim_company(c)}]


async def _scrape_linkedin(p: dict) -> dict | None:
    try:
        return await brightdata.linkedin_profile(p["linkedin_url"])
    except brightdata.BrightDataError as e:
        log.warning("linkedin scrape failed for %s: %s", p["id"], e)
        return None


async def enrich_person(pid: str, *, scrape: bool = True, linkedin: dict | None = None) -> str:
    """scrape=True is a live lookup: LinkedIn, news and the person's own site, fetched in parallel."""
    p = await graph.get_person(pid)
    if not p:
        return "missing"
    web: list[dict] = []
    if scrape and brightdata.enabled():
        if linkedin is None and p.get("linkedin_url"):
            if p.get("is_me"):
                linkedin = await _scrape_linkedin(p)
            else:
                linkedin, web = await asyncio.gather(_scrape_linkedin(p), web_lookups(p))
                web += await _company_page(linkedin)
        elif not p.get("is_me"):
            web = await web_lookups(p)
    if linkedin:
        await apply_linkedin(pid, linkedin)
        p = await graph.get_person(pid) or p
    sources = await _sources(p, linkedin) + _web_source_text(p, web)
    if not sources:
        return p.get("enriched") or "none"
    facts = await llm.extract_person(p.get("name") or pid, "\n\n".join(sources), _source_labels(p, linkedin, web))
    if not facts:
        return p.get("enriched") or "none"

    async with hub.step("falkordb", "FalkorDB", "Write facts to graph",
                        f"{p.get('name')}: {_n(len(facts.roles), 'role')}, {_n(len(facts.topics), 'topic')}, "
                        f"{_n(len(facts.hooks), 'conversation hook')}") as st:
        await graph.clear_derived(pid, "extract")
        for r in facts.roles:
            await graph.set_works_at(pid, r.org, r.title, r.current, "extract")
        for s in facts.schools:
            await graph.link_simple(pid, "STUDIED_AT", "School", s, "extract")
        if facts.city:
            await graph.link_simple(pid, "LIVES_IN", "City", facts.city, "extract")
        await graph.set_topics(pid, facts.topics, "extract")
        for h in facts.hooks:
            await graph.add_fact(pid, "hook", h.text, h.source, h.url, h.date,
                                 via=_via(h.url, h.source, linkedin, web))
        st.result = "saved"
    ev = event_config()
    for s in await graph.person_sessions(pid):
        await graph.add_fact(pid, "talk", f"Speaking at {ev.get('name', 'the event')}: \"{s['title']}\" "
                             f"({_local(s['start'])}, {s['room'] or 'room TBA'})", "event schedule")

    level = "full" if (linkedin or not p.get("linkedin_url")) else "quick"
    if p.get("enriched") == "full":
        level = "full"
    upd = {"id": pid, "enriched": level, "enriched_at": graph.now_ms()}
    if linkedin or web:
        upd["bd_sources"] = json.dumps(_bd_sources(p, linkedin, web))
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
