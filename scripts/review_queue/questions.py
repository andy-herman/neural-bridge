"""Question items: what an agent's session left open, and Andy's answer back.

Producer. hooks/flush.py extracts `open_questions` from every session it
flushes. For an attributed agent (one with a progress log that its next session
reads), each question becomes a question item. Unattributed sessions are
skipped, for the same reason flush and session_start skip them: an answer would
land somewhere nothing reads.

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


def raise_from_flush(agent: str, session_id: str, open_questions: list[str], *,
                     store: st.Store | None = None, now: int | None = None) -> list[st.Item]:
    """Queue each real open question from one flushed session. Idempotent: a
    session flushed twice raises nothing new. Returns the items created."""
    if not agent or agent == UNATTRIBUTED:
        return []
    store = store or st.Store()
    created = []
    for q in open_questions:
        if not is_real(q):
            continue
        item, is_new = store.raise_item(
            source=SOURCE, key=question_key(agent, session_id, q), kind=st.QUESTION,
            title=card_title(q, agent), detail=f"Left open in a {agent} session.",
            agent=agent, payload={"question": q.strip(), "session_id": session_id},
            unique=True, now=now)
        if is_new:
            created.append(item)
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
