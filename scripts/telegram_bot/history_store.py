"""The Telegram bridges' recent chat history, kept across restarts.

Each bridge passes its last turns into the prompt as "Recent context". Until
2026-10-20 that history lived in memory only, so every deploy (auto-reload
restarts the bridges) emptied it; the resumed Claude session still held the
conversation, so it rarely showed. Long sessions are now dropped once they
pass agent_runtime.ROTATE_AT_TOKENS, and a fresh session's only view of the
recent thread is this history, so it is written to disk.

One small JSON file per bridge, local only, readable by Andy's account alone:
~/Library/Application Support/neural-bridge/telegram-history/<bridge>.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HISTORY_DIR = Path.home() / "Library" / "Application Support" / "neural-bridge" / "telegram-history"


def _path(bridge: str) -> Path:
    return HISTORY_DIR / f"{bridge}.json"


def load(bridge: str) -> dict[int, list[dict]]:
    """chat id -> turns ({"author", "content"}); empty when nothing is saved."""
    try:
        raw = json.loads(_path(bridge).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[int, list[dict]] = {}
    for chat, turns in (raw.items() if isinstance(raw, dict) else []):
        try:
            out[int(chat)] = [{"author": str(t["author"]), "content": str(t["content"])}
                              for t in turns if isinstance(t, dict)]
        except (ValueError, KeyError, TypeError):
            continue
    return out


def save(bridge: str, history: dict[int, list[dict]]) -> None:
    """Write atomically; never raises (a reply already sent must not fail)."""
    target = _path(bridge)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text(json.dumps({str(k): v for k, v in history.items()}, ensure_ascii=False),
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(target)
    except OSError:
        pass
