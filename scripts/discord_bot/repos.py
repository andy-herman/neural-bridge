"""Repo registry + per-agent push allowlist.

Defines which repos the daemon can act on (open PRs against) and which
agents are allowed to push to each one. Used by the `open_pr_with_changes`
action handler in pr_proposals.py.

Wiring a new repo:
  1. Add a Repo entry to REPOS with its gh slug, local clone path, and
     default branch.
  2. Add the repo to the relevant agents' sets in AGENT_PUSH_REPOS.
  3. (Optional) Add the local_path to ADD_DIRS_PER_AGENT in mention.py
     so the agents that push to it can also Read existing files there
     before deciding what to change.

The local clone path MUST exist on disk before the action will work —
the handler does git operations in-place rather than cloning on demand,
both for speed and so Andy's existing gh authentication carries over.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Repo:
    """One GitHub repo the daemon can open PRs against."""
    repo_id: str            # short key used in the action JSON
    gh_slug: str            # owner/name for gh CLI calls
    local_path: Path        # working tree on this Mac
    default_branch: str     # what new branches fork off


_DEV = Path.home() / "Development"

REPOS: dict[str, Repo] = {
    "neural-bridge": Repo(
        repo_id="neural-bridge",
        gh_slug="andy-herman/neural-bridge",
        local_path=_DEV / "neural-bridge",
        default_branch="main",
    ),
    "neural-bridge-blog": Repo(
        repo_id="neural-bridge-blog",
        gh_slug="andy-herman/neural-bridge-blog",
        local_path=_DEV / "neural-bridge-blog",
        default_branch="main",
    ),
}


# Which repos each agent may push to. Empty / missing = no push rights.
# Approval is STILL required per-action via Discord chat (see pr_proposals.py);
# this allowlist is the OUTER gate — even with Andy's chat approval an
# agent that isn't in the allowlist can't ship.
AGENT_PUSH_REPOS: dict[str, set[str]] = {
    "luna":                {"neural-bridge-blog", "neural-bridge"},  # blog edits + daemon-side fixes Andy asks her to ship
    "content":             {"neural-bridge-blog"},                   # blog posts, frontmatter fixes
    "ux-designer":         {"neural-bridge-blog"},                   # design / template changes
    "recruiter":           {"neural-bridge"},                        # plugin updates (existing flow uses agent_builder, this is parallel)
    "automation-engineer": {"neural-bridge"},                        # daemon / infra / hook fixes; the natural specialist
    "senior-pm":           {"neural-bridge"},                        # so PM-shaped infra triage can ship its own fix without bouncing
    # All other agents: no push rights. They surface to Andy or hand off.
}


def repo_for(repo_id: str) -> Repo | None:
    """Resolve a repo_id to its Repo, or None if unknown."""
    return REPOS.get(repo_id)


def agent_can_push_to(agent_id: str, repo_id: str) -> bool:
    """Outer gate: is this agent in the allowlist for this repo?"""
    return repo_id in AGENT_PUSH_REPOS.get(agent_id, set())


def pushable_repos_for(agent_id: str) -> set[str]:
    """All repos this agent could potentially push to (still pending per-action approval)."""
    return set(AGENT_PUSH_REPOS.get(agent_id, set()))
