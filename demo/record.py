"""Record the Wingman happy-path demo video. See demo/README.md.

    uv run --with playwright python demo/record.py [--out demo/out/wingman-demo.mp4] [--scale 2]

Synthetic people and companies; Bright Data and Claude responses are replayed from demo/fixtures
(no keys, deterministic).

Starts the server on a separate demo graph, opens demo/stage/ in headless Chrome (your installed Google Chrome,
no browser download), drives the phone app like a user would, and pipes a live screencast into ffmpeg.
Needs FalkorDB running (docker start wingman-falkordb) and ffmpeg on PATH.
"""

import argparse
import asyncio
import base64
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 8077
BASE = f"http://127.0.0.1:{PORT}"

VARIANTS = {
    "fixtures": {
        "env": {"WINGMAN_FIXTURES": "demo/fixtures", "GRAPH_NAME": "wingman_demo", "DATA_DIR": "demo/.data",
                "WINGMAN_ME_FILE": "demo/me.yaml", "WINGMAN_FIXTURE_SPEED": "1"},
        "badge": {"name": "Maya Okafor", "title": "Principal Engineer", "company": "Tessellate AI"},
        "night_before": True,
        "ff": 1,
        "find": ("Snap a badge. Bright Data finds who they are.",
                 "Business Search matches her name and company against Bright Data's people index, so a first "
                 "card is up in seconds."),
        "deeper": ("Then it digs deeper, in parallel",
                   "Her LinkedIn profile and posts through the Scraper API. Company news and her own blog through "
                   "the SERP API and Web Unlocker."),
        "cites": ("Every line cites where it came from",
                  "Claude writes the card only from facts in the FalkorDB graph. A line without a source is dropped."),
        "nudge": ("A nudge when it matters",
                  "She needs evals for 4,000 agent PRs a week. You're looking for design partners. Wingman connects "
                  "the two."),
        "note": "Maya, Tessellate: wants a two-week pilot. Send deck and pricing tomorrow. Ask Jordan for a warm "
                "intro to their CTO.",
        "outro": None,  # the stage's default lines
    },
}


# ---------- screencast -> ffmpeg ----------

class Recorder:
    """Chrome screencast frames, re-timed to a constant frame rate and encoded with x264.
    `skip` > 1 fast-forwards: only every Nth tick becomes a frame."""

    def __init__(self, page, out: Path, fps: int = 30, scale: int = 2):
        self.page, self.out, self.fps, self.scale = page, out, fps, scale
        self.latest: bytes | None = None
        self.frames = 0
        self.ticks = 0
        self.skip = 1
        self.q: queue.Queue = queue.Queue(maxsize=fps * 2)
        self.running = False

    async def start(self):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.ff = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(self.fps),
             "-i", "-", "-vf", "scale=1920:1080:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-preset", "slow",
             "-crf", "17", "-tune", "animation", "-movflags", "+faststart", str(self.out)],
            stdin=subprocess.PIPE)
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.writer.start()
        self.cdp = await self.page.context.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 1920 * self.scale,
                                                      "maxHeight": 1080 * self.scale, "everyNthFrame": 1})
        while self.latest is None:
            await asyncio.sleep(0.02)
        self.running = True
        self.pump = asyncio.create_task(self._pump())

    def _on_frame(self, params):
        self.latest = base64.b64decode(params["data"])
        asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))

    async def _pump(self):
        t = time.perf_counter()
        while self.running:
            self.ticks += 1
            if self.ticks % self.skip == 0:
                try:
                    self.q.put_nowait(self.latest)
                    self.frames += 1
                except queue.Full:
                    pass
            t += 1 / self.fps
            await asyncio.sleep(max(0.0, t - time.perf_counter()))

    def _write(self):
        while True:
            frame = self.q.get()
            if frame is None:
                break
            self.ff.stdin.write(frame)

    async def stop(self):
        self.running = False
        await self.pump
        await self.cdp.send("Page.stopScreencast")
        self.q.put(None)
        self.writer.join()
        self.ff.stdin.close()
        self.ff.wait()
        return self.frames / self.fps


# ---------- driving the stage and the phone ----------

