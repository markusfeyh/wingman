"""FalkorDB access: schema, writes, and the queries cards are built from.

Model (see PLAN.md §5):
  (:Person)-[:WORKS_AT {title, current, source}]->(:Org)
  (:Person)-[:STUDIED_AT]->(:School)   (:Person)-[:LIVES_IN]->(:City)
  (:Person)-[:INTERESTED_IN {source}]->(:Topic {embedding})
  (:Person)-[:HAS_FACT]->(:Fact {kind, text, source, url, date})
  (:Person)-[:SPEAKS_AT]->(:Session)-[:PART_OF]->(:Event)
  (:Person)-[:KNOWS]->(:Person)
  (:Encounter)-[:WITH]->(:Person)   (:Encounter)-[:AT]->(:Event)
  (:Encounter)-[:HAS]->(:Note|:Chunk)   (:Encounter)-[:DISCUSSED]->(:Topic)
  (:ActionItem)-[:FROM]->(:Encounter)   (:ActionItem)-[:FOR]->(:Person)
"""

import re
import time
import unicodedata
import uuid
from typing import Any

from falkordb.asyncio import FalkorDB

from .config import settings

ME = "me"
TOPIC_DIM = 384  # fastembed BAAI/bge-small-en-v1.5

_db: FalkorDB | None = None


def g():
    global _db
    if _db is None:
        s = settings()
        _db = FalkorDB(host=s.falkordb_host, port=s.falkordb_port)
    return _db.select_graph(settings().graph_name)


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def norm_linkedin(url: str | None) -> str | None:
    """https://www.linkedin.com/in/Maya-O/?trk=x -> https://www.linkedin.com/in/maya-o"""
    if not url:
        return None
    m = re.search(r"linkedin\.com/(?:in|pub)/([^/?#\s]+)", url, re.I)
    return f"https://www.linkedin.com/in/{m.group(1).lower()}" if m else None


def person_id_for(linkedin_url: str | None = None, fallback: str | None = None) -> str:
    li = norm_linkedin(linkedin_url)
    if li:
        return "li:" + li.rsplit("/", 1)[1]
    return fallback or new_id("p")


async def q(query: str, params: dict | None = None) -> list[list[Any]]:
    return (await g().query(query, params or {})).result_set


async def rows(query: str, params: dict | None = None) -> list[dict]:
    """Run a query whose RETURN is a single map per row."""
    return [dict(r[0]) for r in await q(query, params) if r and r[0] is not None]


async def ro(query: str, params: dict | None = None, timeout_ms: int = 5000):
    res = await g().ro_query(query, params or {}, timeout=timeout_ms)
    return res.header, res.result_set


async def ensure_schema() -> None:
    stmts = [f"CREATE INDEX FOR (n:{label}) ON (n.id)" for label in
             ("Person", "Org", "School", "City", "Topic", "Fact", "Event", "Session",
              "Encounter", "Note", "Chunk", "ActionItem")]
    stmts += [
        "CREATE INDEX FOR (p:Person) ON (p.linkedin_url)",
        "CREATE INDEX FOR (e:Encounter) ON (e.at)",
        "CREATE INDEX FOR (c:Chunk) ON (c.at)",
        "CREATE FULLTEXT INDEX FOR (p:Person) ON (p.name, p.company)",
        f"CREATE VECTOR INDEX FOR (t:Topic) ON (t.embedding) OPTIONS {{dimension:{TOPIC_DIM}, similarityFunction:'cosine'}}",
    ]
    for s in stmts:
        try:
            await q(s)
        except Exception as e:  # already exists
            if "already" not in str(e).lower():
                raise


# ---------- people ----------

PERSON_FIELDS = ("id", "name", "headline", "company", "title", "photo_url", "linkedin_url",
                 "location", "is_speaker", "enriched")


def public_person(p: dict | None) -> dict | None:
    if not p:
        return None
    out = {k: p.get(k) for k in PERSON_FIELDS}
    out["is_speaker"] = bool(out["is_speaker"])
    out["enriched"] = out["enriched"] or "none"
    return out


async def get_person(pid: str) -> dict | None:
    r = await rows("MATCH (p:Person {id:$id}) RETURN properties(p)", {"id": pid})
    return r[0] if r else None


