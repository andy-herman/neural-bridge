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
`mention.py:add_dirs_for`). For GUILD channels (not DMs), the same
turn is also fan-out-written to a shared archive at
`Agents/_shared/conversations/YYYY-MM/<channel>.md`, which every agent
reads. That way, when @luna and @echo participate in the same
#neural-bridge thread, each can see what the other said. DMs stay
agent-private and are never mirrored to the shared archive.

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
    """The markdown file this turn should be appended to (per-agent archive)."""
    now = now or datetime.now(timezone.utc)
    label, _, _ = channel_label(message)
    return AGENTS_BASE / agent_id / "conversations" / f"{now:%Y-%m}" / f"{label}.md"


def shared_conversation_log_path(message, now: datetime | None = None) -> Path | None:
    """Shared cross-agent log file for this channel, or None for DMs.

    Layout: `Agents/_shared/conversations/YYYY-MM/<channel_label>.md`.
    DMs return None because DMs are agent-private by design — never
    mirror them across agents.
    """
    _, _, kind = channel_label(message)
    if kind != "guild":
        return None
    now = now or datetime.now(timezone.utc)
    label, _, _ = channel_label(message)
    return AGENTS_BASE / "_shared" / "conversations" / f"{now:%Y-%m}" / f"{label}.md"


def agent_conversations_dir(agent_id: str) -> Path:
    """The root dir agents need read access to for their own archive."""
    return AGENTS_BASE / agent_id / "conversations"


def shared_conversations_dir() -> Path:
    """The cross-agent shared archive root. Every agent reads from here
    so guild-channel threads involving multiple agents stay coherent."""
    return AGENTS_BASE / "_shared" / "conversations"


def _header(agent_id: str, message, now: datetime) -> str:
    """Frontmatter + intro for a brand-new per-agent log file."""
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


def _shared_header(message, now: datetime) -> str:
    """Frontmatter + intro for a brand-new SHARED cross-agent log file."""
    _, display, kind = channel_label(message)
    channel_id = getattr(message.channel, "id", 0)
    return (
        f"---\n"
        f"scope: shared\n"
        f"channel_id: {channel_id}\n"
        f"channel_name: {display}\n"
        f"channel_kind: {kind}\n"
        f"month: {now:%Y-%m}\n"
        f"---\n\n"
        f"# Shared conversation log — {display}\n\n"
        f"Automatically maintained by the daemon. Every Discord turn in "
        f"this guild channel from ANY participating agent (Andy plus "
        f"whichever agents he mentions) is appended here. This is the "
        f"cross-agent record so each agent can see what other agents said "
        f"in the same room. DMs are NOT recorded here — those stay "
        f"agent-private. Append-only.\n\n"
        f"---\n"
    )


def append_turn(agent_id: str, message, author: str, content: str,
                now: datetime | None = None) -> Path | None:
    """Append one turn to the per-agent log AND (for guild channels) the
    shared cross-agent log. Returns the per-agent path, or None if the
    content was empty / per-agent write was unwritable.

    `message` is a discord.Message (typed loosely so tests can mock).
    `author` is the human-readable name shown in the section header
    ("Andy", "Luna", "Echo", etc.).
    `content` is the verbatim message body.

    For guild channels, the same turn is fan-out-written to
    `Agents/_shared/conversations/YYYY-MM/<channel>.md`. DMs are
    NEVER mirrored — they stay agent-private. The two writes are
    independent: a shared-write failure does not block the per-agent
    write, and vice versa.

    Never raises — failures are logged and swallowed. Conversation
    archive is a best-effort layer; a write failure shouldn't break
    the live mention flow.
    """
    if not content or not content.strip():
        return None

    now = now or datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%SZ")
    section = f"\n## {timestamp} — {author}\n\n{content.strip()}\n"

    per_agent_path: Path | None = None
    try:
        per_agent_path = conversation_log_path(agent_id, message, now=now)
        per_agent_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not per_agent_path.exists()
        if is_new:
            per_agent_path.write_text(_header(agent_id, message, now) + section, encoding="utf-8")
        else:
            with per_agent_path.open("a", encoding="utf-8") as f:
                f.write(section)
        _logger.info(
            "conv_log appended (per-agent): %s author=%s chars=%d",
            per_agent_path, author, len(content),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort archive
        _logger.warning(
            "conv_log per-agent append failed (non-fatal): agent=%s author=%s err=%s: %s",
            agent_id, author, type(exc).__name__, exc,
        )
        per_agent_path = None
        # Don't return — the shared write + index calls are independent
        # and should still run regardless of the per-agent write outcome.

    # Shared fan-out for guild channels only. DMs return None from the path
    # helper, which short-circuits this branch.
    shared_path = shared_conversation_log_path(message, now=now)
    if shared_path is not None:
        try:
            shared_path.parent.mkdir(parents=True, exist_ok=True)
            is_new_shared = not shared_path.exists()
            if is_new_shared:
                shared_path.write_text(_shared_header(message, now) + section, encoding="utf-8")
            else:
                with shared_path.open("a", encoding="utf-8") as f:
                    f.write(section)
            _logger.info(
                "conv_log appended (shared): %s author=%s chars=%d",
                shared_path, author, len(content),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "conv_log shared append failed (non-fatal): author=%s err=%s: %s",
                author, type(exc).__name__, exc,
            )
            shared_path = None  # mark unsuccessful so we don't index it below

    # Best-effort: also embed + index this turn so semantic search picks it
    # up next query. Index to BOTH the per-agent table AND (if applicable)
    # the `_shared` pseudo-agent table so agents can semantic-search across
    # the cross-agent archive for guild-channel threads. Failures (Ollama
    # down, sqlite-vec missing, etc.) log a warning and don't affect the
    # writes we just did. Import inline to avoid circular import and to
    # keep a sqlite_vec import error from killing conversation_log load.
    try:
        from .semantic_search import index_turn_from_append
        if per_agent_path is not None:
            index_turn_from_append(
                agent_id=agent_id,
                file_path=per_agent_path,
                author=author,
                content=content,
                timestamp=timestamp,
            )
        if shared_path is not None:
            index_turn_from_append(
                agent_id="_shared",
                file_path=shared_path,
                author=author,
                content=content,
                timestamp=timestamp,
            )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "conv_log index_turn skipped (non-fatal): %s: %s",
            type(exc).__name__, exc,
        )

    return per_agent_path
