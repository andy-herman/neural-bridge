"""Staged PR proposals — the approval gate between an agent's
`open_pr_with_changes` action and the actual git/gh push.

Flow:
  1. Agent emits `open_pr_with_changes` in its response.
  2. handlers.py validates the action (path traversal, repo allowlist,
     agent permission, size caps) and stages a PRProposal in the STORE
     instead of executing immediately.
  3. Daemon posts a preview to Discord with a short id, asks Andy to
     reply `approve <id>` (or `cancel <id>`) within the TTL.
  4. main.py's on_message intercepts those approval/cancel patterns
     BEFORE mention routing and calls execute_approved_proposal() /
     cancel_proposal().
  5. Execution: cd local clone → fetch → checkout default branch → pull
     → new branch → write files → git add+commit+push → gh pr create →
     return PR URL.

Why the explicit approval step: pushes are destructive enough (PR
opens, CI runs, reviewers get pinged) that we don't trust the agent's
self-assessment of "this looks ready." Andy stays in the loop.

The store is in-memory only — proposals don't survive a daemon restart.
If a proposal hasn't been approved within TTL_SECONDS, it's pruned and
the next approval attempt finds nothing.
"""

from __future__ import annotations

import logging
import re
import secrets
import subprocess as sp
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .repos import Repo, agent_can_push_to, repo_for

_logger = logging.getLogger("nb_discord.pr_proposals")


# ---------- Constants ----------

TTL_SECONDS = 15 * 60                # how long a proposal stays open for approval
MAX_FILES_PER_PR = 10                # cap files per proposal
MAX_FILE_BYTES = 200_000             # cap individual file size (~200KB)
MAX_TOTAL_BYTES = 800_000            # cap aggregate payload (~800KB)
MAX_BRANCH_LEN = 100
MAX_COMMIT_MSG_LEN = 2000
MAX_PR_TITLE_LEN = 200
MAX_PR_BODY_LEN = 20_000

# Branch names: alphanumeric + - / _ . — same rules git enforces, but
# more conservative so we don't pass weird stuff into shell-out.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,99}$")

# File paths: relative to repo root, no leading slash, no .. segments.
# Forward slashes only (repo paths are normalized).
_PATH_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


# ---------- Approval/cancel pattern matchers ----------

# Conservative: only unambiguous patterns trigger the gate. Plain "yes"/"no"
# would catch ambient conversation, so don't include them. Optional trailing
# proposal-id selects a specific proposal when multiple are pending.
_APPROVE_RE = re.compile(
    r"^\s*(approve(d)?|ship\s*it|go\s*ahead|do\s*it|lgtm)\s*([a-z0-9]{6,12})?\s*$",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    r"^\s*(cancel|drop(\s*it)?|abort|scrap(\s*it)?|never\s*mind|nevermind)\s*([a-z0-9]{6,12})?\s*$",
    re.IGNORECASE,
)


def is_approval_text(text: str) -> tuple[bool, str | None]:
    """Return (matches, optional proposal_id from the message)."""
    m = _APPROVE_RE.match(text or "")
    if not m:
        return False, None
    return True, m.group(3)


def is_cancel_text(text: str) -> tuple[bool, str | None]:
    m = _CANCEL_RE.match(text or "")
    if not m:
        return False, None
    return True, m.group(4)


# ---------- Data ----------

@dataclass
class PRProposal:
    proposal_id: str
    agent_id: str
    channel_id: int          # discord channel where the proposal lives
    repo: Repo
    branch: str
    files: list[tuple[str, str]]   # [(relative_path, content), ...]
    commit_message: str
    pr_title: str
    pr_body: str
    created_at: float = field(default_factory=time.time)

    def expires_at(self) -> float:
        return self.created_at + TTL_SECONDS

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at()


# ---------- Validation ----------

@dataclass
class ValidatedProposal:
    ok: bool
    proposal: PRProposal | None = None
    error: str | None = None


