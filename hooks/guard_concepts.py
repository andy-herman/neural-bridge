#!/usr/bin/env python3
"""guard_concepts.py: PreToolUse hook that blocks direct writes under knowledge/.

knowledge/ is tracked in this public repo and owned by the wiki (ADR-0003).
concepts/ and connections/ are written only by scripts/compile.py, which runs
the filing gate and the outbound guard (docs/OUTBOUND_GUARD.md). quarantine/
is human-review-only. index.md and log.md are refreshed by compile. The one
place an agent may write is its own knowledge/agents/<role>/, which is
gitignored. Any Write/Edit/MultiEdit/NotebookEdit tool call that resolves
anywhere else under knowledge/ exits 2, which blocks the call and feeds
stderr back to the model. Copilot's apply_patch input is checked as a batch:
every add, update, delete, and move destination must pass the same policy.
Malformed write inputs are rejected, not treated as empty operations.

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
WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit", "apply_patch"}


def _under(path: Path, root: Path) -> bool:
    """Component-wise, case-insensitive containment."""
    n = len(root.parts)
    return len(path.parts) >= n and [p.casefold() for p in path.parts[:n]] == [
        p.casefold() for p in root.parts
    ]


def _checked_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing or invalid file path")
    if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("ambiguous whitespace or control character in file path")
    return value


def _patch_paths(patch: str) -> list[str]:
    """Read the apply_patch envelope without executing it or interpreting file content."""
    lines = patch.split("\n")
    if lines[-1] == "":
        lines.pop()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("invalid patch envelope")

    paths: list[str] = []
    end = len(lines) - 1
    i = 1
    while i < end:
        header = lines[i]
        if header.startswith("*** Add File: "):
            paths.append(_checked_path(header[len("*** Add File: "):]))
            i += 1
            start = i
            while i < end and lines[i].startswith("+"):
                i += 1
            if i == start:
                raise ValueError("add-file patch has no content")
        elif header.startswith("*** Delete File: "):
            paths.append(_checked_path(header[len("*** Delete File: "):]))
            i += 1
        elif header.startswith("*** Update File: "):
            paths.append(_checked_path(header[len("*** Update File: "):]))
            i += 1
            if i < end and lines[i].startswith("*** Move to: "):
                paths.append(_checked_path(lines[i][len("*** Move to: "):]))
                i += 1
            start = i
            while i < end and not lines[i].startswith("*** "):
                line = lines[i]
                if line != "@@" and not line.startswith(("@@ ", " ", "+", "-")):
                    raise ValueError("unsupported patch change line")
                i += 1
            if i < end and lines[i] == "*** End of File":
                if i == start:
                    raise ValueError("end-of-file marker without a change")
                i += 1
        else:
            raise ValueError("unsupported patch operation")
    return paths


def _write_paths(payload: dict) -> list[str]:
    tool_input = payload.get("tool_input", {})
    if isinstance(tool_input, str):
        return _patch_paths(tool_input)
    if not isinstance(tool_input, dict):
        raise ValueError("tool_input must be a file-path object or a patch string")

    paths = [
        _checked_path(tool_input[key])
        for key in ("file_path", "notebook_path")
        if key in tool_input
    ]
    tool_name = payload.get("tool_name", "")
    if not isinstance(tool_name, str):
        raise ValueError("invalid tool name")
    if not paths and tool_name.rsplit(".", 1)[-1].lower() in WRITE_TOOLS:
        raise ValueError("write tool has no recognized file path")
    return paths


def _resolve_path(target: Path) -> Path:
    resolved = target.resolve()
    try:
        resolved.resolve(strict=True)
    except FileNotFoundError:
        # New paths are valid; non-strict resolve alone can hide symlink loops.
        pass
    return resolved


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        paths = _write_paths(payload)
        if not paths:
            return 0
        cwd = Path(_checked_path(payload["cwd"])) if "cwd" in payload else REPO_ROOT
        if not cwd.is_absolute():
            raise ValueError("session cwd must be absolute")
        targets = []
        for raw_path in paths:
            target = Path(raw_path)
            if not target.is_absolute():
                target = cwd / target
            targets.append(_resolve_path(target))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"BLOCKED: cannot inspect write input: {exc}", file=sys.stderr)
        return 2

    for target in targets:
        if not _under(target, KNOWLEDGE) or _under(target, AGENT_NOTES):
            continue
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
