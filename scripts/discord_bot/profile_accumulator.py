"""Profile accumulator — captures Andy-authored Discord messages into the vault.

Every message Andy sends across any channel (guild or DM) gets appended to
~/Documents/Luna Master/Andy Profile/raw-conversations.md with a timestamp,
channel label, and a hidden message_id marker for idempotent dedupe.

Echo's future corpus passes read this file as one of her sources, so the
profile gets richer over time as Andy uses the substrate.

Capture happens in every AgentClient's on_message. Each bot tries to append;
the message_id sidecar (.message_ids.txt in the same vault dir) deduplicates
so only the first bot to fire on a given message actually writes. Subsequent
bots see "already captured" and skip cheaply.

DMs work too — the one bot that Andy DM'd fires alone, captures the message.

Only Andy-authored messages (matching authorized_user_ids in agents.json)
are captured. Bot messages, other humans, slash command invocations are
filtered out.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import discord

from .config import BotConfig

VAULT_PROFILE_DIR = Path.home() / "Documents" / "Luna Master" / "Andy Profile"
RAW_CONVERSATIONS_PATH = VAULT_PROFILE_DIR / "raw-conversations.md"
DEDUPE_SIDECAR = VAULT_PROFILE_DIR / ".message_ids.txt"

# Last N message IDs kept in the dedupe sidecar. Beyond this, oldest IDs roll
# off. At human typing speed even a year's worth of messages stays under this.
DEDUPE_BUDGET = 10000

# Cap content length at append time. Long pastes get truncated with a marker;
# Echo can find the truncation and ask Andy if she needs the full content.
MAX_CONTENT_CHARS = 4000

_logger = logging.getLogger("nb_discord")


def _load_recent_ids() -> set[str]:
    """Load recent message IDs from sidecar for dedupe. Returns empty set on first run."""
    if not DEDUPE_SIDECAR.exists():
        return set()
    try:
        lines = DEDUPE_SIDECAR.read_text(encoding="utf-8").splitlines()
        return {line.strip() for line in lines if line.strip()}
    except (OSError, UnicodeDecodeError):
        return set()


def _save_recent_ids(ids: set[str]) -> None:
    """Save dedupe sidecar, trimming to budget. Sorted desc so newest IDs survive."""
    VAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    sorted_ids = sorted(ids, reverse=True)[:DEDUPE_BUDGET]
    DEDUPE_SIDECAR.write_text("\n".join(sorted_ids) + "\n", encoding="utf-8")


def _channel_label(message: discord.Message) -> str:
    """Human-readable channel label for the entry header."""
    if message.guild is None:
        # DM — channel.recipient might not always be populated; fall back gracefully
        bot_name = "bot"
        recipient = getattr(message.channel, "recipient", None)
        if recipient is not None:
            bot_name = getattr(recipient, "name", None) or getattr(recipient, "display_name", None) or "bot"
        return f"DM with {bot_name}"
    name = getattr(message.channel, "name", None) or "unknown"
    return f"#{name}"


def _truncate(text: str, limit: int = MAX_CONTENT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…[content truncated]"


def append_message(message: discord.Message, config: BotConfig) -> bool:
    """Capture an Andy-authored message into raw-conversations.md.

    Returns True if appended, False if skipped (not Andy / bot / empty / dupe / error).
    Idempotent: deduped by message.id via sidecar.
    Never raises — accumulator failures must not break message routing.
    """
    try:
        user_id = str(message.author.id)
        if user_id not in (config.authorized_user_ids or []):
            return False

        if message.author.bot:
            return False

        # Empty messages (attachment-only, embed-only) — skip; we capture text only
        content = (message.content or "").strip()
        if not content:
            return False

        msg_id = str(message.id)
        seen = _load_recent_ids()
        if msg_id in seen:
            return False

        VAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        channel_label = _channel_label(message)
        truncated = _truncate(content)

        # Markdown blockquote so the file reads naturally in Obsidian.
        # Hidden HTML comment carries message_id for dedupe and audit.
        entry = (
            f"\n### {ts} — {channel_label}\n"
            f"<!-- message_id: {msg_id} -->\n\n"
            f"> {truncated}\n"
        )
        with RAW_CONVERSATIONS_PATH.open("a", encoding="utf-8") as f:
            f.write(entry)

        seen.add(msg_id)
        _save_recent_ids(seen)
        return True

    except Exception as exc:  # never let accumulator break the daemon
        _logger.warning(f"profile_accumulator: failed to append message_id={getattr(message, 'id', '?')}: {type(exc).__name__}: {exc}")
        return False
