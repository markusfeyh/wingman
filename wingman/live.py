"""Listen mode: phone mic -> Deepgram streaming (or the browser's own speech recognition) -> transcript
chunks in the graph -> detection of self-introductions (new person) and nudges.

Two detectors run off the rolling transcript, both in the background so transcript handling never waits:
  - fast path: a regex catches "I'm Maya Okafor ... at Tessellate AI" and resolves it at once if it matches
    someone in the graph (works even without an Anthropic key)
  - Claude: every ~15 s (or right after an intro-sounding line) for introductions, topics and nudges

No audio is stored. Only final transcript text is kept, attached to the current encounter.
"""

import asyncio
import json
import logging
import re
import time
import urllib.parse

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

from . import cards, graph, hub, llm, resolver
from .config import settings

log = logging.getLogger("wingman.live")

DETECT_EVERY_S = 15
DETECT_MIN_NEW_CHARS = 120
NUDGE_GAP_PER_ENCOUNTER_S = 120
NUDGE_GAP_GLOBAL_S = 30
INTRO_DEDUPE_S = 600
WINDOW_MS = 150_000
MAX_LINES = 60
MAX_LINE_CHARS = 600
FAST_PATH_MIN_SCORE = 0.6

INTRO_HINT = re.compile(r"\b(i'm|i am|my name is|my name's|this is|call me)\b", re.I)
# trigger phrase is case-insensitive; the name itself must be capitalized (ASR capitalizes proper nouns)
INTRO_RE = re.compile(r"\b(?i:i'm|i am|my name is|my name's|this is|call me)\s+"
                      r"(?P<name>[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,2})")
COMPANY_RE = re.compile(r"\b(?:at|from|with|for)\s+(?P<co>[A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,2})")
NOT_NAMES = {"here", "sure", "glad", "happy", "just", "really", "so", "not", "also", "going", "excited",
             "working", "looking", "on", "in", "the", "a", "an", "good", "great", "fine", "okay", "ok", "sorry",
             "back", "done", "curious", "actually", "still", "now", "definitely", "totally", "super", "pretty",
             "interested", "new", "based", "currently", "part", "one", "trying", "thinking", "with", "from",
             "at", "github", "microsoft", "google", "it", "that", "what", "how", "where", "when", "why"}
SKIP_COMPANY = {"GitHub Universe", "Universe", "The", "This", "That"}


def fast_intro(text: str) -> dict | None:
    """'Hi, I'm Maya Okafor, I'm a developer advocate at Tessellate AI.' -> {name: Maya Okafor, company: Tessellate AI}"""
    m = INTRO_RE.search(text)
    if not m:
        return None
    words = m.group("name").split()
    while words and words[-1].lower() in NOT_NAMES:
        words.pop()
    if not words or words[0].lower() in NOT_NAMES:
        return None
    rest = re.split(r"[.!?]", text[m.end():], maxsplit=1)[0]
    c = COMPANY_RE.search(rest)
    company = c.group("co").strip() if c and c.group("co").strip() not in SKIP_COMPANY else None
    return {"name": " ".join(words), "company": company, "title": None, "raw_text": text[:300]}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]+", "", (name or "").lower()).strip()


def same_person(said: str, partner: str) -> bool:
    """'Maya' or 'Maya Okafor' said by the person you're already talking to (Maya Okafor) is not a new intro."""
    s, p = _norm(said).split(), _norm(partner).split()
    if not s or not p:
        return False
    if len(s) == 1:
        return s[0] == p[0]
    return resolver.name_score(said, partner) > 0.7


