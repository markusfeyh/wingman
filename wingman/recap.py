"""After the event: per-conversation summary, action items with quotes, and a follow-up draft."""

import asyncio
import json
import logging
from datetime import date as Date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import cards, graph, llm, resolver
from .config import event_config, settings

log = logging.getLogger("wingman.recap")

MAX_ENCOUNTER_MS = 45 * 60_000
LEAD_IN_MS = 2 * 60_000  # the introduction usually happens just before the encounter is created


def day_bounds(day: str | None) -> tuple[str, int, int]:
    tz = ZoneInfo(settings().wingman_timezone)
    d = Date.fromisoformat(day) if day else datetime.now(tz).date()
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    return d.isoformat(), int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def _windows(encs: list[dict]) -> list[tuple[dict, int, int]]:
    """Transcript window per encounter: from just before it started until the next one (max 45 min)."""
    out, prev_end = [], 0
    for i, e in enumerate(encs):
        nxt = encs[i + 1]["at"] if i + 1 < len(encs) else None
        end = min(x for x in (e.get("ended_at"), nxt, e["at"] + MAX_ENCOUNTER_MS) if x)
        start = max(e["at"] - LEAD_IN_MS, prev_end)
        out.append((e, start, end))
        prev_end = end
    return out


async def run(day: str | None = None, concurrency: int = 4) -> int:
    day, a, b = day_bounds(day)
    encs = await graph.encounters_between(a, b)
    me = await cards.me_context()
    ev_name = event_config().get("name", "the conference")
    sem = asyncio.Semaphore(concurrency)

    async def one(e: dict, start: int, end: int):
        async with sem:
            notes = [n["text"] for n in await graph.encounter_notes(e["id"])]
            chunks = await graph.chunks_between(start, end)
            transcript = "\n".join(f"[S{c['speaker'] if c.get('speaker') is not None else '?'}] {c['text']}"
                                   for c in chunks)[-60000:]
            if not notes and not transcript:
                return
            person = await graph.get_person(e["person_id"]) if e.get("person_id") else None
            out = await llm.recap(me, person, notes, transcript, ev_name, day)
            if not out:
                return
            await graph.update_encounter(e["id"], {
                "summary": out.summary,
                "follow_up": json.dumps({"channel": out.follow_up_channel, "text": out.follow_up}),
                "recapped_at": graph.now_ms()})
            for t in out.topics[:8]:
                await graph.q("MATCH (e:Encounter {id:$e}) MERGE (t:Topic {id:$t}) ON CREATE SET t.name=$n "
                              "MERGE (e)-[:DISCUSSED]->(t)",
                              {"e": e["id"], "t": graph.slug(t).replace("-", " "), "n": t.lower()})
            await graph.replace_action_items(e["id"], e.get("person_id"),
                                             [i.model_dump() for i in out.action_items])

    await asyncio.gather(*(one(e, s, t) for e, s, t in _windows(encs)))
    return len(encs)


async def get(day: str | None = None) -> dict:
    day, a, b = day_bounds(day)
    out = []
    for e in await graph.encounters_between(a, b):
        view = await resolver.encounter_view(e["id"])
        notes = await graph.encounter_notes(e["id"])
        if not view["person"] and not notes and not e.get("summary"):
            continue  # an abandoned lookup (nobody picked, nothing said): not a conversation
        fu = json.loads(e["follow_up"]) if e.get("follow_up") else None
        out.append({"encounter": view, "summary": e.get("summary"), "recapped": bool(e.get("recapped_at")),
                    "notes": [n | {"at": cards.iso(n["at"])} for n in notes],
                    "action_items": await graph.action_items(e["id"]), "follow_up": fu})
    return {"date": day, "encounters": out, "open_items": await graph.action_items(status="open")}
