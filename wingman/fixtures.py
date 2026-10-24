"""Demo fixtures: replay Bright Data and Claude responses from JSON files instead of calling the APIs.

Enabled by WINGMAN_FIXTURES=<dir> (see demo/README.md). Everything else (resolver, FalkorDB graph, cards,
live listening, recap, the web app) runs for real; only the two network boundaries are replaced.

  <dir>/brightdata.json  {"latency": {...}, "people": [record], "serp": [{match, results}], "pages": {url: md},
                          "linkedin": {url: profile}}
  <dir>/claude.json      {"latency": {...}, "responses": [{schema, match?, absent?, response}]}

A fixture entry applies when every `match` string is in the request and no `absent` string is
(case-insensitive); the first one wins. Card fixtures cite facts by text (`cite: ["Stripe"]`) rather than by
number, and are mapped to the numbered facts in the actual prompt, so uncited items still get dropped.
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ROOT, settings

log = logging.getLogger("wingman.fixtures")


def active() -> bool:
    return bool(settings().wingman_fixtures)


@lru_cache
def _load(name: str) -> dict:
    d = Path(settings().wingman_fixtures)
    if not d.is_absolute():
        d = ROOT / d
    p = d / name
    return json.loads(p.read_text()) if p.exists() else {}


async def _sleep(kind: str, table: dict) -> None:
    lo, hi = (table.get("latency") or {}).get(kind) or (table.get("latency") or {}).get("default") or (0.3, 0.6)
    await asyncio.sleep(random.uniform(lo, hi) / max(settings().wingman_fixture_speed, 0.01))


def _applies(entry: dict, haystack: str) -> bool:
    h = haystack.lower()
    return all(m.lower() in h for m in entry.get("match") or []) and \
        not any(a.lower() in h for a in entry.get("absent") or [])


# ---------- Bright Data ----------

async def people(name: str, company: str | None) -> list[dict]:
    """Business Search fixture: people whose name matches (and current company, if given)."""
    bd = _load("brightdata.json")
    await _sleep("people", bd)
    n, c = name.lower(), (company or "").lower()
    out = [x for x in bd.get("people") or [] if n in x.get("name", "").lower()
           and (not c or c in x.get("current_company_name", "").lower())]
    if not out:
        log.warning("no Business Search fixture for %r / %r", name, company)
    return out


async def serp(query: str) -> list[dict]:
    bd = _load("brightdata.json")
    await _sleep("serp", bd)
    for e in bd.get("serp") or []:
        if _applies(e, query):
            return e["results"]
    log.warning("no SERP fixture for %r", query)
    return []


async def page(url: str) -> str:
    bd = _load("brightdata.json")
    await _sleep("page", bd)
    md = (bd.get("pages") or {}).get(url)
    if md is None:
        log.warning("no page fixture for %s", url)
    return md or ""


async def linkedin(url: str) -> dict | None:
    bd = _load("brightdata.json")
    await _sleep("linkedin", bd)
    return (bd.get("linkedin") or {}).get(url)


async def linkedin_batch(urls: list[str]) -> dict[str, dict]:
    bd = _load("brightdata.json")
    await _sleep("batch", bd)
    profiles = bd.get("linkedin") or {}
    return {u: profiles[u] for u in urls if u in profiles}


# ---------- Claude ----------

_FACT_LINE = re.compile(r"^\[(\d+)\] \((.*?)\) (.*)$", re.M)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _cite(response: dict, prompt: str) -> dict:
    """Card fixtures: turn `cite` strings into fact_ids for the facts actually in this prompt."""
    facts = [(int(i), f"{src} {text}".lower()) for i, src, text in _FACT_LINE.findall(prompt)]
    out = {}
    for key, val in response.items():
        if key in ("common_ground", "ask_about", "goal_fit"):
            items = []
            for it in val:
                ids = [next((i for i, t in facts if c.lower() in t), None) for c in it.get("cite") or []]
                items.append({"text": it["text"], "fact_ids": [i for i in ids if i is not None]})
            out[key] = items
        elif key == "heads_up":
            out[key] = [h["text"] if isinstance(h, dict) else h for h in val
                        if not isinstance(h, dict) or _applies(h, prompt)]
        else:
            out[key] = val
    return out


def _dates(value):
    """'{{date+1}}' -> tomorrow's date (YYYY-MM-DD) in the event timezone."""
    if isinstance(value, str):
        today = datetime.now(ZoneInfo(settings().wingman_timezone)).date()
        return re.sub(r"\{\{date\+(\d+)\}\}", lambda m: (today + timedelta(days=int(m.group(1)))).isoformat(), value)
    if isinstance(value, list):
        return [_dates(v) for v in value]
    if isinstance(value, dict):
        return {k: _dates(v) for k, v in value.items()}
    return value


async def claude(schema, system: str, content):
    cl = _load("claude.json")
    prompt = _text_of(content)
    for e in cl.get("responses") or []:
        if e.get("schema") == schema.__name__ and _applies(e, system + "\n" + prompt):
            await _sleep(schema.__name__, cl)
            resp = _dates(_cite(e["response"], prompt) if schema.__name__ == "CardDraft" else e["response"])
            return schema.model_validate(resp)
    log.warning("no Claude fixture for %s", schema.__name__)
    return None
