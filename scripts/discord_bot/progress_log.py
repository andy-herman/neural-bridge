"""Per-agent progress log: the append-only narrative half of the note store.

docs/MEMORY_CONSOLIDATION.md, Step 1. Target state is two human-readable
artifacts per agent in the vault, both re-read at session start:

    Agents/<id>/notes.md      curated, durable, bounded (rules, decisions)
    Agents/<id>/progress.md   append-only narrative of what happened and
                              what is open (this file)

Yor's Journal/ on the Hermes side is the working model: one short entry per
working session, written at session close, never rewritten.

Who writes it: hooks/flush.py, at SessionEnd, for every non-empty session. It
already extracts decisions, findings, and open questions from the transcript;
this module turns that into one dated entry. Agents may also append by hand.

Who reads it: scripts/discord_bot/mention.py injects the most recent entries
(tail, within a fixed budget) behind notes.md on every mention, and records a
"progress_log" RETRIEVE telemetry event per turn.

Why a missing file is NOT a failure here: lessons_digest died because exactly
one agent ever had a digest directory, so the layer reported 6/7 failures
forever and was retired. A progress.md that does not exist yet means the agent
has not had a non-empty session since this shipped, which is legitimate. The
retrieve records ok=True with chars=0 and a detail saying so; a read error is
the only ok=False. The canary therefore watches whether the path runs, and
`memory_canary --gates` reports how many agents actually have a log.

Stdlib only. Imported by hooks/flush.py, which must not need a venv.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

VAULT_ROOT = Path.home() / "Documents" / "Luna Master"
AGENTS_BASE = VAULT_ROOT / "Agents"
FILENAME = "progress.md"
STORE = "progress_log"

# Injection budget per turn. Entries are short by construction (flush writes
# bullets, not prose), so this is roughly the last five to ten sessions.
MAX_INJECT_CHARS = 3000
MAX_ENTRY_CHARS = 1500

ENTRY_HEADING_RE = re.compile(r"^## \d{4}-\d{2}-\d{2} ", re.MULTILINE)


def progress_path(agent_id: str, agents_base: Path = AGENTS_BASE) -> Path:
    # Same layout as conversation_log: Agents/<agent_id>/. The vault lives on
    # a case-insensitive filesystem, which is why Agents/Luna/notes.md and
    # Agents/luna/conversations/ have coexisted; keep the lowercase id.
    return agents_base / agent_id / FILENAME


def _utc_stamp(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%d %H:%MZ")


def render_entry(*, session_id: str, decisions: list[str], findings: list[str],
                 open_questions: list[str], source: str = "", when: datetime | None = None) -> str:
    """One dated entry. Empty sections are omitted; an entry with nothing to
    say returns "" so callers do not append noise."""
    sections = [("Decided", decisions), ("Found", findings), ("Open", open_questions)]
    body: list[str] = []
    for label, items in sections:
        items = [str(i).strip() for i in (items or []) if str(i).strip()]
        if not items:
            continue
        body.append(f"**{label}**")
        body.extend(f"- {i}" for i in items)
        body.append("")
    if not body:
        return ""
    head = f"## {_utc_stamp(when)} session {session_id[:8]}"
    if source:
        head += f" ({source})"
    text = head + "\n\n" + "\n".join(body).rstrip() + "\n"
    if len(text) > MAX_ENTRY_CHARS:
        marker = "\n- (entry truncated)\n"
        text = text[: MAX_ENTRY_CHARS - len(marker)].rstrip() + marker
    return text


def append_entry(agent_id: str, entry: str, *, agents_base: Path | None = None) -> tuple[bool, str]:
    """Append one entry. Returns (ok, reason). Never raises.

    Refuses to create the vault itself: if Agents/ is absent this machine has
    no vault (CI, a fresh clone), and inventing one under $HOME would be a
    silent fork of Andy's real notes. The agent's own directory is created.
    """
    # Resolved at call time, not definition time, so tests and callers can
    # point the module at another vault by setting AGENTS_BASE.
    agents_base = agents_base or AGENTS_BASE
    if not entry or not entry.strip():
        return False, "empty entry"
    if not agents_base.is_dir():
        return False, "vault absent"
    path = progress_path(agent_id, agents_base)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                f"# {agent_id} progress log\n\n"
                "Append-only narrative, one entry per working session, written at "
                "session close by hooks/flush.py. Newest at the bottom. Curated "
                "rules and decisions belong in notes.md, not here.\n\n",
                encoding="utf-8",
            )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry.rstrip() + "\n\n")
        return True, "appended"
    except (OSError, UnicodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def read_recent(agent_id: str, *, max_chars: int = MAX_INJECT_CHARS,
                agents_base: Path | None = None) -> tuple[str, str]:
    """Most recent entries that fit in `max_chars`, oldest first.

    Returns (text, status). status is one of "ok", "missing", "empty", or an
    error string. Whole entries only: a half entry is worse than none.
    """
    path = progress_path(agent_id, agents_base or AGENTS_BASE)
    if not path.exists():
        return "", "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return "", f"{type(exc).__name__}: {exc}"
    starts = [m.start() for m in ENTRY_HEADING_RE.finditer(text)]
    if not starts:
        return "", "empty"
    entries = [text[s:e].rstrip() for s, e in zip(starts, starts[1:] + [len(text)])]
    kept: list[str] = []
    used = 0
    for entry in reversed(entries):
        if used + len(entry) + 2 > max_chars:
            break
        kept.append(entry)
        used += len(entry) + 2
    if not kept:
        # The newest entry alone is over budget; take it truncated rather than
        # inject nothing, and say so.
        kept = [entries[-1][: max_chars - 30].rstrip() + "\n- (entry truncated)"]
    return "\n\n".join(reversed(kept)) + "\n", "ok"


def render_block(agent_id: str, text: str) -> str:
    return (
        f"## Your recent progress log (auto-injected from "
        f"~/Documents/Luna Master/Agents/{agent_id}/progress.md)\n\n"
        "One entry per past working session, written at session close from the "
        "transcript: what was decided, found, and left open. It is your own "
        "narrative memory, already in context; do not re-read the file. Durable "
        "rules belong in notes.md, not here.\n\n"
        f"<progress-log>\n{text}</progress-log>\n\n"
    )
