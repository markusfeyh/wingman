"""Bright Data: Google search (SERP API), page fetch (Web Unlocker), LinkedIn profiles (Scraper API).

Every response is cached on disk by request, so re-runs and retries cost nothing.
"""

import asyncio
import hashlib
import json
import re
import urllib.parse
from pathlib import Path

import httpx

from .config import settings

API = "https://api.brightdata.com"
LINKEDIN_PROFILES = "gd_l1viktl72bvl7bjuj0"


class BrightDataError(RuntimeError):
    pass


def _cache_path(kind: str, key: str) -> Path:
    d = settings().data_dir / "cache" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")


def _cached(kind: str, key: str):
    p = _cache_path(kind, key)
    return json.loads(p.read_text()) if p.exists() else None


def _store(kind: str, key: str, value) -> None:
    _cache_path(kind, key).write_text(json.dumps(value))


def enabled() -> bool:
    return bool(settings().brightdata_api_key)


def _client(timeout: float = 70.0) -> httpx.AsyncClient:
    if not enabled():
        raise BrightDataError("BRIGHTDATA_API_KEY is not set")
    return httpx.AsyncClient(base_url=API, timeout=timeout,
                             headers={"Authorization": f"Bearer {settings().brightdata_api_key}"})


async def _request(zone: str, url: str, data_format: str | None = None) -> str:
    body = {"zone": zone, "url": url, "format": "raw"}
    if data_format:
        body["data_format"] = data_format
    async with _client() as c:
        r = await c.post("/request", json=body)
    if r.status_code >= 400:
        raise BrightDataError(f"/request {zone} -> {r.status_code}: {r.text[:200]}")
    return r.text


async def google(query: str, num: int = 10) -> list[dict]:
    """Organic Google results: [{title, link, description}]."""
    key = f"{query}|{num}"
    if (hit := _cached("serp", key)) is not None:
        return hit
    url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}&num={num}&hl=en&gl=us&brd_json=1"
    text = await _request(settings().brightdata_serp_zone, url)
    results: list[dict] = []
    try:
        data = json.loads(text)
        for o in data.get("organic", []):
            results.append({"title": o.get("title") or "", "link": o.get("link") or o.get("url") or "",
                            "description": o.get("description") or o.get("snippet") or ""})
    except json.JSONDecodeError:
        # Zone returned HTML/markdown instead of parsed JSON: keep what links we can find.
        for link in dict.fromkeys(re.findall(r"https?://[^\s\"'<>)]+", text)):
            results.append({"title": "", "link": link, "description": ""})
    _store("serp", key, results)
    return results


async def fetch_markdown(url: str) -> str:
    if (hit := _cached("page", url)) is not None:
        return hit
    text = await _request(settings().brightdata_unlocker_zone, url, data_format="markdown")
    _store("page", url, text)
    return text


async def linkedin_profile(url: str) -> dict | None:
    """One profile, synchronously (Bright Data waits up to ~1 min, then hands back a snapshot id)."""
    if (hit := _cached("li", url)) is not None:
        return hit or None
    async with _client(timeout=90) as c:
        r = await c.post("/datasets/v3/scrape", params={"dataset_id": LINKEDIN_PROFILES, "format": "json"},
                         json=[{"url": url}])
        if r.status_code == 202:
            snap = r.json().get("snapshot_id")
            data = await _wait_snapshot(c, snap, max_wait=150)
        elif r.status_code >= 400:
            raise BrightDataError(f"scrape -> {r.status_code}: {r.text[:200]}")
        else:
            data = r.json()
    profile = _first_profile(data)
    _store("li", url, profile or {})
    return profile


async def linkedin_profiles_batch(urls: list[str], max_wait: int = 1800) -> dict[str, dict]:
    """Many profiles via the async /trigger endpoint. Returns {input_url: profile}."""
    out: dict[str, dict] = {}
    todo = []
    for u in urls:
        hit = _cached("li", u)
        if hit is not None:
            if hit:
                out[u] = hit
        else:
            todo.append(u)
    if not todo:
        return out
    async with _client(timeout=120) as c:
        r = await c.post("/datasets/v3/trigger",
                         params={"dataset_id": LINKEDIN_PROFILES, "format": "json", "include_errors": "true"},
                         json=[{"url": u} for u in todo])
        if r.status_code >= 400:
            raise BrightDataError(f"trigger -> {r.status_code}: {r.text[:200]}")
        data = await _wait_snapshot(c, r.json()["snapshot_id"], max_wait=max_wait)
    by_slug = {_li_slug(u): u for u in todo}
    for rec in data if isinstance(data, list) else []:
        src = rec.get("input", {}).get("url") if isinstance(rec.get("input"), dict) else None
        src = src or rec.get("input_url") or rec.get("url") or ""
        u = by_slug.get(_li_slug(src))
        if not u:
            continue
        if rec.get("error") or not rec.get("name"):
            _store("li", u, {})
            continue
        out[u] = rec
        _store("li", u, rec)
    return out


async def _wait_snapshot(c: httpx.AsyncClient, snap: str, max_wait: int):
    waited = 0
    while waited <= max_wait:
        r = await c.get(f"/datasets/v3/progress/{snap}")
        status = r.json().get("status") if r.status_code == 200 else None
        if status == "ready":
            r = await c.get(f"/datasets/v3/snapshot/{snap}", params={"format": "json"})
            return r.json()
        if status == "failed":
            raise BrightDataError(f"snapshot {snap} failed")
        await asyncio.sleep(5)
        waited += 5
    raise BrightDataError(f"snapshot {snap} not ready after {max_wait}s")


def _first_profile(data) -> dict | None:
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict) and data.get("name") and not data.get("error"):
        return data
    return None


def _li_slug(url: str) -> str:
    m = re.search(r"linkedin\.com/(?:in|pub)/([^/?#\s]+)", url or "", re.I)
    return urllib.parse.unquote(m.group(1)).lower().rstrip("/") if m else ""


def trim_profile(p: dict, max_chars: int = 9000) -> str:
    """Keep the parts of a LinkedIn profile that matter for conversation; drop noise like similar-profiles."""
    keep = ("name", "position", "about", "city", "country_code", "location", "current_company",
            "current_company_name", "experience", "education", "educations_details", "certifications",
            "languages", "volunteer_experience", "honors_and_awards", "projects", "publications",
            "courses", "activity", "posts", "recommendations_count", "followers")
    slim = {k: p[k] for k in keep if p.get(k)}
    for k in ("activity", "posts"):
        if isinstance(slim.get(k), list):
            slim[k] = [{kk: v for kk, v in a.items() if kk in ("title", "interaction", "link", "date", "text")}
                       for a in slim[k][:12] if isinstance(a, dict)]
    text = json.dumps(slim, ensure_ascii=False)
    return text[:max_chars]
