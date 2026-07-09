#!/usr/bin/env python3
"""guard_concepts.py — PreToolUse hook: block direct writes to gated wiki paths.

AGENTS.md asks agents not to write to knowledge/concepts/ directly (concept
promotion goes through scripts/compile.py and its filing gate) and never to
touch knowledge/quarantine/ (human-review-only). This hook makes that rule
mechanical instead of polite: any Write/Edit tool call targeting those paths
exits 2, which blocks the tool call and feeds stderr back to the model.

compile.py itself is unaffected: it writes via Python file IO, not via
Claude Code tool calls, so this hook never sees it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GATED = (
    REPO_ROOT / "knowledge" / "concepts",
    REPO_ROOT / "knowledge" / "quarantine",
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed payload: never block on our own bug

    tool_input = payload.get("tool_input") or {}
    raw_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not raw_path:
        return 0

    try:
        target = Path(raw_path)
        if not target.is_absolute():
            target = REPO_ROOT / target
        target = target.resolve()
    except (OSError, ValueError):
        return 0

    for gated in GATED:
        try:
            target.relative_to(gated)
        except ValueError:
            continue
        rel = target.relative_to(REPO_ROOT)
        print(
            f"BLOCKED: direct writes to {rel.parent}/ are not allowed. "
            "Concept promotion goes through scripts/compile.py and its filing gate; "
            "quarantine/ is human-review-only. Write your finding to your own "
            "knowledge/agents/<role>/ subdirectory or a daily log instead. "
            "See AGENTS.md.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
