"""Every Claude call Wingman makes, each returning a validated Pydantic object."""

import base64
import json
import logging
from typing import Literal, TypeVar

import anthropic
import openai
from pydantic import BaseModel

from . import fixtures, hub
from .config import settings

log = logging.getLogger("wingman.llm")
T = TypeVar("T", bound=BaseModel)

_client: anthropic.AsyncAnthropic | None = None
_oai: openai.AsyncOpenAI | None = None

SENSITIVE = ("Never mention or infer health, religion, politics, sexuality, ethnicity, family status, age, "
             "or appearance.")


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        key = settings().anthropic_api_key
        _client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
    return _client


def oai() -> openai.AsyncOpenAI:
    global _oai
    if _oai is None:
        _oai = openai.AsyncOpenAI(api_key=settings().openai_api_key or None)
    return _oai


def provider() -> str:
    s = settings()
    if s.wingman_llm in ("anthropic", "openai"):
        return s.wingman_llm
    if fixtures.active() or s.anthropic_api_key or not s.openai_api_key:
        return "anthropic"
    return "openai"


def model_for(live: bool) -> str:
    s = settings()
    if provider() == "openai":
        return s.wingman_openai_model_live if live else s.wingman_openai_model_recap
    return s.wingman_model_live if live else s.wingman_model_recap


def label(model: str | None = None) -> str:
    """claude-opus-5-5 -> Claude Opus 5.5; gpt-5.5 -> OpenAI GPT-5.5"""
    model = model or model_for(True)
    if model.startswith("claude-"):
        parts = model.removeprefix("claude-").split("-")
        return f"Claude {parts[0].title()} {'.'.join(parts[1:])}".strip()
    return f"OpenAI {model.replace('gpt-', 'GPT-')}"


def _model_label(model: str) -> str:
    return label(model)


async def _parse(schema: type[T], system: str, content, *, live: bool = True, effort: str = "low",
                 max_tokens: int = 8000, label: str | None = None, detail: str = "") -> T | None:
    model = model_for(live)
    if not label:
        return await _call(schema, system, content, model, effort, max_tokens)
    async with hub.step("llm", _model_label(model), label, detail) as st:
        out = await _call(schema, system, content, model, effort, max_tokens)
        st.result = "ok" if out else "no result"
    return out


async def _call(schema: type[T], system: str, content, model: str, effort: str, max_tokens: int) -> T | None:
    if fixtures.active():
        return await fixtures.claude(schema, system, content)
    if provider() == "openai":
        return await _call_openai(schema, system, content, model, effort, max_tokens)
    try:
        resp = await client().beta.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            output_config={"effort": effort},
            # On a policy decline, the API re-runs the request on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.BadRequestError as e:
        log.error("claude bad request (%s): %s", schema.__name__, e.message)
        return None
    except (anthropic.APIError, TypeError) as e:  # TypeError: no API key configured
        log.error("claude call failed (%s): %s", schema.__name__, e)
        return None
    if resp.stop_reason == "refusal":
        log.warning("claude refused %s: %s", schema.__name__, resp.stop_details)
        return None
    if resp.stop_reason == "max_tokens":
        log.warning("claude hit max_tokens on %s", schema.__name__)
    return resp.parsed_output


async def _call_openai(schema: type[T], system: str, content, model: str, effort: str,
                       max_tokens: int) -> T | None:
    """Same contract as the Claude path: a validated Pydantic object or None (structured outputs)."""
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    parts = []
    for b in blocks:
        if b.get("type") == "text":
            parts.append({"type": "input_text", "text": b["text"]})
        elif b.get("type") == "image":
            src = b["source"]
            parts.append({"type": "input_image", "image_url": f"data:{src['media_type']};base64,{src['data']}"})
    try:
        resp = await oai().responses.parse(model=model, instructions=system, text_format=schema,
                                           input=[{"role": "user", "content": parts}],
                                           reasoning={"effort": effort}, max_output_tokens=max_tokens, store=False)
    except openai.APIError as e:
        log.error("openai call failed (%s): %s", schema.__name__, e)
        return None
    if resp.output_parsed is None:
        log.warning("openai returned no parsed output for %s (status %s)", schema.__name__, resp.status)
    return resp.output_parsed


# ---------- badge / note / live ----------

class BadgeRead(BaseModel):
    kind: Literal["badge", "business_card", "screen", "other"]
    name: str | None
    company: str | None
    title: str | None
    raw_text: str | None


async def read_badge(jpeg: bytes) -> BadgeRead | None:
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(jpeg).decode()}},
        {"type": "text", "text": "Read the person's name, company and job title from this conference badge, "
                                 "business card, or screen. Use null for anything not visible. raw_text: all "
                                 "legible text, one line."},
    ]
    return await _parse(BadgeRead, "You read conference badges precisely. Do not guess missing text.", content,
                        label="Read badge photo", detail="name, company and title from the image")


