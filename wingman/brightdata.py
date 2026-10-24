"""Bright Data: people lookup (Business Search), Google search (SERP API), page fetch (Web Unlocker),
LinkedIn profiles (Scraper API).

Every response is cached on disk by request, so re-runs and retries cost nothing.
"""

import asyncio
import hashlib
import json
import re
import urllib.parse
from pathlib import Path

import httpx

from . import fixtures, hub
from .config import settings

API = "https://api.brightdata.com"
LINKEDIN_PROFILES = "gd_l1viktl72bvl7bjuj0"
LINKEDIN_COMPANIES = "gd_l1vikfnt1wgvvqz95w"


class BrightDataError(RuntimeError):
    pass


class NotEnabled(BrightDataError):
    """The account can't use this product (e.g. Business Search is early-access: HTTP 403)."""


_unavailable: set[str] = set()  # products that returned 403 in this process; don't keep asking


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
    return bool(settings().brightdata_api_key) or fixtures.active()


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


PEOPLE_FIELDS = ["name", "url", "current_title", "current_company_name", "location", "about",
                 "past_company_names", "avatar"]


async def people_search(name: str, company: str | None = None, limit: int = 5) -> list[dict]:
    """Business Search: Bright Data's people index (700M+ profiles), name + current company in ~50 ms.

    POST /search/people, mode "ludicrous" (structured query). Beta with early access: other accounts get
    HTTP 403, which raises NotEnabled so callers fall back to the SERP API. Docs:
    https://docs.brightdata.com/products/business-search/people-search
    Returns [{name, url, current_title, current_company_name, location, about, past_company_names, avatar}].
    """
    if "business_search" in _unavailable:
        raise NotEnabled("Business Search not enabled for this account")
    key = f"{name}|{company or ''}|{limit}"
    if (hit := _cached("people", key)) is not None:
        hub.note("brightdata", "Business Search", "People lookup", _people_detail(name, company),
                 _n(len(hit), "match", "matches") + " (cached)")
        return hit
    async with hub.step("brightdata", "Business Search", "People lookup", _people_detail(name, company)) as st:
        if fixtures.active():
            people = await fixtures.people(name, company)
        else:
            conds = [{"text": {"name": name}}] + ([{"text": {"current_company_name": company}}] if company else [])
            body = {"mode": "ludicrous", "query": {"and": conds}, "limit": limit, "view": {"fields": PEOPLE_FIELDS}}
            async with _client(timeout=15) as c:
                r = await c.post("/search/people", json=body)
            if r.status_code == 403:
                _unavailable.add("business_search")
                raise NotEnabled(f"Business Search -> 403 (early access only): {r.text[:120]}")
            if r.status_code >= 400:
                raise BrightDataError(f"/search/people -> {r.status_code}: {r.text[:200]}")
            people = [d.get("data") or {} for d in r.json().get("documents") or []]
        people = [x for x in people if x.get("name") and x.get("url")]
        st.result = _n(len(people), "match", "matches")
    _store("people", key, people)
    return people


def _n(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _people_detail(name: str, company: str | None) -> str:
    return f"name: {name}" + (f" · current company: {company}" if company else "")


async def google(query: str, num: int = 10, news: bool = False) -> list[dict]:
    """Organic Google results (or Google News with news=True): [{title, link, description, source?, date?}]."""
    key = f"{query}|{num}" + ("|news" if news else "")
    action = "Google News search" if news else "Google search"
    if (hit := _cached("serp", key)) is not None:
        hub.note("brightdata", "SERP API", action, query, _n(len(hit), "result") + " (cached)")
        return hit
    async with hub.step("brightdata", "SERP API", action, query) as st:
        if fixtures.active():
            results = await fixtures.serp(query + (" tbm=nws" if news else ""))
        else:
            url = (f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}&num={num}&hl=en&gl=us"
                   + ("&tbm=nws" if news else "") + "&brd_json=1")
            results = _parse_serp(await _request(settings().brightdata_serp_zone, url))
            if not results:  # Google occasionally returns an empty page for a query that has results
                await asyncio.sleep(1)
                results = _parse_serp(await _request(settings().brightdata_serp_zone, url))
        st.result = _n(len(results), "result")
    if results:  # don't cache a miss: the next lookup should ask again
        _store("serp", key, results)
    return results


def _parse_serp(text: str) -> list[dict]:
    results: list[dict] = []
    try:
        data = json.loads(text)
        for o in data.get("organic") or data.get("news") or []:
            results.append({"title": o.get("title") or "", "link": o.get("link") or o.get("url") or "",
                            "description": o.get("description") or o.get("snippet") or "",
                            "source": o.get("source"), "date": o.get("date")})
    except json.JSONDecodeError:
        # Zone returned HTML/markdown instead of parsed JSON: keep what links we can find.
        for link in dict.fromkeys(re.findall(r"https?://[^\s\"'<>)]+", text)):
            results.append({"title": "", "link": link, "description": ""})
    return results


