"""Question items: what an agent's session left open, and Andy's answer back.

Producer. hooks/flush.py extracts `open_questions` from every session it
flushes. For an attributed agent (one with a progress log that its next session
reads), each question becomes a question item. Unattributed sessions are
skipped, for the same reason flush and session_start skip them: an answer would
land somewhere nothing reads.

Conversations. Every turn of a Discord or Telegram conversation is a flush of
the same session, so a question the agent has just asked Andy in the chat is
"open" the moment the turn ends. Pushing it then would duplicate the chat, so:

  - a new question is held for HOLD_SECONDS and only pushed once the
    conversation has been quiet that long;
  - each flush of a session replaces that session's undecided questions: one
    the new flush no longer lists was answered in the chat, and is cleared;
  - a question worded like one already live in the session is the same
    question, and one worded like one Andy already answered or dismissed is
    not raised again (flush rewords questions from turn to turn, and an
    answer given on a card never reaches the session's transcript). For the
    same reason, unanswered() keeps such a question out of the "Open" list
    flush writes to the agent's progress log.

Answer. Andy replies to the card in Telegram (or runs the CLI `answer`). The
applier appends a dated entry to the agent's progress.md, so its next session
opens with the question and the answer. Dismiss records that he saw it.

Privacy. Telegram is a third-party channel, so a card carries a one-line
summary of the question with obvious personal data (emails, phone and account
numbers, links) removed. The full text stays in the local database and the
vault. Agents whose material is private (PRIVATE_AGENTS) show no text at all;
read and answer those at the desk.

Backlog. Flush wrote 339 real questions into daily-logs/ between May and
October 2026, before anything collected them, all from unattributed sessions.
`python -m scripts.review_queue backlog` lists them for one triage pass: keep
the few still worth answering (routed to an agent) and close the rest.

Stdlib only: hooks/flush.py imports this and must not need a venv.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import store as st

SOURCE = "flush"
BACKLOG_SOURCE = "flush_backlog"
BACKLOG_CLOSED_KEY = "question_backlog_closed"
UNATTRIBUTED = "_unattributed"

# Career record and self-knowledge: no question text on a third-party channel.
PRIVATE_AGENTS = frozenset({"loid", "echo"})

TITLE_MAX = 120
HOLD_SECONDS = 30 * 60
# Word overlap at which two questions from one session are the same question.
SAME_QUESTION = 0.6

_NONE_RE = re.compile(r"^[\W_]*(none|n/?a|nothing)[\W_]*$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL_RE = re.compile(r"\bhttps?://\S+", re.IGNORECASE)
# Digit runs with separators. Only ten digits or more count as a phone
# number, so dates ("2026-07-29", "2026-2027") survive.
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
_LONG_NUMBER_RE = re.compile(r"\b\d{6,}\b")
_MARKDOWN_RE = re.compile(r"[*_`#>]+")


def is_real(question: str) -> bool:
    """Flush writes "(none)" when a session left nothing open."""
    return bool(question and question.strip()) and not _NONE_RE.match(question)


def card_title(question: str, agent: str = "") -> str:
    """The one line a Telegram card may show for this question."""
    if agent in PRIVATE_AGENTS:
        return f"{agent.capitalize()} has an open question (read it at your desk)"
    text = _MARKDOWN_RE.sub("", question)
    text = _EMAIL_RE.sub("[email]", text)
    text = _URL_RE.sub("[link]", text)
    text = _PHONE_RE.sub(lambda m: "[number]" if sum(c.isdigit() for c in m.group()) >= 10
                         else m.group(), text)
    text = _LONG_NUMBER_RE.sub("[number]", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > TITLE_MAX:
        text = text[:TITLE_MAX - 1].rstrip() + "…"
    return text or "An open question"


def question_key(agent: str, session_id: str, question: str) -> str:
    return hashlib.sha256(f"{agent}\n{session_id}\n{question.strip()}".encode()).hexdigest()[:16]


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def same_question(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= SAME_QUESTION


_DECIDED = (st.DECIDED, st.APPLYING, st.APPLIED, st.APPLY_FAILED)


def _decided_in_session(store: st.Store, agent: str, session_id: str) -> list[st.Item]:
    return [i for i in store.items(states=_DECIDED, kinds=(st.QUESTION,))
            if i.source == SOURCE and i.agent == agent and i.payload.get("session_id") == session_id]


def unanswered(agent: str, session_id: str, open_questions: list[str], *,
               store: st.Store | None = None) -> list[str]:
    """`open_questions` without the ones Andy already answered or dismissed on
    a card in this session."""
    if not agent or agent == UNATTRIBUTED or not open_questions:
        return list(open_questions)
    handled = [str(i.payload.get("question", "")) for i in
               _decided_in_session(store or st.Store(), agent, session_id)]
    return [q for q in open_questions if not any(same_question(q, h) for h in handled)]


def raise_from_flush(agent: str, session_id: str, open_questions: list[str], *,
                     complete: bool = True, store: st.Store | None = None,
                     now: int | None = None) -> list[st.Item]:
    """Make the session's question items match this flush. Returns the items
    created (each held for HOLD_SECONDS before it is pushed).

    `complete` is False when flush saw only the tail of a long transcript: an
    earlier question missing from it may still be open, so nothing is cleared.
    """
    if not agent or agent == UNATTRIBUTED:
        return []
    store = store or st.Store()
    now = now if now is not None else int(time.time())
    mine = [i for i in store.items(kinds=(st.QUESTION,))
            if i.source == SOURCE and i.agent == agent and i.payload.get("session_id") == session_id]
    live = [i for i in mine if i.state in (st.OPEN, st.SNOOZED) and i.resolved_at is None]
    # Decided by Andy. A cleared (superseded) one may come back if it resurfaces.
    handled = [i for i in mine if i.state in _DECIDED]
    still_open: set[str] = set()
    created = []
    for q in open_questions:
        if not is_real(q):
            continue
        if any(same_question(q, str(i.payload.get("question", ""))) for i in handled):
            continue
        match = next((i for i in live if same_question(q, str(i.payload.get("question", "")))), None)
        if match is not None:
            still_open.add(match.id)
            continue
        item, is_new = store.raise_item(
            source=SOURCE, key=question_key(agent, session_id, q), kind=st.QUESTION,
            title=card_title(q, agent), detail=f"Left open in a {agent} session.",
            agent=agent, payload={"question": q.strip(), "session_id": session_id},
            unique=True, now=now)
        if is_new:
            store.hold(item.id, now + HOLD_SECONDS, actor=SOURCE, now=now)
            created.append(store.get(item.id) or item)
        still_open.add(item.id)
    if complete:
        for i in live:
            if i.id not in still_open:
                store.clear(source=SOURCE, key=i.key, now=now)
    return created


# ---------- answering ----------

def answer_entry(item: st.Item, answer: str, *, when: datetime | None = None) -> str:
    """The progress.md entry an answer becomes. Same heading shape flush
    writes, so the progress log's reader treats it as one more entry."""
    from scripts.discord_bot import progress_log

    when = when or datetime.now(timezone.utc)
    question = str(item.payload.get("question") or item.title)
    session = str(item.payload.get("session_id") or "")
    asked = f"**Asked** (session {session[:8]})" if session else "**Asked**"
    entry = (f"## {when.strftime('%Y-%m-%d %H:%MZ')} answer from Andy (review queue {item.id})\n\n"
             f"{asked}\n- {question}\n\n**Answer**\n- {answer.strip()}")
    entry = progress_log.sanitize(entry)
    return entry[:progress_log.MAX_ENTRY_CHARS]


