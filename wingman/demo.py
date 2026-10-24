"""Stage demo: play a scripted conversation through the same path live listening uses.

  POST /api/demo/play   {script?: "maya", speed?: 1.0}  -> starts in the background
  POST /api/demo/reset                                           -> stops it, ends open encounters
  GET  /api/demo/scripts

Each line is fed to live.session.on_final (exactly what browser listen mode does) and also published as an
SSE `transcript` event ({text, speaker, is_final, at, demo: true}) so the phone can show it.
"""

import asyncio
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from . import cards, graph, hub, live
from .config import me_config

log = logging.getLogger("wingman.demo")
router = APIRouter(prefix="/api/demo")

ME, THEM = 0, 1

# (speaker, text, seconds to wait before this line at speed 1). {me} = the user's first name.
SCRIPTS: dict[str, dict] = {
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
    name = body.script or "maya"
    if name not in SCRIPTS:
        return {"ok": False, "detail": f"unknown script; choose one of {sorted(SCRIPTS)}"}
    speed = min(max(body.speed or 1.0, 0.25), 10.0)
    if _task and not _task.done():
        _task.cancel()
    await end_open_encounters()  # start clean: the intro should open a fresh encounter
    live.session.reset()
    _task = hub.spawn(run_script(name, speed))
    secs = sum(w for _, _, w in SCRIPTS[name]["lines"]) / speed
    return {"ok": True, "script": name, "speed": speed, "lines": len(SCRIPTS[name]["lines"]),
            "seconds": round(secs, 1)}


@router.post("/reset")
async def reset():
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
    n = await end_open_encounters()
    live.session.reset()
    return {"ok": True, "ended_encounters": n}
