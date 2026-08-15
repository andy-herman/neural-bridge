"""Council Telegram bridge — Yor (Hermes) and Loid (Neural Bridge) in one room.

A single Telegram bot fronts a group chat with Andy and both advisors. On each
of Andy's messages the bridge:

  1. appends it to a shared, speaker-labeled transcript,
  2. runs a cheap router to decide who should respond (Yor, Loid, both, or
     neither),
  3. invokes each chosen advisor IN ORDER, feeding the running transcript
     (including any advisor who already spoke this turn) so they build on each
     other,
  4. posts each reply attributed ("Yor" / "Loid").

Two backends, because the advisors live on different runtimes:
  - Loid  -> `claude -p` via scripts.discord_bot.claude_invoke.call_claude,
             with his charter injected by scripts.discord_bot.mention. This is
             the exact path loid_bridge.py uses.
  - Yor   -> `hermes chat -q ... -Q` subprocess. ~/.hermes is Yor's config, so
             Hermes loads her SOUL itself; we only pass the shared transcript.

Telegram fact that shapes this design: a bot never receives another bot's
messages, so two separate bots could not see each other. One orchestrator bot
that invokes both and owns the shared transcript is how the two advisors
actually collaborate.

Both advisors carry an "In the shared room" section in their SOUL that defines
group etiquette (stay in lane, keep it short, build on or defer to the other,
silence is a valid turn). The bridge only supplies the labeled transcript and
the routing; the manners live in the SOUL.

Memory (v1): Andy's turn is written once to the shared Honcho `andyherman`
card so his context still builds from the room. Yor's `hermes chat` invocation
also self-captures via her own runtime. Cross-attribution (Yor's deriver seeing
Loid's labeled lines) is bounded by observe_me=false on the AI peers; tightening
it further is a v2 item.

Usage:

    cd ~/Development/neural-bridge
    COUNCIL_TELEGRAM_ALLOWED_USERS=<your_tg_id> .venv/bin/python -m scripts.telegram_bot.council_bridge

Required:
  - python-telegram-bot + faster-whisper in the venv (already there for Loid)
  - ffmpeg on PATH
  - `hermes` CLI on PATH (Yor's runtime)
  - Council bot token in keychain as `neural-bridge-telegram-council`
  - COUNCIL_TELEGRAM_ALLOWED_USERS: comma-separated Telegram user IDs
  - The council bot added to the group with group-privacy DISABLED in BotFather
    (so it can see all of Andy's messages, not only replies/mentions)

See scripts/telegram_bot/COUNCIL_SETUP.md for the one-time BotFather steps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
from scripts.discord_bot.claude_invoke import DEFAULT_MODEL, call_claude
from scripts.discord_bot.keychain import get_token
from scripts.discord_bot.agent_runtime import TurnRequest, run_agent_turn

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

KEYCHAIN_SERVICE = "neural-bridge-telegram-council"
ALLOWED_USERS_ENV = "COUNCIL_TELEGRAM_ALLOWED_USERS"

# Advisor display + attribution. Replies are posted plain-text with this prefix
# so Andy always sees who is speaking, and so each advisor's transcript is
# unambiguously labeled.
ADVISORS = {
    "loid": {"label": "Loid", "prefix": "\U0001FAAA Loid"},   # detective emoji-ish
    "yor": {"label": "Yor", "prefix": "\U0001F339 Yor"},        # rose
}

# Loid runs on Opus 4.8 in the council (his charter's declared model; the NB
# invocation path does not yet read charter model, so we pass it explicitly).
LOID_MODEL = os.environ.get("COUNCIL_LOID_MODEL", "claude-opus-4.8")  # copilot-api id (dotted)
# Router is a cheap classification; keep it on Haiku (also served by copilot-api).
ROUTER_MODEL = os.environ.get("COUNCIL_ROUTER_MODEL", "claude-haiku-4.5")
ROUTER_TIMEOUT = int(os.environ.get("COUNCIL_ROUTER_TIMEOUT", "60"))
YOR_TIMEOUT = int(os.environ.get("COUNCIL_YOR_TIMEOUT", "240"))

TELEGRAM_CHUNK_BUDGET = 3900
HISTORY_MAX_TURNS = 20

WHISPER_MODEL_NAME = os.environ.get("COUNCIL_WHISPER_MODEL", "base.en")
WHISPER_COMPUTE_TYPE = os.environ.get("COUNCIL_WHISPER_COMPUTE", "int8")

_logger = logging.getLogger("nb_telegram_council")


def _configure_logging() -> None:
    log_dir = Path.home() / "Library" / "Logs" / "neural-bridge"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("nb_telegram_council")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s [council_telegram] %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
    )
    fh = RotatingFileHandler(
        log_dir / "council-telegram.log", maxBytes=10 * 1024 * 1024, backupCount=7, encoding="utf-8"
    )
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
    return {int(t.strip()) for t in raw.split(",") if t.strip().isdigit()}


def _is_authorized(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in _allowed_user_ids()


# ----------------------------------------------------------------------
# Shared transcript (per chat, in-memory rolling window)
# ----------------------------------------------------------------------

_chat_history: dict[int, list[dict]] = {}


def _push_history(chat_id: int, author: str, content: str) -> None:
    hist = _chat_history.setdefault(chat_id, [])
    hist.append({"author": author, "content": content})
    if len(hist) > HISTORY_MAX_TURNS * 2:
        del hist[: len(hist) - HISTORY_MAX_TURNS * 2]


def _chat_history_for(chat_id: int) -> list[dict]:
    return list(_chat_history.get(chat_id, []))


def _format_transcript(history: list[dict]) -> str:
    """Speaker-labeled plain-text transcript for the router and for Yor."""
    return "\n".join(f"{h['author']}: {h['content']}" for h in history)


# ----------------------------------------------------------------------
# Whisper (lazy-loaded)
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
        log(f"WHISPER loading model={WHISPER_MODEL_NAME} compute={WHISPER_COMPUTE_TYPE}")
        from faster_whisper import WhisperModel

        _whisper_model = await asyncio.to_thread(
            WhisperModel, WHISPER_MODEL_NAME, device="cpu", compute_type=WHISPER_COMPUTE_TYPE
        )
        log("WHISPER model loaded")
    return _whisper_model


async def _transcribe_voice(audio_path: str) -> str:
    model = await _get_whisper_model()

    def _do() -> str:
        segments, _info = model.transcribe(audio_path, language="en", beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    return await asyncio.to_thread(_do)


# ----------------------------------------------------------------------
# Router: who should respond?
# ----------------------------------------------------------------------

ROUTER_INSTRUCTION = (
    "You route one message in a group advisory chat. Two advisors are present:\n"
    "- yor: open-ended ideation, writing and editing, framing a problem, thinking-partner work.\n"
    "- loid: career strategy, interview prep, promotion and leveling, positioning, workplace moves.\n\n"
    "Decide who should respond to Andy's LATEST message. Usually exactly one. Both only when it "
    "genuinely spans both lanes. Neither ([]) when it is small talk or an aside that needs no advisor.\n"
    'Return ONLY a JSON object, no prose: {"order": ["loid"]} where order lists the advisor ids '
    "yor and/or loid in the sequence they should speak, or an empty list for neither."
)

_MENTION_RE = re.compile(r"@?\b(yor|loid)\b", re.IGNORECASE)
_CAREER_HINTS = re.compile(
    r"\b(promo|promotion|interview|recruit|offer|manager|skip.?level|l7|leveling|"
    r"career|resume|comp|negotiat|backfill|stakeholder|perf review|calibrat)\w*",
    re.IGNORECASE,
)


def _heuristic_route(text: str) -> list[str]:
    """Fallback when the LLM router is unavailable: explicit names win, else a
    keyword lean, else Yor as the generalist thinking partner."""
    named = [m.group(1).lower() for m in _MENTION_RE.finditer(text)]
    if named:
        # preserve order, dedupe
        seen: list[str] = []
        for n in named:
            if n not in seen:
                seen.append(n)
        return seen
    if _CAREER_HINTS.search(text):
        return ["loid"]
    return ["yor"]


async def _route(text: str, history: list[dict]) -> list[str]:
    # Explicit @mention always wins, no LLM call needed.
    named = [m.group(1).lower() for m in _MENTION_RE.finditer(text)]
    if named:
        seen: list[str] = []
        for n in named:
            if n not in seen:
                seen.append(n)
        log(f"ROUTE explicit-mention -> {seen}")
        return seen

    transcript = _format_transcript(history[-8:])
    prompt = (
        f"{ROUTER_INSTRUCTION}\n\nRECENT CONVERSATION:\n{transcript}\n\n"
        f"ANDY'S LATEST MESSAGE:\n{text}"
    )
    ok, stdout, err = await call_claude(
        prompt,
        model=ROUTER_MODEL,
        timeout=ROUTER_TIMEOUT,
        session_id=str(uuid.uuid4()),
        resume=False,
        # Pure classification: pick a speaker. No reasoning depth required, and
        # this runs on every inbound group message, so it is the hottest path
        # in the bridge.
        effort="low",
    )
    if not ok:
        log(f"ROUTE llm-failed ({err[:60]}); heuristic fallback")
        return _heuristic_route(text)
    try:
        blob = stdout.strip()
        if blob.startswith("```"):
            blob = blob.strip("`").removeprefix("json").strip()
        # tolerate leading/trailing prose by grabbing the first {...}
        match = re.search(r"\{.*\}", blob, re.DOTALL)
        data = json.loads(match.group(0) if match else blob)
        order = [a for a in data.get("order", []) if a in ADVISORS]
        log(f"ROUTE llm -> {order}")
        return order
    except Exception as exc:
        log(f"ROUTE parse-failed ({exc}); heuristic fallback")
        return _heuristic_route(text)


# ----------------------------------------------------------------------
# Advisor invocation
# ----------------------------------------------------------------------

async def _invoke_loid(text: str, history: list[dict]) -> tuple[bool, str]:
    """Loid via `claude -p`, his charter injected by mention. Stateless per
    turn (fresh session id); the shared transcript is his context."""
    # Shared invocation core. Stateless: each council turn is rebuilt from the
    # shared transcript rather than resumed, so there is no session to continue
    # and conversation_key is never used for a session lookup.
    result = await run_agent_turn(
        TurnRequest(
            agent_id="loid",
            conversation_key=0,
            message_content=text,
            channel_kind="COUNCIL (shared Telegram room with Andy and Yor)",
            history=history,
            conversation_log_path="",
            model=LOID_MODEL,
            stateless=True,
        ),
        log=log,
    )
    if not result.ok:
        return False, (result.setup_error or result.error_reason)[:200]
    return True, _strip_structured_blocks(result.response)


async def _invoke_yor(text: str, history: list[dict]) -> tuple[bool, str]:
    """Yor via `hermes chat -q ... -Q`. Hermes loads her SOUL; we pass the
    shared transcript and her group etiquette lives in that SOUL."""
    transcript = _format_transcript(history)
    query = (
        "[COUNCIL MODE] You are in a shared Telegram room with Andy and Loid, his career "
        "strategist. Follow your 'In the shared room' rules. The conversation so far is below, "
        "each line labeled by speaker; a line marked Loid is Loid, not Andy and not you. Respond "
        "as yourself, briefly, and only add what is yours. If nothing here is yours, reply with "
        "exactly [PASS] and nothing else.\n\n"
        f"CONVERSATION:\n{transcript}\n\n"
        f"Andy's latest message: {text}"
    )
    args = ["hermes", "chat", "-q", query, "-Q", "--accept-hooks"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        out, errb = await asyncio.wait_for(proc.communicate(), timeout=YOR_TIMEOUT)
    except asyncio.TimeoutError:
        return False, "yor timed out"
    except Exception as exc:
        return False, f"yor invoke failed: {exc}"
    resp = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return False, (errb.decode("utf-8", "replace")[:200] or "yor exited nonzero")
    # `-Q` may append a trailing "Session: <id>" line; drop a trailing line that
    # looks like session bookkeeping.
    resp = re.sub(r"\n+Session:.*$", "", resp).strip()
    return True, resp


_INVOKERS = {"loid": _invoke_loid, "yor": _invoke_yor}


# ----------------------------------------------------------------------
# Telegram handlers
# ----------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "The room is open. Andy, Yor, and Loid. Say what is on your mind; whoever it is for will answer."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    if message is None or not message.text:
        return
    await _process_message(message, context, text=message.text, kind="text")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    message = update.message
    if message is None or message.voice is None:
        return
    chat_id = message.chat_id
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass
    try:
        tg_file = await context.bot.get_file(message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
    except Exception as exc:
        await message.reply_text(f"_(could not download the voice file: {exc})_")
        return
    try:
        text = await _transcribe_voice(tmp_path)
    except Exception as exc:
        await message.reply_text(f"_(transcription failed: {exc})_")
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if not text:
        await message.reply_text("_(could not make out the audio)_")
        return
    await message.reply_text(f"_(heard: {text})_")
    await _process_message(message, context, text=text, kind="voice")


async def _process_message(
    message: Message, context: ContextTypes.DEFAULT_TYPE, *, text: str, kind: str
) -> None:
    chat_id = message.chat_id
    log(f"MSG kind={kind} chat={chat_id} chars={len(text)}")

    _push_history(chat_id, "Andy", text)

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    order = await _route(text, _chat_history_for(chat_id))
    if not order:
        log(f"ROUTE neither; staying silent chat={chat_id}")
        return

    first_response_for_honcho: tuple[str, str] | None = None

    for agent_id in order:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        # History includes any advisor who already spoke THIS turn, so the next
        # one can build on it.
        history = _chat_history_for(chat_id)
        ok, resp = await _INVOKERS[agent_id](text, history)
        label = ADVISORS[agent_id]["label"]
        if not ok:
            log(f"INVOKE {agent_id} failed: {resp[:120]}")
            await message.reply_text(f"_({label} hit an error and stayed quiet.)_")
            continue
        # An advisor may decline the turn.
        if not resp or resp.strip() == "[PASS]":
            log(f"INVOKE {agent_id} passed")
            _push_history(chat_id, label, "(passed)")
            continue

        _push_history(chat_id, label, resp)
        if first_response_for_honcho is None:
            first_response_for_honcho = (agent_id, resp)

        prefix = ADVISORS[agent_id]["prefix"]
        for i, chunk in enumerate(_chunk_for_telegram(resp)):
            part = f" (part {i + 1})" if i else ""
            await message.reply_text(f"{prefix}{part}\n\n{chunk}")

    # Memory (v1): write Andy's turn once, under the first responder's
    # perspective, so his context builds on the shared card. Yor's hermes-chat
    # invocation also self-captures via her own runtime.
    if first_response_for_honcho is not None:
        agent_id, resp = first_response_for_honcho
        try:
            honcho_client.submit_turn(
                agent_id=agent_id,
                user_message=text,
                agent_response=resp,
                session_id=f"council-{chat_id}",
            )
        except Exception:
            pass

    log(f"MSG done chat={chat_id} responders={order}")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _strip_structured_blocks(text: str) -> str:
    pattern = re.compile(
        r"```(?:actions|attachments|handoff_to_squad)\s*\n.*?\n```\s*", re.DOTALL
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
        log(f"FATAL: keychain service '{KEYCHAIN_SERVICE}' not found")
        sys.exit(2)

    log(f"Council Telegram bridge starting (allowed users: {sorted(allowed)})")

    app: Application = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