def validate_open_pr_action(action: dict, *, agent_id: str, channel_id: int) -> ValidatedProposal:
    """Type-check the action JSON, gate against the per-agent allowlist,
    sanitize file paths against traversal, and enforce size caps.

    Returns a ValidatedProposal — on success its `proposal` is ready to
    stage; on failure `error` describes what's wrong.
    """
    repo_id = action.get("repo")
    if not isinstance(repo_id, str) or not repo_id:
        return ValidatedProposal(ok=False, error="open_pr_with_changes: repo must be non-empty string")

    repo = repo_for(repo_id)
    if repo is None:
        return ValidatedProposal(ok=False, error=f"open_pr_with_changes: unknown repo {repo_id!r}")

    if not agent_can_push_to(agent_id, repo_id):
        return ValidatedProposal(
            ok=False,
            error=f"open_pr_with_changes: agent {agent_id!r} is not in the push allowlist for {repo_id!r}",
        )

    if not repo.local_path.exists():
        return ValidatedProposal(
            ok=False,
            error=f"open_pr_with_changes: local clone missing at {repo.local_path}",
        )

    branch = action.get("branch")
    if not isinstance(branch, str) or not _BRANCH_RE.match(branch):
        return ValidatedProposal(ok=False, error="open_pr_with_changes: branch must match [A-Za-z0-9._-/]+ (start alnum, ≤100 chars)")
    # Block `..` segments — the dot is allowed in branch names (e.g., v1.2) but
    # `foo/../etc` would let an agent write to a path outside the local clone.
    if ".." in branch.split("/") or branch.startswith("/"):
        return ValidatedProposal(ok=False, error=f"open_pr_with_changes: branch must not contain .. segments: {branch!r}")
    if branch == repo.default_branch:
        return ValidatedProposal(ok=False, error=f"open_pr_with_changes: branch must not equal default branch {repo.default_branch!r}")

    commit_message = action.get("commit_message")
    if not isinstance(commit_message, str) or not commit_message.strip():
        return ValidatedProposal(ok=False, error="open_pr_with_changes: commit_message must be non-empty string")
    if len(commit_message) > MAX_COMMIT_MSG_LEN:
        return ValidatedProposal(ok=False, error=f"open_pr_with_changes: commit_message exceeds {MAX_COMMIT_MSG_LEN} chars")

    pr_title = action.get("pr_title")
    if not isinstance(pr_title, str) or not pr_title.strip():
        return ValidatedProposal(ok=False, error="open_pr_with_changes: pr_title must be non-empty string")
    if len(pr_title) > MAX_PR_TITLE_LEN:
        return ValidatedProposal(ok=False, error=f"open_pr_with_changes: pr_title exceeds {MAX_PR_TITLE_LEN} chars")

    pr_body = action.get("pr_body", "")
    if not isinstance(pr_body, str):
        return ValidatedProposal(ok=False, error="open_pr_with_changes: pr_body must be string if present")
    if len(pr_body) > MAX_PR_BODY_LEN:
        return ValidatedProposal(ok=False, error=f"open_pr_with_changes: pr_body exceeds {MAX_PR_BODY_LEN} chars")

    files_in = action.get("files")
    if not isinstance(files_in, list) or not files_in:
        return ValidatedProposal(ok=False, error="open_pr_with_changes: files must be non-empty list")
    if len(files_in) > MAX_FILES_PER_PR:
        return ValidatedProposal(ok=False, error=f"open_pr_with_changes: too many files ({len(files_in)} > {MAX_FILES_PER_PR})")

    sanitized_files: list[tuple[str, str]] = []
    total_bytes = 0
    seen_paths: set[str] = set()
    for i, f in enumerate(files_in):
        if not isinstance(f, dict):
            return ValidatedProposal(ok=False, error=f"open_pr_with_changes: files[{i}] must be object")
        path = f.get("path")
        content = f.get("content")
        if not isinstance(path, str) or not path:
            return ValidatedProposal(ok=False, error=f"open_pr_with_changes: files[{i}].path must be non-empty string")
        if not isinstance(content, str):
            return ValidatedProposal(ok=False, error=f"open_pr_with_changes: files[{i}].content must be string")
        if path.startswith("/") or ".." in path.split("/") or "\\" in path:
            return ValidatedProposal(ok=False, error=f"open_pr_with_changes: files[{i}].path traversal blocked: {path!r}")
        if not _PATH_RE.match(path):
            return ValidatedProposal(ok=False, error=f"open_pr_with_changes: files[{i}].path has invalid chars: {path!r}")

        # Resolve and verify it lands under repo root.
        try:
            resolved = (repo.local_path / path).resolve(strict=False)
            resolved.relative_to(repo.local_path.resolve(strict=False))
        except (ValueError, OSError):
            return ValidatedProposal(ok=False, error=f"open_pr_with_changes: files[{i}].path resolves outside repo root: {path!r}")

        if path in seen_paths:
            return ValidatedProposal(ok=False, error=f"open_pr_with_changes: files[{i}].path duplicated: {path!r}")
        seen_paths.add(path)

        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            return ValidatedProposal(ok=False, error=f"open_pr_with_changes: files[{i}] is {size} bytes; cap is {MAX_FILE_BYTES}")
        total_bytes += size
        sanitized_files.append((path, content))

    if total_bytes > MAX_TOTAL_BYTES:
        return ValidatedProposal(ok=False, error=f"open_pr_with_changes: total payload {total_bytes} > {MAX_TOTAL_BYTES} bytes")

    proposal = PRProposal(
        proposal_id=_new_id(),
        agent_id=agent_id,
        channel_id=int(channel_id),
        repo=repo,
        branch=branch,
        files=sanitized_files,
        commit_message=commit_message,
        pr_title=pr_title,
        pr_body=pr_body,
    )
    return ValidatedProposal(ok=True, proposal=proposal)


