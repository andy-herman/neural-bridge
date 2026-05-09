"""GitHub label state machine for Neural Bridge issues.

Six states from the prior PM-Led Agent Workflow:

    agent-inbox    → not yet picked up
    agent-ready    → senior-pm has triaged, ready for specialist pickup
    agent-running  → specialist is actively working
    needs-human    → paused for Andy specifically (decision, info, secret)
    agent-review   → work done, awaiting senior-pm QA
    agent-done     → closed (issue is also closed via gh issue close)

Plus tag labels that travel orthogonally:
    pm-managed     → created via PM intake (set on issue creation)
    needs-input    → required field missing (overlaps with needs-human; in
                     V1 we use needs-input for "need clarification" and
                     needs-human for "need a human decision")
    squad:<id>     → ownership tag, e.g., squad:senior-pm, squad:research

This module provides helpers that wrap gh CLI label operations. State
transition validation is intentionally light: any state can transition to
any other state (because Andy is the operator and can override), but
helpers like `transition_to_ready` document the canonical paths.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass

DEFAULT_TIMEOUT = 30

STATE_LABELS = (
    "agent-inbox",
    "agent-ready",
    "agent-running",
    "needs-human",
    "agent-review",
    "agent-done",
)

STATE_LABEL_SET = set(STATE_LABELS)

CANONICAL_TRANSITIONS = {
    "agent-inbox": {"agent-ready", "needs-human"},
    "agent-ready": {"agent-running", "needs-human"},
    "agent-running": {"agent-review", "needs-human"},
    "needs-human": {"agent-ready", "agent-running", "agent-done"},
    "agent-review": {"agent-done", "agent-running"},  # back to running if review fails
    "agent-done": set(),
}


@dataclass
class LabelOpResult:
    ok: bool
    error: str | None = None


def _run_gh_label(
    *,
    action: str,  # "add" or "remove"
    repo: str,
    issue_number: int,
    label: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> LabelOpResult:
    if action == "add":
        args = ["gh", "issue", "edit", str(issue_number), "--repo", repo, "--add-label", label]
    elif action == "remove":
        args = ["gh", "issue", "edit", str(issue_number), "--repo", repo, "--remove-label", label]
    else:
        return LabelOpResult(ok=False, error=f"bad_action: {action}")

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return LabelOpResult(ok=False, error="timeout")
    except FileNotFoundError:
        return LabelOpResult(ok=False, error="gh_cli_not_found")
    if proc.returncode != 0:
        snippet = (proc.stderr or "")[:300].replace("\n", " ").strip()
        return LabelOpResult(ok=False, error=f"gh_exit_{proc.returncode}: {snippet}")
    return LabelOpResult(ok=True)


def add_label_sync(*, repo: str, issue_number: int, label: str, timeout: int = DEFAULT_TIMEOUT) -> LabelOpResult:
    return _run_gh_label(action="add", repo=repo, issue_number=issue_number, label=label, timeout=timeout)


def remove_label_sync(*, repo: str, issue_number: int, label: str, timeout: int = DEFAULT_TIMEOUT) -> LabelOpResult:
    return _run_gh_label(action="remove", repo=repo, issue_number=issue_number, label=label, timeout=timeout)


async def add_label(*, repo: str, issue_number: int, label: str, timeout: int = DEFAULT_TIMEOUT) -> LabelOpResult:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: add_label_sync(repo=repo, issue_number=issue_number, label=label, timeout=timeout),
    )


async def remove_label(*, repo: str, issue_number: int, label: str, timeout: int = DEFAULT_TIMEOUT) -> LabelOpResult:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: remove_label_sync(repo=repo, issue_number=issue_number, label=label, timeout=timeout),
    )


async def apply_labels(
    *,
    repo: str,
    issue_number: int,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Apply label changes. Returns (applied, failures) where failures is a
    list of (label, error_reason) tuples.

    Operations run serially to make gh's behavior predictable. Failures on
    one label do not abort the rest.
    """
    add = add or []
    remove = remove or []
    applied: list[str] = []
    failures: list[tuple[str, str]] = []

    for label in remove:
        result = await remove_label(repo=repo, issue_number=issue_number, label=label, timeout=timeout)
        if result.ok:
            applied.append(f"-{label}")
        else:
            failures.append((f"-{label}", result.error or "unknown"))

    for label in add:
        result = await add_label(repo=repo, issue_number=issue_number, label=label, timeout=timeout)
        if result.ok:
            applied.append(f"+{label}")
        else:
            failures.append((f"+{label}", result.error or "unknown"))

    return applied, failures


def is_state_label(label: str) -> bool:
    return label in STATE_LABEL_SET


def is_canonical_transition(from_state: str, to_state: str) -> bool:
    """Whether this transition is in the canonical state-machine graph.

    Returns True iff `to_state` is a successor of `from_state` per the
    PM-Led Workflow. Out-of-graph transitions are allowed (Andy is the
    operator) but callers can use this for advisory warnings.
    """
    return to_state in CANONICAL_TRANSITIONS.get(from_state, set())
