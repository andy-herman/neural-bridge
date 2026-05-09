"""PM summary helpers — pure (no discord import) so they're testable.

Fetches the open-issues list via gh, builds the prompt, validates the
output length. handlers.py wires this into the slash command flow.
"""

from __future__ import annotations

import asyncio
import json
import subprocess as sp
from pathlib import Path

from .claude_invoke import sanitize_untrusted_text

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "pm_summary_v1.md"

DEFAULT_LIMIT = 50  # max issues fetched for the prompt
MAX_OUTPUT_CHARS = 1900  # under Discord's 2000


def list_open_issues_sync(repo: str, *, limit: int = DEFAULT_LIMIT, timeout: int = 30) -> tuple[bool, list[dict] | None, str | None]:
    args = [
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,labels,body,createdAt,updatedAt",
    ]
    try:
        proc = sp.run(args, capture_output=True, text=True, timeout=timeout, stdin=sp.DEVNULL)
    except sp.TimeoutExpired:
        return False, None, "timeout"
    except FileNotFoundError:
        return False, None, "gh_cli_not_found"
    if proc.returncode != 0:
        snippet = (proc.stderr or "")[:300].replace("\n", " ").strip()
        return False, None, f"gh_exit_{proc.returncode}: {snippet}"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return False, None, f"json_decode: {exc.msg}"
    if not isinstance(data, list):
        return False, None, "unexpected_shape: not a list"
    return True, data, None


async def list_open_issues(repo: str, *, limit: int = DEFAULT_LIMIT, timeout: int = 30) -> tuple[bool, list[dict] | None, str | None]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: list_open_issues_sync(repo, limit=limit, timeout=timeout))


def compact_issue(issue: dict, *, body_chars: int = 200) -> dict:
    """Reduce a gh-issue payload to the fields the prompt needs.

    Body is truncated and sanitized so an injection attempt in an issue
    body can't escape the wrapping tag in the prompt.
    """
    body = (issue.get("body") or "").strip()
    if len(body) > body_chars:
        body = body[:body_chars].rstrip() + "..."
    body = sanitize_untrusted_text(body, "issue-list")
    title = sanitize_untrusted_text(issue.get("title") or "", "issue-list")
    labels = [
        lbl["name"]
        for lbl in (issue.get("labels") or [])
        if isinstance(lbl, dict) and isinstance(lbl.get("name"), str)
    ]
    return {
        "number": issue.get("number"),
        "title": title,
        "labels": labels,
        "body_excerpt": body,
        "createdAt": issue.get("createdAt"),
        "updatedAt": issue.get("updatedAt"),
    }


def build_summary_prompt(template: str, *, repo: str, issues: list[dict]) -> str:
    compact = [compact_issue(i) for i in issues]
    issue_list_json = json.dumps(compact, indent=2)
    return (
        template
        .replace("{repo}", repo)
        .replace("{open_count}", str(len(compact)))
        .replace("{issue_list}", issue_list_json)
    )


def truncate_for_discord(text: str, *, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n\n_…truncated. See GitHub for the full board._"
    return text[: limit - len(suffix) - 1].rstrip() + "…" + suffix
