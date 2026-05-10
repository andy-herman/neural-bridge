#!/usr/bin/env python3
"""dashboard.py — Generate markdown dashboards from GitHub data.

Runs `gh` CLI queries against andy-herman/neural-bridge and writes a
dated markdown snapshot to `docs/dashboards/YYYY-MM-DD.md`. Designed
to be run by hand or on a weekly cron.

Sections:
  1. State-machine breakdown (open issues by PM-workflow label)
  2. Epic progress (open vs closed per `epic:*` label)
  3. Build phase (open vs closed per `build:*` label)
  4. Category distribution (`content`, `decision`, `adr`, `agent-driven`)
  5. Activity (last 7 days): opened, closed, recently commented
  6. Stale items: open >14 days with no recent activity
  7. Recent merged PRs (last 10)
  8. Top issue authors (last 30 days)

The output is a single markdown file with section headings, summary
counts, and tabular detail. Useful as a build-in-public artifact
checked into the repo, and as a starting point for the GitHub Projects
views (which can't be created via CLI — see comments at end of this file).

Usage:
  python3 scripts/dashboard.py
  python3 scripts/dashboard.py --repo andy-herman/neural-bridge --output docs/dashboards/
  python3 scripts/dashboard.py --no-write   # print to stdout instead

No external dependencies. Pure stdlib + `gh` CLI.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = "andy-herman/neural-bridge"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs" / "dashboards"
GH_TIMEOUT = 60

# Label taxonomies (from `gh label list`).
STATE_MACHINE_LABELS = [
    "agent-inbox", "agent-ready", "agent-running",
    "agent-review", "needs-human", "blocked", "agent-done",
]
BUILD_PHASE_LABELS = ["build:v1", "build:v2", "build:v3"]
EPIC_LABELS_PREFIX = "epic:"
CATEGORY_LABELS = ["content", "decision", "adr", "agent-driven", "documentation"]


def run_gh(args: list[str]) -> str:
    """Run gh and return stdout. Returns "" on error."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"gh failed: {type(exc).__name__}: {' '.join(args[:4])}", file=sys.stderr)
        return ""
    if result.returncode != 0:
        print(f"gh exit {result.returncode}: {result.stderr[:200]}", file=sys.stderr)
        return ""
    return result.stdout


def list_issues(repo: str, state: str = "open", limit: int = 200) -> list[dict]:
    out = run_gh([
        "issue", "list", "--repo", repo, "--state", state, "--limit", str(limit),
        "--json", "number,title,labels,state,createdAt,updatedAt,closedAt,author,comments",
    ])
    return json.loads(out) if out else []


def list_prs(repo: str, state: str = "all", limit: int = 50) -> list[dict]:
    out = run_gh([
        "pr", "list", "--repo", repo, "--state", state, "--limit", str(limit),
        "--json", "number,title,state,mergedAt,createdAt,closedAt,author,additions,deletions",
    ])
    return json.loads(out) if out else []


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def labels_of(item: dict) -> list[str]:
    return [lbl["name"] for lbl in item.get("labels", [])]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------- section builders ----------


def section_state_machine(open_issues: list[dict]) -> str:
    """Open-issue counts by PM-workflow label."""
    counts: Counter[str] = Counter()
    untagged = 0
    for issue in open_issues:
        lbls = set(labels_of(issue))
        sm = lbls & set(STATE_MACHINE_LABELS)
        if not sm:
            untagged += 1
        for label in sm:
            counts[label] += 1

    lines = [
        "## 1. State-machine breakdown (open issues)",
        "",
        f"_Showing {len(open_issues)} open issues across the PM workflow labels._",
        "",
        "| Label | Count |",
        "|---|---|",
    ]
    for label in STATE_MACHINE_LABELS:
        lines.append(f"| `{label}` | {counts.get(label, 0)} |")
    lines.append(f"| _(no state-machine label)_ | {untagged} |")
    return "\n".join(lines)


def section_epic_progress(open_issues: list[dict], closed_issues: list[dict]) -> str:
    """Per-epic: count open vs closed."""
    open_per_epic: Counter[str] = Counter()
    closed_per_epic: Counter[str] = Counter()
    for issue in open_issues:
        for label in labels_of(issue):
            if label.startswith(EPIC_LABELS_PREFIX):
                open_per_epic[label] += 1
    for issue in closed_issues:
        for label in labels_of(issue):
            if label.startswith(EPIC_LABELS_PREFIX):
                closed_per_epic[label] += 1

    epics = sorted(set(open_per_epic) | set(closed_per_epic))
    lines = [
        "## 2. Epic progress",
        "",
    ]
    if not epics:
        lines.append("_No `epic:*` labels in use._")
        return "\n".join(lines)

    lines += [
        "| Epic | Open | Closed | Total | % done |",
        "|---|---|---|---|---|",
    ]
    for epic in epics:
        o = open_per_epic.get(epic, 0)
        c = closed_per_epic.get(epic, 0)
        total = o + c
        pct = (c / total * 100) if total else 0
        lines.append(f"| `{epic}` | {o} | {c} | {total} | {pct:.0f}% |")
    return "\n".join(lines)


