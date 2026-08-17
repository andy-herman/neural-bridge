"""Pre-fetch Luna's calendar and inbox into her prompt, with a short cache.

WHY THIS EXISTS

Luna has working read-only calendar and inbox CLIs and a Bash grant to run
them. She still told Andy, repeatedly and in fluent detail, that she could not
reach them in "this session". Both CLIs worked when run by hand seconds later.

That is a fabrication, and it is the expensive direction of the two: inventing
a meeting wastes a minute, while inventing a capability gap loses the task and
teaches him the assistant is less useful than it is. Three rounds of charter
wording moved it from always to sometimes and no further, which is the signal
that instructions were the wrong instrument. A model deciding whether it is
allowed to look is a decision that should not exist.

So the state arrives already fetched. She is not asked whether she can reach
the calendar; today's calendar is simply in front of her. The failure mode is
designed out rather than argued out.

WHAT THIS IS NOT

Not a replacement for the CLIs. She still runs them for anything beyond
today-and-soon: searches, specific threads, a different week. This is the
standing context an executive assistant is expected to have already, not the
whole capability.

FAILURE POLICY

Never raises, never blocks a turn. If Google is down or unconfigured the block
says so in words she can repeat to Andy, which is the honest version of the
sentence she was previously inventing.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

CACHE_PATH = Path.home() / ".cache" / "neural-bridge" / "luna-live-state.json"

# Conversation happens in bursts. A few minutes is long enough that a back and
# forth costs one fetch, short enough that "what's next" is not stale. Calendar
# changes Andy makes mid-conversation are the case this trades away, and he can
# always ask her to run the CLI, which bypasses this entirely.
CACHE_TTL_SECONDS = 300

# Kept small on purpose. This is standing context on every turn, so it competes
# with her notes for budget. Enough for today plus what is coming, not a dump.
MAX_BLOCK_CHARS = 1800
UPCOMING_DAYS = 7
WAITING_DAYS = 5
MAX_WAITING = 5


def _render(today_txt: str, upcoming_txt: str, waiting_txt: str) -> str:
    return (
        "## Andy's current state (fetched for you, already current)\n\n"
        "You did not have to ask for this and you do not need to verify it is\n"
        "available. It is here. Use it directly. For anything outside today and\n"
        "the next few days, run the CLIs as usual.\n\n"
        f"### Today\n{today_txt}\n\n"
        f"### Next {UPCOMING_DAYS} days\n{upcoming_txt}\n\n"
        f"### He is waiting on replies to these\n{waiting_txt}\n"
    )


def _fetch() -> dict[str, str]:
    """Call the same functions the CLIs call. Each part fails independently, so
    a broken inbox still leaves her with a calendar."""
    from scripts.luna import calendar as cal
    from scripts.luna import inbox as inb

    out: dict[str, str] = {}

    # Use the CLI's own window helper. Hand-rolling it with a naive datetime
    # produced an API 400, because Google wants RFC3339 with an offset.
    try:
        start, end = cal._window(1)
        out["today"] = cal.format_events(cal.fetch_events(start, end), "Today")
    except Exception as exc:
        out["today"] = f"_(calendar unavailable: {exc}. Tell Andy this verbatim.)_"

    try:
        start, end = cal._window(UPCOMING_DAYS)
        out["upcoming"] = cal.format_events(cal.fetch_events(start, end), "Upcoming")
    except Exception as exc:
        out["upcoming"] = f"_(calendar unavailable: {exc})_"

    try:
        # scan is deliberately small: find_waiting costs one thread GET per
        # candidate, and this runs on a conversational turn rather than a cron.
        stale = inb.find_waiting(WAITING_DAYS, scan=MAX_WAITING * 2)
        out["waiting"] = (inb.format_messages(stale[:MAX_WAITING], "Awaiting reply")
                          if stale else "Nothing sitting unanswered.")
    except Exception as exc:
        out["waiting"] = f"_(inbox unavailable: {type(exc).__name__}. Tell Andy this verbatim.)_"

    return out


def _read_cache(ttl: int) -> dict[str, str] | None:
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - float(raw.get("fetched_at", 0)) > ttl:
            return None
        parts = raw.get("parts")
        return parts if isinstance(parts, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write_cache(parts: dict[str, str]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"fetched_at": time.time(), "parts": parts}),
            encoding="utf-8",
        )
    except OSError:
        pass  # a cache we cannot write is a slow path, not an error


def live_state_block(*, ttl: int = CACHE_TTL_SECONDS,
                     max_chars: int = MAX_BLOCK_CHARS) -> str:
    """Return the block to prepend to Luna's prompt. Never raises, never blocks.

    Returns "" only if everything failed hard enough that there is nothing to
    say, which is different from a fetch that failed and has words for it.
    """
    parts = _read_cache(ttl)
    if parts is None:
        try:
            parts = _fetch()
        except Exception:
            return ""
        _write_cache(parts)

    block = _render(parts.get("today", ""), parts.get("upcoming", ""),
                    parts.get("waiting", ""))
    if len(block) > max_chars:
        # The marker is two characters, so the slice has to leave room for both
        # or the "budget" silently overshoots by one.
        block = block[: max_chars - 2].rstrip() + "…\n"
    return block
