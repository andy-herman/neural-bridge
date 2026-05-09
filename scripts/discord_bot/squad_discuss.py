"""Squad-discuss helpers — pure (no discord) so they're testable.

The full handler in handlers.py wires these into the discord interaction
flow and uses client_registry.post_as_agent to make each specialist bot
speak as itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from .claude_invoke import sanitize_untrusted_text

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
FRAMING_PROMPT_PATH = PROMPTS_DIR / "squad_discuss_framing_v1.md"
TURN_PROMPT_PATH = PROMPTS_DIR / "squad_turn_v1.md"

VALID_SPECIALISTS = {
    "research", "teaching-prep", "content", "social",
    "recruiter", "automation-engineer", "security-reviewer", "docs-editor",
}

MAX_TURN_CHARS = 1500
MAX_FRAMING_CHARS = 1500


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


def validate_framing_output(data: dict) -> tuple[bool, str | None]:
    if "framing" not in data or "selected_agents" not in data:
        return False, "missing required keys"
    if not isinstance(data["framing"], str) or not data["framing"].strip():
        return False, "framing must be a non-empty string"
    if not isinstance(data["selected_agents"], list):
        return False, "selected_agents must be a list"
    agents = data["selected_agents"]
    if not (1 <= len(agents) <= 3):
        return False, f"selected_agents must have 1-3 entries (got {len(agents)})"
    if len(set(agents)) != len(agents):
        return False, "selected_agents has duplicates"
    for a in agents:
        if not isinstance(a, str) or a not in VALID_SPECIALISTS:
            return False, f"invalid specialist: {a!r}"
    return True, None


def build_framing_prompt(template: str, *, topic: str) -> str:
    sanitized = sanitize_untrusted_text(topic, "topic")
    return template.replace("{topic}", sanitized)


def build_turn_prompt(template: str, *, agent_id: str, topic: str, framing: str) -> str:
    return (
        template
        .replace("{agent_id}", agent_id)
        .replace("{topic}", sanitize_untrusted_text(topic, "topic"))
        .replace("{framing}", sanitize_untrusted_text(framing, "framing"))
    )


def truncate_turn(text: str, *, limit: int = MAX_TURN_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def truncate_framing(text: str, *, limit: int = MAX_FRAMING_CHARS) -> str:
    return truncate_turn(text, limit=limit)
