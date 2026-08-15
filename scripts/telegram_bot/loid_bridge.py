"""Loid Telegram bridge — career strategist on a separate Telegram bot.

Mirrors luna_bridge.py for plumbing, adds voice-message ingest via Whisper.
Andy speaks a voice note; faster-whisper transcribes locally; Loid reads
text and replies in text. Same NB infrastructure as Luna: mention pipeline,
session-store --resume continuity, Honcho peer memory, allowlist auth.

Reuses NB's existing infrastructure:
  - mention.build_mention_prompt for prompt assembly (auto-injects shared
    peer card; per-agent injections like echo-voice are not active for Loid
    because his agent.md tells him to Read SOUL/Charter/USER directly)
  - claude_invoke.call_claude for the LLM subprocess call
  - session_store.SESSION_STORE for --resume session continuity across turns
  - honcho_client.submit_turn for shared peer-memory capture

Phase 1 scope: bidirectional text + voice chat with persistent memory.

Does NOT yet support:
  - Handoffs to other agents (handoff files written to Luna Master/Agents/Loid/Handoffs/
    are written by Loid himself; delivery is Andy's job)
  - Slash commands beyond /start
  - Document/image attachment ingest

Usage:

    cd ~/Development/neural-bridge
    .venv/bin/python -m scripts.telegram_bot.loid_bridge

Or via launchd (see scripts/launchd/com.andyherman.neural-bridge.loid-telegram.plist).

Required:
  - python-telegram-bot installed in the venv (already there for Luna's bridge)
  - faster-whisper installed in the venv (pip install faster-whisper)
  - ffmpeg available on PATH (brew install ffmpeg)
  - Loid's Telegram bot token in macOS keychain as
    `neural-bridge-telegram-loid` (account = $USER)
  - LOID_TELEGRAM_ALLOWED_USERS env var: comma-separated Telegram user IDs
    allowed to talk to the bot. Empty = nobody talks (safety default).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from telegram import Message, Update
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
from scripts.discord_bot.agent_runtime import TurnRequest, run_agent_turn
from scripts.discord_bot.keychain import get_token


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

AGENT_ID = "loid"
KEYCHAIN_SERVICE = "neural-bridge-telegram-loid"
ALLOWED_USERS_ENV = "LOID_TELEGRAM_ALLOWED_USERS"

# Telegram's hard per-message cap. Long replies get chunked.
TELEGRAM_CHUNK_BUDGET = 3900  # leave headroom for the "(part N/M)" tag

# History budget — how many prior turns to include in each prompt build.
HISTORY_MAX_TURNS = 20

# Whisper model — base.en is the sweet spot for short coaching voice notes
# on M4: ~150MB, runs in int8 on CPU in 2-5s for typical 30s voice messages.
# Upgrade to small.en if accuracy needs it.
WHISPER_MODEL_NAME = os.environ.get("LOID_WHISPER_MODEL", "base.en")
WHISPER_COMPUTE_TYPE = os.environ.get("LOID_WHISPER_COMPUTE", "int8")

_logger = logging.getLogger("nb_telegram_loid")


def _configure_logging() -> None:
    """Rotating file logger at the daemon entrypoint."""
    log_dir = Path.home() / "Library" / "Logs" / "neural-bridge"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "loid-telegram.log"

    logger = logging.getLogger("nb_telegram_loid")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [loid_telegram] %(message)s",
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
    raw = os.environ.get(ALLOWED_USERS_ENV, "").strip()
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

_chat_history: dict[int, list[dict]] = {}


def _push_history(chat_id: int, author: str, content: str) -> None:
    hist = _chat_history.setdefault(chat_id, [])
    hist.append({"author": author, "content": content})
    if len(hist) > HISTORY_MAX_TURNS * 2:
        del hist[: len(hist) - HISTORY_MAX_TURNS * 2]


def _chat_history_for(chat_id: int) -> list[dict]:
    return list(_chat_history.get(chat_id, []))


# ----------------------------------------------------------------------
# Whisper (lazy-loaded; first voice message pays the load cost)
# ----------------------------------------------------------------------

_whisper_model = None
_whisper_lock = asyncio.Lock()


async def _get_whisper_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    async with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        log(f"WHISPER loading model={WHISPER_MODEL_NAME} compute={WHISPER_COMPUTE_TYPE} (one-time cost)")
        from faster_whisper import WhisperModel
        _whisper_model = await asyncio.to_thread(
            WhisperModel, WHISPER_MODEL_NAME, device="cpu", compute_type=WHISPER_COMPUTE_TYPE
        )
        log("WHISPER model loaded")
    return _whisper_model


async def _transcribe_voice(audio_path: str) -> str:
    model = await _get_whisper_model()

    def _do_transcribe() -> str:
        segments, _info = model.transcribe(
            audio_path,
            language="en",
            beam_size=1,
            vad_filter=True,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    return await asyncio.to_thread(_do_transcribe)


# ----------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        log(f"START unauthorized: user={update.effective_user.id if update.effective_user else '?'}")
        return
    msg = (
        "I am here. Tell me the situation. Start with the facts."
    )
    await update.message.reply_text(msg)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        log(f"TEXT unauthorized: user={update.effective_user.id if update.effective_user else '?'}")
        return
    message = update.message
    if message is None or not message.text:
        return
    await _process_message(message, context, text=message.text, kind="text")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        log(f"VOICE unauthorized: user={update.effective_user.id if update.effective_user else '?'}")
        return
    message = update.message
    if message is None or message.voice is None:
        return

    chat_id = message.chat_id
    voice = message.voice
    log(f"VOICE inbound: chat={chat_id} duration={voice.duration}s size={voice.file_size}")

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Download voice .ogg to a temp file
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
    except Exception as exc:
        log(f"VOICE download failed: {exc}")
        await message.reply_text(f"_(I could not download the voice file: `{exc}`)_")
        return

    # Transcribe via faster-whisper
    try:
        text = await _transcribe_voice(tmp_path)
    except Exception as exc:
        log(f"VOICE transcription failed: {exc}")
        await message.reply_text(f"_(Transcription failed: `{exc}`)_")
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not text:
        log(f"VOICE empty transcript: chat={chat_id}")
        await message.reply_text("_(I could not make out the audio. Could you try again?)_")
        return

    log(f"VOICE transcribed: chat={chat_id} chars={len(text)}")
    # Show Andy what was heard so he can correct if needed
    await message.reply_text(f"_(heard: {text})_")

    await _process_message(message, context, text=text, kind="voice")


async def _process_message(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str,
    kind: str,
) -> None:
    """Shared path: given an inbound text (typed or transcribed), build the
    Loid prompt, call claude -p, post the reply.
    """
    chat_id = message.chat_id
    log(f"MSG processing: kind={kind} chat={chat_id} chars={len(text)}")

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Shared invocation core; see scripts/discord_bot/agent_runtime.py.
    result = await run_agent_turn(
        TurnRequest(
            agent_id=AGENT_ID,
            conversation_key=chat_id,
            message_content=text,
            channel_kind="DM",
            history=_chat_history_for(chat_id),
            conversation_log_path="",
        ),
        log=log,
    )

    if result.setup_error:
        log(result.setup_error)
        await message.reply_text("_(internal: mention prompt missing)_")
        return
    if not result.ok:
        await message.reply_text(f"_(I hit an error: `{result.error_reason[:200]}`. Try again.)_")
        return

    response = _strip_structured_blocks(result.response)

    if not response:
        log(f"MSG empty response: chat={chat_id}")
        await message.reply_text("_(Nothing useful to add. Rephrase?)_")
        return

    chunks = _chunk_for_telegram(response)
    for i, chunk in enumerate(chunks):
        prefix = f"_(part {i + 1}/{len(chunks)})_\n" if len(chunks) > 1 else ""
        await message.reply_text(prefix + chunk)

    _push_history(chat_id, "Andy", text)
    _push_history(chat_id, "Loid", response)

    try:
        honcho_client.submit_turn(
            agent_id=AGENT_ID,
            user_message=text,
            agent_response=response,
            session_id=result.session_id,
        )
    except Exception:
        pass

    log(f"MSG done: chat={chat_id} response_chars={len(response)} chunks={len(chunks)}")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _strip_structured_blocks(text: str) -> str:
    """Remove Discord-specific structured fences from Loid's reply."""
    import re
    pattern = re.compile(
        r"```(?:actions|attachments|handoff_to_squad)\s*\n.*?\n```\s*",
        re.DOTALL,
    )
    return pattern.sub("", text).strip()


def _chunk_for_telegram(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= TELEGRAM_CHUNK_BUDGET:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_CHUNK_BUDGET:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, TELEGRAM_CHUNK_BUDGET)
        if split_at < TELEGRAM_CHUNK_BUDGET // 2:
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
        log(f"FATAL: {ALLOWED_USERS_ENV} unset or empty — refusing to start without an allowlist")
        sys.exit(2)

    token = get_token(KEYCHAIN_SERVICE)
    if not token:
        log(f"FATAL: keychain service '{KEYCHAIN_SERVICE}' not found — run security add-generic-password first")
        sys.exit(2)

    log(f"Loid Telegram bridge starting (allowed users: {sorted(allowed)})")

    app: Application = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