def apply(item: st.Item, *, agents_base: Path | None = None) -> tuple[bool, str]:
    """Applier: an answer goes into the agent's progress log; a dismissal
    needs nothing more."""
    if item.verb == st.REJECT:
        return True, "dismissed"
    if item.verb != st.ANSWER:
        return False, f"questions take an answer or a dismissal, not {item.verb!r}"
    answer = str(item.payload.get("answer") or "").strip()
    if not answer:
        return False, "no answer text was recorded"
    if not item.agent:
        return False, "no agent to give the answer to"
    from scripts.discord_bot import progress_log

    ok, reason = progress_log.append_entry(item.agent, answer_entry(item, answer),
                                           agents_base=agents_base)
    return ok, (f"in {item.agent}'s progress log" if ok else reason)


# ---------- the pre-queue backlog ----------

_SESSION_RE = re.compile(r"(?m)^## ")
_OPEN_RE = re.compile(r"(?ms)^### Open questions\s*\n(.*?)(?=^### |^## |\Z)")
_SESSION_ID_RE = re.compile(r"session[_ ]id[:*\s`]*([0-9a-f-]{8,})", re.IGNORECASE)


def backlog(daily_logs: Path) -> list[dict]:
    """Every real open question in daily-logs/, oldest first:
    {id, agent, day, session_id, question}."""
    out: list[dict] = []
    for path in sorted(daily_logs.glob("*/*.md")):
        if path.parent.name.startswith(".") or "_failed" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for block in _SESSION_RE.split(text)[1:]:
            m = _OPEN_RE.search(block)
            if not m:
                continue
            sid = _SESSION_ID_RE.search(block)
            session_id = sid.group(1) if sid else ""
            for q in re.findall(r"(?m)^- (.+)$", m.group(1)):
                if not is_real(q):
                    continue
                agent = path.parent.name
                key = question_key(agent, f"{path.stem}:{session_id}", q)
                out.append({"id": key[:6], "key": key, "agent": agent, "day": path.stem,
                            "session_id": session_id, "question": q.strip()})
    return out


def keep(store: st.Store, entry: dict, agent: str, *, now: int | None = None) -> tuple[st.Item, bool]:
    """Raise a backlog question as a live item, routed to `agent`."""
    return store.raise_item(
        source=BACKLOG_SOURCE, key=entry["key"], kind=st.QUESTION,
        title=card_title(entry["question"], agent),
        detail=f"From the pre-queue backlog ({entry['day']}).", agent=agent,
        payload={"question": entry["question"], "session_id": entry["session_id"],
                 "backlog_day": entry["day"]},
        unique=True, now=now)
