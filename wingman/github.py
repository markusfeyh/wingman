"""Public GitHub profile + top repos. Optional GITHUB_TOKEN raises the rate limit from 60 to 5000/hour."""

import json

import httpx

from .brightdata import _cached, _store
from .config import settings


async def user_summary(handle: str) -> str | None:
    handle = (handle or "").strip().lstrip("@")
    if not handle or "/" in handle or " " in handle:
        return None
    if (hit := _cached("github", handle)) is not None:
        return hit or None
    headers = {"Accept": "application/vnd.github+json"}
    if tok := settings().github_token:
        headers["Authorization"] = f"Bearer {tok}"
    async with httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=20) as c:
        u = await c.get(f"/users/{handle}")
        if u.status_code == 404:
            _store("github", handle, "")
            return None
        if u.status_code != 200:
            return None  # rate limited etc.: don't cache, try again later
        r = await c.get(f"/users/{handle}/repos", params={"sort": "pushed", "per_page": 30, "type": "owner"})
    user = u.json()
    repos = r.json() if r.status_code == 200 else []
    repos = sorted((x for x in repos if not x.get("fork")), key=lambda x: x.get("stargazers_count", 0), reverse=True)[:6]
    summary = json.dumps({
        "login": user.get("login"), "name": user.get("name"), "bio": user.get("bio"), "company": user.get("company"),
        "location": user.get("location"), "blog": user.get("blog"), "followers": user.get("followers"),
        "top_repos": [{"name": x["name"], "description": x.get("description"), "language": x.get("language"),
                       "stars": x.get("stargazers_count"), "topics": x.get("topics", [])[:5],
                       "pushed_at": (x.get("pushed_at") or "")[:10], "url": x.get("html_url")} for x in repos],
    }, ensure_ascii=False)
    _store("github", handle, summary)
    return summary