def section_build_phase(open_issues: list[dict], closed_issues: list[dict]) -> str:
    open_per: Counter[str] = Counter()
    closed_per: Counter[str] = Counter()
    for issue in open_issues:
        for label in labels_of(issue):
            if label in BUILD_PHASE_LABELS:
                open_per[label] += 1
    for issue in closed_issues:
        for label in labels_of(issue):
            if label in BUILD_PHASE_LABELS:
                closed_per[label] += 1

    lines = [
        "## 3. Build phase",
        "",
        "| Phase | Open | Closed | % done |",
        "|---|---|---|---|",
    ]
    for phase in BUILD_PHASE_LABELS:
        o = open_per.get(phase, 0)
        c = closed_per.get(phase, 0)
        total = o + c
        pct = (c / total * 100) if total else 0
        lines.append(f"| `{phase}` | {o} | {c} | {pct:.0f}% |")
    return "\n".join(lines)


def section_category(open_issues: list[dict], closed_issues: list[dict]) -> str:
    counts_open: Counter[str] = Counter()
    counts_closed: Counter[str] = Counter()
    for issue in open_issues:
        for label in CATEGORY_LABELS:
            if label in labels_of(issue):
                counts_open[label] += 1
    for issue in closed_issues:
        for label in CATEGORY_LABELS:
            if label in labels_of(issue):
                counts_closed[label] += 1

    lines = [
        "## 4. Category distribution",
        "",
        "| Category | Open | Closed |",
        "|---|---|---|",
    ]
    for cat in CATEGORY_LABELS:
        lines.append(f"| `{cat}` | {counts_open.get(cat, 0)} | {counts_closed.get(cat, 0)} |")
    return "\n".join(lines)


def section_activity(open_issues: list[dict], closed_issues: list[dict]) -> str:
    """Last 7 days: opened, closed, recently commented."""
    cutoff = now_utc() - timedelta(days=7)
    opened: list[dict] = []
    closed_recent: list[dict] = []
    commented: list[dict] = []

    for issue in open_issues + closed_issues:
        created = parse_iso(issue.get("createdAt"))
        closed_at = parse_iso(issue.get("closedAt"))
        updated = parse_iso(issue.get("updatedAt"))

        if created and created >= cutoff:
            opened.append(issue)
        if closed_at and closed_at >= cutoff:
            closed_recent.append(issue)
        # "Commented" = updated within 7 days but not opened/closed in the same window
        if updated and updated >= cutoff:
            commented.append(issue)

    lines = [
        "## 5. Activity (last 7 days)",
        "",
        f"- **Opened:** {len(opened)}",
        f"- **Closed:** {len(closed_recent)}",
        f"- **Touched (any update):** {len(commented)}",
        "",
    ]

    if opened:
        lines.append("### Opened this week")
        lines.append("")
        for issue in sorted(opened, key=lambda i: i.get("createdAt", ""), reverse=True)[:10]:
            n = issue["number"]
            t = issue["title"][:80]
            lines.append(f"- [#{n}](https://github.com/{REPO_GLOBAL}/issues/{n}) {t}")
        lines.append("")

    if closed_recent:
        lines.append("### Closed this week")
        lines.append("")
        for issue in sorted(closed_recent, key=lambda i: i.get("closedAt", ""), reverse=True)[:10]:
            n = issue["number"]
            t = issue["title"][:80]
            lines.append(f"- [#{n}](https://github.com/{REPO_GLOBAL}/issues/{n}) {t}")
        lines.append("")

    return "\n".join(lines)


def section_stale(open_issues: list[dict], days: int = 14) -> str:
    """Open issues with no update in N+ days."""
    cutoff = now_utc() - timedelta(days=days)
    stale: list[dict] = []
    for issue in open_issues:
        updated = parse_iso(issue.get("updatedAt"))
        if updated and updated < cutoff:
            stale.append(issue)
    stale.sort(key=lambda i: i.get("updatedAt", ""))

    lines = [
        f"## 6. Stale items (open, no update in {days}+ days)",
        "",
        f"_{len(stale)} stale issues._",
        "",
    ]
    if not stale:
        return "\n".join(lines)

    lines += [
        "| # | Title | Last updated | Labels |",
        "|---|---|---|---|",
    ]
    for issue in stale[:15]:
        n = issue["number"]
        t = issue["title"][:60]
        u = (issue.get("updatedAt") or "")[:10]
        lbls = ", ".join(f"`{x}`" for x in labels_of(issue)[:4])
        lines.append(f"| [#{n}](https://github.com/{REPO_GLOBAL}/issues/{n}) | {t} | {u} | {lbls} |")
    return "\n".join(lines)


