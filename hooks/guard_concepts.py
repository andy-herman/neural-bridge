#!/usr/bin/env python3
"""guard_concepts.py: PreToolUse hook that blocks direct writes under knowledge/.

knowledge/ is tracked in this public repo and owned by the wiki (ADR-0003).
concepts/ and connections/ are written only by scripts/compile.py, which runs
the filing gate and the outbound guard (docs/OUTBOUND_GUARD.md). quarantine/
is human-review-only. index.md and log.md are refreshed by compile. The one
place an agent may write is its own knowledge/agents/<role>/, which is
gitignored. Any Write/Edit/MultiEdit/NotebookEdit tool call that resolves
anywhere else under knowledge/ exits 2, which blocks the call and feeds
stderr back to the model.

Until 2026-09-26 only concepts/ and quarantine/ were blocked, so an agent
could edit the tracked connections/, index.md or log.md directly. That text
then went public on the next manual commit without passing the filing gate
or the outbound guard.

The check runs on the resolved path, so `..` segments and symlinks cannot
route around it: a link inside knowledge/agents/ that points into concepts/
is blocked, while the drafts link that points out into the vault is not. It
ignores case, as the Mac's filesystem does. Relative paths resolve against
the session's cwd from the hook payload.

compile.py itself is unaffected: it writes via Python file IO, not via
Claude Code tool calls, so this hook never sees it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = REPO_ROOT / "knowledge"
AGENT_NOTES = KNOWLEDGE / "agents"  # the only writable part of knowledge/


def _under(path: Path, root: Path) -> bool:
    """Component-wise, case-insensitive containment."""
    n = len(root.parts)
    return len(path.parts) >= n and [p.casefold() for p in path.parts[:n]] == [
        p.casefold() for p in root.parts
    ]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed payload: never block on our own bug
    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw_path:
        return 0

    try:
        target = Path(raw_path)
        if not target.is_absolute():
            target = Path(payload.get("cwd") or REPO_ROOT) / target
        target = target.resolve()
    except (OSError, ValueError):
        return 0

    if not _under(target, KNOWLEDGE) or _under(target, AGENT_NOTES):
        return 0
    rel = Path(*target.parts[len(REPO_ROOT.parts):])
    print(
        f"BLOCKED: direct writes under knowledge/ are not allowed ({rel}). knowledge/ is "
        "tracked in the public repo and owned by the wiki: concepts/ and connections/ come "
        "only from scripts/compile.py, which runs the filing gate and the outbound guard; "
        "quarantine/ is human-review-only; index.md and log.md are refreshed by compile. "
        "Write your finding to your own knowledge/agents/<role>/ subdirectory or a daily "
        "log instead. See AGENTS.md.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
