"""Loop engineer configuration.

All knobs live here with conservative defaults. Everything is overridable via
`NB_LOOP_*` environment variables so the launchd plist (or a foreground test
run) can tune behavior without code edits. `LoopConfig.from_env()` is the one
entry point; `main.py` layers a few CLI flags on top of it.

The defaults are deliberately timid — small per-run and per-day ceilings, a
tight turn cap, draft PRs only. Andy's Risks.md names a runaway agent loop as
the project's #1 risk (a prompt-logic bug can burn a week of Max quota in
90 minutes), so the safe posture is: start small, raise the caps only after a
full observed cycle.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Model default matches the rest of the daemon (claude_invoke.DEFAULT_MODEL).
DEFAULT_MODEL = "claude-sonnet-4-6"

# Narrow allowlist. WebSearch/WebFetch are denied separately so an automated
# run has no outbound-data path. Bash is included so the agent can run the
# test suite and git status inside its worktree.
DEFAULT_ALLOWED_TOOLS = "Read,Edit,Write,Bash,Glob,Grep"
DEFAULT_DISALLOWED_TOOLS = "WebSearch,WebFetch"

# Default test command: unittest discovery over the whole scripts/ tree.
# Uses an ABSOLUTE interpreter (the daemon's venv python) rather than a
# repo-relative `.venv/bin/python`, because the command runs with cwd set to
# an ephemeral worktree that has no .venv of its own (.venv is gitignored).
# `-s scripts` still resolves against the worktree's checkout, so it tests the
# agent's code with the real interpreter + installed deps. Overridable per-run.
DEFAULT_TEST_COMMAND = (
    f"{sys.executable} -m unittest discover -s scripts -p 'test_*.py'"
)

_STATE_DIR = Path.home() / "Library" / "Application Support" / "neural-bridge" / "loop-engineer"


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LoopConfig:
    """Every tunable for one loop-engineer process."""

    # --- Which work to pick up ---
    repo_id: str = "neural-bridge"          # key into discord_bot.repos.REPOS
    owner_label: str | None = None          # e.g. "owner:automation-engineer"; None = any owner
    base_branch: str = "main"

    # --- Label state machine ---
    ready_label: str = "agent-ready"        # queued, claimable
    running_label: str = "agent-running"    # claimed, in progress
    review_label: str = "agent-review"      # PR opened, awaiting human review
    failed_label: str = "agent-failed"      # escalated after strikes / hard error
    blocked_label: str = "agent-blocked"    # needs a human decision (e.g. diff too large)

    # --- Ceilings (the budget kill-switch) ---
    max_issues_per_run: int = 3             # per process invocation
    max_issues_per_day: int = 5             # across invocations, persisted to disk
    max_turns: int = 30                     # claude -p tool-use round trips per attempt
    resume_extra_turns: int = 15            # extra turns granted on a single max-turns resume
    max_strikes: int = 3                    # consecutive test failures before escalation
    max_diff_lines: int = 500               # added+deleted; over this -> human review
    per_issue_timeout: int = 1800           # seconds for one claude -p attempt

    # --- Cadence ---
    poll_interval_seconds: int = 60         # sleep when the queue is empty (daemon mode)

    # --- Invocation ---
    model: str = DEFAULT_MODEL
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS
    disallowed_tools: str = DEFAULT_DISALLOWED_TOOLS
    test_command: str = DEFAULT_TEST_COMMAND

    # --- Worktree layout (under the shared clone; .trees/ is gitignored) ---
    worktrees_dirname: str = ".trees"
    branch_prefix: str = "eng"

    # --- PR behavior ---
    draft_pr: bool = True                   # never auto-merge; always open for review

    # --- State / dirs ---
    state_dir: Path = field(default_factory=lambda: _STATE_DIR)

    # --- Run mode ---
    once: bool = False                      # process available issues then exit (no polling)
    dry_run: bool = False                   # do everything EXCEPT push + open PR + mutate labels

    @classmethod
    def from_env(cls) -> "LoopConfig":
        owner = os.environ.get("NB_LOOP_OWNER_LABEL")
        return cls(
            repo_id=_env_str("NB_LOOP_REPO_ID", "neural-bridge"),
            owner_label=owner if owner else None,
            base_branch=_env_str("NB_LOOP_BASE_BRANCH", "main"),
            ready_label=_env_str("NB_LOOP_READY_LABEL", "agent-ready"),
            running_label=_env_str("NB_LOOP_RUNNING_LABEL", "agent-running"),
            review_label=_env_str("NB_LOOP_REVIEW_LABEL", "agent-review"),
            failed_label=_env_str("NB_LOOP_FAILED_LABEL", "agent-failed"),
            blocked_label=_env_str("NB_LOOP_BLOCKED_LABEL", "agent-blocked"),
            max_issues_per_run=_env_int("NB_LOOP_MAX_ISSUES_PER_RUN", 3),
            max_issues_per_day=_env_int("NB_LOOP_MAX_ISSUES_PER_DAY", 5),
            max_turns=_env_int("NB_LOOP_MAX_TURNS", 30),
            resume_extra_turns=_env_int("NB_LOOP_RESUME_EXTRA_TURNS", 15),
            max_strikes=_env_int("NB_LOOP_MAX_STRIKES", 3),
            max_diff_lines=_env_int("NB_LOOP_MAX_DIFF_LINES", 500),
            per_issue_timeout=_env_int("NB_LOOP_PER_ISSUE_TIMEOUT", 1800),
            poll_interval_seconds=_env_int("NB_LOOP_POLL_INTERVAL", 60),
            model=_env_str("NB_LOOP_MODEL", DEFAULT_MODEL),
            allowed_tools=_env_str("NB_LOOP_ALLOWED_TOOLS", DEFAULT_ALLOWED_TOOLS),
            disallowed_tools=_env_str("NB_LOOP_DISALLOWED_TOOLS", DEFAULT_DISALLOWED_TOOLS),
            test_command=_env_str("NB_LOOP_TEST_COMMAND", DEFAULT_TEST_COMMAND),
            worktrees_dirname=_env_str("NB_LOOP_WORKTREES_DIRNAME", ".trees"),
            branch_prefix=_env_str("NB_LOOP_BRANCH_PREFIX", "eng"),
            draft_pr=_env_bool("NB_LOOP_DRAFT_PR", True),
            once=_env_bool("NB_LOOP_ONCE", False),
            dry_run=_env_bool("NB_LOOP_DRY_RUN", False),
        )

    def ready_label_filter(self) -> list[str]:
        """Labels an issue must carry to be claimable: ready + optional owner."""
        labels = [self.ready_label]
        if self.owner_label:
            labels.append(self.owner_label)
        return labels
