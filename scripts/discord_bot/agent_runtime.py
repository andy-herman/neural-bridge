"""One agent turn, independent of transport.

Every surface that talks to an agent was running its own copy of the same
sequence: read the mention template, load the charter, build the prompt, get or
create a session, look up tools/dirs/timeout/effort, call claude, retry once if
`--resume` failed, touch the session, truncate the reply. Four copies existed
(Discord handlers, and the Luna, Loid and Council Telegram bridges), and the
comments in them said so out loud: "mirrors handlers.py pattern", "Mirrors
luna_bridge.py for plumbing".

Copy number four is how drift starts. The effort flag added on 2026-08-01 had
to be threaded into three files by hand, and one of them was missed on the
first pass. This module is the single copy.

What stays with the transport, deliberately:
  - chunking (Discord 1900, Telegram 3900/4096)
  - structured action and attachment handling (Discord only)
  - delivery and history bookkeeping
  - the Honcho capture call, so it stays AFTER a confirmed successful delivery
    rather than firing for a reply the user never saw

What lives here: everything between "I have a message" and "I have text to
send", which is the part that was identical.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .claude_invoke import DEFAULT_MODEL, call_claude, claude_env, claude_usage
from .mention import (
    MENTION_PROMPT_PATH,
    add_dirs_for,
    allowed_tools_for,
    MentionParts,
    build_mention_parts,
    effort_for,
    load_agent_definition,
    max_response_chars_for,
    model_for,
    timeout_for,
    truncate_response,
)
from .private_tools import grant_for, merge_tools
from .session_store import STORE as SESSION_STORE

# Every agent turn, in the fleet's usage log as "agent_turn", with caps that
# only stop a runaway. A normal Luna turn cost $0.37 to $2.21 on 2026-10-12..20
# (the growing resumed session, not the work), and a research turn may make a
# few dozen tool calls. A capped turn fails once ("error_max_budget_usd" or
# "error_max_turns") and is not retried.
TURN_LIMITS = {"job": "agent_turn", "max_turns": 60, "max_budget_usd": 5.0}

# A resumed session keeps everything it was ever sent, and a turn days after
# the last one pays to write all of it to the prompt cache again. A session
# with this many tokens of context is not resumed: the turn starts a fresh one
# with the full prompt, and the progress log, notes and recent history carry
# the conversation over. Report step 11 (2026-10-20): Luna's
# Telegram session had reached 353K tokens, $2.21 for a one-line turn.
ROTATE_AT_TOKENS = int(os.environ.get("NB_SESSION_ROTATE_TOKENS") or 100_000)


def _noop(_msg: str) -> None:
    return


@dataclass
class TurnResult:
    """Outcome of one agent turn.

    `response` is truncated and ready to chunk. `raw_stdout` is untouched, for
    callers that parse structured blocks out of it. `error_reason` carries the
    claude_invoke reason string when ok is False.
    """
    ok: bool
    response: str = ""
    raw_stdout: str = ""
    error_reason: str = ""
    session_id: str = ""
    is_new_session: bool = False
    resume_retried: bool = False
    prompt_chars: int = 0
    # True when the agent's Claude 5 model failed and the turn was answered by
    # (or failed on) the fleet default instead.
    model_fallback: bool = False
    # Set when the turn failed before claude was ever called (missing template).
    setup_error: str = ""
    # True when a resumed turn sent only the short continuation prompt.
    continued: bool = False
    # The session's context after the turn, in tokens, when it could be read;
    # and the size of the session this turn rotated away from, if it did.
    context_tokens: int | None = None
    rotated_from: int | None = None

    @property
    def empty(self) -> bool:
        """Succeeded but produced nothing usable to send."""
        return self.ok and not self.response.strip()


@dataclass
class TurnRequest:
    agent_id: str
    conversation_key: int          # Discord channel id or Telegram chat id
    message_content: str
    channel_kind: str = "DM"
    history: list[dict] = field(default_factory=list)
    conversation_log_path: str = ""
    # Blocks the transport wants prepended verbatim (attachment manifests etc).
    prompt_prefix: str = ""
    # Override the response cap; defaults to the agent's configured cap.
    max_response_chars: int | None = None
    # Override the model. Only set this when a surface genuinely needs a
    # different one from the fleet default (the council room pins Loid).
    model: str | None = None
    # Stateless turns get a throwaway session id and skip the resume-retry
    # entirely: there is no prior session to resume, so a failure is a real
    # failure. The council room works this way because each turn is built from
    # the shared transcript rather than from Claude-side session continuity.
    stateless: bool = False
    # True only when an authorized human started this turn directly. Gates
    # private tools (private_tools.py); agent handoffs and every other path
    # leave it False, so they never receive them.
    owner_invoked: bool = False


async def run_agent_turn(req: TurnRequest, *, log: Callable[[str], None] = _noop) -> TurnResult:
    """Execute one agent turn. Never raises; failures come back on the result."""
    if not MENTION_PROMPT_PATH.exists():
        return TurnResult(ok=False, setup_error=f"mention prompt missing at {MENTION_PROMPT_PATH}",
                          error_reason="prompt_template_missing")

    try:
        template = MENTION_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return TurnResult(ok=False, setup_error=f"mention prompt unreadable: {exc}",
                          error_reason="prompt_template_unreadable")

    agent_definition = load_agent_definition(req.agent_id)
    parts = build_mention_parts(
        template,
        agent_id=req.agent_id,
        agent_definition=agent_definition,
        channel_kind=req.channel_kind,
        history=req.history,
        message_content=req.message_content,
        conversation_log_path=req.conversation_log_path,
    )
    full_prompt = (req.prompt_prefix or "") + parts.full

    tools = allowed_tools_for(req.agent_id)
    grant = grant_for(req.agent_id, req.owner_invoked)
    mcp_config = None
    if grant:
        tools = merge_tools(tools, grant.tools)
        mcp_config = grant.mcp_config
        log(f"TURN private tools granted: agent={req.agent_id} count={len(grant.tools.split(','))}")
    extra_dirs = add_dirs_for(req.agent_id)
    agent_timeout = timeout_for(req.agent_id)
    agent_effort = effort_for(req.agent_id)
    model = req.model or model_for(req.agent_id)
    model_kwargs = {"model": model} if model else {}

    if req.stateless:
        session_id = str(uuid.uuid4())
        is_new_session = True
    else:
        session_rec, is_new_session = SESSION_STORE.get_or_create(req.conversation_key, req.agent_id)
        session_id = session_rec.session_id

    # Rotate a session that has grown too large before using it: its size from
    # the last turn, or from its transcript when the record predates that.
    rotated_from = None
    if not req.stateless and not is_new_session:
        size = getattr(session_rec, "context_tokens", 0) or claude_usage.context_tokens(session_id) or 0
        if size >= ROTATE_AT_TOKENS:
            rotated_from = size
            log(f"TURN session rotated at {size:,} tokens of context: agent={req.agent_id} "
                f"session={session_id[:8]}...")
            session_rec = SESSION_STORE.reset(req.conversation_key, req.agent_id)
            session_id, is_new_session = session_rec.session_id, True

    # A resumed session that already holds this exact fixed context gets only
    # what changed; anything else (a new session, an edited notes.md or
    # charter, a session from before 2026-10-20) gets the full prompt.
    continued = (not req.stateless and not is_new_session and bool(parts.continuation)
                 and session_rec.static_hash == parts.static_hash)
    prompt = (req.prompt_prefix or "") + parts.continuation if continued else full_prompt

    log(
        f"TURN calling claude: agent={req.agent_id} key={req.conversation_key} "
        f"effort={agent_effort} timeout={agent_timeout}s "
        f"session={session_id[:8]}... "
        f"({'stateless' if req.stateless else 'new' if is_new_session else 'resumed'}"
        f"{', continuation prompt' if continued else ''}, {len(prompt):,} chars)"
    )

    ok, stdout, err = await call_claude(
        prompt,
        timeout=agent_timeout,
        allowed_tools=tools,
        add_dirs=extra_dirs,
        session_id=session_id,
        resume=not is_new_session,
        effort=agent_effort,
        agent_id=req.agent_id,
        mcp_config=mcp_config,
        **TURN_LIMITS,
        **model_kwargs,
    )

    # On the proxy route Claude 5 answers only through the model gateway (on
    # the API route proxy_supports() is always true). If this agent's Claude 5
    # turn fails for any reason but a timeout, run it once more on the fleet
    # default: on the same session when there is one (a session resumes across
    # models, verified 2026-09-30), otherwise on a fresh id, since the failed
    # call may already have claimed the new one.
    fell_back = False
    if not ok and model and not claude_env.proxy_supports(model) and err != "timeout":
        log(f"TURN {model} failed, falling back to {DEFAULT_MODEL}: agent={req.agent_id} err={err[:120]}")
        fell_back = True
        model_kwargs = {"model": DEFAULT_MODEL}
        if is_new_session:
            session_id = (str(uuid.uuid4()) if req.stateless
                          else SESSION_STORE.reset(req.conversation_key, req.agent_id).session_id)
        ok, stdout, err = await call_claude(
            prompt,
            timeout=agent_timeout,
            allowed_tools=tools,
            add_dirs=extra_dirs,
            session_id=session_id,
            resume=not is_new_session,
            effort=agent_effort,
            agent_id=req.agent_id,
            mcp_config=mcp_config,
            **TURN_LIMITS,
            **model_kwargs,
        )

    # Retry ONCE with a fresh id, and only when the session itself is the
    # problem: Claude Code pruned its file, or it outgrew the context window
    # (claude_env.session_unusable). Until 2026-10-20 any failure retried
    # fresh, a 10-minute timeout included: that re-ran the whole tool loop at
    # full price and threw the session away. A second failure is a real
    # problem the user should hear about rather than something to keep
    # retrying.
    retried = False
    if not ok and not is_new_session and claude_env.session_unusable(err):
        log(f"TURN resume failed, retrying fresh: err={err[:80]}")
        retried = True
        session_rec = SESSION_STORE.reset(req.conversation_key, req.agent_id)
        session_id = session_rec.session_id
        # A new session holds nothing yet: it needs the full prompt.
        prompt, continued = full_prompt, False
        ok, stdout, err = await call_claude(
            prompt,
            timeout=agent_timeout,
            allowed_tools=tools,
            add_dirs=extra_dirs,
            session_id=session_id,
            resume=False,
            effort=agent_effort,
            agent_id=req.agent_id,
            mcp_config=mcp_config,
            **TURN_LIMITS,
            **model_kwargs,
        )

    if not ok:
        log(f"TURN claude FAILED: agent={req.agent_id} err={err}")
        return TurnResult(ok=False, raw_stdout=stdout or "", error_reason=err,
                          session_id=session_id,
                          is_new_session=is_new_session, resume_retried=retried,
                          prompt_chars=len(prompt), model_fallback=fell_back, continued=continued)

    # Record the fixed context the session now holds and its size, for the
    # next turn's prompt choice and rotation check.
    context = None
    if not req.stateless:
        context = claude_usage.context_tokens(session_id)
        SESSION_STORE.touch(req.conversation_key, req.agent_id,
                            static_hash=parts.static_hash, context_tokens=context)
    cap = req.max_response_chars or max_response_chars_for(req.agent_id)
    return TurnResult(
        ok=True,
        response=truncate_response(stdout, limit=cap),
        raw_stdout=stdout,
        session_id=session_id,
        is_new_session=is_new_session,
        resume_retried=retried,
        prompt_chars=len(prompt),
        model_fallback=fell_back,
        continued=continued,
        context_tokens=context,
        rotated_from=rotated_from,
    )
