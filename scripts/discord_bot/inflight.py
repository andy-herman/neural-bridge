"""In-flight mention tracking — restart recovery for interrupted work.

A bot restart mid-mention kills the agent's claude call with no trace: the
requester never gets a reply and nothing retries (a Luna→automation-engineer
handoff was silently eaten this way on 2026-08-02). This module keeps a tiny
state file of mentions currently being processed. handle_mention registers on
entry and clears on exit; whatever is left in the file at next startup was
interrupted, and each agent posts a short notice to the affected channel so
the requester knows to re-send.

State file lives next to the other runtime state (scripts/.compile_state.json
precedent) and is gitignored.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / ".inflight_mentions.json"


def _load() -> dict[str, dict]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(state: dict[str, dict]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=0))
    except OSError:
        pass  # tracking is best-effort; never break message handling


def register(agent_id: str, channel_id: int, author_id: str) -> str:
    """Record a mention as in-flight. Returns a token for clear()."""
    token = uuid.uuid4().hex[:12]
    state = _load()
    state[token] = {
        "agent_id": agent_id,
        "channel_id": int(channel_id),
        "author_id": str(author_id),
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save(state)
    return token


def clear(token: str) -> None:
    """Remove a completed mention from the in-flight file."""
    state = _load()
    if token in state:
        del state[token]
        _save(state)


def drain() -> list[dict]:
    """Return all leftover (interrupted) entries and empty the file.

    Call once at process startup, before agents connect. Entries returned
    here were registered by a previous process that never cleared them —
    i.e. mentions killed by a restart or crash.
    """
    state = _load()
    if state:
        _save({})
    return list(state.values())
