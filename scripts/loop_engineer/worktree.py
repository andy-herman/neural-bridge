"""Per-issue git worktree isolation.

The loop daemon and the Discord daemon share one clone at
`~/Development/neural-bridge`, and an auto-reload watcher runs `git pull` on
its `main` checkout. A loop that switched branches on that shared working tree
would corrupt the watcher's view. `git worktree` is the fix: each issue gets a
private working tree under `<clone>/.trees/eng-<n>` with its own HEAD, index,
and branch. Creating a worktree does not touch the main checkout's HEAD, and
git structurally forbids two worktrees checking out the same branch — so
concurrent isolation is free.

`.trees/` is gitignored (see repo .gitignore) so the watcher never sees the
worktrees as untracked and they can never be committed to main.

Teardown removes the worktree on success; on failure it is *kept* (renamed to
`failed-eng-<n>`) so Andy can inspect what the agent did before it gave up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import LoopConfig
from .gitutil import git, parse_numstat


@dataclass(frozen=True)
class WorktreeHandle:
    number: int
    branch: str
    path: Path


def branch_name(cfg: LoopConfig, number: int) -> str:
    """e.g. eng/112 — namespaced so these never collide with human branches."""
    return f"{cfg.branch_prefix}/{number}"


def worktree_path(cfg: LoopConfig, clone: Path, number: int) -> Path:
    return clone / cfg.worktrees_dirname / f"{cfg.branch_prefix}-{number}"


def create(cfg: LoopConfig, clone: Path, number: int) -> tuple[bool, WorktreeHandle | None, str]:
    """Fetch origin and add a fresh worktree branched off origin/<base>.

    Cleans up any stale worktree/branch left by a prior failed attempt on the
    same issue so re-runs are idempotent.
    """
    branch = branch_name(cfg, number)
    path = worktree_path(cfg, clone, number)

    ok, err = git(clone, ["fetch", "origin", cfg.base_branch], timeout=180)
    if not ok:
        return False, None, f"fetch failed: {err}"

    # Idempotency: clear a stale worktree dir and branch from a previous run.
    if path.exists():
        git(clone, ["worktree", "remove", "--force", str(path)])
    git(clone, ["worktree", "prune"])
    # Delete the branch if it lingers (only safe because eng/* is loop-owned).
    git(clone, ["branch", "-D", branch])

    ok, err = git(
        clone,
        ["worktree", "add", "-b", branch, str(path), f"origin/{cfg.base_branch}"],
        timeout=180,
    )
    if not ok:
        return False, None, f"worktree add failed: {err}"
    return True, WorktreeHandle(number=number, branch=branch, path=path), ""


def base_ref(cfg: LoopConfig) -> str:
    return f"origin/{cfg.base_branch}"


def reset_clean(wt: WorktreeHandle) -> None:
    """Return the worktree to its committed state, discarding all uncommitted
    changes and untracked files.

    Used to wipe the side effects of a test run (the compile tests write into
    knowledge/), so the next git-diff gate and the eventual PR see ONLY the
    agent's committed work, never test pollution. `-fd` (no `-x`) leaves
    gitignored files alone; there are none in a fresh worktree anyway.
    """
    git(wt.path, ["reset", "--hard"])
    git(wt.path, ["clean", "-fd"])


def commit_work(wt: WorktreeHandle, subject: str, body: str, *, amend: bool) -> tuple[bool, str]:
    """Stage everything and commit it onto the worktree's branch.

    Called right after each agent turn so the agent's edits become an immutable
    snapshot BEFORE any test run can pollute the tree. `amend` folds a fix-round
    into the single WIP commit so the PR stays one clean commit. Returns
    (committed, reason) — committed=False with reason "empty" if there is
    nothing to commit (agent produced no change).
    """
    ok, _ = git(wt.path, ["add", "-A"])
    if not ok:
        return False, "git add failed"
    # Nothing staged AND not amending -> empty change.
    staged_ok, staged = git(wt.path, ["diff", "--cached", "--name-only"])
    if staged_ok and not staged.strip() and not amend:
        return False, "empty"
    args = ["commit", "-m", subject, "-m", body]
    if amend:
        args = ["commit", "--amend", "-m", subject, "-m", body]
    ok, err = git(wt.path, args)
    if not ok:
        return False, err
    return True, ""


def diff_line_count(cfg: LoopConfig, wt: WorktreeHandle) -> tuple[bool, int, int]:
    """(ok, files, total_changed_lines) for the committed branch vs origin/base.

    Commit-to-commit, so it is immune to test pollution in the working tree.
    Insertions + deletions is the number the diff-size gate caps.
    """
    ok, out = git(wt.path, ["diff", "--numstat", base_ref(cfg), "HEAD"])
    if not ok:
        return False, 0, 0
    files, ins, dels = parse_numstat(out)
    return True, files, ins + dels


def remove(cfg: LoopConfig, clone: Path, wt: WorktreeHandle, *, keep_for_inspection: bool) -> str:
    """Tear down. On success remove cleanly; on failure keep + rename so Andy
    can inspect. Returns a short human status string.
    """
    if keep_for_inspection:
        failed_path = worktree_path(cfg, clone, wt.number).parent / f"failed-{cfg.branch_prefix}-{wt.number}"
        # Detach the worktree registration but leave files on disk for inspection.
        git(clone, ["worktree", "remove", "--force", str(wt.path)])
        git(clone, ["worktree", "prune"])
        return f"branch {wt.branch} kept; worktree removed (inspect via `git checkout {wt.branch}`)"
    git(clone, ["worktree", "remove", "--force", str(wt.path)])
    git(clone, ["worktree", "prune"])
    return f"worktree {wt.path.name} removed"
