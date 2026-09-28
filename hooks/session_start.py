#!/usr/bin/env python3
"""SessionStart hook for Neural Bridge (closes #37).

When Claude Code starts a session in this repo, this hook reads:
- knowledge/index.md (always-loaded wiki entry point)
- The agent's most recent 1-2 entries in knowledge/agents/<agent>/
- The agent's most recent progress.md entries from the vault
  (~/Documents/Luna Master/Agents/<agent>/progress.md), unless the session
  is a Discord daemon turn, where mention.py already injects them

…and prints a context block to stdout. Claude Code's SessionStart hook
contract treats hook stdout as additionalContext to inject into the
session's first turn.

Total budget: 4000 chars by default (env var `NB_SESSION_START_BUDGET`
overrides). The block is structured so partial truncation degrades
gracefully — index.md is always included; per-agent context shrinks
first.

History: until 2026-09-28 the third section was the two most recent files
in daily-logs/<agent>/, read from the top and truncated, so it carried the
oldest sessions of the day, cut mid-block. progress.md is written from the
same flush output, keeps whole entries, newest first within budget, and is
the narrative store docs/MEMORY_CONSOLIDATION.md names as the one re-read
at session start. daily-logs/ remains compile.py's input; it is no longer
injected anywhere.

Schema: ADR-007 (decisions/ADR-007-daily-log-schema.md) for daily-log
structure; AGENTS.md for the wiki layout.
Tracks: issue #37.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOKS_DIR.parent
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
AGENTS_DIR = KNOWLEDGE_DIR / "agents"
DAILY_LOGS_DIR = REPO_ROOT / "daily-logs"
QUEUE_LOG = DAILY_LOGS_DIR / "_queue.log"

sys.path.insert(0, str(HOOKS_DIR))
from schema import KNOWN_AGENTS, UNATTRIBUTED  # noqa: E402

DEFAULT_BUDGET = 4000  # chars total
INDEX_CAP = 1500
PER_AGENT_NOTES_CAP = 2

# The Discord daemon stamps this on every `claude -p` turn (claude_invoke.py)
# and its prompt builder (mention.py) injects progress.md itself, behind
# notes.md, with its own telemetry event. When it is present this hook leaves
# the progress log out so a daemon turn does not carry it twice. NB_AGENT, the
# manual override, does not suppress it.
DAEMON_MARKER_ENV = "NB_AGENT_ID"


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_breadcrumb(agent: str, status: str) -> None:
    try:
        DAILY_LOGS_DIR.mkdir(parents=True, exist_ok=True)
        line = f"{utc_iso()} {agent} session-start {status}\n"
        with QUEUE_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # never fail the hook on logging


def resolve_agent(payload: dict) -> str:
    agent_type = (payload.get("agent_type") or "").strip().lower()
    if agent_type in KNOWN_AGENTS:
        return agent_type
    # NB_AGENT is the manual override; NB_AGENT_ID is what the Discord daemon
    # stamps on every claude -p turn (claude_invoke.py) for guard_bash. Until
    # 2026-09-22 only NB_AGENT was read here, so every Discord turn was filed
    # under _unattributed, which compile.py skips: the wiki never saw any
    # agent's Discord work, which is most of the fleet's work.
    for key in ("NB_AGENT", "NB_AGENT_ID"):
        env_agent = os.environ.get(key, "").strip().lower()
        if env_agent in KNOWN_AGENTS:
            return env_agent
    cwd = payload.get("cwd") or os.getcwd()
    cwd_base = Path(cwd).name.lower()
    if cwd_base in KNOWN_AGENTS:
        return cwd_base
    return UNATTRIBUTED


def read_capped(path: Path, cap: int) -> str:
    """Read up to `cap` chars from `path`. Returns empty string on any error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "…"


def recent_files(directory: Path, *, limit: int) -> list[Path]:
    """Return up to `limit` most-recently-modified .md files in `directory`."""
    if not directory.exists() or not directory.is_dir():
        return []
    candidates = [p for p in directory.glob("*.md") if p.is_file() and not p.name.startswith(".")]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[:limit]


