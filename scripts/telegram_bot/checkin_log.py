"""What Luna's scheduled check-ins said, so her chat can see it.

The check-ins (luna_checkin.py) run as their own launchd jobs and post through
the Bot API directly. Her Telegram chat (luna_bridge.py) is a separate process
with its own history and its own claude session, so until 2026-10-20 it never
saw a check-in: Andy replied "that's done" to a morning message, and the Luna
who answered did not know what the message had asked. This file is the bridge
between the two. Local only, newest last, a few dozen lines at most.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

LOG_PATH = Path.home() / "Library" / "Application Support" / "neural-bridge" / "luna-checkins.jsonl"
KEEP = 40
# On the bridge's first message after a restart, how far back to look.
LOOKBACK_SECONDS = 36 * 3600


def record(chat_id: int, kind: str, text: str, now: float | None = None,
           path: Path | None = None) -> None:
    """Append one sent check-in. Never raises: a check-in that was delivered
    must not fail because this note could not be written."""
    target = path or LOG_PATH
    entry = {"ts": now if now is not None else time.time(), "chat_id": chat_id,
             "kind": kind, "text": text}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
        lines = [*lines, json.dumps(entry, ensure_ascii=False)][-KEEP:]
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass


def since(chat_id: int, after: float, path: Path | None = None) -> list[dict]:
    """Check-ins sent to this chat after `after`, oldest first."""
    target = path or LOG_PATH
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("chat_id") == chat_id and float(entry.get("ts", 0)) > after and entry.get("text"):
            out.append(entry)
    return sorted(out, key=lambda e: e["ts"])