class Intro(BaseModel):
    name: str
    company: str | None
    title: str | None


class NoteParse(BaseModel):
    new_person: Intro | None


async def parse_note(note: str, current_person: str | None) -> NoteParse | None:
    system = ("The user dictates short notes at a conference. Decide whether the note introduces a person the "
              "user just met (e.g. 'just met Priya Shah from Stripe'). If it is about the person they are "
              "already talking to, or names nobody new, return new_person=null.")
    ctx = f"Current conversation partner: {current_person or 'none'}\n\nNote: {note}"
    return await _parse(NoteParse, system, ctx, label="Read voice note", detail=note[:120])


class Nudge(BaseModel):
    text: str
    kind: Literal["intro", "goal", "topic", "reminder"]


class LiveDetect(BaseModel):
    introductions: list[Intro]
    topics: list[str]
    nudge: Nudge | None


async def live_detect(transcript: str, me: dict, partner: str | None, partner_context: str,
                      recent_nudges: list[str]) -> LiveDetect | None:
    system = (
        "You listen to a live conference conversation transcript (speaker numbers come from automatic "
        "diarization and may be wrong) on behalf of the user. Return:\n"
        "- introductions: people who introduce THEMSELVES by name in the transcript (not people merely "
        "mentioned). Include company/title only if said.\n"
        "- topics: 0-5 short lowercase topics discussed.\n"
        "- nudge: at most one short, concrete, high-value suggestion the user can act on right now "
        "(an intro they could offer, a goal match, a promise to remember). Under 18 words. Return null "
        "unless it is clearly worth interrupting for. Never repeat a recent nudge. " + SENSITIVE)
    ctx = (f"User: {me.get('name')}. Goals: {json.dumps(me.get('goals', []))}\n"
           f"Talking to: {partner or 'unknown'}\n{partner_context}\n"
           f"Recent nudges: {json.dumps(recent_nudges)}\n\nTranscript (oldest first):\n{transcript}")
    return await _parse(LiveDetect, system, ctx, label="Follow the conversation",
                        detail="introductions, topics, and a nudge if something matches your goals")


# ---------- profile extraction ----------

class Role(BaseModel):
    org: str
    title: str | None
    current: bool


class Hook(BaseModel):
    text: str
    source: str
    url: str | None
    date: str | None


class PersonFacts(BaseModel):
    headline: str | None
    current_title: str | None
    current_company: str | None
    city: str | None
    roles: list[Role]
    schools: list[str]
    topics: list[str]
    hooks: list[Hook]


async def extract_person(name: str, sources: str, labels: str = "") -> PersonFacts | None:
    system = (
        "Extract conversation-ready facts about one person from the sources (conference bio, LinkedIn "
        "profile JSON, search results). Rules:\n"
        "- roles: employers with titles; current=true only for present roles. Include past employers.\n"
        "- schools: institutions only.\n"
        "- city: metro area if stated.\n"
        "- topics: 4-10 professional or hobby interests as short lowercase noun phrases "
        "('llm evaluation', 'developer experience', 'rust', 'trail running'). Prefer specific over generic; "
        "skip filler like 'technology' or 'innovation'.\n"
        "- hooks: 0-6 specific, recent, positive things worth asking about (a talk, a post, a launch, a "
        "project, a job change). One sentence each, with the source label (e.g. 'LinkedIn post', 'speaker "
        "bio', 'GitHub') and URL/date if given.\n"
        "Only use what the sources say. Web results and news can be about other people with the same name: use "
        "one only if it clearly matches this person (same employer, school, city or field). " + SENSITIVE)
    return await _parse(PersonFacts, system, f"Person: {name}\n\nSources:\n{sources}", effort="low",
                        label="Extract facts", detail=f"{name}, from {labels or 'profile'}")


# ---------- card ----------

class CardItem(BaseModel):
    text: str
    fact_ids: list[int]


class CardDraft(BaseModel):
    common_ground: list[CardItem]
    ask_about: list[CardItem]
    goal_fit: list[CardItem]
    heads_up: list[str]