async def fetch_markdown(url: str) -> str:
    if (hit := _cached("page", url)) is not None:
        hub.note("brightdata", "Web Unlocker", "Fetch page as markdown", url, "cached")
        return hit
    async with hub.step("brightdata", "Web Unlocker", "Fetch page as markdown", url) as st:
        if fixtures.active():
            text = await fixtures.page(url)
        else:
            text = await _request(settings().brightdata_unlocker_zone, url, data_format="markdown")
        st.result = f"{len(text) / 1024:.1f} KB markdown"
    _store("page", url, text)
    return text


async def linkedin_profile(url: str) -> dict | None:
    """One profile, synchronously (Bright Data waits up to ~1 min, then hands back a snapshot id)."""
    if (hit := _cached("li", url)) is not None:
        hub.note("brightdata", "Scraper API", "LinkedIn profile", url, "cached")
        return hit or None
    async with hub.step("brightdata", "Scraper API", "LinkedIn profile", url) as st:
        if fixtures.active():
            profile = await fixtures.linkedin(url)
        else:
            async with _client(timeout=90) as c:
                r = await c.post("/datasets/v3/scrape",
                                 params={"dataset_id": LINKEDIN_PROFILES, "format": "json"}, json=[{"url": url}])
                if r.status_code == 202:
                    snap = r.json().get("snapshot_id")
                    data = await _wait_snapshot(c, snap, max_wait=150)
                elif r.status_code >= 400:
                    raise BrightDataError(f"scrape -> {r.status_code}: {r.text[:200]}")
                else:
                    data = r.json()
            profile = _first_profile(data)
        st.result = _profile_summary(profile)
    _store("li", url, profile or {})
    return profile


async def linkedin_company(url: str) -> dict | None:
    """A LinkedIn company page (about, industry, size, recent updates) via the Scraper API."""
    url = url.split("?")[0].rstrip("/")
    if (hit := _cached("li_company", url)) is not None:
        hub.note("brightdata", "Scraper API", "LinkedIn company page", url, "cached")
        return hit or None
    async with hub.step("brightdata", "Scraper API", "LinkedIn company page", url) as st:
        if fixtures.active():
            company = await fixtures.linkedin(url)
        else:
            async with _client(timeout=90) as c:
                r = await c.post("/datasets/v3/scrape",
                                 params={"dataset_id": LINKEDIN_COMPANIES, "format": "json"}, json=[{"url": url}])
                if r.status_code == 202:
                    data = await _wait_snapshot(c, r.json().get("snapshot_id"), max_wait=150)
                elif r.status_code >= 400:
                    raise BrightDataError(f"company scrape -> {r.status_code}: {r.text[:200]}")
                else:
                    data = r.json()
            company = _first_profile(data)
        n = len((company or {}).get("updates") or [])
        st.result = ("1 company" + (f", {_n(n, 'update')}" if n else "")) if company else "no page"
    _store("li_company", url, company or {})
    return company


def trim_company(c: dict, max_chars: int = 3500) -> str:
    keep = ("name", "about", "industries", "company_size", "employees_in_linkedin", "headquarters", "founded",
            "specialties", "website", "funding", "investors", "stock_info", "slogan")
    slim = {k: c[k] for k in keep if c.get(k)}
    if isinstance(slim.get("about"), str):
        slim["about"] = slim["about"][:900]
    ups = c.get("updates") or []
    if isinstance(ups, list):
        slim["updates"] = [{k: v for k, v in u.items() if k in ("text", "title", "date", "post_url")}
                           for u in ups[:5] if isinstance(u, dict)]
    return json.dumps(slim, ensure_ascii=False)[:max_chars]


def _profile_summary(p: dict | None) -> str:
    if not p:
        return "no profile"
    posts = len(p.get("posts") or []) + len(p.get("activity") or [])
    return "1 profile" + (f", {_n(posts, 'post')}" if posts else "")


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
    async with hub.step("brightdata", "Scraper API", "LinkedIn profiles (batch)", f"{len(todo)} profile URLs") as st:
        if fixtures.active():
            data = [p | {"input": {"url": u}} for u, p in (await fixtures.linkedin_batch(todo)).items()]
        else:
            async with _client(timeout=120) as c:
                r = await c.post("/datasets/v3/trigger",
                                 params={"dataset_id": LINKEDIN_PROFILES, "format": "json", "include_errors": "true"},
                                 json=[{"url": u} for u in todo])
                if r.status_code >= 400:
                    raise BrightDataError(f"trigger -> {r.status_code}: {r.text[:200]}")
                data = await _wait_snapshot(c, r.json()["snapshot_id"], max_wait=max_wait)
        st.result = _n(len(data) if isinstance(data, list) else 0, "profile")
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
        if status in ("failed", "canceled"):
            raise BrightDataError(f"snapshot {snap} {status}")
        # back off on 429: Bright Data blocks IPs that collect 25 of them within 5 minutes
        wait = 5
        if r.status_code == 429:
            try:
                wait = max(5, int(r.headers.get("retry-after", "10")))
            except ValueError:
                wait = 10
        await asyncio.sleep(wait)
        waited += wait
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