def _new_id() -> str:
    """Short proposal id — short enough Andy can type it on mobile, long
    enough to avoid collisions across the 15-min TTL window."""
    return secrets.token_hex(4)  # 8 hex chars


# ---------- Store ----------

class ProposalStore:
    """In-memory map of pending proposals. Keyed by proposal_id, indexed
    by (channel_id, agent_id) for the common 'most recent in this channel
    by this agent' lookup."""

    def __init__(self) -> None:
        self._by_id: dict[str, PRProposal] = {}

    def stage(self, proposal: PRProposal) -> str:
        self._prune()
        self._by_id[proposal.proposal_id] = proposal
        return proposal.proposal_id

    def get(self, proposal_id: str) -> PRProposal | None:
        self._prune()
        return self._by_id.get(proposal_id)

    def pop(self, proposal_id: str) -> PRProposal | None:
        self._prune()
        return self._by_id.pop(proposal_id, None)

    def peek_for_channel_agent(self, channel_id: int, agent_id: str) -> PRProposal | None:
        """Most recent active proposal for this channel + agent. None if none."""
        self._prune()
        candidates = [
            p for p in self._by_id.values()
            if p.channel_id == channel_id and p.agent_id == agent_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.created_at)

    def pop_for_channel_agent(self, channel_id: int, agent_id: str) -> PRProposal | None:
        p = self.peek_for_channel_agent(channel_id, agent_id)
        if p is None:
            return None
        return self.pop(p.proposal_id)

    def all_active(self) -> Iterable[PRProposal]:
        self._prune()
        return list(self._by_id.values())

    def _prune(self) -> None:
        now = time.time()
        expired = [pid for pid, p in self._by_id.items() if p.is_expired(now)]
        for pid in expired:
            del self._by_id[pid]


STORE = ProposalStore()


# ---------- Preview ----------

def format_preview(proposal: PRProposal) -> str:
    """Render the Discord preview block for a staged proposal."""
    minutes = TTL_SECONDS // 60
    files_block = "\n".join(
        f"  - `{path}` ({len(content.encode('utf-8')):,} bytes)"
        for path, content in proposal.files
    )
    return (
        f"**🛫 `@{proposal.agent_id}` wants to open a PR**\n"
        f"- Repo: `{proposal.repo.gh_slug}`\n"
        f"- Branch: `{proposal.branch}`\n"
        f"- PR title: `{proposal.pr_title}`\n"
        f"- Files ({len(proposal.files)}):\n{files_block}\n"
        f"\n"
        f"Reply `approve {proposal.proposal_id}` to ship, or `cancel {proposal.proposal_id}` to drop. "
        f"Expires in {minutes} min. Plain `approve` / `cancel` also works if this is the only pending proposal."
    )


