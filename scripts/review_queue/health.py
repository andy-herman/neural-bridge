"""Is the review queue actually working? A read-back check, not an error count.

The lesson this fleet keeps relearning is that a dead path and a quiet one
look the same. A queue that silently stops pushing would reproduce the
dormancy it exists to fix, at a new address. So this asserts on success:

  1. read-back   write a probe item, read it back, decide it, and confirm the
                 event log recorded both. Proves the store works end to end.
  2. pusher      Luna's bridge stamps a heartbeat every loop. Stale means
                 nothing is being pushed, whatever the queue holds.
  3. backlog     a pushable item still unsent well after it arrived means the
                 pusher is alive but failing to deliver.
  4. appliers    failed or stuck applies are named, never left to rot.

The memory canary runs this daily; the failure watcher reads the status file
the pusher writes and alerts out of band, because the queue cannot be trusted
to report its own death.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from . import store as st

HEARTBEAT_KEY = "pusher_heartbeat"
HEARTBEAT_MAX_AGE = 10 * 60
UNSENT_MAX_AGE = 15 * 60
APPLYING_MAX_AGE = 10 * 60

STATUS_ENV = "NB_REVIEW_QUEUE_STATUS"

# Which kinds Luna's bot pushes as cards. Alerts join after a clean shadow week
# (docs/REVIEW_QUEUE.md): until then the producers' own Telegram messages stay
# the push, so nothing arrives twice, and /queue already lists the alerts.
PUSH_KINDS_ENV = "NB_QUEUE_PUSH_KINDS"
DEFAULT_PUSH_KINDS: tuple[str, ...] = (st.CAPTURE,)


def push_kinds() -> tuple[str, ...]:
    raw = os.environ.get(PUSH_KINDS_ENV, "").strip()
    if not raw:
        return DEFAULT_PUSH_KINDS
    return tuple(k for k in (p.strip() for p in raw.split(",")) if k in st.KINDS)


def status_path() -> Path:
    raw = os.environ.get(STATUS_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return st.db_path().parent / "review-queue-status.json"


def write_status(store: st.Store, push_kinds: tuple[str, ...], now: int | None = None,
                 path: Path | None = None) -> dict:
    """The pusher's heartbeat, as a plain JSON file anyone can read without
    knowing the schema (the failure watcher runs on a different Python)."""
    now = now if now is not None else int(time.time())
    unsent = store.needing_render(push_kinds)
    status = {
        "heartbeat": now,
        "push_kinds": list(push_kinds),
        "oldest_unsent_age": (now - min(i.created_at for i in unsent)) if unsent else None,
        "apply_failed": len(store.items(states=(st.APPLY_FAILED,))),
        "waiting": len(store.items(states=st.WAITING)),
    }
    store.meta_set(HEARTBEAT_KEY, str(now))
    target = path or status_path()
    tmp = target.with_suffix(".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(status))
    os.replace(tmp, target)
    return status


def read_back(store: st.Store, now: int | None = None) -> str:
    """Take one probe item through the whole lifecycle (create, read back,
    decide, claim, apply). Returns "" when healthy, else what broke.

    The key is random, not just the time: the canary and a hand-run `health`
    in the same second once collided on `probe-<epoch>`, and the second
    check reported a healthy queue as broken.
    """
    now = now if now is not None else int(time.time())
    key = f"probe-{now}-{secrets.token_hex(4)}"
    item, created = store.raise_item(source="health", key=key, kind=st.PROBE,
                                     title="read-back probe", now=now)
    if not created:
        return "probe raise did not create an item"
    got = store.get(item.id)
    if got is None or got.title != "read-back probe":
        return "probe written but not read back"
    if not store.decide(item.id, st.ACKNOWLEDGE, actor="health", now=now):
        return "probe could not be decided"
    if not store.claim(item.id, actor="health", now=now):
        return "probe could not be claimed for apply"
    if not store.finish(item.id, True, "", actor="health", resolve=True, now=now):
        return "probe apply could not be recorded"
    events = [e["event"] for e in store.events(item.id)]
    if events != ["created", "decided", "applying", "applied"]:
        return f"event log incomplete for the probe: {events}"
    return ""


def check(store: st.Store, push_kinds: tuple[str, ...], now: int | None = None) -> dict:
    """{"ok": bool, "problems": [...], "facts": {...}}. Never raises."""
    now = now if now is not None else int(time.time())
    problems: list[str] = []
    facts: dict = {}
    try:
        broken = read_back(store, now)
        if broken:
            problems.append(f"read-back: {broken}")

        beat = store.meta_get(HEARTBEAT_KEY)
        facts["pusher_heartbeat_age"] = (now - int(beat)) if beat else None
        if beat is None:
            problems.append("pusher: no heartbeat ever recorded (is Luna's Telegram bridge running?)")
        elif now - int(beat) > HEARTBEAT_MAX_AGE:
            problems.append(f"pusher: last heartbeat {(now - int(beat)) // 60} min ago")

        unsent = [i for i in store.needing_render(push_kinds) if now - i.created_at > UNSENT_MAX_AGE]
        facts["unsent"] = len(unsent)
        if unsent:
            problems.append(f"backlog: {len(unsent)} item(s) never sent, oldest "
                            f"{(now - min(i.created_at for i in unsent)) // 60} min")

        failed = store.items(states=(st.APPLY_FAILED,))
        stuck = [i for i in store.items(states=(st.APPLYING,)) if now - i.updated_at > APPLYING_MAX_AGE]
        facts["apply_failed"] = len(failed)
        if failed:
            problems.append("appliers: failed " + ", ".join(f"{i.id} ({i.result[:60]})" for i in failed))
        if stuck:
            problems.append("appliers: stuck " + ", ".join(i.id for i in stuck))

        sync = store.meta_get("capture_sync") or ""
        if sync.startswith("error"):
            problems.append(f"captures: last quarantine sync failed ({sync.split(' ', 2)[-1][:120]})")

        waiting = store.items(states=st.WAITING)
        facts["waiting"] = len(waiting)
    except Exception as exc:  # the check itself failing is a finding, not a crash
        problems.append(f"check could not run: {type(exc).__name__}: {exc}")
    return {"ok": not problems, "problems": problems, "facts": facts}


def format_check(result: dict) -> str:
    head = "Review queue: ok" if result["ok"] else "Review queue: PROBLEMS"
    facts = ", ".join(f"{k}={v}" for k, v in result["facts"].items())
    lines = [f"{head} ({facts})" if facts else head]
    lines.extend(f"  - {p}" for p in result["problems"])
    return "\n".join(lines)