class Demo:
    def __init__(self, page, rec: Recorder | None = None, ff: int = 1):
        self.page, self.rec, self.ff_factor = page, rec, ff
        self.app = page.frame_locator("#app")

    async def frame(self):
        return await (await self.page.query_selector("#app")).content_frame()

    async def stage(self, js: str, *args):
        return await self.page.evaluate(f"(a) => window.stage.{js}(...a)", list(args))

    async def caption(self, h: str, p: str):
        await self.stage("caption", h, p)

    async def fast(self, on: bool):
        """Fast-forward a long wait (when the variant sets ff > 1), with an on-screen marker."""
        if self.ff_factor <= 1:
            return
        await self.stage("ff", on, self.ff_factor)
        await asyncio.sleep(0.35 if on else 0)  # let the marker land before frames start skipping
        self.rec.skip = self.ff_factor if on else 1

    async def tap(self, selector: str, *, pause: float = 0.18):
        loc = self.app.locator(selector).first
        await loc.scroll_into_view_if_needed()
        box = await loc.bounding_box()
        await self.stage("tap", box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await asyncio.sleep(pause)
        await loc.click()

    async def scroll(self, y: str, ms: int = 1400):
        """Ease the phone screen to a scroll position (a JS expression, e.g. 'document.body.scrollHeight')."""
        f = await self.frame()
        await f.evaluate("""([target, ms]) => new Promise((done) => {
            const el = document.scrollingElement, from = el.scrollTop;
            let want = 0; try { want = eval(target) || 0; } catch (e) { want = from; }
            const to = Math.max(0, Math.min(want, el.scrollHeight - innerHeight));
            const t0 = performance.now(), ease = (t) => t < .5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3) / 2;
            const step = (now) => { const k = Math.min(1, (now - t0) / ms); el.scrollTop = from + (to - from) * ease(k);
                                    k < 1 ? requestAnimationFrame(step) : done(); };
            requestAnimationFrame(step); })""", [y, ms])

    async def flag(self, name: str) -> bool:
        return await self.page.evaluate(f"!!window.stage.flags.{name}")

    async def until(self, *flags: str, timeout: float = 60) -> str:
        t0 = time.monotonic()
        while True:
            for f in flags:
                if await self.flag(f):
                    return f
            if time.monotonic() - t0 > timeout:
                raise TimeoutError(" or ".join(flags))
            await asyncio.sleep(0.1)

    async def until_app(self, js: str, timeout: float = 30):
        f = await self.frame()
        t0 = time.monotonic()
        while not await f.evaluate(js):
            if time.monotonic() - t0 > timeout:
                raise TimeoutError(js)
            await asyncio.sleep(0.1)


async def run(v: dict, out: Path, scale: int, badge: Path):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True,
                                           args=["--autoplay-policy=no-user-gesture-required", "--hide-scrollbars"])
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=scale)

        # the badge photo
        bp = await ctx.new_page()
        await bp.set_viewport_size({"width": 900, "height": 1200})
        await bp.goto(f"{BASE}/stage/badge.html?{urllib.parse.urlencode(v['badge'])}")
        await bp.evaluate("document.fonts.ready")
        await bp.screenshot(path=str(badge), type="jpeg", quality=90, scale="css")
        await bp.close()

        page = await ctx.new_page()
        await page.goto(f"{BASE}/stage/")
        probe = Demo(page)
        await probe.until("fonts")
        await probe.until("sse")
        await probe.until("info")
        await probe.until_app("!!document.querySelector('#event-name') && "
                              "document.querySelector('#event-name').textContent.length > 0")
        await asyncio.sleep(1.0)

        rec = Recorder(page, out, scale=scale)
        await rec.start()
        d = Demo(page, rec, v["ff"])
        http = httpx.AsyncClient(base_url=BASE, timeout=120)

        # opening title
        await asyncio.sleep(3.6)
        await d.stage("start")
        await asyncio.sleep(0.9)

        if v["night_before"]:
            await d.stage("scene", "Last night", {"group": True})
            await d.caption("The night before, your shortlist is ready",
                            "Bright Data's Scraper API pulls every speaker's LinkedIn profile in one batch. "
                            "Claude ranks them against your goals.")
            await asyncio.sleep(1.4)
            await http.post("/api/demo/seed")
            await asyncio.sleep(0.6)
            await d.tap('#tabs [data-tab="people"]')
            await d.until_app("document.querySelectorAll('#people-shortlist .item').length >= 5")
            await asyncio.sleep(3.6)
            await d.stage("scene", "In the hallway")
            await d.caption(*v["find"])
            await d.tap('#tabs [data-tab="now"]')
            await asyncio.sleep(1.3)
        else:
            await d.stage("scene", "In the hallway")
            await d.caption(*v["find"])
            await asyncio.sleep(2.4)

        # badge photo -> who is this -> quick card -> full card
        async with page.expect_file_chooser() as fc:
            await d.tap('[data-action="photo"]')
        await (await fc.value).set_files(str(badge))
        await asyncio.sleep(2.5)
        await d.fast(True)
        await d.until_app("!!document.querySelector('#now-encounter .card .enriching') || "
                          "document.querySelector('#now-encounter').textContent.includes(\"Couldn't find a match\")",
                          timeout=240)
        if not await (await d.frame()).evaluate("!!document.querySelector('#now-encounter .card')"):
            raise RuntimeError("the badge lookup found no match (see the server log); try the take again")
        await d.fast(False)
        await asyncio.sleep(2.2)
        await d.caption(*v["deeper"])
        await asyncio.sleep(1.2)
        await d.fast(True)
        await d.until("cardFull", "enrichFailed", timeout=300)
        await d.fast(False)
        await asyncio.sleep(1.4)
        await d.caption(*v["cites"])
        await d.scroll("document.querySelector('#now-encounter .card .sources').getBoundingClientRect().bottom"
                       " + scrollY - innerHeight + 90", 2600)
        await asyncio.sleep(4.2)
        await d.scroll("0", 1400)
        await asyncio.sleep(0.6)

        # the conversation
        await d.stage("scene", "Listening", {"group": True})
        await d.caption("Listen mode follows the conversation",
                        "Ask first. Each line is saved to the encounter in the graph, and the model listens for "
                        "names, topics and your goals.")
        await asyncio.sleep(1.0)
        await d.tap("#listen-toggle")
        if await d.until("nudge", "demoDone", timeout=120) == "nudge":
            await asyncio.sleep(0.8)
            await d.caption(*v["nudge"])
            await d.until("demoDone", timeout=120)
        await asyncio.sleep(2.0)

        # a voice note
        await d.stage("scene", "Voice note")
        await d.caption("Dictate a note with Wispr Flow",
                        "Wispr types into the note box, and the note goes to the person you're talking to.")
        await d.tap('[data-action="toggle-note"]')
        await asyncio.sleep(0.9)
        await d.stage("dictation", True)
        await asyncio.sleep(2.4)
        await d.app.locator("#note-text").fill(v["note"])
        await d.stage("dictation", False)
        await asyncio.sleep(1.4)
        await d.tap('#note-panel button[type="submit"]')
        await d.until_app("[...document.querySelectorAll('.toast')].some((t) => t.textContent.includes('Note saved'))",
                          timeout=60)
        await asyncio.sleep(1.6)

        # that evening
        await d.stage("scene", "That evening")
        await d.caption("That evening: who you met and what you promised",
                        "Every promise comes with the exact quote, plus a follow-up draft ready to send.")
        await d.tap('#tabs [data-tab="recap"]')
        await asyncio.sleep(1.2)
        await d.tap("#recap-run")
        await asyncio.sleep(1.5)
        await d.fast(True)
        await d.until_app("document.querySelectorAll('#recap-body .ai').length > 0 || "
                          "!!document.querySelector('#recap-body .draft')", timeout=240)
        await d.fast(False)
        await asyncio.sleep(3.0)
        await d.scroll("document.querySelector('#recap-body .draft').getBoundingClientRect().bottom"
                       " + scrollY - innerHeight + 110", 2600)
        await asyncio.sleep(4.0)

        # closing summary, with their card on the phone
        await d.tap('#tabs [data-tab="card"]')
        await asyncio.sleep(0.5)
        await d.stage("outro", {"lines": v["outro"]} if v["outro"] else {})
        await asyncio.sleep(1.5)
        await d.scroll("document.querySelector('#card-view .card .sec:nth-of-type(3)')"
                       ".getBoundingClientRect().top + scrollY - 70", 2400)
        await asyncio.sleep(6.5)

        secs = await rec.stop()
        await http.aclose()
        await browser.close()
        print(f"wrote {out} ({secs:.1f} s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--scale", type=int, default=2, help="device pixel ratio while rendering (2 = crisper text)")
    ap.add_argument("--no-server", action="store_true", help="use a server already running on port 8077")
    args = ap.parse_args()
    v = VARIANTS["fixtures"]
    out = Path(args.out or ROOT / "demo" / "out" / "wingman-demo.mp4")
    data = ROOT / v["env"]["DATA_DIR"]
    data.mkdir(parents=True, exist_ok=True)

    server = None
    if not args.no_server:
        log = open(data / "server.log", "w")
        env = {k: x for k, x in os.environ.items() if k not in ("WINGMAN_FIXTURES", "WINGMAN_DEMO_SCRIPT")}
        server = subprocess.Popen([sys.executable, "-m", "wingman.cli", "serve", "--port", str(PORT)], cwd=ROOT,
                                  env=env | v["env"] | {"WINGMAN_TOKEN": ""}, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{BASE}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            sys.exit("server did not start (is FalkorDB running?)")
        r = httpx.post(f"{BASE}/api/demo/seed", timeout=300)  # a clean demo graph, so the app opens ready
        print("seed:", r.json())
        asyncio.run(run(v, out, args.scale, data / "badge.jpg"))
    finally:
        if server:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
