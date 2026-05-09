"""Triage logic — pure helpers separated from handlers.py.

Anything in here must NOT import discord, so the test suite can run on a
system Python without the venv. Handlers.py wires these into the discord
interaction flow.
"""

from __future__ import annotations

import asyncio
import json
import subprocess as sp
from pathlib import Path

from .claude_invoke import sanitize_untrusted_text
from .state_machine import STATE_LABEL_SET

VALID_TRIAGE_SPECIALISTS = {
    "senior-pm", "research", "teaching-prep", "content", "social",
    "recruiter", "automation-engineer", "security-reviewer", "docs-editor",
}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}

TRIAGE_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "triage_v1.md"


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def validate_triage_output(data: dict) -> tuple[bool, str | None]:
    required = {
        "recommended_specialist", "priority", "recommended_state",
        "labels_to_add", "labels_to_remove", "reason", "quality_flags",
    }
    missing = required - set(data.keys())
    if missing:
        return False, f"missing keys: {sorted(missing)}"
    if data["recommended_specialist"] not in VALID_TRIAGE_SPECIALISTS:
        return False, f"invalid recommended_specialist: {data['recommended_specialist']}"
    if data["priority"] not in VALID_PRIORITIES:
        return False, f"invalid priority: {data['priority']}"
    if data["recommended_state"] not in STATE_LABEL_SET:
        return False, f"invalid recommended_state: {data['recommended_state']}"
    if not isinstance(data["labels_to_add"], list) or not all(isinstance(x, str) for x in data["labels_to_add"]):
        return False, "labels_to_add must be list[str]"
    if not isinstance(data["labels_to_remove"], list) or not all(isinstance(x, str) for x in data["labels_to_remove"]):
        return False, "labels_to_remove must be list[str]"
    if not isinstance(data["reason"], str) or not data["reason"].strip():
        return False, "reason must be a non-empty string"
    if not isinstance(data["quality_flags"], list):
        return False, "quality_flags must be a list"

    # auto_fixes is optional (defaulting to []) for backward compatibility,
    # but if present must be a list of well-formed objects.
    auto_fixes = data.get("auto_fixes", [])
    if not isinstance(auto_fixes, list):
        return False, "auto_fixes must be a list when present"
    for i, fix in enumerate(auto_fixes):
        if not isinstance(fix, dict):
            return False, f"auto_fixes[{i}] must be an object"
        for key in ("description", "section_header", "content"):
            if key not in fix:
                return False, f"auto_fixes[{i}] missing key: {key}"
            if not isinstance(fix[key], str) or not fix[key].strip():
                return False, f"auto_fixes[{i}].{key} must be a non-empty string"

    return True, None


def apply_auto_fixes(body: str, auto_fixes: list[dict]) -> tuple[str, list[str]]:
    """Apply each auto_fix to the issue body. Idempotent: a fix whose
    section header already appears in the body is skipped. Returns
    (new_body, list_of_applied_descriptions)."""
    new_body = body or ""
    applied: list[str] = []
    for fix in auto_fixes:
        header = fix["section_header"].strip()
        # Idempotency: don't re-add a section that already exists. Look for
        # `## <header>` at the start of a line (any case) anywhere in body.
        header_pattern = f"\n## {header}\n"
        if header_pattern.lower() in ("\n" + new_body + "\n").lower():
            continue
        # Also skip if the body literally starts with the header line (no
        # preceding newline).
        if new_body.lower().startswith(f"## {header}\n".lower()):
            continue
        section = f"\n\n## {header}\n\n{fix['content'].strip()}\n"
        new_body = new_body.rstrip() + section
        applied.append(fix["description"])
    return new_body, applied


def fetch_issue_sync(repo: str, issue_number: int, timeout: int = 30) -> tuple[bool, dict | None, str | None]:
    """Fetch issue title/body/labels/state via gh issue view --json."""
    args = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "title,body,labels,state",
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
    return True, data, None


async def fetch_issue(repo: str, issue_number: int, timeout: int = 30) -> tuple[bool, dict | None, str | None]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fetch_issue_sync(repo, issue_number, timeout))


def build_triage_prompt(template: str, *, repo: str, issue_number: int, issue: dict) -> str:
    title = sanitize_untrusted_text(issue.get("title", "") or "", "github-issue")
    body = sanitize_untrusted_text(issue.get("body", "") or "(empty body)", "github-issue")
    labels = ", ".join(
        lbl["name"]
        for lbl in issue.get("labels", [])
        if isinstance(lbl, dict) and "name" in lbl
    ) or "(none)"
    return (
        template
        .replace("{repo}", repo)
        .replace("{issue_number}", str(issue_number))
        .replace("{issue_title}", title)
        .replace("{issue_body}", body)
        .replace("{current_labels}", labels)
    )
