#!/usr/bin/env python3
"""Luna's proactive Telegram check-ins.

    python -m scripts.telegram_bot.luna_checkin --kind morning [--dry-run]

Why this exists: Luna could only ever answer. Every other surface she has
requires Andy to start the conversation, and the Discord fleet went ten weeks
without a single mention because of exactly that. An assistant that never speaks
first is a tool you have to remember to pick up. Yor has three scheduled
check-ins a day and is the agent Andy actually talks to; this is that pattern.

Her primary context is Andy's commitment board (`Herman Tasks.md`), because
that is what an executive assistant actually watches. Fleet health is secondary
and only included when something is actionable: agent uptime is devops, and it
crowds out the commitments that matter to his day. On the day this was built the
board held two regulator-escalated items seventeen days overdue while Luna had
been silent for twelve weeks.

DESIGN NOTE, and it is the load-bearing one: `[PASS]` is a first-class outcome.
A check-in that fires every day regardless of whether anything happened becomes
noise, noise gets muted, and a muted assistant is strictly worse than no
assistant. The same lesson the memory canary taught: a signal that always fires
carries no information. Silence has to be cheap and normal for the messages that
do arrive to mean something.

Deliberately NOT the mention pipeline. That builds an 84,000 character
Discord-shaped prompt full of action protocols and SOPs that do not work on
Telegram. This is a small purpose-built prompt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from urllib import error, parse, request

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.discord_bot import memory_telemetry as mem  # noqa: E402
from scripts.discord_bot.claude_invoke import call_claude_sync, sanitize_untrusted_text  # noqa: E402
from scripts.discord_bot.keychain import get_token  # noqa: E402
from scripts.luna import tasks  # noqa: E402
from scripts.discord_bot.mention import (  # noqa: E402
    LUNA_NOTES_MAX_CHARS,
    LUNA_NOTES_PATH,
    budget_notes,
)

AGENT_ID = "luna"
KEYCHAIN_SERVICE = "neural-bridge-telegram-luna"
ALLOWED_USERS_ENV = "LUNA_TELEGRAM_ALLOWED_USERS"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "luna_checkin_v1.md"

VAULT_LUNA = Path.home() / "Documents" / "Luna Master" / "Agents" / "Luna"
BRIEFINGS_DIR = VAULT_LUNA / "Briefings"

PASS_TOKEN = "[PASS]"
MAX_TELEGRAM_CHARS = 3900
CHECKIN_TIMEOUT = 240
# Check-ins are short judgement calls over pre-gathered context, not research.
CHECKIN_EFFORT = "low"
# Notes carry her standing rules; the briefing is the day's state.
MAX_BRIEFING_CHARS = 4000

KIND_GUIDANCE = {
    "morning": (
        "It is the start of his day. Lead with anything past due or escalated, "
        "then what lands in the next couple of weeks that he has not started. "
        "Deadlines a regulator is chasing outrank everything else. If nothing "
        "is due and nothing is slipping, say nothing."
    ),
    "evening": (
        "His working day is ending. Look for what is still open that he "
        "probably thinks is handled, and anything past due that got no movement "
        "today. Do not recap his day back to him; he was there."
    ),
}


# ---------- context gathering (pure-ish, testable) ----------

def latest_briefing(briefings_dir: Path = BRIEFINGS_DIR) -> tuple[str, str]:
    """Return (filename, text) for today's briefing, or the newest available.

    Returns ("", "") when there is none. Prefers today so a stale briefing is
    never presented as current state.
    """
    if not briefings_dir.is_dir():
        return "", ""
    today = briefings_dir / f"{date.today().isoformat()}.md"
    target = today if today.exists() else None
    if target is None:
        try:
            candidates = sorted(briefings_dir.glob("*.md"))
        except OSError:
            return "", ""
        if not candidates:
            return "", ""
        target = candidates[-1]
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", ""
    if len(text) > MAX_BRIEFING_CHARS:
        text = text[:MAX_BRIEFING_CHARS].rstrip() + "\n[...truncated...]"
    return target.name, text


def gather_tasks(today: date | None = None) -> str:
    """Andy's commitment board, urgency-ordered. The primary EA context.

    This is what an executive assistant actually watches. The board carried two
    regulator-escalated items seventeen days overdue while Luna had been silent
    for twelve weeks, which is the gap this whole check-in exists to close.
    """
    now = today or date.today()
    board = tasks.load_board(today=now)
    if not board:
        mem.record(mem.RETRIEVE, "luna_tasks", agent_id=AGENT_ID,
                   ok=False, detail="board unreadable or empty")
        return ""
    rendered = tasks.format_for_prompt(board, now)
    mem.record(mem.RETRIEVE, "luna_tasks", agent_id=AGENT_ID, ok=True,
               chars=len(rendered),
               detail=f"{len(tasks.open_tasks(board))} open, "
                      f"{len(tasks.overdue(board, now))} past due")
    return f"### Andy's commitment board\n\n{rendered}"


def gather_fleet(briefings_dir: Path = BRIEFINGS_DIR) -> str:
    """Fleet health, secondary. Included only when something needs attention.

    Agent health is devops, not executive assistance. It belongs in a check-in
    only when it is actionable, otherwise it crowds out the commitments that
    actually matter to his day.
    """
    name, briefing = latest_briefing(briefings_dir)
    if not briefing:
        mem.record(mem.RETRIEVE, "luna_briefing", agent_id=AGENT_ID,
                   ok=False, detail="no briefing available")
        return ""
    mem.record(mem.RETRIEVE, "luna_briefing", agent_id=AGENT_ID,
               ok=True, chars=len(briefing), detail=name)
    needs_attention = ("ANOMALY" in briefing.upper()
                       or "failing" in briefing
                       or "Needs attention" in briefing)
    if not needs_attention:
        return ""
    stale = "" if name.startswith(date.today().isoformat()) else " (NOT today's; treat as stale)"
    fenced = sanitize_untrusted_text(briefing, "fleet-briefing")
    return (f"### Agent fleet, secondary ({name}{stale})\n\n"
            f"Mention this only if it is genuinely actionable today.\n\n"
            f"<fleet-briefing>\n{fenced}\n</fleet-briefing>")


def gather_context(briefings_dir: Path = BRIEFINGS_DIR, today: date | None = None) -> str:
    """Assemble what Luna knows right now, commitments first."""
    parts = [p for p in (gather_tasks(today), gather_fleet(briefings_dir)) if p]
    return "\n\n".join(parts)


def gather_notes() -> str:
    """Luna's standing notes, section-budgeted so her rules survive."""
    if not LUNA_NOTES_PATH.exists():
        return "_(no notes file yet)_"
    try:
        raw = LUNA_NOTES_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "_(notes unreadable)_"
    kept, _dropped = budget_notes(raw, LUNA_NOTES_MAX_CHARS)
    return sanitize_untrusted_text(kept, "luna-notes")


