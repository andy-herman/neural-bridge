"""PM intake state machine.

Port (in pattern, not code) of agent-kanban-orchestrator/src/bot/pm-intake-session.ts.

Lives entirely in memory; sessions are keyed by Discord thread ID. If the
daemon restarts mid-intake, sessions are lost — Andy starts over. PR-K can
add SQLite persistence if the volume justifies it.

Flow:
1. /pm-task fires. start_session() creates a Session in DRAFT, returns the
   first clarification question (closure criteria).
2. Andy replies in the thread. continue_session() either asks the next
   clarification, or detects an approval verb (`go`, `yes`, `ship it`) and
   moves to READY_TO_FILE.
3. main.py reads READY_TO_FILE sessions, calls finalize() to get the
   GitHub-issue-ready body, hands off to github_client.create_issue.
4. mark_filed() records the resulting issue number on the session.

Heuristics are intentionally simple: closure-criteria collection is the
non-negotiable per the prior PM-Led Workflow SOP. Other prior heuristics
(ambiguous target, scope boundary) become PR-K work — adding them now risks
making intake feel like a chatbot interrogation.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# Approval verbs that move a session from AWAITING_USER to READY_TO_FILE.
APPROVAL_VERBS_RE = re.compile(
    r"^\s*(go|yes|ship it|file it|do it|approve|sounds good|looks good|ok|okay)\s*[.!]*\s*$",
    re.IGNORECASE,
)

# Cancel verbs.
CANCEL_VERBS_RE = re.compile(
    r"^\s*(cancel|nevermind|never mind|drop it|forget it|stop)\s*[.!]*\s*$",
    re.IGNORECASE,
)


class SessionState(str, Enum):
    DRAFT = "DRAFT"
    AWAITING_USER = "AWAITING_USER"
    READY_TO_FILE = "READY_TO_FILE"
    FILED = "FILED"
    CANCELLED = "CANCELLED"


@dataclass
class ClarificationTurn:
    question: str
    answer: Optional[str] = None


@dataclass
class Session:
    thread_id: str
    user_id: str
    original_request: str
    clarifications: list[ClarificationTurn] = field(default_factory=list)
    state: SessionState = SessionState.DRAFT
    created_at: float = field(default_factory=time.time)
    issue_number: Optional[int] = None  # set after mark_filed()

    def latest_unanswered(self) -> Optional[ClarificationTurn]:
        for turn in reversed(self.clarifications):
            if turn.answer is None:
                return turn
        return None

    def closure_criteria(self) -> Optional[str]:
        """The first clarification answer is treated as the closure criteria."""
        for turn in self.clarifications:
            if turn.answer:
                return turn.answer
        return None


class PMIntake:
    """In-memory store and state machine for PM intake sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, thread_id: str) -> Optional[Session]:
        return self._sessions.get(thread_id)

    def has(self, thread_id: str) -> bool:
        return thread_id in self._sessions

    def start_session(self, *, thread_id: str, user_id: str, request: str) -> tuple[Session, str]:
        """Create a new session. Returns (session, first_clarification_question)."""
        if thread_id in self._sessions:
            raise ValueError(f"session already exists for thread {thread_id}")

        session = Session(thread_id=thread_id, user_id=user_id, original_request=request.strip())
        question = (
            "Got it. **What does done look like?** Give me a one-sentence closure "
            "criteria — something specific I can check against later when deciding "
            "to close the issue."
        )
        session.clarifications.append(ClarificationTurn(question=question))
        session.state = SessionState.AWAITING_USER
        self._sessions[thread_id] = session
        return session, question

    def continue_session(self, *, thread_id: str, user_message: str) -> tuple[Session, str]:
        """Process a user reply.

        Returns (session, bot_response_text). Caller posts the bot_response_text
        in the thread. If session.state == READY_TO_FILE after this call, caller
        should invoke github_client.create_issue(...) next.
        """
        session = self._sessions.get(thread_id)
        if session is None:
            raise KeyError(f"no session for thread {thread_id}")
        if session.state in (SessionState.FILED, SessionState.CANCELLED):
            return session, "_(this session is already closed; start a new one with /pm-task)_"

        text = user_message.strip()

        if CANCEL_VERBS_RE.match(text):
            session.state = SessionState.CANCELLED
            return session, "Cancelled. No issue created."

        latest = session.latest_unanswered()

        # If the latest clarification is unanswered and this message is an
        # approval verb on its own, treat it as approval-without-answer (rare,
        # but handle gracefully — re-prompt for the answer).
        if latest is not None and APPROVAL_VERBS_RE.match(text):
            return session, (
                "I still need an answer to: " + latest.question +
                "\n\nReply with the closure criteria, or say `cancel` to drop this task."
            )

        # Approval verb when nothing is pending → file it.
        if latest is None and APPROVAL_VERBS_RE.match(text):
            session.state = SessionState.READY_TO_FILE
            return session, "Filing the issue now…"

        # Treat the message as the answer to the latest unanswered question.
        if latest is not None:
            latest.answer = text
            session.state = SessionState.READY_TO_FILE
            return session, (
                f"Closure: \"{text}\"\n\n"
                f"Anything else to add, or **`go`** to file?"
            )

        # No pending question and not an approval verb. Treat as additional context.
        # We don't append more clarification turns automatically — keep intake short.
        return session, (
            "Got it. Reply **`go`** when you're ready to file, "
            "or **`cancel`** to drop this task."
        )

    def mark_filed(self, *, thread_id: str, issue_number: int) -> Session:
        """Record the resulting issue number after github_client.create_issue."""
        session = self._sessions.get(thread_id)
        if session is None:
            raise KeyError(f"no session for thread {thread_id}")
        session.issue_number = issue_number
        session.state = SessionState.FILED
        return session

    def cancel(self, thread_id: str) -> Optional[Session]:
        session = self._sessions.get(thread_id)
        if session is None:
            return None
        session.state = SessionState.CANCELLED
        return session

    def render_issue_body(self, thread_id: str, *, thread_url: str) -> tuple[str, str]:
        """Build (title, body) suitable for github_client.create_issue.

        Title: a tightened version of the original request (truncated to 70 chars).
        Body: structured markdown with source request, clarifications, placeholders.
        """
        session = self._sessions.get(thread_id)
        if session is None:
            raise KeyError(f"no session for thread {thread_id}")

        title = _shorten_title(session.original_request, max_len=70)

        body_lines: list[str] = [
            "## Source request",
            "",
            f"> {session.original_request}",
            "",
            "## PM clarification",
            "",
        ]
        for turn in session.clarifications:
            if turn.answer:
                body_lines.extend([
                    f"**Q:** {turn.question}",
                    "",
                    f"**A:** {turn.answer}",
                    "",
                ])

        closure = session.closure_criteria()
        body_lines.extend([
            "## Closure criteria",
            "",
            closure if closure else "_(not captured during intake; senior-pm to follow up)_",
            "",
            "## Routing",
            "",
            "- **Initial owner:** senior-pm",
            "- **Recommended specialist:** _(senior-pm to assign)_",
            "- **Final QA owner:** senior-pm",
            "",
            "## Operating loop",
            "",
            "1. senior-pm reviews and assigns to a specialist (or works it directly).",
            "2. Specialist completes work on this issue (not in Discord).",
            "3. senior-pm QAs against closure criteria and closes when satisfied.",
            "",
            "---",
            "",
            f"_PM-managed. Created from Discord thread: {thread_url} by senior-pm bot._",
        ])
        return title, "\n".join(body_lines)


def _shorten_title(text: str, *, max_len: int) -> str:
    text = " ".join(text.split())  # collapse whitespace
    if len(text) <= max_len:
        return text
    cutoff = text[: max_len - 1].rsplit(" ", 1)[0]
    return cutoff + "…"
