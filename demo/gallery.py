"""Capture every phone-app state at iPhone size, for design review. See demo/README.md.

    uv run --with playwright python demo/gallery.py [--out demo/out/gallery] [--device "iPhone 18 Pro"] [--base URL]

Needs a demo server already running (default port 8077) in fixture mode (the synthetic world), for example:

    WINGMAN_FIXTURES=demo/fixtures GRAPH_NAME=wingman_demo DATA_DIR=demo/.data WINGMAN_ME_FILE=demo/me.yaml \\
      WINGMAN_TOKEN= uv run python -m wingman.cli serve --port 8077

Drives the app through the happy path (seed, shortlist, badge, pick, cards, listening, note, recap) and saves a
viewport screenshot of each state, plus full-page captures of the long screens.
"""

import argparse
import asyncio
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8077"

# Playwright has no descriptor for newer iPhones: 1206×2622 at 3× = 402×874 CSS px (6.3" display)
CUSTOM_DEVICES = {"iPhone 18 Pro": {"width": 402, "height": 874, "base": "iPhone 15 Pro"}}


def device_options(pw, name: str) -> dict:
    if name in CUSTOM_DEVICES:
        c = CUSTOM_DEVICES[name]
        return pw.devices[c["base"]] | {"viewport": {"width": c["width"], "height": c["height"]},
                                         "screen": {"width": c["width"], "height": c["height"]}}
    return pw.devices[name]


async def main(out: Path, device: str, badge: Path):
    out.mkdir(parents=True, exist_ok=True)
    http = httpx.AsyncClient(base_url=BASE, timeout=120)
    print("seed:", (await http.post("/api/demo/seed")).json())
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        bctx = await browser.new_context(viewport={"width": 900, "height": 1200})
        bp = await bctx.new_page()
        await bp.goto(f"{BASE}/stage/badge.html")
        await bp.evaluate("document.fonts.ready")
        await bp.screenshot(path=str(badge), type="jpeg", quality=90)
        await bctx.close()

        ctx = await browser.new_context(**device_options(pw, device))
        page = await ctx.new_page()
        n = 0

        async def shot(name: str, full: bool = False):
            nonlocal n
            n += 1
            await asyncio.sleep(0.35)
            await page.screenshot(path=str(out / f"{n:02d}-{name}.png"), full_page=full)
            print("shot", n, name)

        async def until(js: str, timeout: float = 60):
            t0 = time.monotonic()
            while not await page.evaluate(js):
                if time.monotonic() - t0 > timeout:
                    raise TimeoutError(js)
                await asyncio.sleep(0.1)

        await page.goto(f"{BASE}/")
        await until("document.querySelector('#event-name').textContent.length > 0")
        await shot("now-empty")

        await page.tap('#tabs [data-tab="people"]')
        await until("document.querySelectorAll('#people-shortlist .item').length >= 5")
        await shot("people-shortlist")
        await shot("people-shortlist-full", full=True)

        await page.tap("#people-shortlist .item")
        await until("!!document.querySelector('#card-view .card h2.card-name')")
        await shot("card-speaker")
        await page.tap('[data-action="back"]')
        await asyncio.sleep(0.4)

        # who is this? a name alone matches three people -> pick screen
        await page.tap('#tabs [data-tab="now"]')
        await page.tap('[data-action="toggle-lookup"]')
        await shot("now-lookup-panel")
        await page.fill("#lookup-name", "Maya Okafor")
        await page.tap('#lookup-panel button[type="submit"]')
        await until("!!document.querySelector('.cand-list')")
        await shot("now-pick")
        await page.tap('[data-action="none-of-these"]')
        await asyncio.sleep(1.5)
        await shot("now-no-match")

        # badge photo -> quick card -> full card
        async with page.expect_file_chooser() as fc:
            await page.tap('[data-action="photo"]')
        await (await fc.value).set_files(str(badge))
        await asyncio.sleep(0.4)
        await shot("now-reading-badge")
        await until("!!document.querySelector('#now-encounter .card .enriching')")
        await shot("now-quick-card")
        await until("!!document.querySelector('#now-encounter details.sources') && "
                    "!document.querySelector('#now-encounter .enriching')", timeout=90)
        await shot("now-full-card")
        await page.tap("#now-encounter details.sources summary")
        await shot("now-full-card-full", full=True)

        # listening with a nudge
        await page.evaluate("window.scrollTo(0, 0)")
        await page.tap("#listen-toggle")
        await until("[...document.querySelectorAll('.toast')].some((t) => t.classList.contains('nudge'))", timeout=90)
        await shot("now-listening-nudge")
        await asyncio.sleep(12)
        await shot("now-listening-later")

        # a voice note
        await page.tap('[data-action="toggle-note"]')
        await asyncio.sleep(0.6)
        await page.fill("#note-text", "Maya, Tessellate: wants a two-week pilot. Send deck and pricing tomorrow.")
        await shot("now-note-panel")
        await page.tap('#note-panel button[type="submit"]')
        await asyncio.sleep(1.5)
        await shot("now-note-saved")

        # recap
        await page.tap('#tabs [data-tab="recap"]')
        await asyncio.sleep(0.8)
        await shot("recap-before-run")
        await page.tap("#recap-run")
        await until("document.querySelectorAll('#recap-body .ai').length > 0", timeout=90)
        await shot("recap")
        await shot("recap-full", full=True)

        await page.tap('#recap-body [data-action="open-person"]')
        await until("!!document.querySelector('#card-view .card h2.card-name')")
        await shot("card-from-recap", full=True)

        await page.tap('#tabs [data-tab="settings"]')
        await asyncio.sleep(0.5)
        await shot("settings")
        await shot("settings-full", full=True)

        # offline banner and the login overlay (forced visible; the demo server runs without a token)
        await page.tap('#tabs [data-tab="now"]')
        await ctx.set_offline(True)
        await page.evaluate("window.dispatchEvent(new Event('offline'))")
        await shot("now-offline")
        await ctx.set_offline(False)
        await page.evaluate("window.dispatchEvent(new Event('online'))")
        await page.evaluate("document.getElementById('login').hidden = false")
        await shot("login")
        await page.evaluate("document.getElementById('login').hidden = true")

        await browser.close()
    await http.aclose()
    print(f"wrote {n} screenshots to {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "demo" / "out" / "gallery"))
    ap.add_argument("--device", default="iPhone 18 Pro")
    ap.add_argument("--base", default=BASE, help="demo server URL")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    asyncio.run(main(Path(args.out), args.device, ROOT / "demo" / ".data" / "badge.jpg"))