def _progress_module():
    """Import scripts/discord_bot/progress_log.py (stdlib only) by repo path.

    Resolved lazily and by path, not by package, so the hook keeps working from
    any cwd and never needs the daemon's venv. Returns None if the module is
    missing, which a fresh checkout cannot be but a partial one could.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from scripts.discord_bot import progress_log
    except Exception:
        return None
    return progress_log


def _record_progress_telemetry(agent: str, *, ok: bool, chars: int, detail: str) -> None:
    """One RETRIEVE event for store "progress_log", same shape mention.py
    records per Discord turn, so the canary sees Claude Code sessions as
    agent traffic too. Never raises."""
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from scripts.discord_bot import memory_telemetry as mem
        mem.record(mem.RETRIEVE, "progress_log", agent_id=agent, ok=ok, chars=chars,
                   detail=f"session_start: {detail}"[:160])
    except Exception:
        pass


def progress_section(agent: str, *, max_chars: int, env=None) -> tuple[str, str] | None:
    """(header, content) for the agent's recent progress.md entries, or None.

    None means "nothing to inject", which covers: a daemon turn (mention.py
    injects it), no vault on this machine, no log yet for this agent, or a
    read error. Only the read error is recorded as a failure; the rest are
    legitimate (see progress_log.py on why a missing file is not a failure).
    """
    env = os.environ if env is None else env
    if env.get(DAEMON_MARKER_ENV, "").strip():
        return None
    progress_log = _progress_module()
    if progress_log is None:
        return None
    try:
        text, status = progress_log.read_recent(agent, max_chars=max_chars)
    except Exception as exc:  # the reader never raises, but the hook must not depend on that
        _record_progress_telemetry(agent, ok=False, chars=0, detail=f"{type(exc).__name__}: {exc}")
        return None
    if status in ("missing", "empty"):
        _record_progress_telemetry(agent, ok=True, chars=0, detail=f"progress.md {status}")
        return None
    if status != "ok":
        _record_progress_telemetry(agent, ok=False, chars=0, detail=status)
        return None
    _record_progress_telemetry(agent, ok=True, chars=len(text), detail="injected")
    return (progress_log.block_heading(agent), progress_log.render_body(text).rstrip())


def render_block(agent: str, sections: list[tuple[str, str]]) -> str:
    """Render the final additionalContext block."""
    lines = [
        f"<!-- Neural Bridge SessionStart context (agent: {agent}, generated: {utc_iso()}) -->",
        "",
    ]
    for header, content in sections:
        if not content.strip():
            continue
        lines.append(f"## {header}")
        lines.append("")
        lines.append(content.rstrip())
        lines.append("")
    lines.append("<!-- end SessionStart context -->")
    return "\n".join(lines)


def build_context(agent: str, *, budget: int) -> str:
    sections: list[tuple[str, str]] = []
    remaining = budget

    # 1. Index.md (always loaded if it exists)
    if INDEX_FILE.exists():
        index_text = read_capped(INDEX_FILE, min(INDEX_CAP, remaining))
        if index_text.strip():
            header = f"Wiki index ({INDEX_FILE.relative_to(REPO_ROOT)})"
            sections.append((header, index_text))
            remaining -= len(index_text) + len(header) + 12  # rough header overhead

    # 2. Per-agent prior session notes
    if agent != UNATTRIBUTED and remaining > 200:
        agent_dir = AGENTS_DIR / agent
        files = recent_files(agent_dir, limit=PER_AGENT_NOTES_CAP)
        if files:
            chunks: list[str] = []
            per_file_cap = max(200, remaining // (PER_AGENT_NOTES_CAP * 2))
            for f in files:
                content = read_capped(f, per_file_cap)
                if content.strip():
                    chunks.append(f"### {f.relative_to(REPO_ROOT)}\n\n{content}")
            if chunks:
                joined = "\n\n".join(chunks)
                if len(joined) > remaining:
                    joined = joined[: max(0, remaining - 100)].rstrip() + "\n\n_(truncated)_"
                header = f"Recent {agent} session notes"
                sections.append((header, joined))
                remaining -= len(joined) + len(header) + 12

    # 3. Per-agent progress log (the narrative half of the vault note store,
    #    written by flush.py at session close). Whole entries, newest first,
    #    within what is left of the budget. render_block's own framing text
    #    is ~350 chars, so leave room for it or the section is all wrapper.
    if agent != UNATTRIBUTED and remaining > 600:
        section = progress_section(agent, max_chars=remaining - 400)
        if section:
            sections.append(section)

    return render_block(agent, sections)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        write_breadcrumb(UNATTRIBUTED, "failed:bad_payload")
        return 0

    agent = resolve_agent(payload)
    budget_env = os.environ.get("NB_SESSION_START_BUDGET", "").strip()
    try:
        budget = int(budget_env) if budget_env else DEFAULT_BUDGET
    except ValueError:
        budget = DEFAULT_BUDGET

    if agent == UNATTRIBUTED:
        write_breadcrumb(agent, "skipped:unattributed")
        return 0  # don't pollute generic Claude Code sessions

    try:
        block = build_context(agent, budget=budget)
        sys.stdout.write(block)
        sys.stdout.write("\n")
        sys.stdout.flush()
        write_breadcrumb(agent, f"injected:{len(block)}b")
    except Exception as exc:
        write_breadcrumb(agent, f"failed:{type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
