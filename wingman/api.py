"""HTTP API (docs/API.md) + the web app. Run: uv run python -m wingman.cli serve"""

import asyncio
import hmac
import io
import json
import logging
import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel

from . import cards, fixtures, graph, hub, live, llm, recap, resolver
from .config import ROOT, event_config, me_config, save_me_config, settings

log = logging.getLogger("wingman.api")
mimetypes.add_type("application/manifest+json", ".webmanifest")
COOKIE = "wm"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await graph.ensure_schema()
    if not await graph.get_person(graph.ME):
        await graph.upsert_person({"id": graph.ME, "name": me_config().get("name"), "is_me": True})
    yield


app = FastAPI(title="Wingman", lifespan=lifespan)


# ---------- auth ----------

def _token_ok(value: str | None) -> bool:
    tok = settings().wingman_token
    return bool(tok) and bool(value) and hmac.compare_digest(value, tok)


def public() -> bool:
    return not settings().wingman_token  # hackathon mode: no login at all


def auth(request: Request) -> None:
    if public():
        return
    if not _token_ok(request.cookies.get(COOKIE)):
        raise HTTPException(401, "login required")


class Login(BaseModel):
    token: str


@app.post("/api/login")
async def login(body: Login, response: Response, request: Request):
    if public():
        return {"ok": True}
    if not _token_ok(body.token):
        raise HTTPException(401, "wrong token")
    response.set_cookie(COOKIE, body.token, max_age=365 * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    return {"ok": True}


A = [Depends(auth)]


@app.get("/api/me", dependencies=A)
async def me():
    m, ev = me_config(), event_config()
    p = await graph.get_person(graph.ME) or {}
    return {"name": m.get("name"), "headline": p.get("headline"), "goals": m.get("goals") or [],
            "event": {"id": ev.get("id"), "name": ev.get("name"), "dates": ev.get("dates")} if ev else None,
            "listen_mode": "demo" if fixtures.active() or settings().wingman_demo_script else live.listen_mode(),
            "push_public_key": settings().vapid_public_key or None}


# ---------- capture ----------

def _jpeg(raw: bytes) -> tuple[bytes, Image.Image]:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), img


def _qr_links(img: Image.Image) -> list[str]:
    try:
        import zxingcpp
        return [r.text for r in zxingcpp.read_barcodes(img) if r.text]
    except Exception:
        log.exception("qr decode failed")
        return []


