"""Weekly auto-summarization of per-agent conversation logs.

For each agent that has any conversation logs from the past 7 days:

  1. Read all the relevant `Agents/<id>/conversations/YYYY-MM/*.md` files
  2. Concatenate them as the data block
  3. Run `claude -p` against Sonnet with the lessons-learned prompt
  4. Write the digest to `Agents/<id>/lessons-learned/YYYY-WW.md`
  5. Print a one-line summary per agent to stdout

The mention prompt's `_lessons_block(agent_id)` helper (in mention.py)
reads the most recent lessons-learned file and auto-injects it into
every mention prompt. So a digest written here becomes effective on
the NEXT mention to that agent.

Runs as a launchd cron — `com.andyherman.neural-bridge.summarize-weekly`,
Mondays at 04:00 local time. Idempotent: re-running the same week
overwrites the file with the new digest. Output to stdout/stderr goes
to `~/Library/Logs/neural-bridge/summarize-weekly.{stdout,stderr}.log`.

Stdlib only except for the existing project imports. No new deps.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Make project root importable so we can reuse the conversation_log paths.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.discord_bot.conversation_log import AGENTS_BASE  # noqa: E402

LOGS_DIR = Path.home() / "Library" / "Logs" / "neural-bridge"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "summarize_lessons_v1.md"
MODEL = "claude-sonnet-4-6"
CLAUDE_TIMEOUT_S = 600  # generous; per-agent summarization is bounded but model can be slow

# Cap on how much raw conversation text we send to claude. The model can handle
# a lot, but at some point we're paying tokens for low-signal scrollback. 200k
# chars is roughly a week of heavy Discord use for one agent; cut older if needed.
MAX_RAW_CONTENT_CHARS = 200_000


def _setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    return logging.getLogger("summarize_weekly")


def _iso_week(d: date) -> str:
    """Return ISO 8601 week label, e.g. `2026-W19`."""
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _discover_agents() -> list[str]:
    """All agent_ids that have ever logged a conversation. Discovered from
    the on-disk directory layout — keeps in sync without a registry."""
    if not AGENTS_BASE.exists():
        return []
    return sorted(p.name for p in AGENTS_BASE.iterdir() if (p / "conversations").is_dir())


def _gather_recent_turns(agent_id: str, cutoff: datetime) -> str:
    """Concatenate every conversation log file for this agent that was
    modified after `cutoff`. The 7-day window is approximate — we use
    file mtime, which matches "last appended to" since the daemon
    appends + immediately closes the file."""
    conv_dir = AGENTS_BASE / agent_id / "conversations"
    if not conv_dir.exists():
        return ""

    parts: list[str] = []
    cutoff_ts = cutoff.timestamp()
    for md_file in sorted(conv_dir.rglob("*.md")):
        try:
            if md_file.stat().st_mtime < cutoff_ts:
                continue
        except OSError:
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Lead each file with a separator so the model knows where channel
        # boundaries are. Don't strip the existing frontmatter — agent name +
        # channel name in the frontmatter is exactly the context the
        # summarization prompt benefits from.
        parts.append(f"\n\n========== FILE: {md_file.relative_to(AGENTS_BASE)} ==========\n\n{content}")

    raw = "".join(parts).strip()
    if len(raw) > MAX_RAW_CONTENT_CHARS:
        # Keep the most recent end — chronologically newest content is what
        # next week's lessons should be most weighted toward.
        raw = "[...older turns truncated to fit prompt budget...]\n\n" + raw[-MAX_RAW_CONTENT_CHARS:]
    return raw


def _build_prompt(agent_id: str, week_label: str, raw_turns: str) -> str:
    """Substitute variables into the summarization template and append the
    raw conversation data wrapped in a defensive tag."""
    template = PROMPT_PATH.read_text(encoding="utf-8")
    rendered = (
        template
        .replace("{agent_id}", agent_id)
        .replace("{week_iso}", week_label)
    )
    return f"{rendered}\n\n<conversation-data>\n{raw_turns}\n</conversation-data>"


def _run_claude(prompt: str) -> tuple[bool, str, str]:
    """Synchronous claude -p invocation. Returns (ok, stdout, error)."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text", "--model", MODEL],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT_S, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except FileNotFoundError:
        return False, "", "claude_cli_not_found"
    if result.returncode != 0:
        snippet = (result.stderr or "")[:300].replace("\n", " ")
        return False, result.stdout, f"exit_{result.returncode}: {snippet}"
    return True, result.stdout, ""


def _write_digest(agent_id: str, week_label: str, digest: str) -> Path:
    """Write the digest to `Agents/<agent_id>/lessons-learned/<week>.md`."""
    out_dir = AGENTS_BASE / agent_id / "lessons-learned"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{week_label}.md"
    out_path.write_text(digest.strip() + "\n", encoding="utf-8")
    return out_path


def summarize_one_agent(agent_id: str, *, today: date | None = None,
                        logger: logging.Logger | None = None) -> tuple[bool, str]:
    """Generate this week's lessons digest for one agent. Returns
    (ok, summary_line) where summary_line is a stdout-friendly status."""
    log = logger or logging.getLogger("summarize_weekly")
    today = today or datetime.now(timezone.utc).date()
    week_label = _iso_week(today)
    cutoff = datetime.combine(today - timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc)

    raw = _gather_recent_turns(agent_id, cutoff)
    if not raw.strip():
        return True, f"{agent_id}: no conversation activity in the past 7 days, skipped"

    prompt = _build_prompt(agent_id, week_label, raw)
    log.info(f"{agent_id}: running claude (prompt {len(prompt):,} chars, raw {len(raw):,} chars)")

    ok, digest, err = _run_claude(prompt)
    if not ok:
        return False, f"{agent_id}: claude failed: {err}"
    if not digest.strip():
        return False, f"{agent_id}: claude returned empty output"

    out_path = _write_digest(agent_id, week_label, digest)
    return True, f"{agent_id}: wrote {out_path.relative_to(AGENTS_BASE.parent)} ({len(digest):,} chars)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly lessons-learned summarization per agent.")
    parser.add_argument("--agent", help="Run for a single agent_id (default: all discovered).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discover agents and report what would happen, but don't call claude.")
    parser.add_argument("--today", help="Override today's date (YYYY-MM-DD), for testing.")
    args = parser.parse_args(argv)

    log = _setup_logging()

    today = date.fromisoformat(args.today) if args.today else None
    agents = [args.agent] if args.agent else _discover_agents()

    if not agents:
        log.info("no agents with conversation logs found; nothing to do")
        return 0

    log.info(f"summarize-weekly: {len(agents)} agent(s) → {', '.join(agents)}")

    if args.dry_run:
        for agent_id in agents:
            cutoff = datetime.combine(
                (today or datetime.now(timezone.utc).date()) - timedelta(days=7),
                datetime.min.time(), tzinfo=timezone.utc,
            )
            raw = _gather_recent_turns(agent_id, cutoff)
            log.info(f"DRY {agent_id}: would summarize {len(raw):,} chars of raw turns")
        return 0

    failures = 0
    for agent_id in agents:
        ok, line = summarize_one_agent(agent_id, today=today, logger=log)
        log.info(line)
        if not ok:
            failures += 1
    log.info(f"summarize-weekly done: {len(agents) - failures}/{len(agents)} succeeded")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