async def find_person_by_linkedin(url: str) -> dict | None:
    li = norm_linkedin(url)
    if not li:
        return None
    r = await rows("MATCH (p:Person {linkedin_url:$u}) RETURN properties(p) LIMIT 1", {"u": li})
    return r[0] if r else None


async def upsert_person(props: dict) -> str:
    """MERGE on id. None values are dropped so they never erase existing data."""
    props = {k: v for k, v in props.items() if v is not None}
    if props.get("linkedin_url"):
        props["linkedin_url"] = norm_linkedin(props["linkedin_url"])
    if props.get("name"):
        props["name_lc"] = props["name"].lower()
    await q("MERGE (p:Person {id:$id}) ON CREATE SET p.created_at=timestamp() SET p += $props",
            {"id": props["id"], "props": props})
    return props["id"]


async def search_people(text: str, limit: int = 20) -> list[dict]:
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", text) if len(t) >= 2]
    if not tokens:
        return []
    ft = "|".join(f"{t}|%{t}%" if len(t) >= 4 else t for t in tokens)
    try:
        return await rows(
            "CALL db.idx.fulltext.queryNodes('Person', $q) YIELD node, score "
            "WHERE node.id <> 'me' RETURN properties(node) ORDER BY score DESC LIMIT $limit",
            {"q": ft, "limit": limit})
    except Exception:
        return []


async def set_works_at(pid: str, org: str, title: str | None, current: bool, source: str) -> None:
    if not org:
        return
    await q("MATCH (p:Person {id:$pid}) MERGE (o:Org {id:$oid}) ON CREATE SET o.name=$org "
            "MERGE (p)-[r:WORKS_AT]->(o) SET r.title=coalesce($title, r.title), "
            "r.current=$current OR coalesce(r.current,false), r.source=$source",
            {"pid": pid, "oid": slug(org), "org": org, "title": title, "current": current, "source": source})


async def link_simple(pid: str, rel: str, label: str, name: str, source: str) -> None:
    assert rel in ("STUDIED_AT", "LIVES_IN") and label in ("School", "City")
    if not name:
        return
    await q(f"MATCH (p:Person {{id:$pid}}) MERGE (x:{label} {{id:$xid}}) ON CREATE SET x.name=$name "
            f"MERGE (p)-[r:{rel}]->(x) SET r.source=$source",
            {"pid": pid, "xid": slug(name), "name": name, "source": source})


async def set_topics(pid: str, topics: list[str], source: str) -> list[str]:
    """Link topics; returns ids of topics that still need an embedding."""
    tids = []
    for t in topics:
        tid = slug(t).replace("-", " ")
        if not tid:
            continue
        tids.append(tid)
        await q("MATCH (p:Person {id:$pid}) MERGE (t:Topic {id:$tid}) ON CREATE SET t.name=$name "
                "MERGE (p)-[r:INTERESTED_IN]->(t) SET r.source=$source",
                {"pid": pid, "tid": tid, "name": t.strip().lower(), "source": source})
    r = await q("MATCH (t:Topic) WHERE t.id IN $ids AND t.embedding IS NULL RETURN t.id", {"ids": tids})
    return [x[0] for x in r]


async def set_topic_embeddings(vectors: dict[str, list[float]]) -> None:
    for tid, vec in vectors.items():
        await q("MATCH (t:Topic {id:$id}) SET t.embedding=vecf32($v)", {"id": tid, "v": vec})


async def topics_missing_embeddings(limit: int = 5000) -> list[str]:
    return [r[0] for r in await q("MATCH (t:Topic) WHERE t.embedding IS NULL RETURN t.id LIMIT $l", {"l": limit})]


async def add_fact(pid: str, kind: str, text: str, source: str, url: str | None = None,
                   date: str | None = None) -> str:
    fid = "f_" + slug(f"{pid}-{kind}-{text}")[:80]
    await q("MATCH (p:Person {id:$pid}) MERGE (f:Fact {id:$fid}) "
            "SET f.kind=$kind, f.text=$text, f.source=$source, f.url=$url, f.date=$date "
            "MERGE (p)-[:HAS_FACT]->(f)",
            {"pid": pid, "fid": fid, "kind": kind, "text": text, "source": source, "url": url, "date": date})
    return fid


