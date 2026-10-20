"""Parse Andy's Obsidian kanban into something Luna can act on as an EA.

`Luna Master/Herman Tasks.md` is an Obsidian Kanban board: `## Lane` headings,
`- [ ]` / `- [x]` cards, `[[wikilinks]]`, and an `@{DATE}` trigger.

Why this module exists: Luna's proactive check-in originally opened with the
fleet briefing, which is agent health. That is devops, not executive assistance.
The board is where the actual commitments live.

THE DATE TRAP, and it is the whole reason this file is careful:

`@{DATE}` is usually the date the card was ADDED, and the real deadline is
written into the text ("by 2026-09-07"). Reading the trigger as a due date marks
a card that says "Prep for the regulator's first CTP Annual Review by 2028-01"
as 33 days overdue, and produces twenty false alarms on a board of twenty-two.
An assistant that cries wolf on its first run is muted by its second, which is
the same failure the memory canary exists to avoid.

But the trigger is not used consistently: on some cards it IS the deadline. The
disambiguation is that a date in the future cannot be an added date, so a
forward-dated trigger is read as a deadline and a past one as an added date. An
explicit in-text "by <date>" always wins over both.

So: `added` drives staleness, a deadline drives overdue, and the two are never
conflated. Everything is pure and takes `today` as an argument so the arithmetic
is testable and does not drift with the clock.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

BOARD_PATH = Path.home() / "Documents" / "Luna Master" / "Herman Tasks.md"

# Lanes holding commitments. "Done" is history. "Now" is what Andy has actually
# committed to; "Inbox" is untriaged and is judged on staleness instead.
ACTIVE_LANES = ("Now", "Next", "Waiting", "Inbox")
COMMITTED_LANES = ("Now", "Next", "Waiting")

STALE_AFTER_DAYS = 30      # an Inbox card older than this has been avoided
DUE_SOON_DAYS = 14

_LANE_RE = re.compile(r"^##\s+(.+?)\s*$")
_CARD_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")
_ADDED_RE = re.compile(r"@\{(\d{4}-\d{2}-\d{2})\}")
_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# Deadlines as Andy writes them: "by 2026-09-07", "by 2028-01".
_DEADLINE_FULL_RE = re.compile(r"\bby\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
_DEADLINE_MONTH_RE = re.compile(r"\bby\s+(\d{4})-(\d{2})\b(?!-)", re.IGNORECASE)
# Andy sometimes states the state directly. Trust it over inference.
_FLAG_OVERDUE_RE = re.compile(r"\bOVERDUE\s*(\d+)?\s*d?\b", re.IGNORECASE)
_FLAG_ESCALATED_RE = re.compile(r"escalat", re.IGNORECASE)


@dataclass(frozen=True)
class Task:
    lane: str
    text: str
    raw: str
    added: date | None        # from @{...}: when it landed on the board
    deadline: date | None     # from "by <date>" in the text: when it is due
    links: tuple[str, ...]
    done: bool
    flagged_overdue: bool     # Andy wrote OVERDUE on the card himself
    escalated: bool

    def days_stale(self, today: date) -> int | None:
        if self.added is None:
            return None
        return (today - self.added).days

    def days_overdue(self, today: date) -> int | None:
        """Only ever computed from a real deadline. None when undated."""
        if self.deadline is None:
            return None
        return (today - self.deadline).days

    def is_overdue(self, today: date) -> bool:
        if self.flagged_overdue:
            return True
        over = self.days_overdue(today)
        return over is not None and over > 0

    def is_stale(self, today: date) -> bool:
        stale = self.days_stale(today)
        return self.lane == "Inbox" and stale is not None and stale >= STALE_AFTER_DAYS


def _parse_deadline(text: str) -> date | None:
    m = _DEADLINE_FULL_RE.search(text)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    m = _DEADLINE_MONTH_RE.search(text)
    if m:
        try:
            # "by 2028-01" means by the end of that month; use the 1st, which
            # is conservative (surfaces slightly early rather than late).
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            pass
    return None


def parse_board(text: str, today: date | None = None) -> list[Task]:
    """Parse kanban markdown into Tasks.

    `today` is only used to disambiguate the `@{DATE}` trigger, which Andy uses
    both ways: as the date a card was added, and on some cards as the deadline
    itself. A date in the future cannot be an added date, so a forward-dated
    trigger is read as a deadline. Defaults to the real today.
    """
    now = today or date.today()
    tasks: list[Task] = []
    lane = ""
    for line in text.splitlines():
        lane_match = _LANE_RE.match(line)
        if lane_match:
            lane = lane_match.group(1).strip()
            continue
        card = _CARD_RE.match(line)
        if not card or not lane:
            continue
        raw = card.group(2)
        added = None
        m = _ADDED_RE.search(raw)
        if m:
            try:
                added = date.fromisoformat(m.group(1))
            except ValueError:
                added = None
        clean = _ADDED_RE.sub("", _LINK_RE.sub("", raw)).strip()
        clean = re.sub(r"\s{2,}", " ", clean).strip(" .,")
        deadline = _parse_deadline(raw)
        # A forward-dated trigger is a deadline, not an added date. An in-text
        # "by <date>" still wins, since it is unambiguous.
        if added is not None and added > now:
            if deadline is None:
                deadline = added
            added = None
        tasks.append(Task(
            lane=lane,
            text=clean,
            raw=raw,
            added=added,
            deadline=deadline,
            links=tuple(_LINK_RE.findall(raw)),
            done=card.group(1).lower() == "x",
            flagged_overdue=bool(_FLAG_OVERDUE_RE.search(raw)),
            escalated=bool(_FLAG_ESCALATED_RE.search(raw)),
        ))
    return tasks


def open_tasks(tasks: list[Task], lanes: tuple[str, ...] = ACTIVE_LANES) -> list[Task]:
    return [t for t in tasks if not t.done and t.lane in lanes]


def overdue(tasks: list[Task], today: date) -> list[Task]:
    out = [t for t in open_tasks(tasks) if t.is_overdue(today)]
    # Escalated first, then by how far past due, then flagged-without-a-date.
    return sorted(out, key=lambda t: (not t.escalated, -(t.days_overdue(today) or 0)))


def due_soon(tasks: list[Task], today: date, within: int = DUE_SOON_DAYS) -> list[Task]:
    out = []
    for t in open_tasks(tasks):
        if t.is_overdue(today):
            continue
        over = t.days_overdue(today)
        if over is not None and -within <= over <= 0:
            out.append(t)
    return sorted(out, key=lambda t: t.deadline or date.max)


def committed_undated(tasks: list[Task]) -> list[Task]:
    """Cards in Now/Next/Waiting with no deadline. Committed but unscheduled,
    which is where things quietly rot."""
    return [t for t in open_tasks(tasks, COMMITTED_LANES)
            if t.deadline is None and not t.flagged_overdue]


def stale_inbox(tasks: list[Task], today: date) -> list[Task]:
    out = [t for t in open_tasks(tasks) if t.is_stale(today)]
    return sorted(out, key=lambda t: t.days_stale(today) or 0, reverse=True)


def load_board(path: Path | None = None, today: date | None = None) -> list[Task]:
    target = path or BOARD_PATH
    try:
        return parse_board(target.read_text(encoding="utf-8"), today=today)
    except (OSError, UnicodeDecodeError):
        return []


def _fmt(t: Task, today: date) -> str:
    bits = [t.lane]
    over = t.days_overdue(today)
    if t.escalated:
        bits.append("ESCALATED")
    if over is not None and over > 0:
        bits.append(f"{over}d past due")
    elif t.flagged_overdue:
        bits.append("marked overdue")
    elif over is not None:
        bits.append("due today" if over == 0 else f"due in {-over}d")
    stale = t.days_stale(today)
    if not t.deadline and not t.flagged_overdue and stale is not None:
        bits.append(f"on board {stale}d")
    src = f" [{t.links[0]}]" if t.links else ""
    return f"- ({', '.join(bits)}) {t.text}{src}"


def format_for_prompt(tasks: list[Task], today: date, max_per_section: int = 8) -> str:
    """Urgency-ordered brief. Sections only appear when non-empty."""
    if not tasks:
        return ""
    od = overdue(tasks, today)
    soon = due_soon(tasks, today)
    undated = committed_undated(tasks)
    stale = stale_inbox(tasks, today)

    lines: list[str] = []
    if od:
        lines.append(f"PAST DUE ({len(od)}):")
        lines += [_fmt(t, today) for t in od[:max_per_section]]
    if soon:
        lines.append(f"\nDUE WITHIN {DUE_SOON_DAYS} DAYS ({len(soon)}):")
        lines += [_fmt(t, today) for t in soon[:max_per_section]]
    if undated:
        lines.append(f"\nCOMMITTED, NO DATE ({len(undated)}):")
        lines += [_fmt(t, today) for t in undated[:max_per_section]]
    if stale:
        lines.append(f"\nINBOX SITTING OVER {STALE_AFTER_DAYS} DAYS ({len(stale)}):")
        lines += [_fmt(t, today) for t in stale[:max_per_section]]
    lines.append(f"\n({len(open_tasks(tasks))} open across the board.)")
    return "\n".join(lines)


# ---------- closing a card ----------
#
# Why Luna can write here at all: Andy tells her in chat that something is done,
# and until 2026-10-20 nothing she could do made that stick. Her charter allowed
# writes to her own notes.md only, while every reader that raises open work (her
# morning and evening check-ins, the Needs Andy digest, the Gate Registry) reads
# this board. So three overdue cards he called done on 2026-10-13 lived in her
# chat memory alone, and the board kept them overdue for a week of check-ins.
# This is the one board write she gets: move an open card to Done, dated, on
# Andy's word. It never edits, adds or deletes a card.

DONE_LANE = "Done"
BACKUP_DIR = Path.home() / "Library" / "Application Support" / "neural-bridge" / "board-backups"
BACKUPS_KEPT = 30


class CloseError(ValueError):
    """The card could not be closed; the message says why, for Andy."""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _LINK_RE.sub(lambda m: m.group(1), text).lower())


def _open_cards(text: str) -> list[tuple[int, str, str]]:
    """(line index, lane, card text) for every open card in an active lane."""
    out = []
    lane = ""
    for i, line in enumerate(text.splitlines()):
        lane_match = _LANE_RE.match(line)
        if lane_match:
            lane = lane_match.group(1).strip()
            continue
        card = _CARD_RE.match(line)
        if card and card.group(1) == " " and lane in ACTIVE_LANES:
            out.append((i, lane, card.group(2)))
    return out


def matching_open_cards(text: str, query: str) -> list[tuple[int, str, str]]:
    """The open cards holding every word of `query`, case-insensitive. Words,
    not substrings: "part 3" must not match "Part 30". A query with no words
    matches nothing."""
    wanted = _words(query)
    if not wanted:
        return []
    return [c for c in _open_cards(text) if all(w in set(_words(c[2])) for w in wanted)]


def _closure_note(done_on: date, note: str) -> str:
    detail = f": {note.strip().rstrip('.')}" if note and note.strip() else ""
    return f"Done {done_on.isoformat()} (Andy confirmed to Luna{detail})."


def close_card(text: str, query: str, done_on: date, note: str = "") -> tuple[str, str, str]:
    """Move the one open card matching `query` to the Done lane.

    Returns (new board text, the card's text, the lane it left). Raises
    CloseError when no card or more than one card matches, or the board has no
    Done lane. The closure note goes before the card's links and `@{date}`, the
    way the cards closed by hand already read.
    """
    matches = matching_open_cards(text, query)
    if not matches:
        raise CloseError(f"No open card matches {query!r}.")
    if len(matches) > 1:
        listed = "\n".join(f"- ({lane}) {card[:160]}" for _, lane, card in matches)
        raise CloseError(f"{len(matches)} open cards match {query!r}; use more of its words:\n{listed}")
    index, lane, card = matches[0]

    cut = min((p for p in (card.find("[["), card.find("@{")) if p >= 0), default=len(card))
    head, tail = card[:cut].rstrip(), card[cut:]
    closed = f"- [x] {head} {_closure_note(done_on, note)}" + (f" {tail}" if tail else "")

    lines = text.splitlines()
    del lines[index]
    done_at = next((i for i, line in enumerate(lines)
                    if (m := _LANE_RE.match(line)) and m.group(1).strip() == DONE_LANE), None)
    if done_at is None:
        raise CloseError(f"The board has no '## {DONE_LANE}' lane.")
    insert_at = done_at + 1
    # Skip the blank line and the Kanban plugin's "**Complete**" marker, so the
    # newest closure sits at the top of the lane's cards.
    while insert_at < len(lines) and (not lines[insert_at].strip() or lines[insert_at].strip() == "**Complete**"):
        insert_at += 1
    lines.insert(insert_at, closed)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), card, lane


def _backup(path: Path, text: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = BACKUP_DIR / f"{path.stem}.{stamp}.md"
    target.write_text(text, encoding="utf-8")
    for old in sorted(BACKUP_DIR.glob(f"{path.stem}.*.md"))[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
    return target


def close_on_board(query: str, done_on: date | None = None, note: str = "",
                   path: Path | None = None) -> str:
    """Close a card in the real board file. Backs the board up first and
    writes atomically, so a half-written board never reaches Obsidian."""
    target = path or BOARD_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CloseError(f"The board could not be read: {exc}") from exc
    new_text, card, lane = close_card(text, query, done_on or date.today(), note)
    _backup(target, text)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)
    clean = re.sub(r"\s{2,}", " ", _ADDED_RE.sub("", _LINK_RE.sub("", card))).strip(" .,")
    return f"Closed: {clean} (moved from {lane} to {DONE_LANE})."


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m scripts.luna.tasks",
                                     description="Andy's commitment board")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_open = sub.add_parser("open", help="list open cards, optionally only those holding these words")
    p_open.add_argument("words", nargs="*")
    p_close = sub.add_parser("close", help="move the one open card holding these words to Done")
    p_close.add_argument("words", nargs="+")
    p_close.add_argument("--note", default="", help="a few words on the outcome")
    p_close.add_argument("--on", default="", help="the day it was done (YYYY-MM-DD); default today")
    args = parser.parse_args(argv)

    if args.cmd == "open":
        try:
            text = BOARD_PATH.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"The board could not be read: {exc}")
            return 1
        query = " ".join(args.words)
        cards = matching_open_cards(text, query) if query else _open_cards(text)
        for _, lane, card in cards:
            print(f"- ({lane}) {card}")
        print(f"({len(cards)} open{' matching ' + repr(query) if query else ''}.)")
        return 0

    try:
        done_on = date.fromisoformat(args.on) if args.on else None
        print(close_on_board(" ".join(args.words), done_on, args.note))
    except ValueError as exc:  # CloseError and a bad --on date
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
