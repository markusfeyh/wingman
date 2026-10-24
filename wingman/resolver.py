"""Who is this? Badge text / name / LinkedIn URL -> a confirmed person or 2-3 candidates to pick from.

Wrong-person cards are worse than none, so auto-confirm only when one candidate is clearly ahead.
"""

import json
import logging
import re
from difflib import SequenceMatcher

from . import brightdata, cards, graph
from .config import settings

log = logging.getLogger("wingman.resolver")

AUTO_MIN, AUTO_MARGIN = 0.86, 0.15


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def _sim(a: str | None, b: str | None) -> float:
    a, b = _norm(a), _norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def name_score(read: str, cand: str) -> float:
    r, c = _norm(read).split(), _norm(cand).split()
    if not r or not c:
        return 0.0
    full = _sim(read, cand)
    if len(r) == 1:  # first name only ("I'm Priya")
        return 0.6 * max(SequenceMatcher(None, r[0], x).ratio() for x in c)
    first = SequenceMatcher(None, r[0], c[0]).ratio()
    last = SequenceMatcher(None, r[-1], c[-1]).ratio()
    return max(full, 0.4 * first + 0.6 * last)


def company_score(read: str | None, *texts: str | None) -> float | None:
    if not read:
        return None
    rn = _norm(read)
    best = 0.0
    for t in texts:
        tn = _norm(t)
        if not tn:
            continue
        if rn and (rn in tn or tn in rn):
            return 1.0
        best = max(best, _sim(rn, tn))
    return best


def score(read: dict, cand: dict) -> tuple[float, str]:
    ns = name_score(read.get("name") or "", cand.get("name") or "")
    cs = company_score(read.get("company"), cand.get("company"), cand.get("headline"), cand.get("snippet"))
    s = ns if cs is None else 0.65 * ns + 0.35 * cs
    why = []
    if cand.get("is_speaker"):
        s += 0.05
        why.append("on speaker list")
    if cs is not None and cs >= 0.9:
        why.append("company matches")
    elif cs is not None and cs < 0.5:
        why.append("different company")
    if cand.get("met_before"):
        s += 0.03
        why.append("met before")
    if cand.get("from_search"):
        why.append("from Google")
    return min(s, 1.0), " · ".join(why) or "name match"


async def _graph_candidates(read: dict) -> list[dict]:
    text = " ".join(x for x in (read.get("name"), read.get("company")) if x)
    out = []
    for p in await graph.search_people(text, limit=10):
        if p.get("is_me"):
            continue
        met = await graph.q("MATCH (:Encounter)-[:WITH]->(:Person {id:$id}) RETURN count(*)", {"id": p["id"]})
        c = cards._person_view(p) | {"snippet": p.get("bio", "")[:300] if p.get("bio") else None,
                                     "met_before": bool(met and met[0][0])}
        out.append(c)
    return out


_LI_TITLE = re.compile(r"^(?P<name>[^|\-–—]+?)\s*[-–—]\s*(?P<rest>.+?)(?:\s*\|\s*LinkedIn.*)?$")


async def _search_candidates(read: dict) -> list[dict]:
    if not brightdata.enabled() or not read.get("name"):
        return []
    query = f"\"{read['name']}\" {read.get('company') or ''} site:linkedin.com/in".strip()
    try:
        results = await brightdata.google(query)
    except brightdata.BrightDataError as e:
        log.warning("search failed: %s", e)
        return []
    out, seen = [], set()
    for r in results:
        li = graph.norm_linkedin(r.get("link"))
        if not li or li in seen:
            continue
        seen.add(li)
        title = r.get("title") or ""
        m = _LI_TITLE.match(title)
        # no title -> derive from the URL slug ("priya-shah-1a2b3c" -> "Priya Shah"); never assume a match
        slug_name = " ".join(w for w in li.rsplit("/", 1)[1].split("-") if w.isalpha())
        name = (m.group("name") if m else title.split("|")[0]).strip() or slug_name.title() or "Unknown"
        headline = (m.group("rest").strip() if m else None) or None
        existing = await graph.find_person_by_linkedin(li)
        if existing:
            out.append(cards._person_view(existing) | {"snippet": r.get("description"), "from_search": True})
            continue
        out.append({"id": graph.person_id_for(li), "name": name, "headline": headline, "company": None,
                    "title": None, "photo_url": None, "linkedin_url": li, "location": None, "is_speaker": False,
                    "enriched": "none", "snippet": r.get("description"), "from_search": True})
        if len(out) >= 6:
            break
    return out


def _encounter_view(e: dict, person: dict | None) -> dict:
    return {"id": e["id"], "at": cards.iso(e.get("at")), "ended_at": cards.iso(e.get("ended_at")),
            "place": e.get("place"), "person": person, "status": e.get("status") or "resolving",
            "candidates": json.loads(e.get("candidates") or "[]"), "summary": e.get("summary")}