@app.post("/api/capture/photo", dependencies=A)
async def capture_photo(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        jpeg, img = await asyncio.to_thread(_jpeg, raw)
    except Exception:
        raise HTTPException(400, "could not read image (try JPEG)")
    links = await asyncio.to_thread(_qr_links, img)
    li = next((graph.norm_linkedin(x) for x in links if graph.norm_linkedin(x)), None)
    if li:
        return await resolver.resolve({"raw_text": links[0], "name": None, "company": None, "title": None},
                                      linkedin_url=li)
    read = await llm.read_badge(jpeg)
    data = read.model_dump() if read else {"name": None, "company": None, "title": None, "raw_text": None}
    if links and not data.get("raw_text"):
        data["raw_text"] = links[0]
    return await resolver.resolve({k: data.get(k) for k in ("name", "company", "title", "raw_text")}, via="badge")


class Link(BaseModel):
    url: str


@app.post("/api/capture/link", dependencies=A)
async def capture_link(body: Link):
    li = graph.norm_linkedin(body.url)
    if not li:
        raise HTTPException(400, "not a LinkedIn profile URL")
    return await resolver.resolve(None, linkedin_url=li)


class NameIn(BaseModel):
    name: str
    company: str | None = None


@app.post("/api/capture/name", dependencies=A)
async def capture_name(body: NameIn):
    return await resolver.resolve({"name": body.name.strip(), "company": (body.company or "").strip() or None,
                                   "title": None, "raw_text": None}, via="lookup")


class NoteIn(BaseModel):
    text: str
    encounter_id: str | None = None


@app.post("/api/capture/note", dependencies=A)
async def capture_note(body: NoteIn):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty note")
    eid = body.encounter_id
    resolution = None
    if not eid:
        cur = await graph.current_encounter()
        partner = None
        if cur:
            e = await graph.get_encounter_raw(cur["id"])
            if e and e.get("person_id"):
                partner = (await graph.get_person(e["person_id"]) or {}).get("name")
        parsed = await llm.parse_note(text, partner)
        if parsed and parsed.new_person:
            resolution = await resolver.resolve(parsed.new_person.model_dump() | {"raw_text": None}, via="note")
            eid = resolution["encounter"]["id"]
        elif cur:
            eid = cur["id"]
    nid = await graph.add_note(eid, text)
    return {"note_id": nid, "encounter_id": eid, "resolution": resolution}


class PersonRef(BaseModel):
    person_id: str


@app.post("/api/encounters/{eid}/confirm", dependencies=A)
async def encounter_confirm(eid: str, body: PersonRef):
    out = await resolver.confirm(eid, body.person_id)
    if not out:
        raise HTTPException(404, "unknown encounter or person")
    return out


@app.post("/api/encounters/{eid}/reject", dependencies=A)
async def encounter_reject(eid: str, body: PersonRef):
    out = await resolver.reject(eid, body.person_id)
    if not out:
        raise HTTPException(404, "unknown encounter")
    return out


@app.post("/api/encounters/{eid}/end", dependencies=A)
async def encounter_end(eid: str):
    if not await graph.get_encounter_raw(eid):
        raise HTTPException(404, "unknown encounter")
    await graph.update_encounter(eid, {"ended_at": graph.now_ms()})
    return await resolver.encounter_view(eid)


@app.post("/api/encounters/{eid}/reopen", dependencies=A)
async def encounter_reopen(eid: str):
    """Undo for End."""
    if not await graph.get_encounter_raw(eid):
        raise HTTPException(404, "unknown encounter")
    await graph.q("MATCH (e:Encounter {id:$id}) REMOVE e.ended_at", {"id": eid})
    return await resolver.encounter_view(eid)


@app.get("/api/encounters/current", dependencies=A)
async def encounter_current():
    cur = await graph.current_encounter()
    return await resolver.encounter_view(cur["id"]) if cur else None


@app.get("/api/encounters", dependencies=A)
async def encounters(date: str | None = None):
    _, a, b = recap.day_bounds(date)
    return [await resolver.encounter_view(e["id"]) for e in await graph.encounters_between(a, b)]


# ---------- people & cards ----------

@app.get("/api/people/{pid}", dependencies=A)
async def person(pid: str):
    p = await graph.get_person(pid)
    if not p:
        raise HTTPException(404, "unknown person")
    return cards._person_view(p)


@app.get("/api/people/{pid}/card", dependencies=A)
async def person_card(pid: str):
    card = await cards.get_card(pid)
    if not card:
        raise HTTPException(404, "unknown person")
    if card["status"] == "pending" and pid not in cards._jobs:
        cards.start_card_job(pid)
    return card


@app.post("/api/people/{pid}/card/refresh", dependencies=A)
async def person_card_refresh(pid: str):
    if not await graph.get_person(pid):
        raise HTTPException(404, "unknown person")
    cards.start_card_job(pid, force=True)
    card = await cards.get_card(pid)
    card["status"] = "pending"
    return card


@app.get("/api/people", dependencies=A)
async def people(q: str = "", limit: int = 20):
    return [cards._person_view(p) for p in await graph.search_people(q, limit)] if q.strip() else []


@app.get("/api/shortlist", dependencies=A)
async def get_shortlist(limit: int = 30):
    return await cards.shortlist(limit)


# ---------- listen ----------

class ListenText(BaseModel):
    text: str
    is_final: bool = True
    at: str | None = None


@app.post("/api/listen/text", dependencies=A)
async def listen_text(body: ListenText):
    if body.is_final:
        await live.session.on_final(body.text, None)
    return {"ok": True}


@app.websocket("/ws/listen")
async def ws_listen(ws: WebSocket):
    if not public() and not _token_ok(ws.cookies.get(COOKIE)):
        await ws.close(code=4401)
        return
    if live.listen_mode() != "deepgram":
        await ws.close(code=4400)
        return
    await live.deepgram_bridge(ws)


# ---------- events (SSE) ----------

@app.get("/api/events", dependencies=A)
async def events(request: Request):
    qu = hub.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(qu.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            hub.unsubscribe(qu)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- recap ----------

class RecapRun(BaseModel):
    date: str | None = None


@app.post("/api/recap/run", dependencies=A)
async def recap_run(body: RecapRun):
    n = await recap.run(body.date)
    return {"ok": True, "encounters": n}


@app.get("/api/recap", dependencies=A)
async def recap_get(date: str | None = None):
    return await recap.get(date)


class ItemStatus(BaseModel):
    status: str


@app.patch("/api/action-items/{aid}", dependencies=A)
async def action_item(aid: str, body: ItemStatus):
    if body.status not in ("open", "done"):
        raise HTTPException(400, "status must be open or done")
    out = await graph.set_action_status(aid, body.status)
    if not out:
        raise HTTPException(404, "unknown action item")
    return out


class Ask(BaseModel):
    question: str


@app.post("/api/ask", dependencies=A)
async def ask(body: Ask):
    today = datetime.now(ZoneInfo(settings().wingman_timezone)).strftime("%Y-%m-%d (%A)")
    plan = await llm.plan_query(body.question, graph.SCHEMA_FOR_LLM, today)
    if not plan:
        return {"answer": "I couldn't turn that into a query."}
    try:
        header, rows_ = await graph.ro(plan.cypher)
    except Exception as e:
        log.warning("ask query failed: %s | %s", e, plan.cypher)
        return {"answer": "That question didn't work against the graph. Try rephrasing it."}
    result = json.dumps({"columns": [h[1] if isinstance(h, (list, tuple)) else h for h in header],
                         "rows": rows_[:50]}, default=str)[:30000]
    ans = await llm.answer_from_rows(body.question, plan.cypher, result)
    return {"answer": ans.answer if ans else "No answer."}


# ---------- settings & push ----------

@app.get("/api/settings", dependencies=A)
async def get_settings():
    m = me_config()
    return {"goals": m.get("goals") or [], "me": {k: m.get(k) for k in ("name", "linkedin_url", "about")}}


class SettingsIn(BaseModel):
    goals: list[str] | None = None


@app.put("/api/settings", dependencies=A)
async def put_settings(body: SettingsIn):
    m = me_config()
    if body.goals is not None:
        m["goals"] = [g.strip() for g in body.goals if g.strip()]
        save_me_config(m)
    return await get_settings()


class PushSub(BaseModel):
    subscription: dict


@app.post("/api/push/subscribe", dependencies=A)
async def push_subscribe(body: PushSub):
    hub.add_push_subscription(body.subscription)
    return {"ok": True}


@app.get("/healthz")
async def healthz():
    try:
        await graph.q("RETURN 1")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=503)


from . import demo  # noqa: E402  (hackathon demo routes: /api/demo/*)

app.include_router(demo.router)

# Fixture demo only: the recording stage (phone frame + live pipeline trace), see demo/README.md.
if (fixtures.active() or settings().wingman_demo_script) and (ROOT / "demo" / "stage").is_dir():
    app.mount("/stage", StaticFiles(directory=ROOT / "demo" / "stage", html=True), name="stage")

# Web app last, so /api routes win.
_web = ROOT / "web"
_web.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=_web, html=True), name="web")