async def clear_derived(pid: str, source: str) -> None:
    """Drop facts/edges from one source before re-extracting, so re-runs don't pile up."""
    await q("MATCH (p:Person {id:$pid})-[r]->() WHERE r.source=$s AND type(r) <> 'SPEAKS_AT' DELETE r",
            {"pid": pid, "s": source})
    await q("MATCH (p:Person {id:$pid})-[:HAS_FACT]->(f:Fact {source:$s}) DETACH DELETE f", {"pid": pid, "s": source})


# ---------- events ----------

async def upsert_event(ev: dict) -> None:
    await q("MERGE (e:Event {id:$id}) SET e.name=$name, e.dates=$dates",
            {"id": ev["id"], "name": ev.get("name"), "dates": ev.get("dates")})


async def upsert_session(event_id: str, s: dict) -> None:
    await q("MATCH (e:Event {id:$eid}) MERGE (s:Session {id:$id}) "
            "SET s.title=$title, s.abstract=$abstract, s.start=$start, s.end=$end, s.room=$room, s.type=$type "
            "MERGE (s)-[:PART_OF]->(e)", {"eid": event_id, **s})


async def link_speaker(pid: str, session_id: str) -> None:
    await q("MATCH (p:Person {id:$pid}), (s:Session {id:$sid}) MERGE (p)-[:SPEAKS_AT]->(s)",
            {"pid": pid, "sid": session_id})


async def person_sessions(pid: str) -> list[dict]:
    return await rows("MATCH (:Person {id:$pid})-[:SPEAKS_AT]->(s:Session) "
                      "RETURN {id:s.id, title:s.title, start:s.start, end:s.end, room:s.room} ORDER BY s.start",
                      {"pid": pid})


# ---------- common ground (each row becomes a numbered fact for the card writer) ----------

async def common_ground(pid: str) -> list[dict]:
    p = {"me": ME, "pid": pid}
    out: list[dict] = []
    for r in await q(
            "MATCH (me:Person {id:$me})-[r1:WORKS_AT|STUDIED_AT|LIVES_IN|INTERESTED_IN]->(x)"
            "<-[r2:WORKS_AT|STUDIED_AT|LIVES_IN|INTERESTED_IN]-(them:Person {id:$pid}) "
            "RETURN labels(x)[0], x.name, type(r1), r1.current, type(r2), r2.current, r2.source", p):
        kind, name, t1, cur1, t2, cur2, src = r
        verb = {"Org": "worked at", "School": "studied at", "City": "are based in", "Topic": "care about"}[kind]
        tense = ""
        if kind == "Org":
            tense = f" (you: {'now' if cur1 else 'past'}; them: {'now' if cur2 else 'past'})"
        out.append({"kind": "shared_" + kind.lower(), "text": f"You both {verb} {name}{tense}", "source": "graph"})
    for r in await q(
            "MATCH (them:Person {id:$pid})-[:INTERESTED_IN]->(t:Topic) WHERE t.embedding IS NOT NULL "
            "CALL db.idx.vector.queryNodes('Topic','embedding',4,t.embedding) YIELD node, score "
            "MATCH (me:Person {id:$me})-[:INTERESTED_IN]->(node) WHERE node <> t AND score < 0.23 "
            "AND NOT (them)-[:INTERESTED_IN]->(node) "
            "RETURN t.name, node.name, score ORDER BY score LIMIT 4", p):
        out.append({"kind": "similar_topic", "text": f"Their interest in {r[0]} is close to yours in {r[1]}",
                    "source": "graph"})
    for r in await q(
            "MATCH (me:Person {id:$me})-[:KNOWS]-(f:Person)-[:WORKS_AT]->(o:Org)<-[:WORKS_AT]-(them:Person {id:$pid}) "
            "WHERE f <> them RETURN DISTINCT f.name, o.name LIMIT 3", p):
        out.append({"kind": "mutual", "text": f"Your contact {r[0]} also worked at {r[1]}", "source": "graph"})
    for r in await q(
            "MATCH (them:Person {id:$pid})-[:INTERESTED_IN]->(t:Topic)<-[:INTERESTED_IN]-(other:Person)"
            "<-[:WITH]-(:Encounter) WHERE other <> them AND other.id <> $me "
            "RETURN other.name, collect(DISTINCT t.name)[0..3] AS ts, count(t) AS n ORDER BY n DESC LIMIT 2", p):
        out.append({"kind": "intro", "text": f"You met {r[0]}, who shares their interest in {', '.join(r[1])}",
                    "source": "your encounters"})
    return out