async def encounter_view(eid: str) -> dict | None:
    e = await graph.get_encounter_raw(eid)
    if not e:
        return None
    person = cards._person_view(await graph.get_person(e["person_id"])) if e.get("person_id") else None
    return _encounter_view(e, person)


async def _ensure_person(cand: dict) -> str:
    if await graph.get_person(cand["id"]):
        return cand["id"]
    await graph.upsert_person({"id": cand["id"], "name": cand.get("name"), "headline": cand.get("headline"),
                               "linkedin_url": cand.get("linkedin_url"), "serp_snippet": cand.get("snippet"),
                               "company": cand.get("company"), "enriched": "none"})
    return cand["id"]


async def _confirm(eid: str, pid: str) -> None:
    await graph.set_encounter_person(eid, pid)
    cards.start_card_job(pid, eid)


async def resolve(read: dict | None, *, linkedin_url: str | None = None, encounter_id: str | None = None,
                  new_encounter: bool = True) -> dict:
    """Create (or reuse) an encounter and resolve who it is with. Returns a Resolution."""
    ev_id = settings().wingman_event
    if encounter_id:
        eid = encounter_id
    else:
        if new_encounter and (cur := await graph.current_encounter()):
            await graph.update_encounter(cur["id"], {"ended_at": graph.now_ms()})
        eid = await graph.create_encounter(ev_id, read)
    read = read or {}

    li = graph.norm_linkedin(linkedin_url)
    if li:
        p = await graph.find_person_by_linkedin(li)
        pid = p["id"] if p else await _ensure_person({"id": graph.person_id_for(li), "linkedin_url": li,
                                                      "name": read.get("name") or li.rsplit("/", 1)[1]})
        await _confirm(eid, pid)
        return {"encounter": await encounter_view(eid), "read": read or None}

    if not read.get("name"):
        await graph.update_encounter(eid, {"status": "unknown"})
        return {"encounter": await encounter_view(eid), "read": read or None}

    cands = await _graph_candidates(read)
    scored = sorted(((score(read, c), c) for c in cands), key=lambda x: -x[0][0])
    if not scored or scored[0][0][0] < AUTO_MIN:
        known = {c["id"] for c in cands}
        for c in await _search_candidates(read):
            if c["id"] not in known:
                cands.append(c)
        scored = sorted(((score(read, c), c) for c in cands), key=lambda x: -x[0][0])
    rejected = set(json.loads((await graph.get_encounter_raw(eid) or {}).get("rejected") or "[]"))
    top = [c | {"score": round(s, 3), "why": why} for (s, why), c in scored if c["id"] not in rejected
           and s >= 0.55][:3]
    await graph.update_encounter(eid, {"candidates": json.dumps(top)})
    clear_lead = bool(top) and top[0]["score"] >= AUTO_MIN and (
        len(top) == 1 or top[0]["score"] - top[1]["score"] >= AUTO_MARGIN)
    if clear_lead and top[0].get("from_search") and not top[0].get("is_speaker"):
        # a Google hit alone isn't enough: require the company to show up in its title/snippet
        cs = company_score(read.get("company"), top[0].get("headline"), top[0].get("snippet"))
        clear_lead = cs is not None and cs >= 0.9
    if clear_lead:
        pid = await _ensure_person(top[0])
        await _confirm(eid, pid)
    else:
        await graph.update_encounter(eid, {"status": "needs_pick" if top else "unknown"})
    return {"encounter": await encounter_view(eid), "read": read}


async def confirm(eid: str, pid: str) -> dict | None:
    e = await graph.get_encounter_raw(eid)
    if not e:
        return None
    cand = next((c for c in json.loads(e.get("candidates") or "[]") if c["id"] == pid), None)
    if cand:
        await _ensure_person(cand)
    elif not await graph.get_person(pid):
        return None
    await _confirm(eid, pid)
    return await encounter_view(eid)


async def reject(eid: str, pid: str) -> dict | None:
    e = await graph.get_encounter_raw(eid)
    if not e:
        return None
    rejected = set(json.loads(e.get("rejected") or "[]")) | {pid}
    cands = [c for c in json.loads(e.get("candidates") or "[]") if c["id"] != pid]
    props = {"rejected": json.dumps(sorted(rejected)), "candidates": json.dumps(cands),
             "status": "needs_pick" if cands else "unknown"}
    await graph.update_encounter(eid, props)
    if e.get("person_id") == pid:
        await graph.q("MATCH (e:Encounter {id:$id})-[w:WITH]->() DELETE w", {"id": eid})
        # undo the "met" edge unless another encounter with them exists
        await graph.q("MATCH (me:Person {id:$me})-[k:KNOWS {source:'met'}]->(p:Person {id:$pid}) "
                      "WHERE NOT (:Encounter)-[:WITH]->(p) DELETE k", {"me": graph.ME, "pid": pid})
    return await encounter_view(eid)
