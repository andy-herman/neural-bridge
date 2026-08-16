#!/usr/bin/env python3
"""Luna's calendar CLI. Read-only.

    python -m scripts.luna.calendar today
    python -m scripts.luna.calendar week [--days 7]
    python -m scripts.luna.calendar next [--count 3]
    python -m scripts.luna.calendar conflicts [--days 7]

Read-only on purpose. Luna's allowlist previously granted `delete_event` with
nothing in her charter authorizing deletion, which the 2026-08 audit flagged.
Creating and moving events is a separate decision from reading them, and it can
be added later behind its own scope and its own confirmation. Deleting is not
on the roadmap: an assistant that can silently drop a meeting is a liability.

Output is plain text shaped for an agent to quote into a message, not JSON, so
Luna does not have to parse anything to say "your 2pm has no agenda".

Exit codes: 0 fine, 1 API or auth failure, 2 not configured.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.luna.google_auth import (  # noqa: E402
    GoogleAuthError,
    api_get,
    config_status,
    is_configured,
)

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MAX_RESULTS = 50


@dataclass(frozen=True)
class Event:
    summary: str
    start: datetime | None
    end: datetime | None
    all_day: bool
    location: str
    attendees: int
    has_agenda: bool          # a description, which is the usual agenda carrier
    organizer_is_me: bool

    def duration_minutes(self) -> int | None:
        if not self.start or not self.end:
            return None
        return int((self.end - self.start).total_seconds() // 60)


def _parse_dt(node: dict) -> tuple[datetime | None, bool]:
    """Google gives dateTime for timed events and date for all-day ones."""
    if not node:
        return None, False
    if "dateTime" in node:
        raw = node["dateTime"].replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw), False
        except ValueError:
            return None, False
    if "date" in node:
        try:
            d = date.fromisoformat(node["date"])
            return datetime.combine(d, time.min, tzinfo=timezone.utc), True
        except ValueError:
            return None, True
    return None, False


def parse_event(item: dict) -> Event:
    start, all_day = _parse_dt(item.get("start", {}))
    end, _ = _parse_dt(item.get("end", {}))
    attendees = item.get("attendees") or []
    organizer = item.get("organizer") or {}
    return Event(
        summary=(item.get("summary") or "(no title)").strip(),
        start=start,
        end=end,
        all_day=all_day,
        location=(item.get("location") or "").strip(),
        attendees=len([a for a in attendees if not a.get("resource")]),
        has_agenda=bool((item.get("description") or "").strip()),
        organizer_is_me=bool(organizer.get("self")),
    )


def overlaps(a: Event, b: Event) -> bool:
    """True when two timed events collide. All-day events never conflict."""
    if a.all_day or b.all_day:
        return False
    if not (a.start and a.end and b.start and b.end):
        return False
    return a.start < b.end and b.start < a.end


# Tokens that survive translation: latin letters and digits. Andy's calendar
# carries the same flight twice, once in English and once in Korean, so a
# similarity check over full titles would score them as unrelated.
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DUP_TOKEN_RATIO = 0.6


def _title_tokens(summary: str) -> set[str]:
    return set(_TOKEN_RE.findall(summary.lower()))


def looks_duplicate(a: Event, b: Event) -> bool:
    """True when two events are the same thing entered twice, not a clash.

    "Flight to Seattle (KE 41)" and "Flight to 시애틀 (KE 41)" occupy identical
    minutes and are one flight. Reporting that as an overlap is a false alarm,
    and an assistant whose first real conflict report is wrong does not get
    believed on the second.

    Requires identical start AND end, then either a shared token containing a
    digit (flight number, room, course code: the parts that do not get
    translated) or a high share of common latin tokens. Two genuinely different
    meetings booked over each other have neither, so they stay conflicts.
    """
    if a.start != b.start or a.end != b.end:
        return False
    ta, tb = _title_tokens(a.summary), _title_tokens(b.summary)
    if not ta or not tb:
        # Identical slot, nothing comparable in the titles: treat as duplicate
        # rather than invent a conflict.
        return True
    shared = ta & tb
    if any(any(ch.isdigit() for ch in tok) for tok in shared):
        return True
    return len(shared) / min(len(ta), len(tb)) >= _DUP_TOKEN_RATIO


def find_duplicates(events: list[Event]) -> list[tuple[Event, Event]]:
    """Same event entered more than once. Worth telling Andy about; it means
    his calendar is lying about how full he is."""
    ordered = sorted([e for e in events if e.start and not e.all_day],
                     key=lambda e: e.start)
    out = []
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if second.start > first.start:
                break
            if looks_duplicate(first, second):
                out.append((first, second))
    return out


def find_conflicts(events: list[Event]) -> list[tuple[Event, Event]]:
    """Genuine overlaps. Duplicates are excluded; see looks_duplicate."""
    ordered = sorted([e for e in events if e.start and not e.all_day],
                     key=lambda e: e.start)
    out = []
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if second.start >= first.end:
                break
            if overlaps(first, second) and not looks_duplicate(first, second):
                out.append((first, second))
    return out


def fetch_events(start: datetime, end: datetime) -> list[Event]:
    payload = api_get(EVENTS_URL, {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(MAX_RESULTS),
    })
    return [parse_event(i) for i in payload.get("items", [])]


def _fmt_time(e: Event) -> str:
    if e.all_day:
        return "all day"
    if not e.start:
        return "??"
    s = e.start.astimezone().strftime("%H:%M")
    if e.end:
        return f"{s}-{e.end.astimezone().strftime('%H:%M')}"
    return s


def format_events(events: list[Event], header: str) -> str:
    if not events:
        return f"{header}: nothing scheduled."
    lines = [f"{header} ({len(events)}):"]
    for e in events:
        bits = [_fmt_time(e), e.summary]
        extra = []
        if e.attendees > 1:
            extra.append(f"{e.attendees} attendees")
        if e.location:
            extra.append(e.location)
        if e.attendees > 1 and not e.has_agenda:
            # The single most useful thing an assistant flags about a meeting.
            extra.append("NO AGENDA")
        lines.append("- " + "  ".join(bits) + (f"  ({', '.join(extra)})" if extra else ""))
    return "\n".join(lines)


def format_conflicts(pairs: list[tuple[Event, Event]],
                     dupes: list[tuple[Event, Event]] | None = None) -> str:
    lines: list[str] = []
    if pairs:
        lines.append(f"OVERLAPS ({len(pairs)}):")
        for a, b in pairs:
            lines.append(f"- {_fmt_time(a)} {a.summary}  vs  {_fmt_time(b)} {b.summary}")
    else:
        lines.append("No overlapping meetings.")
    if dupes:
        # Reported, not hidden: a duplicated event makes the calendar look
        # busier than the day actually is.
        lines.append(f"\nDUPLICATES ({len(dupes)}), same thing entered twice:")
        for a, b in dupes:
            lines.append(f"- {_fmt_time(a)} {a.summary}  ==  {b.summary}")
    return "\n".join(lines)


def _window(days: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = datetime.combine(now.astimezone().date(), time.min).astimezone()
    return start, start + timedelta(days=days)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="luna-calendar", description="Luna's read-only calendar")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("today")
    w = sub.add_parser("week"); w.add_argument("--days", type=int, default=7)
    n = sub.add_parser("next"); n.add_argument("--count", type=int, default=3)
    c = sub.add_parser("conflicts"); c.add_argument("--days", type=int, default=7)
    args = p.parse_args(argv)

    if not is_configured():
        # Explicit and actionable: Luna should be able to tell Andy WHY she
        # cannot see the calendar rather than inventing an answer.
        print(f"CALENDAR_UNAVAILABLE: {config_status()}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "today":
            start, end = _window(1)
            print(format_events(fetch_events(start, end), "Today"))
        elif args.cmd == "week":
            start, end = _window(args.days)
            print(format_events(fetch_events(start, end), f"Next {args.days} days"))
        elif args.cmd == "next":
            start, end = _window(14)
            events = [e for e in fetch_events(start, end)
                      if e.start and e.start >= datetime.now(timezone.utc)]
            print(format_events(events[: args.count], f"Next {args.count}"))
        else:
            start, end = _window(args.days)
            evs = fetch_events(start, end)
            print(format_conflicts(find_conflicts(evs), find_duplicates(evs)))
    except GoogleAuthError as exc:
        print(f"CALENDAR_ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
