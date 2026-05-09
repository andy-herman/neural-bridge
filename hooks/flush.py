#!/usr/bin/env python3
"""flush.py — STUB implementation.

V2 will use a `claude` CLI subprocess to summarize the transcript and
write a structured session block per ADR-007. For V1 plumbing, this is
a stub that just appends a STUB session block to
`daily-logs/<agent>/YYYY-MM-DD.md` so the end-to-end SessionEnd hook
flow can be verified before any LLM logic ships.

Tracks: issue #9 (Phase A — filing gate). This stub will be replaced
when the real flush logic ships.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAILY_LOGS_DIR = REPO_ROOT / "daily-logs"
QUEUE_LOG = DAILY_LOGS_DIR / "_queue.log"


def write_queue_status(agent: str, session_id: str, status: str) -> None:
    DAILY_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with QUEUE_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {agent} {session_id} {status}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Flush transcript to daily log (STUB)")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--hook-event", default="SessionEnd")
    args = parser.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    agent_dir = DAILY_LOGS_DIR / args.agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    log_file = agent_dir / f"{today}.md"

    # Stub block: structured per ADR-007 but with placeholder content.
    block = (
        f"\n---\n\n"
        f"## STUB Session — {timestamp}\n\n"
        f"```yaml\n"
        f"session_id: {args.session_id}\n"
        f"transcript_path: {args.transcript}\n"
        f"hook_event: {args.hook_event}\n"
        f"flush_version: stub-0.1\n"
        f"```\n\n"
        f"### Decisions\n\n- (stub)\n\n"
        f"### Findings\n\n- (stub)\n\n"
        f"### Open questions\n\n- (stub)\n\n"
        f"### Proposed concepts\n\n- (stub)\n"
    )
    with log_file.open("a", encoding="utf-8") as f:
        f.write(block)

    write_queue_status(args.agent, args.session_id, "stub_flushed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
