"""Stage the agent's edits, commit, push the branch, open a DRAFT PR.

Only reached after all three verify gates pass. Draft-only, never auto-merge —
Andy reviews every PR. Commit messages carry NO Claude attribution (no
Co-Authored-By, no generated-by footer), per Andy's standing rule.

The worktree started as a clean checkout of origin/base, so `git add -A` stages
exactly the agent's changes and nothing else.
"""

from __future__ import annotations

import re
import subprocess as sp
from dataclasses import dataclass

from .config import LoopConfig
from .gitutil import git
from .queue import Issue
from .worktree import WorktreeHandle

# Conventional-commit type inferred from the issue title's leading word.
_TYPE_HINT_RE = re.compile(r"^\s*(fix|feat|docs|refactor|test|chore|perf|build|ci)\b", re.IGNORECASE)


@dataclass
class PRResult:
    ok: bool
    pr_url: str | None
    error: str


def infer_commit_type(title: str) -> str:
    """Best-effort conventional-commit type from the issue title, default fix."""
    m = _TYPE_HINT_RE.match(title or "")
    if m:
        return m.group(1).lower()
    lowered = (title or "").lower()
    if any(w in lowered for w in ("add", "implement", "support", "introduce")):
        return "feat"
    if any(w in lowered for w in ("document", "docs", "readme")):
        return "docs"
    return "fix"


def _strip_leading_type(title: str) -> str:
    """Drop a leading `fix:`/`feat:` if the issue title already has one."""
    return re.sub(r"^\s*(fix|feat|docs|refactor|test|chore|perf|build|ci)\s*:\s*",
                  "", title or "", flags=re.IGNORECASE).strip()


def commit_message(issue: Issue) -> tuple[str, str]:
    """(subject, body). Subject: `<type>: <title> (#n)`. Body references the issue."""
    ctype = infer_commit_type(issue.title)
    subject = f"{ctype}: {_strip_leading_type(issue.title)} (#{issue.number})"
    body = f"Closes #{issue.number}.\n\nImplemented autonomously by the loop engineer; opened as a draft for review."
    return subject[:200], body


def pr_body(issue: Issue, agent_summary: str, files: int, lines: int, tests_ok: bool) -> str:
    """Assemble the PR description from the agent's own summary + gate results."""
    status = "no new failures vs base" if tests_ok else "NEW failures introduced"
    header = (
        f"Autonomous implementation of #{issue.number}.\n\n"
        f"**Gates:** tests {status} · {files} file(s), {lines} line(s) changed · "
        f"within diff cap · no existing tests modified.\n\n"
        f"---\n\n"
    )
    summary = (agent_summary or "").strip() or "_(agent produced no summary)_"
    return header + summary + f"\n\n---\nCloses #{issue.number}."


def open_pr(
    cfg: LoopConfig,
    gh_slug: str,
    wt: WorktreeHandle,
    issue: Issue,
    agent_summary: str,
    files: int,
    lines: int,
) -> PRResult:
    """Push the already-committed branch and open a draft PR. Never raises.

    The agent's work was committed by main.process_issue BEFORE any test run, so
    the branch is a clean snapshot with no test pollution. Here we only publish.
    """
    subject, _ = commit_message(issue)

    ok, err = git(wt.path, ["push", "-u", "origin", wt.branch], timeout=180)
    if not ok:
        return PRResult(False, None, f"git push: {err}")

    body = pr_body(issue, agent_summary, files, lines, tests_ok=True)
    args = [
        "pr", "create",
        "--repo", gh_slug,
        "--base", cfg.base_branch,
        "--head", wt.branch,
        "--title", subject,
        "--body-file", "-",
    ]
    if cfg.draft_pr:
        args.append("--draft")

    try:
        proc = sp.run(
            ["gh"] + args,
            input=body,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (sp.TimeoutExpired, FileNotFoundError) as exc:
        return PRResult(False, None, f"gh pr create: {type(exc).__name__}")
    if proc.returncode != 0:
        snippet = (proc.stderr or "")[:300].replace("\n", " ")
        return PRResult(False, None, f"gh pr create exit {proc.returncode}: {snippet}")

    url = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""
    return PRResult(True, url or None, "")