def section_recent_prs(prs: list[dict], limit: int = 10) -> str:
    """Last N merged PRs."""
    merged = [p for p in prs if p.get("mergedAt")]
    merged.sort(key=lambda p: p.get("mergedAt", ""), reverse=True)

    lines = [
        f"## 7. Recent merged PRs (last {limit})",
        "",
    ]
    if not merged:
        lines.append("_No merged PRs found._")
        return "\n".join(lines)

    lines += [
        "| # | Title | Merged | +/- |",
        "|---|---|---|---|",
    ]
    for pr in merged[:limit]:
        n = pr["number"]
        t = pr["title"][:70]
        m = (pr.get("mergedAt") or "")[:10]
        adds = pr.get("additions", 0)
        dels = pr.get("deletions", 0)
        lines.append(f"| [#{n}](https://github.com/{REPO_GLOBAL}/pull/{n}) | {t} | {m} | +{adds}/-{dels} |")
    return "\n".join(lines)


def section_authors(open_issues: list[dict], closed_issues: list[dict],
                    days: int = 30) -> str:
    """Issue authors over the window. (Senior-pm files via daemon → author=Andy.
    This is a placeholder for richer agent-attribution; the structured signal
    isn't in the issue payload today, so this section reflects raw GitHub authors.)"""
    cutoff = now_utc() - timedelta(days=days)
    counts: Counter[str] = Counter()
    for issue in open_issues + closed_issues:
        created = parse_iso(issue.get("createdAt"))
        if created and created >= cutoff:
            login = (issue.get("author") or {}).get("login", "<unknown>")
            counts[login] += 1

    lines = [
        f"## 8. Issue authors (last {days} days)",
        "",
        "_Raw GitHub author. Agent-driven issues currently filed under the daemon's gh user (see follow-up: structured agent attribution)._",
        "",
        "| Author | Count |",
        "|---|---|",
    ]
    for author, count in counts.most_common():
        lines.append(f"| `{author}` | {count} |")
    return "\n".join(lines)


# ---------- main ----------


REPO_GLOBAL = ""  # set in main, used by section helpers


def main(argv: list[str] | None = None) -> int:
    global REPO_GLOBAL
    parser = argparse.ArgumentParser(description="Generate GitHub dashboards markdown.")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR),
                        help="Output directory; file is YYYY-MM-DD.md inside it.")
    parser.add_argument("--no-write", action="store_true",
                        help="Print to stdout instead of writing to disk.")
    args = parser.parse_args(argv)

    REPO_GLOBAL = args.repo

    print(f"Fetching issues + PRs from {args.repo}...", file=sys.stderr)
    open_issues = list_issues(args.repo, state="open", limit=200)
    closed_issues = list_issues(args.repo, state="closed", limit=200)
    prs = list_prs(args.repo, state="all", limit=50)
    print(f"  open={len(open_issues)} closed={len(closed_issues)} prs={len(prs)}", file=sys.stderr)

    today = now_utc().strftime("%Y-%m-%d")
    header = (
        f"# Neural Bridge dashboard — {today}\n\n"
        f"_Generated by `scripts/dashboard.py` against `{args.repo}`._\n\n"
        f"Open issues: **{len(open_issues)}**. "
        f"Closed issues (sample of last 200): **{len(closed_issues)}**. "
        f"PRs sampled: **{len(prs)}**.\n"
    )

    sections = [
        header,
        section_state_machine(open_issues),
        section_epic_progress(open_issues, closed_issues),
        section_build_phase(open_issues, closed_issues),
        section_category(open_issues, closed_issues),
        section_activity(open_issues, closed_issues),
        section_stale(open_issues),
        section_recent_prs(prs),
        section_authors(open_issues, closed_issues),
    ]

    body = "\n\n".join(sections) + "\n"

    if args.no_write:
        print(body)
        return 0

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{today}.md"
    target.write_text(body, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# Notes on GitHub Projects v2 views
# ---------------------------------------------------------------------------
# GitHub Projects v2 *views* (the saved board / table / roadmap configurations
# at https://github.com/users/andy-herman/projects/2/views/N) cannot be created
# or modified via gh CLI today. The recommended additional views to create
# manually in the UI:
#
#   1. PM workflow board — Layout: Board, Group by: Labels, Filter:
#      label:agent-inbox,agent-ready,agent-running,agent-review,needs-human,blocked,agent-done
#
#   2. Epic table — Layout: Table, Group by: Labels (start typing "epic:"),
#      Sort: Status asc, Filter: label:epic:*
#
#   3. Build phase board — Layout: Board, Group by: Labels with build:v1/v2/v3
#
#   4. Stale board — Layout: Table, Filter: is:open updated:<14d, Sort: updated asc
#
# Once those views exist they read the same items this script reads. The
# script's job is to produce a versioned markdown snapshot you can commit
# to the repo and read in Obsidian.
