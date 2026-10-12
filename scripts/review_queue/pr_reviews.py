"""PR review items: the loop engineer's draft PRs, merged from Andy's phone.

The loop engineer (scripts/loop_engineer/) opens a draft PR on an `eng/<issue>`
branch once its gates pass and labels the issue `agent-review`. Until slice C
the only way to approve one was to merge it on GitHub.

Producer. Luna's bridge syncs every open `eng/*` PR on the loop's repo into
the queue every SYNC_SECONDS. The item carries the PR's head commit and the
state of its checks, refreshed on each sync, so a card gains its Merge button
the moment the checks go green. A PR merged or closed on GitHub clears its own
card.

Merge (Approve). Offered only while every check has passed and GitHub calls
the PR mergeable. Merging main deploys the fleet within about two minutes, so
it takes two taps, and the confirm screen names the commit. That commit is
what gets merged: the applier re-reads the PR, refuses if the head moved or
the checks are no longer green, marks the draft ready, and squash-merges with
--match-head-commit, with a subject and body of its own so no tool trailer
reaches main. The issue closes through the PR's "Closes #n".

The issue then moves from agent-review to agent-done.

Close (Reject). Closes the PR with a comment and moves the issue from
agent-review to agent-blocked: a human decision, so the loop does not simply
retry the same work. The branch is kept.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from . import store as st
from .captures import GhFn, run_gh

SOURCE = "loop_engineer"
SYNC_SECONDS = 120
DEFAULT_REPO_ID = "neural-bridge"
BRANCH_PREFIX = "eng/"

GREEN, PENDING, FAILING, NO_CHECKS = "green", "pending", "failing", "no checks"

_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_BAD = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR", "STARTUP_FAILURE", "STALE"}
_ISSUE_RE = re.compile(r"\bCloses #(\d+)\b", re.IGNORECASE)
_PR_FIELDS = "number,title,url,isDraft,mergeable,headRefOid,headRefName,statusCheckRollup,body,state"


def repo_slug(repo_id: str = DEFAULT_REPO_ID) -> str:
    from scripts.discord_bot.repos import repo_for

    repo = repo_for(repo_id)
    if repo is None:
        raise ValueError(f"unknown repo {repo_id!r}")
    return repo.gh_slug


def check_state(rollup: list[dict] | None) -> str:
    """One word for a PR's checks. Status contexts carry `state`, check runs
    carry `status` and `conclusion`."""
    rollup = rollup or []
    if not rollup:
        return NO_CHECKS
    results = []
    for c in rollup:
        if c.get("__typename") == "StatusContext" or ("state" in c and "conclusion" not in c):
            state = str(c.get("state") or "").upper()
            results.append({"SUCCESS": "ok", "PENDING": "pending", "EXPECTED": "pending"}.get(state, "bad"))
        elif str(c.get("status") or "").upper() != "COMPLETED":
            results.append("pending")
        else:
            conclusion = str(c.get("conclusion") or "").upper()
            results.append("ok" if conclusion in _OK else "bad" if conclusion in _BAD else "pending")
    if "bad" in results:
        return FAILING
    if "pending" in results:
        return PENDING
    return GREEN


def issue_number(pr: dict) -> int | None:
    branch = str(pr.get("headRefName") or "")
    if branch.startswith(BRANCH_PREFIX) and branch[len(BRANCH_PREFIX):].isdigit():
        return int(branch[len(BRANCH_PREFIX):])
    m = _ISSUE_RE.search(str(pr.get("body") or ""))
    return int(m.group(1)) if m else None


def mergeable_now(payload: dict) -> bool:
    return payload.get("checks") == GREEN and payload.get("mergeable") == "MERGEABLE"


def entry(pr: dict, repo_id: str) -> dict:
    """The sync entry for one open PR."""
    checks = check_state(pr.get("statusCheckRollup"))
    issue = issue_number(pr)
    sha = str(pr.get("headRefOid") or "")
    mergeable = str(pr.get("mergeable") or "UNKNOWN")
    status = {GREEN: "checks passed", PENDING: "checks running", FAILING: "checks failing",
              NO_CHECKS: "no checks yet"}[checks]
    if checks == GREEN and mergeable == "CONFLICTING":
        status = "checks passed, but it conflicts with main"
    detail = (f"{'Issue #' + str(issue) + ' · ' if issue else ''}{status} · head {sha[:7]}"
              f"{' · draft' if pr.get('isDraft') else ''}")
    return {"key": f"{repo_id}#{pr['number']}", "title": f"PR #{pr['number']}: {pr.get('title', '')}"[:200],
            "detail": detail, "ref": str(pr.get("url") or ""),
            "payload": {"repo": repo_id, "number": int(pr["number"]), "issue": issue, "head_sha": sha,
                        "checks": checks, "mergeable": mergeable, "draft": bool(pr.get("isDraft"))}}


def sync(store: st.Store, *, repo_id: str = DEFAULT_REPO_ID, gh: GhFn = run_gh,
         now: int | None = None) -> tuple[list[st.Item], list[st.Item]]:
    """Make the queue's PR review items match the loop's open PRs."""
    ok, out = gh(["pr", "list", "--repo", repo_slug(repo_id), "--state", "open", "--limit", "50",
                  "--json", _PR_FIELDS])
    if not ok:
        raise RuntimeError(f"gh pr list failed: {out}")
    prs = [p for p in json.loads(out or "[]") if str(p.get("headRefName") or "").startswith(BRANCH_PREFIX)]
    return store.sync(source=SOURCE, kind=st.PR_REVIEW, entries=[entry(p, repo_id) for p in prs], now=now)


def _fetch(slug: str, number: int, gh: GhFn) -> dict:
    ok, out = gh(["pr", "view", str(number), "--repo", slug, "--json", _PR_FIELDS])
    if not ok:
        raise RuntimeError(f"gh pr view failed: {out}")
    return json.loads(out)


def apply(item: st.Item, *, gh: GhFn = run_gh, slug_for: Callable[[str], str] = repo_slug) -> tuple[bool, str]:
    """Applier: approve merges the commit Andy confirmed; reject closes the PR."""
    p = item.payload
    try:
        slug = slug_for(str(p.get("repo") or DEFAULT_REPO_ID))
        number = int(p["number"])
    except (KeyError, ValueError) as exc:
        return False, f"unreadable PR item: {exc}"
    issue = p.get("issue")

    if item.verb == st.REJECT:
        ok, out = gh(["pr", "close", str(number), "--repo", slug,
                      "--comment", "Closed from Andy's review queue. The branch is kept."])
        if not ok:
            return False, f"gh pr close failed: {out}"
        if issue:
            gh(["issue", "edit", str(issue), "--repo", slug,
                "--remove-label", "agent-review", "--add-label", "agent-blocked"])
        return True, f"closed PR #{number}" + (f"; issue #{issue} is agent-blocked" if issue else "")

    if item.verb != st.APPROVE:
        return False, f"PR reviews take merge or close, not {item.verb!r}"
    approved = str(p.get("approved_sha") or "")
    if not approved:
        return False, "no approved commit was recorded; open the card again"
    try:
        pr = _fetch(slug, number, gh)
    except (RuntimeError, ValueError) as exc:
        return False, str(exc)
    if str(pr.get("state")) != "OPEN":
        return False, f"PR #{number} is {str(pr.get('state')).lower()}, not open"
    if str(pr.get("headRefOid")) != approved:
        return False, (f"PR #{number} has new commits since you approved {approved[:7]}; "
                       f"its card will refresh for a fresh look")
    checks = check_state(pr.get("statusCheckRollup"))
    if checks != GREEN:
        return False, f"checks are {checks} on {approved[:7]}; nothing merged"
    if pr.get("isDraft"):
        ok, out = gh(["pr", "ready", str(number), "--repo", slug])
        if not ok:
            return False, f"gh pr ready failed: {out}"
    title = re.sub(r"\s*\(#\d+\)\s*$", "", str(pr.get("title") or f"PR #{number}"))
    body = (f"Closes #{issue}.\n\n" if issue else "") + \
        "Implemented by the loop engineer; merged from Andy's review queue once its checks passed."
    ok, out = gh(["pr", "merge", str(number), "--repo", slug, "--squash", "--match-head-commit", approved,
                  "--subject", f"{title} (#{number})", "--body", body])
    if not ok:
        return False, f"gh pr merge failed: {out}"
    if issue:
        gh(["issue", "edit", str(issue), "--repo", slug,
            "--remove-label", "agent-review", "--add-label", "agent-done"])
    return True, str(pr.get("url") or f"merged PR #{number}")
