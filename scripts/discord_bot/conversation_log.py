"""Per-agent Discord conversation archive in the Obsidian vault.

Complements two existing memory layers:

- Claude session resumption (`session_store.py` + `--resume`): short-term
  working memory within a 7-day TTL window. Verbatim, includes tool
  calls, but ephemeral.
- Luna's `notes.md` (auto-injected): summarized standing observations
  about Andy across all conversations. Tight, opinion-shaped.

This module adds the third layer: long-term, verbatim, searchable
conversation archive. Every Discord turn (Andy's message + the agent's
response) is appended to a markdown file in the agent's vault subpage.
After the session TTL expires and the in-memory claude state is gone,
the markdown file is what's left.

File layout:

    Agents/
        <agent_id>/
            conversations/
                2026-05/
                    neural-bridge.md     # guild channel
                    DM-andy.md           # DM with username "andy"
                2026-06/
                    neural-bridge.md
                    ...

One file per (agent × channel × month). Month rotation bounds growth.
The files are plain markdown — Andy can read them in Obsidian; agents
search them via the Glob + Grep tools they already have.

Agents see their own conversation directory via `--add-dir` (wired in
`mention.py:add_dirs_for`). Cross-agent visibility is intentionally NOT
shipped here — each agent reads only their own archive. Cross-agent
memory is a separate ask with different privacy considerations.

The daemon writes through a tempfile-aware append model: the file is
opened with mode "a", which on POSIX is atomic for writes under the
PIPE_BUF limit (4 KB on macOS). Discord messages cap at 2000 chars per
chunk and we batch by turn (Andy + agent), so each append stays well
under that limit in practice. Concurrent writes from multiple mentions
to the same file aren't a concern — the daemon is single-process.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger("nb_discord.conv_log")


# Roots — kept module-level so tests can monkey-patch them without
# touching every internal call site.
VAULT_ROOT = Path.home() / "Documents" / "Luna Master"
AGENTS_BASE = VAULT_ROOT / "Agents"


# Filesystem-safe label. Lowercase alphanumerics + dashes only; everything
# else collapses to a single dash. Length-capped so a pathological channel
# name can't blow up filenames.
_SANITIZE_RE = re.compile(r"[^a-z0-9\-]+")
_MAX_LABEL_LEN = 60


def _sanitize_name(s: str | None) -> str:
    s = (s or "unknown").lower().strip()
    s = _SANITIZE_RE.sub("-", s)
    s = s.strip("-") or "unknown"
    return s[:_MAX_LABEL_LEN]


def channel_label(message) -> tuple[str, str, str]:
    """Resolve (filesystem_label, display_name, kind) for `message.channel`.

    Guild channels: ('<sanitized-name>', '<original-name>', 'guild')
    DMs: ('DM-<sanitized-username>', 'DM with <username>', 'DM')
    """
    channel = message.channel
    name = getattr(channel, "name", None)
    if name:
        return _sanitize_name(name), name, "guild"
    # DM — no `.name`. Use the author's username so multiple DM partners
    # would naturally land in separate files.
    author = message.author
    username = getattr(author, "name", None) or getattr(author, "display_name", None) or "unknown"
    safe = _sanitize_name(username)
    return f"DM-{safe}", f"DM with {username}", "DM"


def conversation_log_path(agent_id: str, message, now: datetime | None = None) -> Path:
    """The markdown file this turn should be appended to."""
    now = now or datetime.now(timezone.utc)
    label, _, _ = channel_label(message)
    return AGENTS_BASE / agent_id / "conversations" / f"{now:%Y-%m}" / f"{label}.md"


def agent_conversations_dir(agent_id: str) -> Path:
    """The root dir agents need read access to for their own archive."""
    return AGENTS_BASE / agent_id / "conversations"


def _header(agent_id: str, message, now: datetime) -> str:
    """Frontmatter + intro for a brand-new log file."""
    label, display, kind = channel_label(message)
    channel_id = getattr(message.channel, "id", 0)
    return (
        f"---\n"
        f"agent: {agent_id}\n"
        f"channel_id: {channel_id}\n"
        f"channel_name: {display}\n"
        f"channel_kind: {kind}\n"
        f"month: {now:%Y-%m}\n"
        f"---\n\n"
        f"# Conversation log — {agent_id} × {display}\n\n"
        f"Automatically maintained by the daemon. Every Discord turn in "
        f"this channel is appended below as a `## <timestamp> — <author>` "
        f"section. Append-only — don't manually rewrite. Search via "
        f"Glob/Grep across `Agents/{agent_id}/conversations/**/*.md` "
        f"to find prior context.\n\n"
        f"---\n"
    )


def append_turn(agent_id: str, message, author: str, content: str,
                now: datetime | None = None) -> Path | None:
    """Append one turn to the conversation log. Returns the path written,
    or None if the input was empty / unwritable.

    `message` is a discord.Message (typed loosely so tests can mock).
    `author` is the human-readable name shown in the section header
    ("Andy", "Luna", "Echo", etc.).
    `content` is the verbatim message body.

    Never raises — failures are logged and swallowed. Conversation
    archive is a best-effort layer; a write failure shouldn't break
    the live mention flow.
    """
    if not content or not content.strip():
        return None

    now = now or datetime.now(timezone.utc)

    try:
        path = conversation_log_path(agent_id, message, now=now)
        path.parent.mkdir(parents=True, exist_ok=True)

        is_new = not path.exists()
        timestamp = now.strftime("%Y-%m-%d %H:%M:%SZ")
        section = f"\n## {timestamp} — {author}\n\n{content.strip()}\n"

        if is_new:
            path.write_text(_header(agent_id, message, now) + section, encoding="utf-8")
        else:
            with path.open("a", encoding="utf-8") as f:
                f.write(section)

        _logger.info(
            "conv_log appended: %s author=%s chars=%d",
            path, author, len(content),
        )
        return path
    except Exception as exc:  # noqa: BLE001 — best-effort archive
        _logger.warning(
            "conv_log append failed (non-fatal): agent=%s author=%s err=%s: %s",
            agent_id, author, type(exc).__name__, exc,
        )
        return None
