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

import uuid
from dataclasses import dataclass, field
from typing import Callable

from .claude_invoke import call_claude
from .mention import (
    MENTION_PROMPT_PATH,
    add_dirs_for,
    allowed_tools_for,
    build_mention_prompt,
    effort_for,
    load_agent_definition,
    max_response_chars_for,
    timeout_for,
    truncate_response,
)
from .session_store import STORE as SESSION_STORE


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
    # Set when the turn failed before claude was ever called (missing template).
    setup_error: str = ""

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
    prompt = build_mention_prompt(
        template,
        agent_id=req.agent_id,
        agent_definition=agent_definition,
        channel_kind=req.channel_kind,
        history=req.history,
        message_content=req.message_content,
        conversation_log_path=req.conversation_log_path,
    )
    if req.prompt_prefix:
        prompt = req.prompt_prefix + prompt

    tools = allowed_tools_for(req.agent_id)
    extra_dirs = add_dirs_for(req.agent_id)
    agent_timeout = timeout_for(req.agent_id)
    agent_effort = effort_for(req.agent_id)
    model_kwargs = {"model": req.model} if req.model else {}

    if req.stateless:
        session_id = str(uuid.uuid4())
        is_new_session = True
    else:
        session_rec, is_new_session = SESSION_STORE.get_or_create(req.conversation_key, req.agent_id)
        session_id = session_rec.session_id

    log(
        f"TURN calling claude: agent={req.agent_id} key={req.conversation_key} "
        f"effort={agent_effort} timeout={agent_timeout}s "
        f"session={session_id[:8]}... "
        f"({'stateless' if req.stateless else 'new' if is_new_session else 'resumed'})"
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
        **model_kwargs,
    )

    # A failure under --resume usually means Claude Code pruned the session
    # file. Retry ONCE with a fresh id; a second failure is a real problem the
    # user should hear about rather than something to keep retrying.
    retried = False
    if not ok and not is_new_session:
        log(f"TURN resume failed, retrying fresh: err={err[:80]}")
        retried = True
        session_rec = SESSION_STORE.reset(req.conversation_key, req.agent_id)
        session_id = session_rec.session_id
        ok, stdout, err = await call_claude(
            prompt,
            timeout=agent_timeout,
            allowed_tools=tools,
            add_dirs=extra_dirs,
            session_id=session_id,
            resume=False,
            effort=agent_effort,
            agent_id=req.agent_id,
            **model_kwargs,
        )

    if not ok:
        log(f"TURN claude FAILED: agent={req.agent_id} err={err}")
        return TurnResult(ok=False, raw_stdout=stdout or "", error_reason=err,
                          session_id=session_id,
                          is_new_session=is_new_session, resume_retried=retried,
                          prompt_chars=len(prompt))

    if not req.stateless:
        SESSION_STORE.touch(req.conversation_key, req.agent_id)
    cap = req.max_response_chars or max_response_chars_for(req.agent_id)
    return TurnResult(
        ok=True,
        response=truncate_response(stdout, limit=cap),
        raw_stdout=stdout,
        session_id=session_id,
        is_new_session=is_new_session,
        resume_retried=retried,
        prompt_chars=len(prompt),
    )
