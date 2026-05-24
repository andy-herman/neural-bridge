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
REACT_PROMPT_PATH = PROMPTS_DIR / "squad_react_v1.md"
ROUND_DECISION_PROMPT_PATH = PROMPTS_DIR / "squad_round_decision_v1.md"
REPORT_PROMPT_PATH = PROMPTS_DIR / "squad_report_v1.md"
LUNA_BRIEF_PROMPT_PATH = PROMPTS_DIR / "squad_luna_brief_v1.md"

VALID_SPECIALISTS = {
    "research", "teaching-prep", "content", "social",
    "recruiter", "automation-engineer", "security-reviewer", "docs-editor",
}

# Owners that may appear in action items (specialists + cross-cutting agents).
# Used to validate the report's action-item lines before filing GitHub issues.
VALID_OWNERS = VALID_SPECIALISTS | {"luna", "librarian", "ux-designer", "echo", "senior-pm"}

MAX_ROUNDS = 3

MAX_TURN_CHARS = 50000
MAX_FRAMING_CHARS = 20000


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


# ---------- Multi-round helpers ----------

import re
from dataclasses import dataclass


def format_turn_block(agent_id: str, content: str) -> str:
    """Format one agent's turn for inclusion in a prompt block.

    Used to feed prior turns into the round-decision prompt, the react
    prompt, and the report prompt. Wraps the content with a header that
    cites the agent_id so downstream prompts can reason about who said what.
    """
    return f"### {agent_id}\n\n{content.strip()}"


def format_round_block(round_n: int, turns: list[tuple[str, str]]) -> str:
    """Format one round's turns for inclusion in a prompt block.

    `turns` is a list of (agent_id, content) tuples in posting order.
    """
    parts = [f"## Round {round_n}"]
    for agent_id, content in turns:
        parts.append(format_turn_block(agent_id, content))
    return "\n\n".join(parts)


def format_all_rounds(rounds: list[list[tuple[str, str]]]) -> str:
    """Format every round in chronological order.

    `rounds` is a list-of-rounds; each round is a list of (agent_id, content)
    tuples. Used by the round-decision prompt (only the latest round) and
    by the report prompt (all rounds).
    """
    return "\n\n".join(format_round_block(i + 1, r) for i, r in enumerate(rounds))


def build_round_decision_prompt(
    template: str,
    *,
    topic: str,
    framing: str,
    round_n: int,
    turns: list[tuple[str, str]],
    max_rounds: int = MAX_ROUNDS,
) -> str:
    """Substitute variables into the round-decision template."""
    return (
        template
        .replace("{topic}", sanitize_untrusted_text(topic, "topic"))
        .replace("{framing}", sanitize_untrusted_text(framing, "framing"))
        .replace("{round_n}", str(round_n))
        .replace("{max_rounds}", str(max_rounds))
        .replace("{turns}", format_round_block(round_n, turns))
    )


def build_react_prompt(
    template: str,
    *,
    agent_id: str,
    topic: str,
    framing: str,
    round_n: int,
    prior_rounds: list[list[tuple[str, str]]],
    round_prompt: str,
) -> str:
    """Substitute variables into the react (round 2+) turn template."""
    return (
        template
        .replace("{agent_id}", agent_id)
        .replace("{topic}", sanitize_untrusted_text(topic, "topic"))
        .replace("{framing}", sanitize_untrusted_text(framing, "framing"))
        .replace("{round_n}", str(round_n))
        .replace("{prior_turns}", format_all_rounds(prior_rounds))
        .replace("{round_prompt}", sanitize_untrusted_text(round_prompt, "round_prompt"))
    )


def build_report_prompt(
    template: str,
    *,
    topic: str,
    framing: str,
    rounds: list[list[tuple[str, str]]],
    thread_url: str,
    date: str,
) -> str:
    """Substitute variables into the report template."""
    return (
        template
        .replace("{topic}", sanitize_untrusted_text(topic, "topic"))
        .replace("{framing}", sanitize_untrusted_text(framing, "framing"))
        .replace("{round_count}", str(len(rounds)))
        .replace("{full_discussion}", format_all_rounds(rounds))
        .replace("{thread_url}", thread_url)
        .replace("{date}", date)
    )


def build_luna_brief_prompt(
    template: str,
    *,
    topic: str,
    report: str,
    vault_path: str,
    issue_lines: str,
    thread_url: str,
) -> str:
    """Substitute variables into the luna-brief template."""
    return (
        template
        .replace("{topic}", sanitize_untrusted_text(topic, "topic"))
        .replace("{report}", sanitize_untrusted_text(report, "report"))
        .replace("{vault_path}", vault_path)
        .replace("{issue_lines}", issue_lines)
        .replace("{thread_url}", thread_url)
    )


def validate_round_decision_output(data: dict) -> tuple[bool, str | None]:
    """Schema check for the round-decision JSON."""
    if "continue" not in data:
        return False, "missing 'continue' field"
    if not isinstance(data["continue"], bool):
        return False, "'continue' must be boolean"
    if not isinstance(data.get("reason", ""), str):
        return False, "'reason' must be string"
    if not isinstance(data.get("next_round_prompt", ""), str):
        return False, "'next_round_prompt' must be string"
    if data["continue"] and not data.get("next_round_prompt", "").strip():
        return False, "'next_round_prompt' must be non-empty when continue=true"
    return True, None


