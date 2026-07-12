"""GitHub issue queue as a label state machine.

The queue is GitHub itself; the "lock" is GitHub's atomic label API. There is
no separate database and no distributed locking:

    agent-ready  --claim-->  agent-running  --success-->  agent-review
                                            --strikes--->  agent-failed
                                            --too-big--->  agent-blocked

Claiming is: list oldest `agent-ready` issue, re-fetch it, verify it still has
`agent-ready` and NOT `agent-running` (idempotent re-check to lose gracefully
if a second process grabbed it a millisecond earlier), then swap the label.
Because `gh issue edit --remove-label ready --add-label running` is a single
API mutation and the list query filters on the label, a racing process simply
won't see a claimed issue.

Crash recovery: on startup and on graceful shutdown, any issue stuck in
`agent-running` is reset to `agent-ready` (see `recover_stuck`).

The JSON-parsing helpers are pure and unit-tested; the functions that shell
out to `gh` are thin and never raise.
"""

from __future__ import annotations

import json
import subprocess as sp
from dataclasses import dataclass

DEFAULT_TIMEOUT = 30


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    url: str

    def has_label(self, label: str) -> bool:
        return label in self.labels


# ---------- Pure parsing ----------

def label_names(raw_labels: object) -> tuple[str, ...]:
    """gh returns labels as a list of {name, color, ...} objects. Extract names.

    Tolerant of plain-string label lists too (some gh versions / manual JSON).
    """
    if not isinstance(raw_labels, list):
        return ()
    names: list[str] = []
    for item in raw_labels:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
        elif isinstance(item, str) and item:
            names.append(item)
    return tuple(names)


def parse_issue_obj(obj: dict) -> Issue | None:
    """Build an Issue from one gh JSON object. Returns None if malformed."""
    number = obj.get("number")
    if not isinstance(number, int):
        return None
    return Issue(
        number=number,
        title=str(obj.get("title") or ""),
        body=str(obj.get("body") or ""),
        labels=label_names(obj.get("labels")),
        url=str(obj.get("url") or ""),
    )


def parse_issues_json(raw: str) -> list[Issue]:
    """Parse `gh issue list --json ...` output into Issues. Never raises."""
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[Issue] = []
    for obj in data:
        if isinstance(obj, dict):
            issue = parse_issue_obj(obj)
            if issue is not None:
                out.append(issue)
    return out


def select_next(issues: list[Issue]) -> Issue | None:
    """Oldest-first: pick the lowest issue number. FIFO by creation order."""
    if not issues:
        return None
    return min(issues, key=lambda i: i.number)


# ---------- gh shell-outs (thin, never raise) ----------

def _gh(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
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


def list_ready(gh_slug: str, filter_labels: list[str], timeout: int = DEFAULT_TIMEOUT) -> list[Issue]:
    """List open issues carrying ALL of `filter_labels`, oldest first."""
    args = ["issue", "list", "--repo", gh_slug, "--state", "open",
            "--json", "number,title,body,labels,url", "--limit", "50"]
    for label in filter_labels:
        args.extend(["--label", label])
    ok, out = _gh(args, timeout=timeout)
    if not ok:
        return []
    return sorted(parse_issues_json(out), key=lambda i: i.number)


def fetch_issue(gh_slug: str, number: int, timeout: int = DEFAULT_TIMEOUT) -> Issue | None:
    """Re-fetch a single issue's current state (labels may have changed)."""
    ok, out = _gh(
        ["issue", "view", str(number), "--repo", gh_slug,
         "--json", "number,title,body,labels,url"],
        timeout=timeout,
    )
    if not ok:
        return None
    try:
        obj = json.loads(out)
    except json.JSONDecodeError:
        return None
    return parse_issue_obj(obj) if isinstance(obj, dict) else None


def transition(
    gh_slug: str,
    number: int,
    *,
    remove: list[str] | None = None,
    add: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Swap labels on an issue. Missing remove-labels are ignored by gh."""
    args = ["issue", "edit", str(number), "--repo", gh_slug]
    for label in add or []:
        args.extend(["--add-label", label])
    for label in remove or []:
        args.extend(["--remove-label", label])
    return _gh(args, timeout=timeout)


def claim(gh_slug: str, issue: Issue, ready_label: str, running_label: str) -> bool:
    """Atomically move an issue from ready -> running, losing races gracefully.

    Re-fetches immediately before mutating so that if another process already
    claimed it (running_label present, or ready_label gone), we back off.
    """
    fresh = fetch_issue(gh_slug, issue.number)
    if fresh is None:
        return False
    if fresh.has_label(running_label) or not fresh.has_label(ready_label):
        return False  # someone else got it, or it was pulled from the queue
    ok, _ = transition(gh_slug, issue.number, remove=[ready_label], add=[running_label])
    return ok


def recover_stuck(gh_slug: str, running_label: str, ready_label: str) -> list[int]:
    """Reset any issue stuck in `running` back to `ready`. Returns reset numbers.

    Called on daemon startup (orphans from a crash) and on graceful shutdown
    (the issue in flight when a signal arrived).
    """
    stuck = list_ready(gh_slug, [running_label])
    reset: list[int] = []
    for issue in stuck:
        ok, _ = transition(gh_slug, issue.number, remove=[running_label], add=[ready_label])
        if ok:
            reset.append(issue.number)
    return reset
