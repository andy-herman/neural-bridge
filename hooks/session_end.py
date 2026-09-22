#!/usr/bin/env python3
"""SessionEnd / PreCompact hook for Neural Bridge.

Reads a Claude Code hook event from stdin, resolves which specialist
agent the session was for, spawns flush.py as a detached subprocess,
writes a breadcrumb to daily-logs/_queue.log, and exits 0.

Schema: ADR-007 (decisions/ADR-007-daily-log-schema.md)
Tracks: issue #8

Designed to NEVER block the parent CLI shutdown. Failures are recorded
in _queue.log; flush.py runs out-of-band so a slow LLM call never
delays Claude Code from exiting cleanly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAILY_LOGS_DIR = REPO_ROOT / "daily-logs"
QUEUE_LOG = DAILY_LOGS_DIR / "_queue.log"
FLUSH_SCRIPT = REPO_ROOT / "hooks" / "flush.py"

sys.path.insert(0, str(REPO_ROOT / "hooks"))
from schema import KNOWN_AGENTS, UNATTRIBUTED  # noqa: E402


def resolve_agent(payload: dict) -> str:
    """Determine which specialist agent owns this session.

    Priority order:
      1. payload['agent_type'] from the Claude Code hook event
      2. NB_AGENT (manual override) or NB_AGENT_ID (stamped by the Discord daemon)
      3. cwd basename, if it matches a known agent name
      4. _unattributed (fallback)
    """
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


def write_breadcrumb(agent: str, session_id: str, status: str) -> None:
    """Append a single status line to daily-logs/_queue.log."""
    DAILY_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} {agent} {session_id} {status}\n"
    with QUEUE_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def spawn_flush(agent: str, session_id: str, transcript_path: str, hook_event: str) -> None:
    """Spawn flush.py as a detached subprocess and return immediately."""
    args = [
        sys.executable,
        str(FLUSH_SCRIPT),
        "--agent", agent,
        "--session-id", session_id,
        "--transcript", transcript_path,
        "--hook-event", hook_event,
    ]
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            args,
            creationflags=flags,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            args,
            start_new_session=True,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        write_breadcrumb(UNATTRIBUTED, "unknown", "failed:bad_payload")
        return 0

    session_id = payload.get("session_id", "unknown")
    transcript_path = payload.get("transcript_path", "")
    hook_event = payload.get("hook_event_name", "SessionEnd")
    agent = resolve_agent(payload)

    if not transcript_path:
        write_breadcrumb(agent, session_id, "failed:no_transcript_path")
        return 0
    if not Path(transcript_path).exists():
        write_breadcrumb(agent, session_id, "failed:transcript_missing")
        return 0
    if not FLUSH_SCRIPT.exists():
        write_breadcrumb(agent, session_id, "failed:flush_script_missing")
        return 0

    try:
        spawn_flush(agent, session_id, transcript_path, hook_event)
        write_breadcrumb(agent, session_id, "flush_spawned")
    except Exception as exc:
        write_breadcrumb(agent, session_id, f"failed:spawn_{type(exc).__name__}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
