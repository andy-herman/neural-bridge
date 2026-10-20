"""The part of a session transcript a flush has not seen yet, as conversation.

Why: a resumed conversation keeps one session id, and Claude Code appends each
new turn to the same JSONL file. Until 2026-10-20 every turn's flush sent that
whole file, capped to its last 400,000 characters: raw Claude Code bookkeeping
(prompt snapshots, hook and queue records, tool output) around a few lines of
conversation, plus every agent turn's 80,000-character injected context. One
Luna turn on 2026-10-20 flushed 400K characters of a 2.3 MB session, and the
flush came back empty: the turn that mattered was buried among turns already
flushed.

So a flush now takes:

  - only the bytes after this session's last flushed offset (claimed when a
    flush starts, handed back when it fails, so two flushes of one session
    never send the same turn and a failed one is retried by the next);
  - user and assistant text, tool-call names with a short input, and the
    head of each tool result; every other record kind is dropped;
  - from an agent turn's prompt, only the `<message>` under "## Andy's
    mention": the rest is injected context, the same on every turn;
  - a short lead-in of the already-flushed conversation, marked context only.

Offsets live outside the repo, one small file per session, pruned after 30
days. Never raises into flush: a broken offset store means a full flush.
"""

from __future__ import annotations

import fcntl
import json
import re
import time
from pathlib import Path

OFFSETS_DIR = Path.home() / "Library" / "Application Support" / "neural-bridge" / "flush-offsets"
OFFSET_TTL_SECONDS = 30 * 86400
LEAD_IN_CHARS = 6_000
TOOL_INPUT_CHARS = 200
TOOL_RESULT_CHARS = 300

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")
_MENTION_MESSAGE = re.compile(r"## Andy's mention\s*<message>\n?(.*?)\n?</message>", re.DOTALL)


# ---------- condensing ----------

def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _user_text(text: str) -> str:
    """An agent turn's prompt reduced to Andy's message. Anything else as is."""
    found = list(_MENTION_MESSAGE.finditer(text))
    return found[-1].group(1).strip() if found else text.strip()


def condense_record(record: dict) -> tuple[str, str] | None:
    """(role, text) for a conversation record; None for everything else."""
    kind = record.get("type")
    if kind not in ("user", "assistant") or record.get("isMeta") or record.get("isSidechain"):
        return None
    content = (record.get("message") or {}).get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return None
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and str(block.get("text", "")).strip():
            text = str(block["text"])
            parts.append(_user_text(text) if kind == "user" else text.strip())
        elif btype == "tool_use":
            arg = json.dumps(block.get("input", {}), ensure_ascii=False)
            parts.append(f"[tool call: {block.get('name', '?')} {_short(arg, TOOL_INPUT_CHARS)}]")
        elif btype == "tool_result":
            parts.append(f"[tool result: {_short(_result_text(block.get('content')), TOOL_RESULT_CHARS)}]")
    if not parts:
        return None
    return ("USER" if kind == "user" else "ASSISTANT"), "\n".join(parts)


def condense(jsonl: str) -> list[str]:
    """Conversation turns from JSONL text, consecutive same-role records joined."""
    turns: list[tuple[str, str, list[str]]] = []
    for line in jsonl.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        got = condense_record(record)
        if got is None:
            continue
        role, text = got
        if turns and turns[-1][0] == role:
            turns[-1][2].append(text)
        else:
            turns.append((role, str(record.get("timestamp", ""))[:19], [text]))
    return [f"{role} ({ts or 'no time'}):\n" + "\n".join(texts) for role, ts, texts in turns]


def lead_in(turns: list[str], limit: int = LEAD_IN_CHARS) -> str:
    """The last turns that fit in `limit` characters, whole turns only; a
    single longer turn is cut to its end."""
    kept, used = [], 0
    for turn in reversed(turns):
        if used + len(turn) > limit:
            if not kept:
                kept.append("…" + turn[-limit:])
            break
        kept.append(turn)
        used += len(turn) + 2
    return "\n\n".join(reversed(kept))