# ---------- Execution ----------

@dataclass
class ExecutionResult:
    ok: bool
    pr_url: str | None = None
    branch: str | None = None
    error: str | None = None


def _parse_porcelain(output: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain` output into `[(status, path), ...]`.

    Format per line is `XY<space>path` where XY is the two-char status
    code (`??` for untracked, `M ` for modified-staged, ` M` for modified-
    unstaged, `R ` for renamed, etc.).

    Renames in porcelain v1 look like `R  old -> new`. We emit one entry
    per affected path: both the source (since the user's intent treats
    the source as "moved away") and the destination (where new content
    lives). Either is a collision for our purposes.

    Paths with shell-unsafe chars are wrapped in double quotes by git;
    we strip those quotes. Escape sequences (`\\xxx` for non-ASCII) are
    left literal; collisions involving non-ASCII paths fall back to the
    surgical `git add` for correctness.

    Empty and malformed lines are skipped.
    """
    entries: list[tuple[str, str]] = []
    for raw_line in output.splitlines():
        if not raw_line.strip() or len(raw_line) < 4:
            continue
        status = raw_line[:2]
        rest = raw_line[3:]
        # Rename: `<old> -> <new>`. Emit both paths.
        if " -> " in rest:
            old_part, new_part = rest.split(" -> ", 1)
            for part in (old_part, new_part):
                part = part.strip()
                if part.startswith('"') and part.endswith('"') and len(part) >= 2:
                    part = part[1:-1]
                entries.append((status, part))
            continue
        if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
            rest = rest[1:-1]
        entries.append((status, rest))
    return entries


def _check_working_tree_collision(
    cwd: Path,
    proposal_paths: set[str],
) -> tuple[bool, str | None, list[str]]:
    """Decide whether the working tree is safe for a proposal to land on.

    Returns `(safe, error_message, non_colliding_dirty_paths)`:
      - `safe=True` means no dirty entry's path is in `proposal_paths`.
        Unrelated dirty files don't block: the surgical `git add` in
        step 5 only stages the proposal's own paths, so the user's
        in-flight work stays uncommitted and untouched.
      - `safe=False` means at least one dirty path collides with a path
        the proposal wants to write. We refuse rather than clobber.
        `error_message` lists the colliding paths so Andy can fix.
      - `non_colliding_dirty_paths` is the rest. Returned so the caller
        can log them for telemetry — useful when triaging "why did the
        post-push state look weird?"

    The collision is path-exact (not directory-containment). Two files
    in the same directory are independent: the daemon writes its own,
    the user's unrelated edit stays untouched.
    """
    ok, raw = _git(cwd, ["status", "--porcelain"])
    if not ok:
        return False, f"git status failed: {raw}", []

    if not raw.strip():
        return True, None, []

    entries = _parse_porcelain(raw)
    dirty_paths = {path for _, path in entries}
    collisions = sorted(dirty_paths & proposal_paths)
    non_colliding = sorted(dirty_paths - proposal_paths)

    if collisions:
        collision_list = ", ".join(collisions[:5])
        more = "" if len(collisions) <= 5 else f" (+{len(collisions) - 5} more)"
        return (
            False,
            (
                f"working tree at {cwd} has uncommitted changes that collide with "
                f"proposal file(s): {collision_list}{more}. Commit or revert those "
                f"paths before retrying."
            ),
            non_colliding,
        )

    return True, None, non_colliding


def execute_proposal(proposal: PRProposal) -> ExecutionResult:
    """Run the full git/gh workflow. Synchronous — call from a thread.

    Steps:
      1. Verify the working tree has no changes that collide with the
         proposal's file list. Unrelated dirty files are tolerated and
         logged.
      2. fetch + checkout default branch + pull.
      3. Create the new branch.
      4. Write files (mkdir -p for parent dirs).
      5. Surgical git add of just the proposal's paths + commit + push.
         We do NOT `git add -A`: that would sweep up unrelated dirty
         files into the agent's commit.
      6. gh pr create.

    Idempotency: if the branch already exists locally, error out — we
    don't want to silently overwrite a previous push attempt. Andy can
    delete the branch and retry.
    """
    cwd = proposal.repo.local_path
    proposal_paths = {p for p, _ in proposal.files}

    # 1. Working-tree collision check (intersection-based, not blanket).
    safe, err, non_colliding = _check_working_tree_collision(cwd, proposal_paths)
    if not safe:
        return ExecutionResult(ok=False, error=err)
    if non_colliding:
        _logger.info(
            "proposal %s: working tree at %s has %d unrelated dirty path(s); proceeding. First: %s",
            proposal.proposal_id, cwd, len(non_colliding), non_colliding[0],
        )

    # 2. Sync default branch.
    ok, msg = _git(cwd, ["fetch", "origin", proposal.repo.default_branch])
    if not ok:
        return ExecutionResult(ok=False, error=f"git fetch failed: {msg}")
    ok, msg = _git(cwd, ["checkout", proposal.repo.default_branch])
    if not ok:
        return ExecutionResult(ok=False, error=f"git checkout {proposal.repo.default_branch} failed: {msg}")
    ok, msg = _git(cwd, ["pull", "--ff-only", "origin", proposal.repo.default_branch])
    if not ok:
        return ExecutionResult(ok=False, error=f"git pull --ff-only failed: {msg}")

    # 3. Branch off — refuse if branch already exists.
    ok, _ = _git(cwd, ["show-ref", "--verify", "--quiet", f"refs/heads/{proposal.branch}"])
    if ok:
        return ExecutionResult(
            ok=False,
            error=f"branch {proposal.branch} already exists locally; delete it (or pick another name) and retry",
        )
    ok, msg = _git(cwd, ["checkout", "-b", proposal.branch])
    if not ok:
        return ExecutionResult(ok=False, error=f"git checkout -b failed: {msg}")

    # 4. Write files.
    for rel_path, content in proposal.files:
        target = cwd / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    # 5. Surgical add (one path at a time) + commit + push. We avoid
    # `git add -A` because the collision check now tolerates unrelated
    # dirty files in the working tree; we don't want those staged.
    for rel_path in sorted(proposal_paths):
        ok, msg = _git(cwd, ["add", "--", rel_path])
        if not ok:
            return ExecutionResult(ok=False, error=f"git add {rel_path} failed: {msg}")
    ok, msg = _git(cwd, ["commit", "-m", proposal.commit_message])
    if not ok:
        return ExecutionResult(ok=False, error=f"git commit failed: {msg}")
    ok, msg = _git(cwd, ["push", "-u", "origin", proposal.branch])
    if not ok:
        return ExecutionResult(ok=False, error=f"git push failed: {msg}")

    # 6. Open PR via gh.
    ok, msg = _gh([
        "pr", "create",
        "--repo", proposal.repo.gh_slug,
        "--title", proposal.pr_title,
        "--body", proposal.pr_body,
        "--head", proposal.branch,
        "--base", proposal.repo.default_branch,
    ])
    if not ok:
        return ExecutionResult(
            ok=False,
            branch=proposal.branch,
            error=f"gh pr create failed (branch pushed): {msg}",
        )

    pr_url = msg.strip().splitlines()[-1] if msg else None
    return ExecutionResult(ok=True, pr_url=pr_url, branch=proposal.branch)


def _git(cwd: Path, args: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        proc = sp.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=sp.DEVNULL,
        )
    except (sp.TimeoutExpired, FileNotFoundError) as exc:
        return False, type(exc).__name__
    if proc.returncode != 0:
        snippet = (proc.stderr or proc.stdout or "")[:300].replace("\n", " ")
        return False, f"git_exit_{proc.returncode}: {snippet}"
    return True, proc.stdout.strip()


def _gh(args: list[str], timeout: int = 90) -> tuple[bool, str]:
    try:
        proc = sp.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=sp.DEVNULL,
        )
    except (sp.TimeoutExpired, FileNotFoundError) as exc:
        return False, type(exc).__name__
    if proc.returncode != 0:
        snippet = (proc.stderr or "")[:300].replace("\n", " ")
        return False, f"gh_exit_{proc.returncode}: {snippet}"
    return True, proc.stdout.strip()
