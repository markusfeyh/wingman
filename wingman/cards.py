"""Person cards (common ground, questions, goal fit) and the pre-event "worth meeting" shortlist."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import brightdata, embed, enrich, graph, hub, llm
from .config import event_config, me_config, settings

log = logging.getLogger("wingman.cards")
_jobs: dict[str, asyncio.Task] = {}


def iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ms else None


async def me_context() -> dict:
    me = me_config()
    bits = await graph.person_profile_bits(graph.ME)
    about = me.get("about") or ""
    if bits["orgs"] or bits["topics"]:
        about += (f" Employers: {', '.join(o['org'] for o in bits['orgs'][:8])}."
                  f" Interests: {', '.join(bits['topics'][:12])}.")
    return {"name": me.get("name"), "about": about.strip(), "goals": me.get("goals") or []}


def _person_view(p: dict) -> dict:
    v = graph.public_person(p)
    if v and not v.get("photo_url") and p.get("photo_url_li"):
        v["photo_url"] = p["photo_url_li"]
    return v


async def build_card(pid: str, encounter_id: str | None = None) -> dict | None:
    p = await graph.get_person(pid)
    if not p:
        return None
    ev = event_config()
    facts: list[dict] = []

    def add(text, source, url=None):
        facts.append({"text": text, "source": source, "url": url})

    if p.get("headline"):
        add(f"{p.get('name')}: {p['headline']}", "profile")
    bits = await graph.person_profile_bits(pid)
    past = [o["org"] for o in bits["orgs"] if not o.get("current")]
    if past:
        add(f"Previously at {', '.join(past[:5])}", "profile")
    if bits["schools"]:
        add(f"Studied at {', '.join(bits['schools'][:3])}", "profile")
    if bits["topics"]:
        add(f"Interested in {', '.join(bits['topics'][:10])}", "profile")
    for f in await graph.person_facts(pid):
        add(f["text"], f["source"] or "profile", f.get("url"))
    for c in await graph.common_ground(pid):
        add(c["text"], c["source"])
    prev = await graph.previous_encounters(pid, exclude=encounter_id)
    for e in prev:
        when = datetime.fromtimestamp(e["at"] / 1000, ZoneInfo(settings().wingman_timezone)).strftime("%a %b %-d")
        add(f"You met them on {when}" + (f": {e['summary']}" if e.get("summary") else ""), "your notes")
    for t in await graph.open_items_for(pid):
        add(f"Open promise from your last conversation: {t}", "your notes")
    if p.get("enriched") != "full":
        add("Profile data is thin so far (LinkedIn not fetched yet)", "system")

    sessions = await graph.person_sessions(pid)
    card = {"person": _person_view(p), "status": "full" if p.get("enriched") == "full" else "quick",
            "common_ground": [], "ask_about": [], "goal_fit": [], "heads_up": [], "sessions": sessions,
            "previous_encounters": [{"id": e["id"], "at": iso(e["at"]), "summary": e.get("summary")} for e in prev],
            "generated_at": iso(graph.now_ms())}
    draft = await llm.write_card(await me_context(), p, facts, ev.get("name", "the conference")) if facts else None
    if draft:
        for key in ("common_ground", "ask_about", "goal_fit"):
            for item in getattr(draft, key):
                ids = [i for i in item.fact_ids if 0 <= i < len(facts)]
                if not ids:
                    continue  # unsupported claim: drop it
                src = facts[ids[0]]
                card[key].append({"text": item.text, "source": src["source"], "url": src["url"]})
        card["heads_up"] = draft.heads_up[:2]
    elif facts:
        card["heads_up"] = ["Talking points unavailable right now (Claude call failed); showing basics."]
    await graph.upsert_person({"id": pid, "card_json": json.dumps(card), "card_at": graph.now_ms()})
    return card


async def get_card(pid: str) -> dict | None:
    p = await graph.get_person(pid)
    if not p:
        return None
    if pid in _jobs and not _jobs[pid].done() and not p.get("card_json"):
        return {"person": _person_view(p), "status": "pending", "common_ground": [], "ask_about": [],
                "goal_fit": [], "heads_up": [], "sessions": await graph.person_sessions(pid),
                "previous_encounters": [], "generated_at": None}
    if p.get("card_json"):
        card = json.loads(p["card_json"])
        card["person"] = _person_view(p)
        if pid in _jobs and not _jobs[pid].done():
            card["status"] = "pending"
        return card
    return {"person": _person_view(p), "status": "pending", "common_ground": [], "ask_about": [], "goal_fit": [],
            "heads_up": [], "sessions": await graph.person_sessions(pid), "previous_encounters": [],
            "generated_at": None}


def start_card_job(pid: str, encounter_id: str | None = None, force: bool = False) -> None:
    if pid in _jobs and not _jobs[pid].done():
        return
    _jobs[pid] = hub.spawn(_card_job(pid, encounter_id, force))


async def _card_job(pid: str, encounter_id: str | None, force: bool) -> None:
    p = await graph.get_person(pid) or {}
    name = p.get("name") or "them"
    fresh = p.get("card_at") and graph.now_ms() - p["card_at"] < 6 * 3600_000
    # 1) quick card from what the graph already has (instant for pre-enriched speakers)
    if p.get("enriched") in (None, "none"):
        await enrich.enrich_person(pid, scrape=False)
    if force or not fresh or encounter_id:
        card = await build_card(pid, encounter_id)
        if card:
            hub.publish("card", {"person_id": pid, "encounter_id": encounter_id, "card": card})
    # 2) full card after scraping LinkedIn (30-60 s)
    p = await graph.get_person(pid) or {}
    if p.get("enriched") != "full" and p.get("linkedin_url") and brightdata.enabled():
        level = await enrich.enrich_person(pid, scrape=True)
        hub.publish("enrich", {"person_id": pid, "status": level if level in ("quick", "full") else "failed"})
        if level == "full":
            card = await build_card(pid, encounter_id)
            if card:
                hub.publish("card", {"person_id": pid, "encounter_id": encounter_id, "card": card},
                            push_text=f"Card ready: {name}")


# ---------- shortlist ----------

async def rank_shortlist(limit: int = 30) -> int:
    me = await me_context()
    people = await graph.rows(
        "MATCH (p:Person {is_speaker:true}) OPTIONAL MATCH (p)-[:INTERESTED_IN]->(t:Topic) "
        "OPTIONAL MATCH (p)-[:HAS_FACT]->(f:Fact {kind:'hook'}) "
        "RETURN {id:p.id, name:p.name, title:p.title, company:p.company, "
        "topics:collect(DISTINCT t.name)[0..8], hooks:collect(DISTINCT f.text)[0..2]}")
    if not people:
        return 0
    # cheap pre-score: topic similarity to goals + direct common ground
    goal_vecs = await embed.embed(me["goals"]) if me["goals"] else []
    pre: dict[str, float] = {}
    for p in people:
        score = 0.0
        if goal_vecs and p["topics"]:
            tv = await embed.embed(p["topics"])
            score = max(sum(a * b for a, b in zip(g, t)) for g in goal_vecs for t in tv)
        cg = await graph.common_ground(p["id"])
        p["common"] = [c["text"] for c in cg[:3]]
        pre[p["id"]] = score + 0.05 * len(cg)
    top = sorted(people, key=lambda p: pre[p["id"]], reverse=True)[:max(limit * 2, 40)]
    ranked = await llm.rank_people(me, top, limit)
    await graph.q("MATCH (p:Person) REMOVE p.shortlist_score, p.shortlist_why")
    items = ranked.items if ranked else [llm.ShortlistItem(person_id=p["id"], score=min(1.0, pre[p["id"]]),
                                                           why=", ".join(p["common"][:1]) or "Speaker")
                                         for p in top[:limit]]
    for it in items[:limit]:
        await graph.q("MATCH (p:Person {id:$id}) SET p.shortlist_score=$s, p.shortlist_why=$w",
                      {"id": it.person_id, "s": float(it.score), "w": it.why})
    return len(items[:limit])


async def shortlist(limit: int = 30) -> list[dict]:
    out = []
    for p in await graph.rows("MATCH (p:Person) WHERE p.shortlist_score IS NOT NULL RETURN properties(p) "
                              "ORDER BY p.shortlist_score DESC LIMIT $l", {"l": limit}):
        out.append({"person": _person_view(p), "score": p["shortlist_score"], "why": p.get("shortlist_why") or "",
                    "sessions": await graph.person_sessions(p["id"])})
    return out
