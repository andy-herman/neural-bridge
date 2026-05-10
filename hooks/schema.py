"""Schema validation for daily-log files (ADR-007).

Shared by flush.py, compile.py, and lint.py. Pure stdlib, no external deps.
"""

from __future__ import annotations

import re
from typing import Any

SCHEMA_VERSION = "1.0"

KNOWN_AGENTS = {
    "research", "teaching-prep", "content", "senior-pm", "social",
    "recruiter", "automation-engineer", "security-reviewer", "docs-editor",
    "luna",
    "_unattributed",
}
HOOK_EVENTS = {"SessionEnd", "PreCompact"}

REQUIRED_FLUSH_KEYS = {"decisions", "findings", "open_questions", "proposed_concepts"}
SECTION_HEADERS_ORDERED = ("Decisions", "Findings", "Open questions", "Proposed concepts")

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def validate_flush_output(data: Any) -> tuple[bool, str | None]:
    """Validate the JSON output produced by the flush prompt.

    Returns (True, None) on success, (False, reason) on failure.
    """
    if not isinstance(data, dict):
        return False, "output is not a JSON object"

    keys = set(data.keys())
    if keys != REQUIRED_FLUSH_KEYS:
        missing = REQUIRED_FLUSH_KEYS - keys
        extra = keys - REQUIRED_FLUSH_KEYS
        parts = []
        if missing:
            parts.append(f"missing={sorted(missing)}")
        if extra:
            parts.append(f"extra={sorted(extra)}")
        return False, "keys mismatch: " + ", ".join(parts)

    for key in ("decisions", "findings", "open_questions"):
        items = data[key]
        if not isinstance(items, list):
            return False, f"{key} is not a list"
        for i, item in enumerate(items):
            if not isinstance(item, str):
                return False, f"{key}[{i}] is not a string"
            if not item.strip():
                return False, f"{key}[{i}] is empty or whitespace"

    concepts = data["proposed_concepts"]
    if not isinstance(concepts, list):
        return False, "proposed_concepts is not a list"
    for i, item in enumerate(concepts):
        if not isinstance(item, dict):
            return False, f"proposed_concepts[{i}] is not an object"
        if set(item.keys()) != {"slug", "summary"}:
            return False, f"proposed_concepts[{i}] keys must be exactly slug,summary"
        slug = item["slug"]
        summary = item["summary"]
        if not isinstance(slug, str) or not slug:
            return False, f"proposed_concepts[{i}].slug must be a non-empty string"
        if not SLUG_RE.match(slug):
            return False, f"proposed_concepts[{i}].slug is not kebab-case: {slug!r}"
        if not isinstance(summary, str) or not summary.strip():
            return False, f"proposed_concepts[{i}].summary must be a non-empty string"

    return True, None


def is_empty_session(data: dict) -> bool:
    """True iff all four flush-output sections are empty."""
    return (
        not data.get("decisions")
        and not data.get("findings")
        and not data.get("open_questions")
        and not data.get("proposed_concepts")
    )