def render(new_part: str, earlier_turns: list[str], earlier_questions: list[str] | None) -> str:
    """The text the flush prompt gets as its transcript."""
    if not earlier_turns:
        return new_part
    parts = ["=== ALREADY FLUSHED: context only. Record nothing from this part. ===",
             lead_in(earlier_turns)]
    if earlier_questions:
        parts.append("=== Questions this session left open earlier (still on Andy's review queue) ===\n"
                     "List again under open_questions any that the new part leaves open; leave out "
                     "any it settles.\n" + "\n".join(f"- {q}" for q in earlier_questions))
    parts += ["=== NEW SINCE THE LAST FLUSH: summarize this part. ===", new_part]
    return "\n\n".join(parts)


# ---------- offsets ----------

def _path(session_id: str, offsets_dir: Path | None = None) -> Path:
    return (offsets_dir or OFFSETS_DIR) / f"{_SAFE_ID.sub('_', session_id)[:80]}.json"


def _locked(path: Path, update):
    """Run update(current offset) -> new offset under an exclusive lock;
    returns (old, new)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            try:
                old = int(json.loads(f.read() or "{}").get("offset", 0))
            except (ValueError, TypeError, AttributeError):
                old = 0
            new = update(old)
            f.seek(0)
            f.truncate()
            f.write(json.dumps({"offset": new, "updated": int(time.time())}))
            f.flush()
            return old, new
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def claim(session_id: str, size: int, offsets_dir: Path | None = None) -> tuple[int, int]:
    """Claim bytes [start, size) of the transcript for this flush. A stored
    offset past the end means the file was rewritten: start over at 0."""
    try:
        start, end = _locked(_path(session_id, offsets_dir), lambda old: size)
    except OSError:
        return 0, size
    return (start if start <= size else 0), end


def release(session_id: str, start: int, end: int, offsets_dir: Path | None = None) -> None:
    """Hand a failed flush's bytes back, unless a later flush claimed past
    them already."""
    try:
        _locked(_path(session_id, offsets_dir), lambda old: start if old == end else old)
    except OSError:
        pass


def prune(offsets_dir: Path | None = None, now: float | None = None) -> None:
    root = offsets_dir or OFFSETS_DIR
    cutoff = (now if now is not None else time.time()) - OFFSET_TTL_SECONDS
    try:
        for p in root.glob("*.json"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except OSError:
        pass


def complete_lines(data: bytes) -> int:
    """Length of `data` up to and including its last newline: a record still
    being written is left for the next flush."""
    cut = data.rfind(b"\n")
    return cut + 1 if cut >= 0 else 0


def split(data: bytes, start: int, end: int) -> tuple[str, str]:
    """(already flushed text, new text) of a transcript's bytes."""
    return (data[:start].decode("utf-8", errors="replace"),
            data[start:end].decode("utf-8", errors="replace"))



# ---------- seeding, once, at deploy ----------

TRANSCRIPTS_ROOT = Path.home() / ".claude" / "projects"


def seed(root: Path | None = None, offsets_dir: Path | None = None,
         now: float | None = None) -> int:
    """Mark every recent transcript as flushed up to its current end, unless
    it already has an offset. Run once when this ships: until then each
    resumed turn flushed the whole session, so without a seed the first flush
    of an old resumed session would summarize its earlier turns again.
    Returns how many sessions were seeded."""
    cutoff = (now if now is not None else time.time()) - OFFSET_TTL_SECONDS
    seeded = 0
    for transcript in (root or TRANSCRIPTS_ROOT).glob("*/*.jsonl"):
        try:
            if transcript.stat().st_mtime < cutoff or _path(transcript.stem, offsets_dir).exists():
                continue
            size = complete_lines(transcript.read_bytes())
        except OSError:
            continue
        _locked(_path(transcript.stem, offsets_dir), lambda old, size=size: size)
        seeded += 1
    return seeded


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["seed"]:
        print("usage: python hooks/transcript_delta.py seed")
        raise SystemExit(2)
    print(f"seeded {seed()} session(s)")