async def person_facts(pid: str, limit: int = 25) -> list[dict]:
    return await rows("MATCH (:Person {id:$pid})-[:HAS_FACT]->(f:Fact) "
                      "RETURN {kind:f.kind, text:f.text, source:f.source, url:f.url, date:f.date} "
                      "ORDER BY f.date DESC LIMIT $l", {"pid": pid, "l": limit})


async def person_profile_bits(pid: str) -> dict:
    orgs = await rows("MATCH (:Person {id:$pid})-[r:WORKS_AT]->(o:Org) "
                      "RETURN {org:o.name, title:r.title, current:r.current}", {"pid": pid})
    schools = [r[0] for r in await q("MATCH (:Person {id:$pid})-[:STUDIED_AT]->(s) RETURN s.name", {"pid": pid})]
    topics = [r[0] for r in await q("MATCH (:Person {id:$pid})-[:INTERESTED_IN]->(t) RETURN t.name", {"pid": pid})]
    return {"orgs": orgs, "schools": schools, "topics": topics}


# ---------- encounters ----------

ENC_FIELDS = ("id", "at", "ended_at", "place", "status", "summary")


async def create_encounter(event_id: str | None, read: dict | None, place: str | None = None) -> str:
    eid = new_id("enc")
    await q("CREATE (e:Encounter {id:$id, at:$at, status:'resolving', place:$place, "
            "read_name:$rn, read_company:$rc, read_title:$rt, read_raw:$raw, candidates:'[]', rejected:'[]'})",
            {"id": eid, "at": now_ms(), "place": place, "rn": (read or {}).get("name"),
             "rc": (read or {}).get("company"), "rt": (read or {}).get("title"), "raw": (read or {}).get("raw_text")})
    if event_id:
        await q("MATCH (e:Encounter {id:$id}), (ev:Event {id:$ev}) MERGE (e)-[:AT]->(ev)", {"id": eid, "ev": event_id})
    return eid


async def get_encounter_raw(eid: str) -> dict | None:
    r = await rows("MATCH (e:Encounter {id:$id}) OPTIONAL MATCH (e)-[:WITH]->(p:Person) "
                   "RETURN properties(e) + {person_id: p.id}", {"id": eid})
    return r[0] if r else None


async def update_encounter(eid: str, props: dict) -> None:
    await q("MATCH (e:Encounter {id:$id}) SET e += $p", {"id": eid, "p": props})


async def set_encounter_person(eid: str, pid: str) -> None:
    await q("MATCH (e:Encounter {id:$id}) OPTIONAL MATCH (e)-[w:WITH]->() DELETE w", {"id": eid})
    await q("MATCH (e:Encounter {id:$id}), (p:Person {id:$pid}) MERGE (e)-[:WITH]->(p) "
            "SET e.status='confirmed'", {"id": eid, "pid": pid})
    # meeting someone is a KNOWS edge: it powers "you met X, who shares..." and mutual-contact paths
    await q("MATCH (me:Person {id:$me}), (p:Person {id:$pid}) WHERE p.id <> $me MERGE (me)-[k:KNOWS]->(p) "
            "SET k.source='met'", {"me": ME, "pid": pid})


async def current_encounter(max_age_min: int = 20) -> dict | None:
    r = await rows("MATCH (e:Encounter) WHERE e.ended_at IS NULL AND e.at > $since "
                   "RETURN properties(e) ORDER BY e.at DESC LIMIT 1",
                   {"since": now_ms() - max_age_min * 60_000})
    return r[0] if r else None


async def encounters_between(start_ms: int, end_ms: int) -> list[dict]:
    return await rows("MATCH (e:Encounter) WHERE e.at >= $a AND e.at < $b "
                      "OPTIONAL MATCH (e)-[:WITH]->(p:Person) RETURN properties(e) + {person_id: p.id} ORDER BY e.at",
                      {"a": start_ms, "b": end_ms})


