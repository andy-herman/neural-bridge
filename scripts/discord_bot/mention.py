"""Mention routing helpers — pure (no discord import) so they're testable.

When any Neural Bridge agent is @-mentioned in Discord, the daemon
spawns claude -p with that agent's plugin definition + conversation
context, and posts the response.
"""

from __future__ import annotations

from pathlib import Path

from .claude_invoke import sanitize_untrusted_text

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MENTION_PROMPT_PATH = PROMPTS_DIR / "mention_v1.md"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = REPO_ROOT / "plugins" / "neural-bridge-core" / "agents"

MAX_RESPONSE_CHARS = 1500
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS_PER_MESSAGE = 500


def load_agent_definition(agent_id: str, agents_dir: Path = AGENTS_DIR) -> str:
    """Read plugins/neural-bridge-core/agents/<agent-id>.md and strip the
    YAML frontmatter. Returns the body — the agent's role definition,
    operating rules, voice, etc."""
    path = agents_dir / f"{agent_id}.md"
    if not path.exists():
        return f"_(agent definition not found at {path.name})_"
    text = path.read_text(encoding="utf-8")
    # Strip leading frontmatter
    if text.startswith("---"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5:]
    return text.strip()


def format_discord_history(messages: list[dict]) -> str:
    """Render a list of dicts (each with `author`, `content`) as a wrapped
    history block. Truncates per-message content to keep the prompt budgeted.

    Each dict shape:
      {"author": "<display name>", "content": "<message text>"}
    """
    if not messages:
        return "(no recent messages)"
    lines: list[str] = []
    for msg in messages[-MAX_HISTORY_MESSAGES:]:
        author = sanitize_untrusted_text(str(msg.get("author", "?")), "discord-history")
        content = str(msg.get("content", ""))
        if len(content) > MAX_HISTORY_CHARS_PER_MESSAGE:
            content = content[: MAX_HISTORY_CHARS_PER_MESSAGE - 1].rstrip() + "…"
        content = sanitize_untrusted_text(content, "discord-history")
        lines.append(f"[{author}] {content}")
    return "\n".join(lines)


def build_mention_prompt(
    template: str,
    *,
    agent_id: str,
    agent_definition: str,
    channel_kind: str,
    history: list[dict],
    message_content: str,
) -> str:
    history_block = format_discord_history(history)
    sanitized_message = sanitize_untrusted_text(message_content, "message")
    sanitized_definition = sanitize_untrusted_text(agent_definition, "agent-definition")
    return (
        template
        .replace("{agent_id}", agent_id)
        .replace("{agent_definition}", sanitized_definition)
        .replace("{channel_kind}", channel_kind)
        .replace("{discord_history}", history_block)
        .replace("{message}", sanitized_message)
    )


def truncate_response(text: str, *, limit: int = MAX_RESPONSE_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def is_mention_for_self(message_mentions: list, my_user) -> bool:
    """Return True if this message @-mentions me (and not just everyone-mentions
    or other bots). Discord.py message.mentions is a list of User objects."""
    if my_user is None:
        return False
    return any(getattr(u, "id", None) == getattr(my_user, "id", None) for u in message_mentions)