# ---------- Report parsing (action items + slug) ----------


@dataclass
class ActionItem:
    """One row from the report's Action items section."""
    owner: str
    action: str
    when: str

    def issue_title(self, *, max_chars: int = 80) -> str:
        """Truncate the action text to a reasonable issue title."""
        title = self.action.strip()
        if len(title) <= max_chars:
            return title
        return title[: max_chars - 1].rstrip() + "…"


# Match `- [ ] **<owner>**: <action> — <when>` with em-dash OR `--` OR `-` separator.
# Owner is captured WITHOUT the surrounding asterisks. Action and when are split
# on the FIRST occurrence of em-dash, en-dash, or " - " so action text containing
# colons still parses.
ACTION_ITEM_RE = re.compile(
    r"^\s*-\s*\[\s*\]\s*\*\*([a-z0-9-]+)\*\*\s*:\s*(.+?)\s*(?:—|–|--| - )\s*(.+?)\s*$",
    re.MULTILINE,
)


def parse_action_items(report: str, *, valid_owners: set[str] = VALID_OWNERS) -> list[ActionItem]:
    """Extract action items from a squad report markdown body.

    Returns only items where the owner is in `valid_owners`. Silently skips
    malformed lines and lines with unknown owners; the report prompt enforces
    the format but we don't want one bad line to drop all action items.
    """
    # Scope the regex search to the Action items section so we don't pick up
    # bullets from other sections that happen to match the shape.
    action_section = _extract_section(report, "Action items")
    if action_section is None:
        return []
    items: list[ActionItem] = []
    for match in ACTION_ITEM_RE.finditer(action_section):
        owner = match.group(1).strip().lower()
        action = match.group(2).strip()
        when = match.group(3).strip()
        if owner not in valid_owners:
            continue
        if not action or not when:
            continue
        items.append(ActionItem(owner=owner, action=action, when=when))
    return items


def _extract_section(report: str, header: str) -> str | None:
    """Pull out the body under a `## <header>` heading, up to the next `##` or EOF."""
    pattern = rf"^##\s+{re.escape(header)}\s*\n(.*?)(?=^##\s+|\Z)"
    match = re.search(pattern, report, re.MULTILINE | re.DOTALL)
    if not match:
        return None
    return match.group(1)


VAULT_SQUAD_REPORTS_DIR = Path.home() / "Documents" / "Luna Master" / "Neural Bridge" / "Squad Reports"


def squad_report_path(*, date: str, slug: str, base_dir: Path = VAULT_SQUAD_REPORTS_DIR) -> Path:
    """Canonical path for a squad report file in the Obsidian vault."""
    return base_dir / f"{date}-{slug}.md"


def write_squad_report(
    report_md: str,
    *,
    date: str,
    slug: str,
    base_dir: Path = VAULT_SQUAD_REPORTS_DIR,
) -> Path:
    """Write the report markdown to the vault. Returns the written path.

    Creates the parent dir if missing. Overwrites if the same date+slug
    has been used before (rare; same-day duplicate topics would collide).
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    path = squad_report_path(date=date, slug=slug, base_dir=base_dir)
    path.write_text(report_md, encoding="utf-8")
    return path


def obsidian_link_for(path: Path, *, vault_name: str = "Luna Master") -> str:
    """Build an Obsidian URI deep-link for the given vault file."""
    import urllib.parse
    # The path INSIDE the vault, relative to the vault root.
    try:
        vault_root = path
        while vault_root.name != vault_name and vault_root.parent != vault_root:
            vault_root = vault_root.parent
        if vault_root.name == vault_name:
            relative = path.relative_to(vault_root)
        else:
            relative = path
    except (ValueError, RuntimeError):
        relative = path
    encoded_file = urllib.parse.quote(str(relative))
    encoded_vault = urllib.parse.quote(vault_name)
    return f"obsidian://open?vault={encoded_vault}&file={encoded_file}"


def build_issue_body_for_action(
    item: ActionItem,
    *,
    topic: str,
    report_vault_path: Path,
    thread_url: str,
) -> str:
    """Compose the body of the GitHub issue auto-filed for an action item."""
    return (
        f"Filed from a squad discussion.\n\n"
        f"**Topic:** {topic}\n\n"
        f"**Action:** {item.action}\n\n"
        f"**When:** {item.when}\n\n"
        f"**Owner:** `@{item.owner}`\n\n"
        f"**Full report:** `{report_vault_path}` "
        f"([open in Obsidian]({obsidian_link_for(report_vault_path)}))\n\n"
        f"**Discord thread:** {thread_url}\n"
    )


def report_slug(topic: str, *, max_chars: int = 60) -> str:
    """Slugify a topic for the report filename.

    Lowercase, replace non-alphanumeric runs with hyphens, trim leading/trailing
    hyphens, cap length, strip trailing hyphen after the cap.
    """
    s = topic.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if not s:
        return "untitled"
    if len(s) > max_chars:
        s = s[:max_chars].rstrip("-")
    return s