def build_checkin_prompt(kind: str, context: str, notes: str,
                         template: str | None = None) -> str:
    tpl = template if template is not None else PROMPT_PATH.read_text(encoding="utf-8")
    return (tpl
            .replace("{kind}", kind)
            .replace("{kind_guidance}", KIND_GUIDANCE.get(kind, ""))
            .replace("{context}", context or "_(nothing gathered)_")
            .replace("{notes}", notes))


# ---------- response handling (pure) ----------

def is_pass(response: str) -> bool:
    """True when Luna chose silence.

    Tolerant of the model wrapping the token in punctuation or backticks, and
    of an empty response, which is also silence.
    """
    stripped = (response or "").strip().strip("`*_ \n.")
    return not stripped or stripped.upper() == PASS_TOKEN.strip("[]").upper() \
        or stripped.upper() == PASS_TOKEN.upper()


def clean_for_telegram(response: str) -> str:
    text = (response or "").strip()
    if len(text) > MAX_TELEGRAM_CHARS:
        text = text[: MAX_TELEGRAM_CHARS - 1].rstrip() + "…"
    return text


# ---------- delivery ----------

def allowed_chat_ids() -> list[int]:
    raw = os.environ.get(ALLOWED_USERS_ENV, "").strip()
    return [int(t.strip()) for t in raw.split(",") if t.strip().isdigit()]


def send_telegram(chat_id: int, text: str, token: str, timeout: int = 15) -> bool:
    """Send one message as Luna's bot. Returns True on success. Never raises."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (error.HTTPError, error.URLError, TimeoutError, OSError):
        return False


# ---------- main ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Luna proactive Telegram check-in")
    parser.add_argument("--kind", choices=sorted(KIND_GUIDANCE), required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="build and generate, print, do not send")
    args = parser.parse_args(argv)

    context = gather_context()
    notes = gather_notes()
    prompt = build_checkin_prompt(args.kind, context, notes)

    ok, stdout, err = call_claude_sync(
        prompt, timeout=CHECKIN_TIMEOUT, effort=CHECKIN_EFFORT,
    )
    if not ok:
        print(f"check-in generation failed: {err}", file=sys.stderr)
        mem.record(mem.WRITE, "luna_checkin", agent_id=AGENT_ID, ok=False,
                   detail=f"{args.kind}: {err[:120]}")
        return 1

    if is_pass(stdout):
        # The normal, healthy outcome on a quiet day. Recorded as a success:
        # she ran and decided, which is different from failing to run.
        print(f"[{args.kind}] Luna passed (nothing worth sending).")
        if args.dry_run:
            # Calibration needs her reasoning, not just the verdict. Tuning the
            # silence bias blind is how you end up with an assistant that is
            # either mute or noisy with no way to tell which.
            print(f"--- raw response ---\n{(stdout or '').strip()[:600]}")
        mem.record(mem.WRITE, "luna_checkin", agent_id=AGENT_ID, ok=True,
                   chars=0, detail=f"{args.kind}: pass")
        return 0

    message = clean_for_telegram(stdout)
    if args.dry_run:
        print(f"--- [{args.kind}] would send ({len(message)} chars) ---\n{message}")
        return 0

    chat_ids = allowed_chat_ids()
    if not chat_ids:
        print(f"no {ALLOWED_USERS_ENV} configured; nothing to send to", file=sys.stderr)
        return 2
    token = get_token(KEYCHAIN_SERVICE)
    if not token:
        print(f"no Telegram token in keychain ({KEYCHAIN_SERVICE})", file=sys.stderr)
        return 2

    sent = sum(1 for cid in chat_ids if send_telegram(cid, message, token))
    mem.record(mem.WRITE, "luna_checkin", agent_id=AGENT_ID, ok=sent > 0,
               chars=len(message),
               detail=f"{args.kind}: sent to {sent}/{len(chat_ids)}")
    if sent == 0:
        print("check-in generated but delivery failed", file=sys.stderr)
        return 1
    print(f"[{args.kind}] sent to {sent} chat(s), {len(message)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
