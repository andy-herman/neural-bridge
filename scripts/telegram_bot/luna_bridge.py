"""Luna Telegram bridge — parallel transport to the Discord daemon.

Lets Andy talk to Luna in Telegram with the same brain, same notes.md
auto-injection, same vault read access, same Honcho memory pool as her
Discord side. Different transport, same agent.

Reuses NB's existing infrastructure:
  - mention.build_mention_prompt for prompt assembly (auto-injects Luna's
    notes.md, weekly lessons, Echo voice profile, Honcho peer card)
  - claude_invoke.call_claude for the LLM subprocess call
  - session_store.SESSION_STORE for --resume session continuity across turns
  - honcho_client.submit_turn for shared peer-memory capture

Does NOT yet support:
  - Handoffs to other agents (Discord-only for now)
  - Slash commands
  - Attachment ingest (images, documents)
  - PR proposals / structured action blocks

Phase 1 scope: bidirectional text chat with persistent memory.

Usage:

    cd ~/Development/neural-bridge
    .venv/bin/python -m scripts.telegram_bot.luna_bridge

Or via launchd (see scripts/launchd/com.andyherman.neural-bridge.luna-telegram.plist).

Required:
  - python-telegram-bot installed in the venv
  - Luna's Telegram bot token in macOS keychain as
    `neural-bridge-telegram-luna` (account = $USER)
  - LUNA_TELEGRAM_ALLOWED_USERS env var: comma-separated Telegram user IDs
    that are allowed to talk to the bot. Empty = nobody talks (safety
    default — explicit allowlist required)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.discord_bot import honcho_client
from scripts.discord_bot.claude_invoke import call_claude
from scripts.discord_bot.keychain import get_token
from scripts.discord_bot.mention import (
    AGENTS_DIR,
    MENTION_PROMPT_PATH,
    add_dirs_for,
    allowed_tools_for,
    build_mention_prompt,
    load_agent_definition,
    max_response_chars_for,
    effort_for,
    timeout_for,
    truncate_response,
)
from scripts.discord_bot.session_store import STORE as SESSION_STORE


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

AGENT_ID = "luna"
KEYCHAIN_SERVICE = "neural-bridge-telegram-luna"

# Telegram's hard per-message cap. Long replies get chunked.
TELEGRAM_CHUNK_BUDGET = 3900  # leave headroom for the "(part N/M)" tag

# History budget — how many prior turns to include in each prompt build.
HISTORY_MAX_TURNS = 20

_logger = logging.getLogger("nb_telegram")


def _configure_logging() -> None:
    """Rotating file logger at the daemon entrypoint."""
    log_dir = Path.home() / "Library" / "Logs" / "neural-bridge"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "luna-telegram.log"

    logger = logging.getLogger("nb_telegram")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [luna_telegram] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fh = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def log(msg: str) -> None:
    _logger.info(msg)


def _allowed_user_ids() -> set[int]:
    raw = os.environ.get("LUNA_TELEGRAM_ALLOWED_USERS", "").strip()
    if not raw:
        return set()
    result = set()
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit():
            result.add(int(token))
    return result


def _is_authorized(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return user.id in _allowed_user_ids()


# ----------------------------------------------------------------------
# Conversation history (per chat, in-memory rolling window)
# ----------------------------------------------------------------------
# Session continuity inside Claude's process is handled by SESSION_STORE +
# --resume. This local history is what build_mention_prompt expects in the
# `history` arg — recent Telegram turns formatted as discord-style dicts.

_chat_history: dict[int, list[dict]] = {}


def _push_history(chat_id: int, author: str, content: str) -> None:
    """Append a turn to the in-memory chat history, trimming to budget."""
    hist = _chat_history.setdefault(chat_id, [])
    hist.append({"author": author, "content": content})
    if len(hist) > HISTORY_MAX_TURNS * 2:
        del hist[: len(hist) - HISTORY_MAX_TURNS * 2]


def _chat_history_for(chat_id: int) -> list[dict]:
    return list(_chat_history.get(chat_id, []))


# ----------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        log(f"START unauthorized: user={update.effective_user.id if update.effective_user else '?'}")
        return
    msg = (
        "Hi Andy. This is Luna, on Telegram. I have my notes and my access to the vault. "
        "Same me you talk to on Discord — just a different room. What's on your mind?"
    )
    await update.message.reply_text(msg)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        log(f"MSG unauthorized: user={update.effective_user.id if update.effective_user else '?'}")
        return

    message = update.message
    if message is None or not message.text:
        return

    chat_id = message.chat_id
    text = message.text
    log(f"MSG inbound: chat={chat_id} chars={len(text)}")

    # Typing indicator while we work
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Build the prompt using NB's existing mention pipeline. The history
    # we pass is what Andy has typed + Luna has replied in this Telegram
    # chat, in the same shape build_mention_prompt expects.
    if not MENTION_PROMPT_PATH.exists():
        log(f"MENTION prompt missing at {MENTION_PROMPT_PATH}")
        await message.reply_text("_(internal: mention prompt missing)_")
        return

    template = MENTION_PROMPT_PATH.read_text(encoding="utf-8")
    agent_definition = load_agent_definition(AGENT_ID)
    history = _chat_history_for(chat_id)

    prompt = build_mention_prompt(
        template,
        agent_id=AGENT_ID,
        agent_definition=agent_definition,
        channel_kind="DM",  # 1:1 chat — closest match to Discord DM
        history=history,
        message_content=text,
        conversation_log_path="",  # no NB log file for Telegram turns yet
    )

    # Per (chat × agent) session-id for --resume continuity
    session_rec, is_new_session = SESSION_STORE.get_or_create(chat_id, AGENT_ID)

    tools = allowed_tools_for(AGENT_ID)
    extra_dirs = add_dirs_for(AGENT_ID)
    agent_timeout = timeout_for(AGENT_ID)
    agent_effort = effort_for(AGENT_ID)

    log(
        f"MENTION calling claude: chat={chat_id} effort={agent_effort} "
        f"session={session_rec.session_id[:8]}... "
        f"({'new' if is_new_session else f'turn {session_rec.turn_count + 1}'})"
    )

    ok, stdout, err = await call_claude(
        prompt,
        timeout=agent_timeout,
        allowed_tools=tools,
        add_dirs=extra_dirs,
        session_id=session_rec.session_id,
        resume=not is_new_session,
        effort=agent_effort,
    )

    # Resume-failed retry (mirrors handlers.py pattern)
    if not ok and not is_new_session:
        log(f"MENTION resume failed, retry fresh: err={err[:80]}")
        session_rec = SESSION_STORE.reset(chat_id, AGENT_ID)
        ok, stdout, err = await call_claude(
            prompt,
            timeout=agent_timeout,
            allowed_tools=tools,
            add_dirs=extra_dirs,
            session_id=session_rec.session_id,
            resume=False,
            effort=agent_effort,
        )

    if ok:
        SESSION_STORE.touch(chat_id, AGENT_ID)
    else:
        log(f"MENTION claude FAILED: err={err}")
        await message.reply_text(f"_(I hit an error: `{err[:200]}`. Try again.)_")
        return

    # Strip any structured action / attachments blocks (not yet supported on
    # Telegram — they'd show as raw markdown). Phase 2 will handle these.
    response = _strip_structured_blocks(stdout)
    response = truncate_response(response, limit=max_response_chars_for(AGENT_ID))

    if not response:
        log(f"MENTION empty response: chat={chat_id}")
        await message.reply_text("_(I had nothing useful to add. Try rephrasing.)_")
        return

    # Chunk for Telegram's 4096-char wall
    chunks = _chunk_for_telegram(response)
    for i, chunk in enumerate(chunks):
        prefix = f"_(part {i + 1}/{len(chunks)})_\n" if len(chunks) > 1 else ""
        await message.reply_text(prefix + chunk)

    # Record turns in local history + submit to Honcho
    _push_history(chat_id, "Andy", text)
    _push_history(chat_id, "Luna", response)

    try:
        honcho_client.submit_turn(
            agent_id=AGENT_ID,
            user_message=text,
            agent_response=response,
            session_id=session_rec.session_id,
        )
    except Exception:
        pass

    log(f"MSG done: chat={chat_id} response_chars={len(response)} chunks={len(chunks)}")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _strip_structured_blocks(text: str) -> str:
    """Remove fenced ```actions / ```attachments / ```handoff_to_squad blocks.

    Discord parses these as instructions to the daemon. Telegram doesn't
    (yet) so leaving them visible would be noise. Phase 2 will handle them.
    """
    import re

    pattern = re.compile(
        r"```(?:actions|attachments|handoff_to_squad)\s*\n.*?\n```\s*",
        re.DOTALL,
    )
    return pattern.sub("", text).strip()


def _chunk_for_telegram(text: str) -> list[str]:
    """Split text into Telegram-safe chunks at paragraph boundaries when possible."""
    text = text.strip()
    if len(text) <= TELEGRAM_CHUNK_BUDGET:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_CHUNK_BUDGET:
            chunks.append(remaining)
            break
        # Try to break on a double-newline boundary near the budget
        split_at = remaining.rfind("\n\n", 0, TELEGRAM_CHUNK_BUDGET)
        if split_at < TELEGRAM_CHUNK_BUDGET // 2:
            # No good paragraph break — fall back to single-newline, then hard cut
            split_at = remaining.rfind("\n", 0, TELEGRAM_CHUNK_BUDGET)
            if split_at < TELEGRAM_CHUNK_BUDGET // 2:
                split_at = TELEGRAM_CHUNK_BUDGET
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def main() -> None:
    _configure_logging()

    allowed = _allowed_user_ids()
    if not allowed:
        log("FATAL: LUNA_TELEGRAM_ALLOWED_USERS unset or empty — refusing to start without an allowlist")
        sys.exit(2)

    token = get_token(KEYCHAIN_SERVICE)
    if not token:
        log(f"FATAL: keychain service '{KEYCHAIN_SERVICE}' not found — run security add-generic-password first")
        sys.exit(2)

    log(f"Luna Telegram bridge starting (allowed users: {sorted(allowed)})")

    app: Application = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # python-telegram-bot 20+ has a synchronous run_polling that owns the loop
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