async def previous_encounters(pid: str, exclude: str | None = None) -> list[dict]:
    return await rows("MATCH (e:Encounter)-[:WITH]->(:Person {id:$pid}) WHERE e.id <> $ex "
                      "RETURN {id:e.id, at:e.at, summary:e.summary} ORDER BY e.at DESC LIMIT 5",
                      {"pid": pid, "ex": exclude or ""})


async def add_note(eid: str | None, text: str) -> str:
    nid = new_id("note")
    await q("CREATE (n:Note {id:$id, text:$text, at:$at})", {"id": nid, "text": text, "at": now_ms()})
    if eid:
        await q("MATCH (e:Encounter {id:$eid}), (n:Note {id:$nid}) MERGE (e)-[:HAS]->(n)", {"eid": eid, "nid": nid})
    return nid


async def add_chunk(text: str, speaker: int | None, at: int, eid: str | None) -> None:
    cid = new_id("chk")
    await q("CREATE (c:Chunk {id:$id, text:$text, speaker:$sp, at:$at})",
            {"id": cid, "text": text, "sp": speaker, "at": at})
    if eid:
        await q("MATCH (e:Encounter {id:$eid}), (c:Chunk {id:$cid}) MERGE (e)-[:HAS]->(c)", {"eid": eid, "cid": cid})


async def encounter_notes(eid: str) -> list[dict]:
    return await rows("MATCH (:Encounter {id:$id})-[:HAS]->(n:Note) RETURN {id:n.id, text:n.text, at:n.at} "
                      "ORDER BY n.at", {"id": eid})


async def chunks_between(a: int, b: int) -> list[dict]:
    return await rows("MATCH (c:Chunk) WHERE c.at >= $a AND c.at < $b "
                      "RETURN {text:c.text, speaker:c.speaker, at:c.at} ORDER BY c.at", {"a": a, "b": b})


# ---------- action items ----------

async def replace_action_items(eid: str, pid: str | None, items: list[dict]) -> None:
    await q("MATCH (a:ActionItem)-[:FROM]->(:Encounter {id:$eid}) WHERE a.status = 'open' DETACH DELETE a",
            {"eid": eid})
    for it in items:
        aid = new_id("act")
        await q("MATCH (e:Encounter {id:$eid}) CREATE (a:ActionItem {id:$id, text:$text, owner:$owner, due:$due, "
                "quote:$quote, status:'open', created_at:$now})-[:FROM]->(e)",
                {"eid": eid, "id": aid, "now": now_ms(), **{k: it.get(k) for k in ("text", "owner", "due", "quote")}})
        if pid:
            await q("MATCH (a:ActionItem {id:$id}), (p:Person {id:$pid}) MERGE (a)-[:FOR]->(p)", {"id": aid, "pid": pid})


async def action_items(eid: str | None = None, status: str | None = None) -> list[dict]:
    return await rows(
        "MATCH (a:ActionItem)-[:FROM]->(e:Encounter) WHERE ($eid IS NULL OR e.id = $eid) "
        "AND ($st IS NULL OR a.status = $st) OPTIONAL MATCH (a)-[:FOR]->(p:Person) "
        "RETURN {id:a.id, text:a.text, owner:a.owner, due:a.due, quote:a.quote, status:a.status, "
        "person_id:p.id, encounter_id:e.id} ORDER BY e.at", {"eid": eid, "st": status})


async def set_action_status(aid: str, status: str) -> dict | None:
    await q("MATCH (a:ActionItem {id:$id}) SET a.status=$s", {"id": aid, "s": status})
    r = await rows("MATCH (a:ActionItem {id:$id})-[:FROM]->(e:Encounter) OPTIONAL MATCH (a)-[:FOR]->(p:Person) "
                   "RETURN {id:a.id, text:a.text, owner:a.owner, due:a.due, quote:a.quote, status:a.status, "
                   "person_id:p.id, encounter_id:e.id}", {"id": aid})
    return r[0] if r else None


async def open_items_for(pid: str) -> list[str]:
    return [r[0] for r in await q("MATCH (a:ActionItem {status:'open'})-[:FOR]->(:Person {id:$pid}) RETURN a.text",
                                  {"pid": pid})]


SCHEMA_FOR_LLM = __doc__
