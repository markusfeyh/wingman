"""Load a conference's speakers and sessions into the graph.

Adapters:
  rainfocus - the public session-catalog API behind many large conferences (GitHub Universe, ...)
  generic   - any speaker/agenda pages: Bright Data fetches them as markdown, Claude extracts the people
"""

import html
import logging
import re
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel

from . import brightdata, graph, llm

log = logging.getLogger("wingman.events")


def _text(h: str | None) -> str | None:
    if not h:
        return None
    t = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(t)).strip() or None


def _utc(s: str | None) -> str | None:
    """'2026/10/27 01:30:00' (UTC) -> '2026-10-27T01:30:00Z'"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


async def import_event(ev: dict) -> dict:
    await graph.ensure_schema()
    await graph.upsert_event(ev)
    adapter = ev.get("adapter")
    if adapter == "rainfocus":
        stats = await _rainfocus(ev)
    elif adapter == "generic":
        stats = await _generic(ev)
    else:
        raise ValueError(f"unknown adapter {adapter!r}")
    log.info("imported %s: %s", ev["id"], stats)
    return stats


# ---------- RainFocus ----------

async def _rainfocus(ev: dict) -> dict:
    rf = ev["rainfocus"]
    headers = {"rfapiprofileid": rf["profile_id"], "rfwidgetid": rf["widget_id"], "referer": rf["referer"],
               "content-type": "application/x-www-form-urlencoded; charset=UTF-8"}
    sessions: list[dict] = []
    async with httpx.AsyncClient(timeout=30, headers=headers) as c:
        start, total = 0, None
        while total is None or start < total:
            r = await c.post(f"{rf['api']}/sessions", content=f"{rf['search_params']}&from={start}")
            r.raise_for_status()
            data = r.json()
            if "sectionList" in data:
                sec = data["sectionList"][0] if data["sectionList"] else {"items": [], "total": 0}
                items, total = sec.get("items", []), sec.get("total", 0)
            else:
                items, total = data.get("items", []), data.get("total", 0)
            if not items:
                break
            sessions += items
            start += len(items)

    people: dict[str, dict] = {}
    for s in sessions:
        t = (s.get("times") or [{}])[0]
        sess = {"id": f"rf:{s['sessionID']}", "title": (s.get("title") or "").strip(),
                "abstract": _text(s.get("abstract")), "start": _utc(t.get("utcStartTime") or s.get("utcStartTime")),
                "end": _utc(t.get("utcEndTime") or s.get("utcEndTime")), "room": t.get("room"), "type": s.get("type")}
        await graph.upsert_session(ev["id"], sess)
        for p in s.get("participants") or []:
            if p.get("testRecord"):
                continue
            pid = graph.person_id_for(p.get("linkedIn"), fallback=f"rf:{p['userRef']}")
            people.setdefault(pid, {"raw": p, "sessions": []})["sessions"].append(sess)

    for pid, info in people.items():
        p = info["raw"]
        github = next((a.get("value") for a in p.get("attributevalues") or []
                       if a.get("attribute") == "GitHub Handle" and a.get("value") not in (None, "", "N/A")), None)
        title, company = p.get("jobTitle"), p.get("companyName")
        await graph.upsert_person({
            "id": pid, "name": p.get("fullName"), "title": title, "company": company,
            "headline": " at ".join(x for x in (title, company) if x) or None,
            "bio": p.get("bio") or p.get("globalBio"), "photo_url": p.get("photoURL"),
            "linkedin_url": p.get("linkedIn"), "twitter": p.get("twitter"), "github": github,
            "is_speaker": True, "source_event": ev["id"],
        })
        await graph.set_works_at(pid, company, title, current=True, source="event")
        for sess in info["sessions"]:
            await graph.link_speaker(pid, sess["id"])
    return {"sessions": len(sessions), "speakers": len(people)}


# ---------- generic (Bright Data + Claude) ----------

class _Speaker(BaseModel):
    name: str
    title: str | None
    company: str | None
    bio: str | None
    linkedin_url: str | None
    photo_url: str | None
    session_title: str | None
    session_start_iso: str | None
    room: str | None


class _Speakers(BaseModel):
    speakers: list[_Speaker]


async def _generic(ev: dict) -> dict:
    n_people, sessions = 0, set()
    for url in ev.get("generic", {}).get("urls", []):
        md = await brightdata.fetch_markdown(url)
        out = await llm._parse(_Speakers, "Extract every speaker listed on this conference page. Use null for "
                               "missing fields; never invent.", f"Page: {url}\n\n{md[:150000]}",
                               live=False, effort="low", max_tokens=32000)
        for sp in (out.speakers if out else []):
            pid = graph.person_id_for(sp.linkedin_url, fallback="ev:" + graph.slug(f"{ev['id']}-{sp.name}"))
            await graph.upsert_person({
                "id": pid, "name": sp.name, "title": sp.title, "company": sp.company,
                "headline": " at ".join(x for x in (sp.title, sp.company) if x) or None, "bio": sp.bio,
                "photo_url": sp.photo_url, "linkedin_url": sp.linkedin_url, "is_speaker": True,
                "source_event": ev["id"]})
            await graph.set_works_at(pid, sp.company, sp.title, current=True, source="event")
            if sp.session_title:
                sid = "s:" + graph.slug(f"{ev['id']}-{sp.session_title}")[:90]
                await graph.upsert_session(ev["id"], {"id": sid, "title": sp.session_title, "abstract": None,
                                                      "start": sp.session_start_iso, "end": None,
                                                      "room": sp.room, "type": None})
                await graph.link_speaker(pid, sid)
                sessions.add(sid)
            n_people += 1
    return {"sessions": len(sessions), "speakers": n_people}
