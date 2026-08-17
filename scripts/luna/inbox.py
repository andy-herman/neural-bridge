#!/usr/bin/env python3
"""Luna's inbox CLI. Read-only.

    python -m scripts.luna.inbox unread [--count 10]
    python -m scripts.luna.inbox search "from:regulator" [--count 10]
    python -m scripts.luna.inbox thread <thread_id>
    python -m scripts.luna.inbox waiting [--days 5]

THERE IS NO SEND COMMAND, AND THERE WILL NOT BE ONE.

Luna's charter is explicit that Gmail is draft-only and she never sends on
Andy's behalf. Drafting is also deliberately absent here for now, because
Google has no draft-without-send scope: `gmail.compose` grants both in a single
consent, so the safe default is `gmail.readonly` and no write path at all. If
drafting is added later it goes behind its own opt-in scope and its own
confirmation step, and sending stays a thing Andy does himself.

`waiting` is the command that earns this tool its place: threads where Andy
sent the last message and nobody has replied. That is the thing an executive
assistant tracks and a human forgets.

Exit codes: 0 fine, 1 API or auth failure, 2 not configured.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.luna.google_auth import (  # noqa: E402
    GoogleAuthError,
    api_get,
    config_status,
    is_configured,
)

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MESSAGES_URL = f"{BASE}/messages"
THREADS_URL = f"{BASE}/threads"
MAX_BODY_CHARS = 1500


@dataclass(frozen=True)
class Message:
    msg_id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    received: datetime | None
    unread: bool
    from_me: bool

    def age_days(self, now: datetime | None = None) -> int | None:
        if not self.received:
            return None
        return ((now or datetime.now(timezone.utc)) - self.received).days


def _header(payload: dict, name: str) -> str:
    for h in (payload.get("payload", {}) or {}).get("headers", []) or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def parse_message(raw: dict, me: str = "") -> Message:
    labels = raw.get("labelIds") or []
    sender = _header(raw, "From")
    received = None
    internal = raw.get("internalDate")
    if internal:
        try:
            received = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            received = None
    return Message(
        msg_id=raw.get("id", ""),
        thread_id=raw.get("threadId", ""),
        sender=sender,
        subject=_header(raw, "Subject") or "(no subject)",
        snippet=(raw.get("snippet") or "").strip(),
        received=received,
        unread="UNREAD" in labels,
        from_me="SENT" in labels or (bool(me) and me.lower() in sender.lower()),
    )


def _fetch_ids(query: str, count: int) -> list[str]:
    payload = api_get(MESSAGES_URL, {"q": query, "maxResults": str(count)})
    return [m["id"] for m in payload.get("messages", []) if m.get("id")]


def fetch_messages(query: str, count: int) -> list[Message]:
    """One metadata call per message. Gmail accepts repeated metadataHeaders,
    so From and Subject come back together rather than costing two round trips."""
    out = []
    for mid in _fetch_ids(query, count):
        raw = api_get(f"{MESSAGES_URL}/{mid}", {
            "format": "metadata",
            "metadataHeaders": ["From", "Subject", "Date"],
        })
        out.append(parse_message(raw))
    return out


def format_messages(messages: list[Message], header: str) -> str:
    if not messages:
        return f"{header}: nothing."
    lines = [f"{header} ({len(messages)}):"]
    for m in messages:
        age = m.age_days()
        when = f"{age}d ago" if age is not None else "?"
        who = m.sender.split("<")[0].strip().strip('"') or m.sender
        lines.append(f"- [{when}] {who}: {m.subject}")
        if m.snippet:
            lines.append(f"    {m.snippet[:140]}")
    return "\n".join(lines)


def find_waiting(days: int = 5, scan: int = 20) -> list[Message]:
    """Threads Andy spoke last in, older than `days`, still with no reply.

    The one command he will not think to ask for, so it is also the one worth
    surfacing unprompted. Costs one thread GET per candidate, which is why
    `scan` is tunable: the CLI can afford 20, a per-turn prefetch cannot.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    stalled: list[Message] = []
    for m in fetch_messages(f"in:sent before:{cutoff}", scan):
        thread = api_get(f"{THREADS_URL}/{m.thread_id}", {"format": "minimal"})
        msgs = thread.get("messages", [])
        if msgs and "SENT" in (msgs[-1].get("labelIds") or []):
            stalled.append(m)
    return stalled


def format_thread(messages: list[Message]) -> str:
    if not messages:
        return "Thread not found or empty."
    lines = [f"Thread ({len(messages)} messages):"]
    for m in messages:
        who = "Andy" if m.from_me else (m.sender.split("<")[0].strip().strip('"') or m.sender)
        stamp = m.received.astimezone().strftime("%Y-%m-%d %H:%M") if m.received else "?"
        lines.append(f"\n[{stamp}] {who}")
        lines.append(m.snippet[:MAX_BODY_CHARS] or "(no preview)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="luna-inbox", description="Luna's read-only inbox")
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("unread"); u.add_argument("--count", type=int, default=10)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--count", type=int, default=10)
    t = sub.add_parser("thread"); t.add_argument("thread_id")
    w = sub.add_parser("waiting"); w.add_argument("--days", type=int, default=5)
    args = p.parse_args(argv)

    if not is_configured():
        print(f"INBOX_UNAVAILABLE: {config_status()}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "unread":
            print(format_messages(fetch_messages("is:unread in:inbox", args.count),
                                  "Unread"))
        elif args.cmd == "search":
            print(format_messages(fetch_messages(args.query, args.count),
                                  f"Search: {args.query}"))
        elif args.cmd == "thread":
            payload = api_get(f"{THREADS_URL}/{args.thread_id}", {"format": "metadata"})
            msgs = [parse_message(m) for m in payload.get("messages", [])]
            print(format_thread(msgs))
        else:
            print(format_messages(find_waiting(args.days),
                                  f"Sent over {args.days}d ago, no reply"))
    except GoogleAuthError as exc:
        print(f"INBOX_ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
