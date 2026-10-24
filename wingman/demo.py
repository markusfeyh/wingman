"""Stage demo: play a scripted conversation through the same path live listening uses.

  POST /api/demo/play   {script?: "maya", speed?: 1.0}  -> starts in the background
  POST /api/demo/reset                                           -> stops it, ends open encounters
  GET  /api/demo/scripts

Each line is fed to live.session.on_final (exactly what browser listen mode does) and also published as an
SSE `transcript` event ({text, speaker, is_final, at, demo: true}) so the phone can show it.
"""

import asyncio
import logging
import shutil
from pathlib import Path

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

from . import cards, enrich, fixtures, graph, hub, live
from .config import ROOT, event_config, me_config, settings

log = logging.getLogger("wingman.demo")
router = APIRouter(prefix="/api/demo")

ME, THEM = 0, 1

# (speaker, text, seconds to wait before this line at speed 1). {me} = the user's first name.
SCRIPTS: dict[str, dict] = {
    "maya": {
        "title": "Synthetic demo: talk to Maya Okafor (Tessellate AI) after scanning her badge",
        "description": "For the fixture demo (demo/README.md). Keeps the badge encounter open; a goal nudge, "
                       "promises for Recap.",
        "person_id": "li:maya-okafor-tessellate",
        "keep_encounter": True,
        "lines": [
            (ME, "Mind if my notetaker listens while we chat?", 0.8),
            (THEM, "Go for it. I'm Maya, by the way. I run agent reliability at Tessellate.", 2.4),
            (ME, "{me}. I saw you open-sourced Tidepool last week. Congrats!", 2.6),
            (THEM, "Thanks! Our agents open about four thousand PRs a week now, and evaluation is the bottleneck.",
             3.0),
            (ME, "Funny, I'm working on exactly that. Our eval harness replays agent PRs, and we're looking for "
                 "design partners.", 3.4),
            (THEM, "We'd be interested. Can you send me a short write-up?", 3.6),
            (ME, "Absolutely. I'll send you the deck tomorrow morning.", 2.4),
            (THEM, "Perfect. And if you know anyone great at GPU scheduling, we're hiring.", 2.8),
            (ME, "I do. I'll make an intro this week.", 2.6),
        ],
    },
}

_task: asyncio.Task | None = None


class PlayIn(BaseModel):
    script: str | None = None
    speed: float | None = None


def _first_name() -> str:
    return ((me_config().get("name") or "Sam").split() or ["Sam"])[0]


async def run_script(name: str, speed: float = 1.0) -> int:
    script = SCRIPTS[name]
    me = _first_name()
    for speaker, text, wait in script["lines"]:
        await asyncio.sleep(wait / speed)
        text = text.format(me=me)
        at = graph.now_ms()
        hub.publish("transcript", {"text": text, "speaker": speaker, "is_final": True, "at": cards.iso(at),
                                   "demo": True})
        await live.session.on_final(text, speaker, at)
    hub.publish("demo", {"state": "done", "script": name})
    return len(script["lines"])


async def end_open_encounters() -> int:
    r = await graph.q("MATCH (e:Encounter) WHERE e.ended_at IS NULL SET e.ended_at=$now RETURN count(e)",
                      {"now": graph.now_ms()})
    return r[0][0] if r else 0


@router.get("/scripts")
async def scripts():
    out = []
    for sid, s in SCRIPTS.items():
        ready = True
        if s["person_id"]:
            ready = bool(await graph.get_person(s["person_id"]))
        out.append({"id": sid, "title": s["title"], "description": s["description"],
                    "seconds": round(sum(w for _, _, w in s["lines"]), 1), "ready": ready})
    return out


@router.post("/play")
async def play(body: PlayIn | None = None):
    global _task
    body = body or PlayIn()
    name = body.script or settings().wingman_demo_script or "maya"
    if name not in SCRIPTS:
        return {"ok": False, "detail": f"unknown script; choose one of {sorted(SCRIPTS)}"}
    speed = min(max(body.speed or 1.0, 0.25), 10.0)
    if _task and not _task.done():
        _task.cancel()
    if not SCRIPTS[name].get("keep_encounter"):
        await end_open_encounters()  # start clean: the intro should open a fresh encounter
    live.session.reset()
    _task = hub.spawn(run_script(name, speed))
    secs = sum(w for _, _, w in SCRIPTS[name]["lines"]) / speed
    return {"ok": True, "script": name, "speed": speed, "lines": len(SCRIPTS[name]["lines"]),
            "seconds": round(secs, 1)}


def demo_mode() -> bool:
    return fixtures.active() or bool(settings().wingman_demo_script)


@router.get("/info")
async def info():
    """What the recording stage should say about this run: replayed fixtures or live API calls."""
    from . import llm
    return {"fixtures": fixtures.active(), "llm": llm.label(), "script": settings().wingman_demo_script or None}


@router.post("/seed")
async def seed_route():
    """Demo mode only: reload the demo world (its pipeline steps stream to the stage as trace events)."""
    if not demo_mode():
        return {"ok": False, "detail": "seed only runs in demo mode"}
    global _task
    if _task and not _task.done():
        _task.cancel()
    live.session.reset()
    out = await seed()
    hub.publish("demo", {"state": "seeded"})
    return {"ok": True, **out}


@router.post("/reset")
async def reset():
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
    n = await end_open_encounters()
    live.session.reset()
    return {"ok": True, "ended_encounters": n}


# ---------- synthetic world for the fixture demo (demo/README.md) ----------

async def seed(world_file: str | Path = ROOT / "demo" / "world.yaml") -> dict:
    """Wipe the demo graph and load a synthetic event: sessions, speakers (enriched through the normal batch
    path, with Bright Data and Claude replayed from fixtures), your own profile, and the shortlist."""
    s = settings()
    if not demo_mode() or s.graph_name == "wingman":
        raise RuntimeError("seed only runs in demo mode on a separate graph "
                           "(WINGMAN_FIXTURES or WINGMAN_DEMO_SCRIPT, plus GRAPH_NAME)")
    world = yaml.safe_load(Path(world_file).read_text())
    try:
        await graph.g().delete()
    except Exception:
        pass
    shutil.rmtree(s.data_dir / "cache", ignore_errors=True)
    await graph.ensure_schema()
    ev = event_config()
    await graph.upsert_event(ev)
    for sess in world["sessions"]:
        await graph.upsert_session(ev["id"], {"abstract": None, "end": None, "type": "Session"} | sess)
    if not fixtures.active():
        # live mode: the synthetic speakers have made-up LinkedIn URLs, so only load your own profile
        return {"me": await enrich.enrich_me(), "speakers": {}, "shortlist": 0}
    for sp in world["speakers"]:
        await graph.upsert_person({"id": sp["id"], "name": sp["name"], "title": sp.get("title"),
                                   "company": sp.get("company"), "linkedin_url": sp.get("linkedin_url"),
                                   "bio": sp.get("bio"), "is_speaker": True, "enriched": "none"})
        for sid in sp.get("sessions") or []:
            await graph.link_speaker(sp["id"], sid)
    me = await enrich.enrich_me()
    speakers = await enrich.enrich_speakers()
    ranked = await cards.rank_shortlist(limit=len(world["speakers"]))
    return {"me": me, "speakers": speakers, "shortlist": ranked}
