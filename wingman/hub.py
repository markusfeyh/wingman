"""Fan-out of server-sent events to open app tabs, with web push as the fallback when none are open."""

import asyncio
import json
import logging

from .config import settings

log = logging.getLogger("wingman.hub")

_subscribers: set[asyncio.Queue] = set()
_tasks: set[asyncio.Task] = set()


def subscribe() -> asyncio.Queue:
    qu: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.add(qu)
    return qu


def unsubscribe(qu: asyncio.Queue) -> None:
    _subscribers.discard(qu)


def publish(event: str, data: dict, push_text: str | None = None) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    for qu in list(_subscribers):
        try:
            qu.put_nowait(msg)
        except asyncio.QueueFull:
            pass
    if push_text and not _subscribers:
        spawn(asyncio.to_thread(_push_all, push_text))


def spawn(coro) -> asyncio.Task:
    """Fire-and-forget with a strong reference and logged errors."""
    t = asyncio.ensure_future(coro)
    _tasks.add(t)

    def done(task: asyncio.Task):
        _tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())

    t.add_done_callback(done)
    return t


# ---------- web push ----------

def _subs_file():
    return settings().data_dir / "push_subscriptions.json"


def add_push_subscription(sub: dict) -> None:
    f = _subs_file()
    subs = json.loads(f.read_text()) if f.exists() else []
    if sub not in subs:
        subs.append(sub)
    f.write_text(json.dumps(subs))


def _push_all(text: str) -> None:
    s = settings()
    f = _subs_file()
    if not (s.vapid_private_key and f.exists()):
        return
    from pywebpush import WebPushException, webpush
    keep = []
    for sub in json.loads(f.read_text()):
        try:
            webpush(subscription_info=sub, data=json.dumps({"title": "Wingman", "body": text}),
                    vapid_private_key=s.vapid_private_key, vapid_claims={"sub": s.vapid_subject})
            keep.append(sub)
        except WebPushException as e:
            if e.response is None or e.response.status_code not in (404, 410):
                keep.append(sub)
            log.warning("push failed: %s", e)
    f.write_text(json.dumps(keep))
