#!/usr/bin/env python3
"""SessionStart hook for Neural Bridge (closes #37).

When Claude Code starts a session in this repo, this hook reads:
- knowledge/index.md (always-loaded wiki entry point)
- The agent's most recent 1-2 entries in knowledge/agents/<agent>/
- The most recent 2-3 entries in daily-logs/<agent>/

…and prints a context block to stdout. Claude Code's SessionStart hook
contract treats hook stdout as additionalContext to inject into the
session's first turn.

Total budget: 4000 chars by default (env var `NB_SESSION_START_BUDGET`
overrides). The block is structured so partial truncation degrades
gracefully — index.md is always included; per-agent context shrinks
first.

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

KNOWN_AGENTS = {
    "research", "teaching-prep", "content", "senior-pm", "social",
    "recruiter", "automation-engineer", "security-reviewer", "docs-editor",
}
UNATTRIBUTED = "_unattributed"

DEFAULT_BUDGET = 4000  # chars total
INDEX_CAP = 1500
PER_AGENT_NOTES_CAP = 2
DAILY_LOG_ENTRIES_CAP = 2


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
    env_agent = os.environ.get("NB_AGENT", "").strip().lower()
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

    # 3. Per-agent recent daily-logs (the flush-produced entries)
    if agent != UNATTRIBUTED and remaining > 200:
        log_dir = DAILY_LOGS_DIR / agent
        files = recent_files(log_dir, limit=DAILY_LOG_ENTRIES_CAP)
        if files:
            chunks = []
            per_file_cap = max(200, remaining // (DAILY_LOG_ENTRIES_CAP * 2))
            for f in files:
                content = read_capped(f, per_file_cap)
                if content.strip():
                    chunks.append(f"### {f.relative_to(REPO_ROOT)}\n\n{content}")
            if chunks:
                joined = "\n\n".join(chunks)
                if len(joined) > remaining:
                    joined = joined[: max(0, remaining - 100)].rstrip() + "\n\n_(truncated)_"
                header = f"Recent {agent} daily logs"
                sections.append((header, joined))

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