class ListenSession:
    """Single user, so one rolling transcript."""

    def __init__(self):
        self.reset()
        self._intro_lock = asyncio.Lock()

    def reset(self) -> None:
        self.lines: list[tuple[int, int | None, str]] = []  # (at_ms, speaker, text)
        self.new_chars = 0
        self.last_detect = 0.0
        self.detecting = False
        self.rerun = False
        self.nudges: list[str] = []
        self.nudged_at: dict[str, float] = {}
        self.last_nudge_at = 0.0
        self.recent_intros: dict[str, float] = {}

    async def on_final(self, text: str, speaker: int | None, at_ms: int | None = None) -> None:
        text = (text or "").strip()[:MAX_LINE_CHARS]
        if not text:
            return
        at_ms = at_ms or graph.now_ms()
        try:
            cur = await graph.current_encounter(max_age_min=30)
            await graph.add_chunk(text, speaker, at_ms, cur["id"] if cur else None)
            hub.note("falkordb", "FalkorDB", "Save transcript line", text[:120], "saved")
        except Exception:
            log.exception("could not store transcript chunk")
        self.lines.append((at_ms, speaker, text))
        self.lines = [x for x in self.lines if x[0] > graph.now_ms() - WINDOW_MS][-MAX_LINES:]
        self.new_chars += len(text)

        if (read := fast_intro(text)):
            hub.spawn(self._fast_path(read))
        due = time.monotonic() - self.last_detect >= DETECT_EVERY_S and self.new_chars >= DETECT_MIN_NEW_CHARS
        if due or INTRO_HINT.search(text):
            if self.detecting:
                self.rerun = True  # analyze the newest lines as soon as the running pass finishes
            else:
                self.detecting = True  # set before spawning so two lines can't start two passes
                hub.spawn(self._detect())

    # ---------- introductions ----------

    async def _partner(self) -> tuple[dict | None, dict | None]:
        cur = await graph.current_encounter(max_age_min=30)
        if not cur:
            return None, None
        e = await graph.get_encounter_raw(cur["id"]) or cur
        p = await graph.get_person(e["person_id"]) if e.get("person_id") else None
        return e, p

    async def _handle_intro(self, read: dict, source: str, min_score: float | None = None) -> None:
        """Resolve a self-introduction once; publish the resolution for the UI."""
        name = (read.get("name") or "").strip()
        if not name:
            return
        async with self._intro_lock:
            key = _norm(name)
            now = time.monotonic()
            if now - self.recent_intros.get(key, -1e9) < INTRO_DEDUPE_S:
                return
            me = await cards.me_context()
            my_name = me.get("name") or ""
            my_first = _norm(my_name).split()[0] if my_name else ""
            if resolver.name_score(name, my_name) > 0.8 or (my_first and key == my_first):
                return  # the user introducing themselves
            enc, partner = await self._partner()
            if partner and same_person(name, partner.get("name") or ""):
                self.recent_intros[key] = now
                return  # already talking to them
            if enc and not partner and enc.get("status") in ("needs_pick", "resolving") \
                    and graph.now_ms() - (enc.get("at") or 0) < 120_000:
                return  # user is mid-pick on a fresh encounter; don't pile on
            if min_score is not None:
                cands = await resolver._graph_candidates(read)
                best = max((resolver.score(read, c)[0] for c in cands), default=0.0)
                if best < min_score:
                    return  # not someone we know; leave it to the Claude pass (which can also search)
            self.recent_intros[key] = now
            res = await resolver.resolve(read, new_encounter=True)
            log.info("intro (%s): %s -> %s", source, name, res["encounter"]["status"])
            hub.publish("resolution", res, push_text=f"Talking to {name}?")

    async def _fast_path(self, read: dict) -> None:
        try:
            await self._handle_intro(read, "regex", min_score=FAST_PATH_MIN_SCORE)
        except Exception:
            log.exception("fast intro path failed")

    # ---------- Claude pass ----------

    async def _detect(self) -> None:
        self.detecting = True
        try:
            while True:
                self.rerun = False
                self.last_detect = time.monotonic()
                self.new_chars = 0
                await self._detect_once()
                if not self.rerun:
                    break
        finally:
            self.detecting = False

    async def _detect_once(self) -> None:
        try:
            transcript = "\n".join(f"[S{sp if sp is not None else '?'}] {t}" for _, sp, t in self.lines)
            if not transcript:
                return
            me = await cards.me_context()
            enc, partner = await self._partner()
            ctx = ""
            if partner:
                card = json.loads(partner.get("card_json") or "{}")
                ctx = "Card shown to user: " + json.dumps({k: card.get(k) for k in ("common_ground", "goal_fit")})
            out = await llm.live_detect(transcript, me, partner.get("name") if partner else None, ctx,
                                        self.nudges[-5:])
            if not out:
                return  # no key / refusal / API error: nothing to do
            for intro in out.introductions:
                await self._handle_intro(intro.model_dump() | {"raw_text": None}, "claude")
            enc, _ = await self._partner()  # may be a new encounter now
            eid = enc["id"] if enc else None
            if out.nudge and out.nudge.text not in self.nudges:
                now = time.monotonic()
                if now - self.nudged_at.get(eid or "_", -1e9) > NUDGE_GAP_PER_ENCOUNTER_S \
                        and now - self.last_nudge_at > NUDGE_GAP_GLOBAL_S:
                    self.nudged_at[eid or "_"] = self.last_nudge_at = now
                    self.nudges.append(out.nudge.text)
                    hub.publish("nudge", {"encounter_id": eid, "text": out.nudge.text, "kind": out.nudge.kind},
                                push_text=out.nudge.text)
                    hub.note("llm", llm.label(), "Live nudge", out.nudge.text, out.nudge.kind)
            if eid and out.topics:
                for t in out.topics[:5]:
                    tid = graph.slug(t).replace("-", " ")
                    if tid:
                        await graph.q("MATCH (e:Encounter {id:$e}) MERGE (t:Topic {id:$t}) ON CREATE SET t.name=$n "
                                      "MERGE (e)-[:DISCUSSED]->(t)", {"e": eid, "t": tid, "n": t.lower()})
        except Exception:
            log.exception("live detection failed")


