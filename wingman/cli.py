"""Command line: uv run python -m wingman.cli <command>

  prep            import-event + me + enrich + rank (run the night before)
  import-event    load speakers and sessions for WINGMAN_EVENT
  me              enrich your own profile from config/me.yaml (+ LinkedIn if set)
  enrich          enrich speakers (Bright Data LinkedIn batch + GitHub + Claude)  [--limit N] [--no-scrape]
  rank            build the "worth meeting" shortlist                              [--limit N]
  card ID         print a person's card
  ask "question"  ask the graph
  recap           summarize a day's conversations                                 [--date YYYY-MM-DD]
  serve           run the API + web app                                           [--host] [--port]
  vapid           generate web-push keys to paste into .env
  demo [SCRIPT]   play a scripted conversation into a running server (maya | reset)
                                                                                  [--speed 2] [--url URL]
"""

import argparse
import asyncio
import json
import logging

from . import cards, enrich, events, graph
from .config import event_config


async def _prep(args) -> None:
    print("import:", await events.import_event(event_config()))
    print("me:", await enrich.enrich_me())
    print("enrich:", await enrich.enrich_speakers(limit=args.limit, scrape=not args.no_scrape))
    print("shortlist:", await cards.rank_shortlist())


def main() -> None:
    ap = argparse.ArgumentParser(prog="wingman", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-scrape", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--speed", type=float, default=1.0, help="demo playback speed")
    ap.add_argument("--url", help="demo: server URL (default http://127.0.0.1:PORT)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    c = args.command
    if c == "serve":
        import uvicorn
        uvicorn.run("wingman.api:app", host=args.host, port=args.port, proxy_headers=True,
                    forwarded_allow_ips="*")
        return
    if c == "vapid":
        _vapid()
        return
    if c == "demo":
        _demo(args)
        return

    async def run():
        await graph.ensure_schema()
        if c == "prep":
            await _prep(args)
        elif c == "import-event":
            print(await events.import_event(event_config()))
        elif c == "me":
            print(await enrich.enrich_me())
        elif c == "enrich":
            print(await enrich.enrich_speakers(limit=args.limit, scrape=not args.no_scrape))
        elif c == "rank":
            print(await cards.rank_shortlist(limit=args.limit or 30))
            for s in await cards.shortlist(10):
                print(f"{s['score']:.2f}  {s['person']['name']} ({s['person']['company']}): {s['why']}")
        elif c == "card":
            print(json.dumps(await cards.build_card(args.arg), indent=2))
        elif c == "ask":
            from .api import Ask, ask
            print((await ask(Ask(question=args.arg)))["answer"])
        elif c == "recap":
            from . import recap
            print("encounters:", await recap.run(args.date))
            print(json.dumps(await recap.get(args.date), indent=2, default=str))
        else:
            ap.error(f"unknown command {c}")

    asyncio.run(run())


def _demo(args) -> None:
    """Play a demo script on a running server (so the phone sees it); fall back to in-process if none."""
    import os
    import time

    import httpx

    url = (args.url or f"http://127.0.0.1:{args.port}").rstrip("/")
    script = args.arg or "maya"
    try:
        with httpx.Client(base_url=url, timeout=10) as http:
            if tok := os.environ.get("WINGMAN_TOKEN"):
                http.post("/api/login", json={"token": tok})
            if script == "reset":
                print(http.post("/api/demo/reset").json())
                return
            r = http.post("/api/demo/play", json={"script": script, "speed": args.speed})
            print(r.json())
            return
    except httpx.ConnectError:
        print(f"no server at {url}; playing in-process (events print here, not on the phone)")

    async def run():
        from . import demo, hub

        await graph.ensure_schema()
        qu = hub.subscribe()
        await demo.play(demo.PlayIn(script=script, speed=args.speed))
        deadline = time.monotonic() + sum(w for _, _, w in demo.SCRIPTS[script]["lines"]) / args.speed + 45
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(qu.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            event, data = msg.split("\n", 1)
            print(event.removeprefix("event: "), data.removeprefix("data: ")[:300].strip())

    asyncio.run(run())


def _vapid() -> None:
    import base64

    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    v = Vapid01()
    v.generate_keys()
    priv = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    print(f"VAPID_PRIVATE_KEY={b64(priv)}\nVAPID_PUBLIC_KEY={b64(pub)}")


if __name__ == "__main__":
    main()
