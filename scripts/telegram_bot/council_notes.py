"""Council meeting notes: one Obsidian note per day, rebuilt from the room log.

The council bridge (council_bridge.py) appends every message posted in the room
to a JSONL room log. This module turns that log into a daily note in the vault:

  - render_day(day) rebuilds <COUNCIL_NOTES_DIR>/YYYY-MM-DD.md from the room log
    alone. It is idempotent, so the bridge calls it after every exchange and
    after every /discuss, and the note is current while the meeting runs.
  - `summarize` (the CLI below, run nightly by launchd) asks a small model for
    3 to 6 bullets on a day with council activity and writes them into the
    note's summary block, which every later render keeps byte for byte.

The note is the room log's view, not a document of record: anything edited by
hand outside the summary block is replaced on the next render.

Usage:

    .venv/bin/python -m scripts.telegram_bot.council_notes render [--date YYYY-MM-DD]
    .venv/bin/python -m scripts.telegram_bot.council_notes summarize [--date YYYY-MM-DD] [--dry-run]

Without --date, `render` rebuilds today's note and `summarize` covers yesterday,
plus today when run after 22:00 local. Layout and env vars: COUNCIL_SETUP.md.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.discord_bot.claude_invoke import call_claude_sync, wrap_untrusted  # noqa: E402
from scripts.env_file import load_default_env  # noqa: E402

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

# The room log the bridge writes. Shared with council_bridge, which imports
# these, so the two can never disagree about where it lives.
ROOM_LOG_ENV = "COUNCIL_ROOM_LOG"
DEFAULT_ROOM_LOG = (
    Path.home() / "Library" / "Application Support" / "neural-bridge" / "council-room.jsonl"
)

NOTES_DIR_ENV = "COUNCIL_NOTES_DIR"
DEFAULT_NOTES_DIR = Path.home() / "Documents" / "Luna Master" / "Council"

SUMMARY_MODEL = os.environ.get("COUNCIL_SUMMARY_MODEL", "claude-haiku-5-5")
SUMMARY_TIMEOUT = int(os.environ.get("COUNCIL_SUMMARY_TIMEOUT", "180"))
# Far above a busy day in the room; a guard against a runaway log, not a budget.
SUMMARY_MAX_TRANSCRIPT_CHARS = 200_000
SUMMARY_MAX_BULLETS = 6
# After this hour, a default `summarize` also covers today.
SUMMARIZE_TODAY_AFTER_HOUR = 22

# Normal exchanges more than this far apart are separate conversations.
CONVERSATION_GAP_SECONDS = 3 * 3600
HEADING_QUESTION_CHARS = 80

# Speakers in the order the frontmatter lists them; anyone else follows.
SPEAKER_ORDER = ("Andy", "Yor", "Loid", "Luna")

# None is the machine's local zone (Andy's). Tests set a fixed zone.
LOCAL_TZ: tzinfo | None = None

SUMMARY_START = "<!-- council-summary:start"
SUMMARY_END = "<!-- council-summary:end -->"
_SUMMARY_BLOCK_RE = re.compile(
    r"<!-- council-summary:start(?: last_ts=(?P<ts>[0-9][0-9.eE+-]*))? -->\n.*?"
    r"<!-- council-summary:end -->",
    re.DOTALL,
)
SUMMARY_PLACEHOLDER = "_No summary yet. It is added at the end of the day._"


def room_log_path() -> Path:
    raw = os.environ.get(ROOM_LOG_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_ROOM_LOG


def notes_dir() -> Path:
    raw = os.environ.get(NOTES_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_NOTES_DIR


def note_path(day: date) -> Path:
    return notes_dir() / f"{day.isoformat()}.md"


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, LOCAL_TZ)


def local_date(ts: float) -> date:
    return local_dt(ts).date()


def _hhmm(ts: float) -> str:
    return local_dt(ts).strftime("%H:%M")


# ----------------------------------------------------------------------
# Reading the room log
# ----------------------------------------------------------------------

def read_room_log(path: Path | None = None) -> list[dict]:
    """Every well-formed line, in time order. A malformed line is skipped: one
    bad write must not cost the whole day its note. Lines written before the
    `discussion` and `part` fields existed read as normal messages."""
    path = path or room_log_path()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    lines: list[dict] = []
    for row in raw.splitlines():
        try:
            data = json.loads(row)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        ts, speaker, text = data.get("ts"), data.get("speaker"), data.get("text")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not 0 < ts < 1e11:
            continue  # also NaN and infinity, which would break the date math
        if not isinstance(speaker, str) or not isinstance(text, str):
            continue
        discussion = data.get("discussion")
        lines.append({
            "ts": float(ts),
            "speaker": speaker,
            "text": text,
            "discussion": str(discussion) if discussion not in (None, "") else None,
            "part": data.get("part") if isinstance(data.get("part"), str) else "message",
        })
    lines.sort(key=lambda line: line["ts"])
    return lines


def lines_for_day(day: date, lines: list[dict]) -> list[dict]:
    """The lines whose note is `day`'s: a normal line by its own local date, a
    discussion line by the date its discussion started, so a /discuss that runs
    past midnight stays in one piece in the note it began in."""
    started: dict[str, float] = {}
    for line in lines:
        disc = line["discussion"]
        if disc is not None and disc not in started:
            started[disc] = line["ts"]
    return [
        line for line in lines
        if local_date(started[line["discussion"]] if line["discussion"] else line["ts"]) == day
    ]


# ----------------------------------------------------------------------
# Text hygiene
# ----------------------------------------------------------------------

_EM_DASH_BOL = re.compile(r"^([ \t]*)\u2014+[ \t]*", re.MULTILINE)
_EM_DASH_EOL = re.compile(r"[ \t]*\u2014+[ \t]*$", re.MULTILINE)
_EM_DASH_MID = re.compile(r"[ \t]*\u2014+[ \t]*")


def scrub(text: str) -> str:
    """Make room text safe to place in the note.

    No em-dashes (the vault's rule): one that opens a line is dropped, one that
    ends a line becomes a colon, any other becomes a comma. No HTML comment
    delimiters either: an unclosed `<!--` would hide the rest of the note in
    reading view, and a copied summary marker would confuse the summary block.
    They are written as entities, which still read as `<!--` and `-->`.
    """
    text = text.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    text = _EM_DASH_BOL.sub(r"\1", text)
    text = _EM_DASH_EOL.sub(":", text)
    text = _EM_DASH_MID.sub(", ", text)
    return text.replace("\u2014", ",")


_HEADING_LINE_RE = re.compile(r"^([ \t]{0,3})(#{1,6})(?=[ \t]|$)", re.MULTILINE)


def _plain_block(text: str) -> str:
    """Room text placed outside a callout: a line that would read as a Markdown
    heading has its `#` escaped, so a reply cannot restructure the note."""
    return _HEADING_LINE_RE.sub(r"\1\\\2", scrub(text).strip())


def _heading_snippet(question: str) -> str:
    flat = " ".join(scrub(question).split())
    if not flat:
        return "(question not recorded)"
    if len(flat) <= HEADING_QUESTION_CHARS:
        return flat
    cut = flat[:HEADING_QUESTION_CHARS]
    space = cut.rfind(" ")
    if space >= HEADING_QUESTION_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + "..."


_NEXT_STEPS_TITLE_RE = re.compile(r"\A\s*[#*_ ]*next steps[*_:. ]*\n", re.IGNORECASE)


def _strip_next_steps_title(text: str) -> str:
    """Luna opens her post with a 'Next steps' line; the note's heading already
    says it."""
    return _NEXT_STEPS_TITLE_RE.sub("", text, count=1)


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

def _callout(title: str, lines: list[dict]) -> str:
    """A folded callout of room lines, one paragraph per message:
    `> **Speaker** (HH:MM): text`, continuation lines kept inside it."""
    out = [f"> [!note]- {title}"]
    for i, line in enumerate(lines):
        if i:
            out.append(">")
        body = scrub(line["text"]).strip().splitlines() or [""]
        out.append(f"> **{line['speaker']}** ({_hhmm(line['ts'])}): {body[0]}".rstrip())
        out.extend(f"> {row}".rstrip() for row in body[1:])
    return "\n".join(out)


def _participants(lines: list[dict]) -> list[str]:
    present = []
    for line in lines:
        name = line["speaker"]
        if name not in present and re.fullmatch(r"[A-Za-z][A-Za-z0-9 _.-]{0,40}", name):
            present.append(name)
    known = [name for name in SPEAKER_ORDER if name in present]
    return known + [name for name in present if name not in SPEAKER_ORDER]


def _discussions(lines: list[dict]) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    for line in lines:
        if line["discussion"] is not None:
            groups.setdefault(line["discussion"], []).append(line)
    return sorted(groups.values(), key=lambda group: group[0]["ts"])


def _conversations(lines: list[dict]) -> list[list[dict]]:
    """Normal exchanges, split wherever the room was quiet for over 3 hours."""
    groups: list[list[dict]] = []
    for line in lines:
        if line["discussion"] is not None:
            continue
        if groups and line["ts"] - groups[-1][-1]["ts"] <= CONVERSATION_GAP_SECONDS:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def _discussion_section(group: list[dict]) -> str:
    question = next((line for line in group if line["part"] == "question"), None)
    summary = [line for line in group if line["part"] == "summary"]
    steps = [line for line in group if line["part"] == "next_steps"]
    debate = [line for line in group if line["part"] not in ("summary", "next_steps")]
    opened = question["ts"] if question else group[0]["ts"]

    parts = [f"## {_hhmm(opened)} Discussion: {_heading_snippet(question['text'] if question else '')}"]
    parts.append("### Where they landed")
    if summary:
        parts.append("\n\n".join(_plain_block(line["text"]) for line in summary))
    else:
        parts.append("_No closing summary was posted._")
    if steps:
        parts.append("### Next steps")
        parts.append("\n\n".join(_plain_block(_strip_next_steps_title(line["text"])) for line in steps))
    if debate:
        parts.append(_callout("Transcript", debate))
    return "\n\n".join(parts)


def _placeholder_block() -> str:
    return f"{SUMMARY_START} -->\n{SUMMARY_PLACEHOLDER}\n{SUMMARY_END}"


def summary_block(bullets: list[str], last_ts: float) -> str:
    """The summary block, stamped with the newest room line it covers. repr()
    round-trips a float exactly, so the stamp compares equal to that line."""
    return f"{SUMMARY_START} last_ts={last_ts!r} -->\n" + "\n".join(bullets) + f"\n{SUMMARY_END}"


def existing_summary_block(note_text: str) -> str | None:
    match = _SUMMARY_BLOCK_RE.search(note_text or "")
    return match.group(0) if match else None


def summarized_through(note_text: str) -> float | None:
    """The last_ts stamp of the note's summary block, or None when the block
    is missing or has not been filled."""
    match = _SUMMARY_BLOCK_RE.search(note_text or "")
    if not match or not match.group("ts"):
        return None
    try:
        return float(match.group("ts"))
    except ValueError:
        return None


def render_note(day: date, lines: list[dict], block: str | None = None) -> str:
    """The whole note for `day` from that day's lines. Pure: no I/O."""
    discussions = _discussions(lines)
    conversations = _conversations(lines)
    front = ["---", "type: council", f"date: {day.isoformat()}", "participants:"]
    front += [f"  - {name}" for name in _participants(lines)]
    front += [f"discussions: {len(discussions)}", "tags:", "  - council", "---"]

    sections = [
        "\n".join(front),
        "<!-- Rebuilt from the council room log after every exchange. "
        "Edits outside the summary block are replaced. -->",
        f"# Council, {day.isoformat()}",
        "## Summary",
        block or _placeholder_block(),
    ]
    sections += [_discussion_section(group) for group in discussions]
    if conversations:
        sections.append("## Other exchanges")
        sections += [
            _callout(f"{_hhmm(group[0]['ts'])} to {_hhmm(group[-1]['ts'])}", group)
            for group in conversations
        ]
    return "\n\n".join(sections) + "\n"


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------

@contextlib.contextmanager
def _notes_lock():
    """One writer at a time across the bridge and the nightly job, so a render
    can never read the note just before the summary lands and then overwrite
    it. The lock file sits beside the room log, outside the vault."""
    log_path = room_log_path()
    lock_path = log_path.with_name(log_path.name + ".notes.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_note(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _write_atomic(path: Path, text: str) -> None:
    """Temp file in the same folder, then rename: Obsidian never sees half a
    note. The temp name starts with a dot, which Obsidian ignores."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def render_day(day: date, *, block: str | None = None) -> Path | None:
    """Rebuild `day`'s note from the room log. Returns its path, or None when
    the day had no council activity (no note is created then).

    `block` replaces the summary block (the nightly job passes it); otherwise
    the note's existing block is kept byte for byte. A note whose text would
    not change is not rewritten."""
    with _notes_lock():
        lines = lines_for_day(day, read_room_log())
        if not lines:
            return None
        path = note_path(day)
        current = _read_note(path)
        text = render_note(day, lines, block or existing_summary_block(current))
        if text != current:
            _write_atomic(path, text)
        return path


# ----------------------------------------------------------------------
# Nightly summary
# ----------------------------------------------------------------------

SUMMARY_INSTRUCTION = (
    "You write the end-of-day summary of a private advisory chat. Andy talks with three AI "
    "advisors: Yor (his thinking partner), Loid (his career strategist) and Luna (his executive "
    "assistant). The day's transcript is below, one line per message, oldest first.\n\n"
    "Write 3 to 6 bullets covering, in this order: decisions made, open questions, and next steps "
    "with an owner. Start each bullet with '- ' and then 'Decided:', 'Open:' or "
    "'Next (<owner>):'. Plain English, one line per bullet. Use only what the transcript says: no "
    "new facts, names, dates or numbers, and no advice of your own. If nothing was decided or left "
    "open, say that in a single bullet. Never use em-dashes; use commas, colons or parentheses. "
    "Return only the bullets, nothing else."
)

_PART_LABELS = {
    "question": "opens a /discuss",
    "turn": "discussion turn",
    "summary": "closing summary",
    "next_steps": "next steps",
}


def build_summary_prompt(day: date, lines: list[dict]) -> str:
    rows = []
    for line in lines:
        label = _PART_LABELS.get(line["part"])
        who = f"{line['speaker']} ({label})" if label else line["speaker"]
        rows.append(f"{_hhmm(line['ts'])} {who}: {line['text'].strip()}")
    transcript = "\n".join(rows)
    if len(transcript) > SUMMARY_MAX_TRANSCRIPT_CHARS:
        transcript = "[earlier lines omitted]\n" + transcript[-SUMMARY_MAX_TRANSCRIPT_CHARS:]
    return (
        f"{SUMMARY_INSTRUCTION}\n\nDATE: {day.isoformat()}\n\n"
        f"{wrap_untrusted(transcript, 'council-day')}"
    )


_BULLET_RE = re.compile(r"^\s*(?:[-*+\u2022]|\d{1,2}[.)])\s+(?P<body>\S.*?)\s*$")


def parse_bullets(text: str) -> list[str]:
    """The model's bullets as `- ...` lines, scrubbed, at most six. Prose that
    is not a bullet is dropped."""
    bullets = []
    for row in (text or "").splitlines():
        match = _BULLET_RE.match(row)
        if match:
            body = " ".join(scrub(match.group("body")).split())
            if body:
                bullets.append(f"- {body}")
    return bullets[:SUMMARY_MAX_BULLETS]


def summarize_day(day: date, *, dry_run: bool = False) -> bool:
    """Summarize `day` if it needs it. True on success or nothing to do.

    Nothing to do: no council lines that day (no model call), or a summary
    stamped at or after the day's newest line. New lines since the stamp mean
    the day is summarized again, whole."""
    lines = lines_for_day(day, read_room_log())
    if not lines:
        print(f"{day}: no council activity, nothing to summarize")
        return True
    newest = max(line["ts"] for line in lines)
    done = summarized_through(_read_note(note_path(day)))
    if done is not None and done >= newest:
        print(f"{day}: summary is current")
        return True

    ok, out, err = call_claude_sync(
        build_summary_prompt(day, lines),
        model=SUMMARY_MODEL,
        timeout=SUMMARY_TIMEOUT,
        effort="low",
        no_tools=True,
        flush=False,
        job="council_summary",
    )
    if not ok:
        print(f"{day}: summary call failed: {err[:200]}", file=sys.stderr)
        return False
    bullets = parse_bullets(out)
    if not bullets:
        print(f"{day}: the model returned no bullets; note left as it was", file=sys.stderr)
        return False
    if dry_run:
        print(f"{day}: dry run, {len(bullets)} bullets for {note_path(day)} (not written):")
        print("\n".join(bullets))
        return True
    render_day(day, block=summary_block(bullets, newest))
    print(f"{day}: wrote {len(bullets)} summary bullets over {len(lines)} lines")
    return True


def default_summary_dates(now: datetime) -> list[date]:
    """Yesterday (late lines after last night's run land there), and today
    once it is late enough that the day is over in practice."""
    today = now.date()
    dates = [today - timedelta(days=1)]
    if now.hour >= SUMMARIZE_TODAY_AFTER_HOUR:
        dates.append(today)
    return dates


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {raw!r}")


def main(argv: list[str] | None = None) -> int:
    # Config lives in ~/.hermes/.env so a hand run behaves like the scheduled
    # one. Anything already in the environment wins.
    load_default_env()

    parser = argparse.ArgumentParser(description="Council meeting notes in the vault")
    sub = parser.add_subparsers(dest="command", required=True)
    render_p = sub.add_parser("render", help="rebuild a day's note from the room log")
    render_p.add_argument("--date", type=_parse_date, help="YYYY-MM-DD (default: today)")
    summarize_p = sub.add_parser("summarize", help="add the end-of-day summary")
    summarize_p.add_argument("--date", type=_parse_date,
                             help="YYYY-MM-DD (default: yesterday, plus today after 22:00)")
    summarize_p.add_argument("--dry-run", action="store_true",
                             help="call the model and print the bullets; write nothing")
    args = parser.parse_args(argv)

    now = datetime.now(LOCAL_TZ)
    if args.command == "render":
        day = args.date or now.date()
        path = render_day(day)
        print(f"{day}: {'rendered ' + str(path) if path else 'no council activity, no note'}")
        return 0

    days = [args.date] if args.date else default_summary_dates(now)
    results = [summarize_day(day, dry_run=args.dry_run) for day in days]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