session = ListenSession()


def listen_mode() -> str:
    return "deepgram" if settings().deepgram_api_key else "browser"


DG_PARAMS = {"model": "nova-3", "language": "en", "encoding": "linear16", "sample_rate": 16000, "channels": 1,
             "diarize": "true", "smart_format": "true", "punctuate": "true", "interim_results": "true",
             "utterance_end_ms": 1000, "vad_events": "true", "endpointing": 300}


def _speaker_turns(words: list[dict], fallback: str) -> list[tuple[int | None, str]]:
    """Split one Deepgram result into (speaker, text) runs using per-word diarization."""
    turns: list[tuple[int | None, list[str]]] = []
    for w in words:
        sp = w.get("speaker")
        token = w.get("punctuated_word") or w.get("word") or ""
        if turns and turns[-1][0] == sp:
            turns[-1][1].append(token)
        else:
            turns.append((sp, [token]))
    out = [(sp, " ".join(t).strip()) for sp, t in turns if any(t)]
    return out or [(None, fallback)]


async def deepgram_bridge(ws: WebSocket) -> None:
    await ws.accept()
    url = "wss://api.deepgram.com/v1/listen?" + urllib.parse.urlencode(DG_PARAMS)
    try:
        async with ws_connect(url, additional_headers={"Authorization": f"Token {settings().deepgram_api_key}"},
                              max_size=None, ping_interval=20) as dg:
            await ws.send_json({"type": "status", "state": "connected"})
            last_audio = time.monotonic()

            async def upstream():
                nonlocal last_audio
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("bytes"):
                        await dg.send(msg["bytes"])
                        last_audio = time.monotonic()
                    elif msg.get("text"):
                        try:
                            if json.loads(msg["text"]).get("type") == "stop":
                                break
                        except json.JSONDecodeError:
                            pass
                await dg.send(json.dumps({"type": "CloseStream"}))

            async def keepalive():
                while True:
                    await asyncio.sleep(4)
                    if time.monotonic() - last_audio > 4:
                        await dg.send(json.dumps({"type": "KeepAlive"}))

            async def downstream():
                async for raw in dg:
                    data = json.loads(raw)
                    if data.get("type") != "Results":
                        continue
                    alt = (data.get("channel", {}).get("alternatives") or [{}])[0]
                    text = alt.get("transcript", "")
                    if not text:
                        continue
                    words = alt.get("words") or []
                    speaker = words[0].get("speaker") if words else None
                    final = bool(data.get("is_final"))
                    at = graph.now_ms()
                    await ws.send_json({"type": "transcript", "text": text, "speaker": speaker,
                                        "is_final": final, "at": cards.iso(at)})
                    if final:
                        for sp, turn in _speaker_turns(words, text):
                            await session.on_final(turn, sp, at)

            up, down, ka = (asyncio.create_task(c()) for c in (upstream, downstream, keepalive))
            done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if up in done:  # client stopped: give Deepgram a moment to flush final results
                try:
                    await asyncio.wait_for(down, timeout=3)
                except (asyncio.TimeoutError, Exception):
                    pass
            for t in (up, down, ka):
                t.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("deepgram bridge error")
        try:
            await ws.send_json({"type": "status", "state": "upstream_error", "detail": str(e)[:200]})
        except Exception:
            pass
    finally:
        try:
            await ws.send_json({"type": "status", "state": "closed"})
            await ws.close()
        except Exception:
            pass