async def write_card(me: dict, person: dict, facts: list[dict], event_name: str) -> CardDraft | None:
    numbered = "\n".join(f"[{i}] ({f['source']}) {f['text']}" for i, f in enumerate(facts))
    system = (
        "You write a card the user glances at for five seconds before or during a conversation at a "
        "conference. Use ONLY the numbered facts and cite their numbers in fact_ids; an item without a "
        "supporting fact must not exist.\n"
        "- common_ground: up to 3 concrete overlaps the facts state directly (a shared employer, school, city, "
        "topic or contact, or an interest marked as close to the user's), strongest first. Never generalize or "
        "infer an overlap; fewer is better, and empty is fine.\n"
        "- ask_about: up to 2 warm, specific questions or openers tied to something recent or distinctive.\n"
        "- goal_fit: up to 2 ways this person relates to the user's goals; empty if none.\n"
        "- heads_up: up to 2 short cautions (thin or possibly stale data, open promises, previous meetings).\n"
        "Each item under 16 words, plain and natural, addressed to the user ('You both...', 'Ask how...'). "
        + SENSITIVE)
    ctx = (f"Event: {event_name}\nUser: {me.get('name')} — {me.get('about') or ''}\n"
           f"User goals: {json.dumps(me.get('goals', []))}\n"
           f"Person: {person.get('name')} — {person.get('title') or ''} at {person.get('company') or ''}\n\n"
           f"Facts:\n{numbered}")
    return await _parse(CardDraft, system, ctx, label="Write card",
                        detail=f"{person.get('name')}: {len(facts)} facts, every line must cite one")


# ---------- shortlist ----------

class ShortlistItem(BaseModel):
    person_id: str
    score: float
    why: str


class Shortlist(BaseModel):
    items: list[ShortlistItem]


async def rank_people(me: dict, people: list[dict], limit: int) -> Shortlist | None:
    system = (f"Pick the {limit} people at this conference most worth the user's time, given the user's "
              "goals and background. score is 0-1. why: one concrete line under 14 words that names the "
              "overlap or goal it serves. Only use the given data.")
    ctx = (f"User: {me.get('name')} — {me.get('about') or ''}\nGoals: {json.dumps(me.get('goals', []))}\n\n"
           f"People (JSON lines):\n" + "\n".join(json.dumps(p, ensure_ascii=False) for p in people))
    return await _parse(Shortlist, system, ctx, live=False, effort="medium", max_tokens=16000,
                        label="Rank who's worth meeting", detail=f"{len(people)} people against your goals")


# ---------- recap ----------

class ActionItemOut(BaseModel):
    text: str
    owner: Literal["me", "them", "unknown"]
    due: str | None
    quote: str | None


class RecapOut(BaseModel):
    summary: str
    topics: list[str]
    action_items: list[ActionItemOut]
    follow_up_channel: Literal["email", "linkedin"]
    follow_up: str


async def recap(me: dict, person: dict | None, notes: list[str], transcript: str, event_name: str,
                date: str) -> RecapOut | None:
    system = (
        "Summarize one conference conversation for the user and pull out commitments. Speaker numbers in "
        "the transcript come from automatic diarization; infer who is the user from context (the user's "
        "voice notes are authoritative). Write to the user as 'you' and never use the user's name. "
        "action_items: concrete promises or next steps in imperative form ('Send Priya the deck'); owner 'me' if "
        "the user committed, 'them' if the other person did; due as YYYY-MM-DD only for an explicit day or date "
        f"('tomorrow', 'Friday', 'Nov 3'; the conversation was on {date}), otherwise null; quote: the exact "
        "supporting words. summary: 2-3 sentences. "
        "follow_up: a short, warm, specific message from the user to send today referencing what was "
        "discussed and any promise; email if they shared email or it's business, else linkedin. " + SENSITIVE)
    who = f"{person.get('name')} — {person.get('title') or ''} at {person.get('company') or ''}" if person else "unknown"
    ctx = (f"Event: {event_name}\nUser: {me.get('name')}\nGoals: {json.dumps(me.get('goals', []))}\n"
           f"Other person: {who}\n\nUser's voice notes:\n" + ("\n".join(f"- {n}" for n in notes) or "(none)") +
           f"\n\nTranscript:\n{transcript or '(none)'}")
    return await _parse(RecapOut, system, ctx, live=False, effort="medium", max_tokens=12000,
                        label="Recap conversation", detail=f"{person.get('name') if person else 'unknown'}: summary, promises, follow-up")


# ---------- ask ----------

class CypherPlan(BaseModel):
    cypher: str


class Answer(BaseModel):
    answer: str


async def plan_query(question: str, schema_doc: str, today: str) -> CypherPlan | None:
    system = ("Write ONE read-only openCypher query for FalkorDB that answers the user's question about "
              "people they met or may meet at conferences. The user is (:Person {id:'me'}). Encounter.at and "
              "Chunk.at are epoch milliseconds. Return maps or scalars, LIMIT 50. Use toLower(...) CONTAINS "
              "for fuzzy text matching. Schema:\n" + schema_doc)
    return await _parse(CypherPlan, system, f"Today is {today}.\nQuestion: {question}")


async def answer_from_rows(question: str, cypher: str, result: str) -> Answer | None:
    system = "Answer the user's question in 1-5 short lines using only the query result. Say so if it's empty."
    return await _parse(Answer, system, f"Question: {question}\nQuery: {cypher}\nResult: {result}")
