"""Telegram cards for queue items. Pure: text and button specs, no I/O.

Telegram is a third-party channel, so a card carries metadata only: the kind,
the agent, a title, one line of detail and a link to the full content. The
content itself (a capture's article, an agent's question) stays in the repo or
the vault.

Buttons carry callback data "q:<id>:<action>", well under Telegram's 64 bytes.
Anything that publishes or deletes (filing or rejecting a capture) asks for a
second tap, because a stray thumb on a phone should not merge a pull request.
"""

from __future__ import annotations

import html
import re
import time

from . import store as st

CALLBACK_RE = re.compile(r"^q:([0-9a-f]{6}):([a-z]+!?)$")

# action -> (verb it records, needs a confirming second tap)
APPROVE, APPROVE_CONFIRMED = "approve", "approve!"
REJECT, REJECT_CONFIRMED = "reject", "reject!"
ACK, SNOOZE, RETRY, SHOW, OPEN = "ack", "snooze", "retry", "show", "open"
ACTIONS = frozenset({APPROVE, APPROVE_CONFIRMED, REJECT, REJECT_CONFIRMED,
                     ACK, SNOOZE, RETRY, SHOW, OPEN})

SNOOZE_SECONDS = 24 * 3600

_KIND_LABEL = {
    st.CAPTURE: "Capture",
    st.ALERT: "Alert",
    st.QUESTION: "Question",
    st.ACTION: "Action",
    st.PR_REVIEW: "PR review",
    st.PUBLISH: "Publish",
    st.PROBE: "Probe",
}

# What approve/reject are called on each kind's buttons.
_APPROVE_LABEL = {st.CAPTURE: "File it"}
_CONFIRM_TEXT = {
    (st.CAPTURE, st.APPROVE): ("File this into the wiki? This opens a pull request that "
                               "moves it from quarantine into knowledge/concepts and merges it."),
    (st.CAPTURE, st.REJECT): ("Reject it? This opens a pull request that deletes it from "
                              "quarantine and merges it. Git history keeps the text."),
}


def callback(item_id: str, action: str) -> str:
    return f"q:{item_id}:{action}"


def parse_callback(data: str | None) -> tuple[str, str] | None:
    """(item_id, action) for our buttons; None for anything else."""
    m = CALLBACK_RE.match(data or "")
    if not m or m.group(2) not in ACTIONS:
        return None
    return m.group(1), m.group(2)


def _when(epoch: int | None) -> str:
    if not epoch:
        return "?"
    return time.strftime("%a %d %b %H:%M", time.localtime(epoch))


def _status_line(item: st.Item) -> str:
    verb = item.verb
    if item.state == st.SNOOZED:
        return f"Snoozed until {_when(item.snooze_until)}."
    if item.state in (st.DECIDED, st.APPLYING):
        return {st.APPROVE: "Filing it now…", st.REJECT: "Rejecting it now…"}.get(
            verb, "Recorded, applying now…")
    if item.state == st.APPLIED:
        done = {st.APPROVE: "Filed", st.REJECT: "Rejected",
                st.ACKNOWLEDGE: "Acknowledged"}.get(verb, verb.capitalize() or "Done")
        extra = f": {item.result}" if item.result and item.result.startswith("http") else ""
        tail = " It has since cleared." if item.kind == st.ALERT and item.resolved_at else ""
        return f"{done} {_when(item.decided_at)}{extra}.{tail}"
    if item.state == st.APPLY_FAILED:
        return f"That did not go through: {item.result or 'no detail'}"
    if item.state == st.SUPERSEDED:
        if item.kind == st.ALERT:
            return f"Cleared on its own {_when(item.resolved_at)}. Nothing to do."
        return f"No longer waiting ({_when(item.resolved_at)}). Nothing to do."
    return ""


def card_text(item: st.Item) -> str:
    head = f"<b>{_KIND_LABEL.get(item.kind, item.kind)}</b>"
    who = item.agent or item.source
    if who:
        head += f" · {html.escape(who)}"
    lines = [head, f"<b>{html.escape(item.title)}</b>"]
    if item.detail:
        lines.append(html.escape(item.detail))
    if item.ref.startswith("https://"):
        lines.append(f'<a href="{html.escape(item.ref, quote=True)}">Read it</a>')
    status = _status_line(item)
    if status:
        lines.append("")
        lines.append(f"<i>{html.escape(status)}</i>")
    lines.append(f"<code>{item.id}</code>")
    return "\n".join(lines)


def keyboard(item: st.Item) -> list[list[tuple[str, str]]]:
    """Rows of (label, callback data). Empty once nothing is left to press."""
    if item.state == st.OPEN:
        if item.kind == st.ALERT or item.kind == st.PROBE:
            return [[("Acknowledge", callback(item.id, ACK)),
                     ("Snooze 1 day", callback(item.id, SNOOZE))]]
        return [[(_APPROVE_LABEL.get(item.kind, "Approve"), callback(item.id, APPROVE)),
                 ("Reject", callback(item.id, REJECT))],
                [("Snooze 1 day", callback(item.id, SNOOZE))]]
    if item.state == st.APPLY_FAILED:
        return [[("Retry", callback(item.id, RETRY))]]
    return []


def confirm_text(item: st.Item, verb: str) -> str:
    question = _CONFIRM_TEXT.get((item.kind, verb), f"{verb.capitalize()} this?")
    return card_text(item) + "\n\n" + html.escape(question)


def confirm_keyboard(item: st.Item, verb: str) -> list[list[tuple[str, str]]]:
    confirmed = APPROVE_CONFIRMED if verb == st.APPROVE else REJECT_CONFIRMED
    label = "Yes, file it" if (item.kind, verb) == (st.CAPTURE, st.APPROVE) else f"Yes, {verb}"
    return [[(label, callback(item.id, confirmed)), ("Cancel", callback(item.id, SHOW))]]


def is_closed(item: st.Item) -> bool:
    """A message for this item needs no further edits once it gets here."""
    return item.state in st.FINAL or item.state == st.SNOOZED


def list_text(items: list[st.Item], *, limit: int, now: int | None = None) -> str:
    now = now if now is not None else int(time.time())
    if not items:
        return "Nothing is waiting on you."
    lines = [f"<b>{len(items)} waiting</b>, oldest first:"]
    for it in items[:limit]:
        age_days = (now - it.created_at) / 86400
        age = f"{age_days:.0f}d" if age_days >= 1 else f"{(now - it.created_at) / 3600:.0f}h"
        state = "" if it.state == st.OPEN else f" [{it.state.replace('_', ' ')}]"
        lines.append(f"• {_KIND_LABEL.get(it.kind, it.kind)}: {html.escape(it.title)} "
                     f"({age}){state} <code>{it.id}</code>")
    if len(items) > limit:
        lines.append(f"…and {len(items) - limit} more: <code>python -m scripts.review_queue list</code>")
    return "\n".join(lines)


def list_keyboard(items: list[st.Item], *, limit: int) -> list[list[tuple[str, str]]]:
    rows = []
    for it in items[:limit]:
        label = it.title if len(it.title) <= 40 else it.title[:39] + "…"
        rows.append([(label, callback(it.id, OPEN))])
    return rows
