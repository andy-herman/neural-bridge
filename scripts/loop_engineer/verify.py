"""Verification gates the agent cannot self-report around.

Every gate here is computed by the daemon from git / the test runner's exit
code, never from the agent's own claim of success. This is the difference
between "a loop that opens PRs" and "a loop Andy will let run unattended."

Three gates, in order of severity:

  1. TEST INTEGRITY — the agent must not delete or edit EXISTING tests to make
     a suite pass. Adding new tests is fine (a pure addition). A deleted test
     file, or deletions inside an existing test file, is a hard escalation.
     This is the anti-reward-hacking gate.

  2. DIFF SIZE — a change larger than `max_diff_lines` is escalated to a human
     regardless of test result. Scope creep is nearly invisible otherwise.

  3. TEST EXIT CODE — the PR does not open unless the configured test command
     exits zero in the worktree.
"""

from __future__ import annotations

import re
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path

from .config import LoopConfig
from .gitutil import git
from .worktree import WorktreeHandle, base_ref

# What counts as a test file. Editing/deleting any of these that already exists
# on the base branch is a violation; creating a new one is not.
_TEST_PATTERNS = (
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"(^|/)[^/]+_test\.py$"),
    re.compile(r"\.test\.[^/]+$"),      # foo.test.ts / foo.test.js
    re.compile(r"\.spec\.[^/]+$"),      # foo.spec.ts
    re.compile(r"(^|/)tests?/"),        # anything under a test/ or tests/ dir
)


def is_test_file(path: str) -> bool:
    """True if `path` looks like a test file by convention."""
    p = path.strip()
    return any(rx.search(p) for rx in _TEST_PATTERNS)


# ---------- Gate 1: test integrity ----------

@dataclass
class IntegrityResult:
    ok: bool
    violations: list[str]  # human-readable descriptions of each tamper


def check_test_integrity(cfg: LoopConfig, wt: WorktreeHandle) -> IntegrityResult:
    """Fail if any EXISTING test file was deleted or had lines removed.

    Compares the committed branch to origin/base (commit-to-commit, so test
    pollution in the working tree can't affect it): a test path with deletions
    > 0, or a test path in the deleted-files list, is a violation. A test file
    that is purely added (all insertions, zero deletions) passes.
    """
    violations: list[str] = []

    # Deleted files (status D).
    ok, out = git(wt.path, ["diff", "--diff-filter=D", "--name-only", base_ref(cfg), "HEAD"])
    if ok and out:
        for path in out.splitlines():
            path = path.strip()
            if path and is_test_file(path):
                violations.append(f"deleted existing test file: {path}")

    # Per-file numstat: test files with any deletions.
    ok, out = git(wt.path, ["diff", "--numstat", base_ref(cfg), "HEAD"])
    if ok and out:
        for line in out.splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 3:
                continue
            added, deleted, path = parts[0], parts[1], parts[-1]
            if not is_test_file(path):
                continue
            if deleted != "-" and deleted.isdigit() and int(deleted) > 0:
                violations.append(f"removed {deleted} line(s) from existing test: {path}")

    return IntegrityResult(ok=not violations, violations=violations)


# ---------- Gate 2: diff size ----------

@dataclass
class SizeResult:
    ok: bool
    files: int
    lines: int
    limit: int


def check_diff_size(cfg: LoopConfig, files: int, changed_lines: int) -> SizeResult:
    """Pure gate: changed_lines must be <= cfg.max_diff_lines."""
    return SizeResult(
        ok=changed_lines <= cfg.max_diff_lines,
        files=files,
        lines=changed_lines,
        limit=cfg.max_diff_lines,
    )


# ---------- Gate 3: test exit code ----------

@dataclass
class TestResult:
    ok: bool                    # exit code 0 (whole suite green)
    exit_code: int
    output_tail: str
    failures: frozenset[str]    # unique ids of FAIL/ERROR tests


_TAIL_CHARS = 2000

# unittest prints `FAIL: <name> (<dotted.path>)` / `ERROR: <name> (<dotted.path>)`.
# The dotted path in parens is the stable unique id; fall back to the name.
_FAILURE_LINE_RE = re.compile(r"^(?:FAIL|ERROR):\s+(\S+)(?:\s+\((?P<path>[^)]+)\))?")


def parse_unittest_failures(output: str) -> frozenset[str]:
    """Extract the set of failing/erroring test ids from unittest output."""
    ids: set[str] = set()
    for line in output.splitlines():
        m = _FAILURE_LINE_RE.match(line.strip())
        if m:
            ids.add(m.group("path") or m.group(1))
    return frozenset(ids)


def run_tests(cfg: LoopConfig, wt: WorktreeHandle) -> TestResult:
    """Run the configured test command in the worktree. exit 0 == whole suite green.

    The command is a shell string (absolute interpreter + discovery flags); it
    runs with shell=True and cwd set to the worktree so `-s scripts` resolves
    against the worktree checkout. `failures` is the parsed set of failing test
    ids, used by the baseline-diff gate so a pre-existing red baseline doesn't
    block every issue.
    """
    try:
        proc = sp.run(
            cfg.test_command,
            shell=True,
            cwd=str(wt.path),
            capture_output=True,
            text=True,
            timeout=cfg.per_issue_timeout,
            stdin=sp.DEVNULL,
        )
    except sp.TimeoutExpired:
        return TestResult(False, 124, "test command timed out", frozenset())
    except OSError as exc:
        return TestResult(False, 127, f"could not run tests: {exc}", frozenset())
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    tail = combined[-_TAIL_CHARS:].strip()
    return TestResult(
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        output_tail=tail,
        failures=parse_unittest_failures(combined),
    )


@dataclass
class GateResult:
    ok: bool
    new_failures: list[str]     # failures present on the branch but not the baseline
    detail: str


def check_no_new_failures(baseline: TestResult, branch: TestResult) -> GateResult:
    """Pass iff the agent's change introduces NO new test failures vs baseline.

    Semantics chosen for a repo whose baseline may already be red: the loop is
    not responsible for pre-existing failures, only for not adding any. A branch
    that fixes a baseline failure also passes. An infra failure to run the suite
    at all (exit 124/127 with no parsed failures) is treated as a hard fail so a
    broken runner can't masquerade as "no new failures".
    """
    if branch.exit_code in (124, 127) and not branch.failures:
        return GateResult(False, [], f"test runner error (exit {branch.exit_code}): {branch.output_tail[:300]}")
    new = sorted(branch.failures - baseline.failures)
    if new:
        return GateResult(False, new, f"{len(new)} new failure(s): {', '.join(new[:5])}")
    return GateResult(True, [], "no new failures vs base")
