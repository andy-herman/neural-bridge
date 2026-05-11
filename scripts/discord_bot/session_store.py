"""Per (channel × agent) session-ID store for claude -p --resume.

Solves the "Luna can't remember earlier in this thread" problem. Each
Discord mention spawns a fresh `claude -p` subprocess, so without help
the only context that carries forward between turns is whatever the
daemon explicitly re-injects (Luna's notes, Echo's voice, the last N
Discord messages). File contents Luna Read in turn 3 are gone by turn 4.

Fix: assign each (channel_id, agent_id) pair a persistent UUID. First
call uses `claude -p --session-id <uuid>` to create the session; every
subsequent call uses `claude -p --resume <uuid>` to continue from the
prior turn's full state. The resumed session has all earlier tool
calls, reads, and reasoning in scope.

Storage: a single JSON file at
`~/Library/Application Support/neural-bridge/sessions.json`. Writes go
through a tempfile + atomic rename so concurrent mentions don't shred
the file. The store survives daemon restarts; an outright wipe just
means every channel + agent gets a fresh session on its next turn.

TTL: sessions older than 7 days by `last_used_at` are pruned. Long
threads accumulate context (and token cost); after a week of silence
it's cleaner to start fresh than to keep dragging the old transcript.

Failure mode: if `--resume <uuid>` fails because the underlying session
file got deleted by Claude Code's own cleanup, the daemon calls
`reset_session()` and retries with a fresh `--session-id`. Caller
handles the retry; this module just provides the primitives.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterator

_logger = logging.getLogger("nb_discord.session_store")


SESSION_DIR = Path.home() / "Library" / "Application Support" / "neural-bridge"
SESSION_FILE = SESSION_DIR / "sessions.json"

# Sessions inactive for this long get pruned on next access. A week balances
# "still useful for thread continuity" against "stale context burns tokens."
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


@dataclass
class SessionRecord:
    """One persisted session — a UUID plus housekeeping timestamps."""
    session_id: str
    created_at: float
    last_used_at: float
    turn_count: int = 0

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.last_used_at >= SESSION_TTL_SECONDS


def _key(channel_id: int | str, agent_id: str) -> str:
    """Stable composite key. Stringify channel_id to survive a JSON round-trip
    where int keys would have been silently coerced to strings anyway."""
    return f"{channel_id}:{agent_id}"


class SessionStore:
    """In-memory map + atomic JSON persistence.

    Reads happen lazily on first access. Writes go through a tempfile +
    atomic rename. The class isn't designed for cross-process
    concurrency (the daemon is single-process; asyncio + run_in_executor
    is the concurrency model). If a second daemon were ever spawned
    against the same file, the last-writer-wins behavior could lose
    updates — but multi-daemon was never the design.
    """

    def __init__(self, path: Path = SESSION_FILE) -> None:
        self._path = path
        self._records: dict[str, SessionRecord] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("session_store load failed (starting fresh): %s", exc)
            return
        if not isinstance(raw, dict):
            _logger.warning("session_store malformed (not a dict); starting fresh")
            return
        for k, v in raw.items():
            try:
                self._records[k] = SessionRecord(
                    session_id=v["session_id"],
                    created_at=float(v["created_at"]),
                    last_used_at=float(v["last_used_at"]),
                    turn_count=int(v.get("turn_count", 0)),
                )
            except (KeyError, TypeError, ValueError):
                _logger.warning("session_store record malformed; skipping: %r", k)

    def _persist(self) -> None:
        """Write the current map to disk via tempfile + atomic rename."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(rec) for k, rec in self._records.items()}
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False,
            dir=self._path.parent, prefix=".sessions.", suffix=".tmp",
        )
        try:
            json.dump(payload, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self._path)
        except Exception:
            # If anything failed mid-write, clean up the temp file.
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    def get(self, channel_id: int | str, agent_id: str) -> SessionRecord | None:
        """Return the existing record for this (channel, agent), pruning if
        expired. Returns None when there's nothing usable on file."""
        self._ensure_loaded()
        key = _key(channel_id, agent_id)
        rec = self._records.get(key)
        if rec is None:
            return None
        if rec.is_expired():
            self._records.pop(key, None)
            self._persist()
            return None
        return rec

    def get_or_create(self, channel_id: int | str, agent_id: str) -> tuple[SessionRecord, bool]:
        """Return (record, is_new). On `is_new=True` the caller should pass
        the session_id via `--session-id`. On `is_new=False` the caller
        passes it via `--resume`."""
        rec = self.get(channel_id, agent_id)
        if rec is not None:
            return rec, False
        rec = SessionRecord(
            session_id=str(uuid.uuid4()),
            created_at=time.time(),
            last_used_at=time.time(),
            turn_count=0,
        )
        self._records[_key(channel_id, agent_id)] = rec
        self._persist()
        return rec, True

    def touch(self, channel_id: int | str, agent_id: str) -> None:
        """Mark the session as used. Caller invokes after a successful claude
        call so the TTL clock restarts."""
        self._ensure_loaded()
        key = _key(channel_id, agent_id)
        rec = self._records.get(key)
        if rec is None:
            return
        rec.last_used_at = time.time()
        rec.turn_count += 1
        self._persist()

    def reset(self, channel_id: int | str, agent_id: str) -> SessionRecord:
        """Drop the existing record (if any) and create a fresh one with a
        new UUID. Caller invokes after `--resume` fails because the underlying
        Claude Code session file was deleted."""
        self._ensure_loaded()
        key = _key(channel_id, agent_id)
        self._records.pop(key, None)
        rec = SessionRecord(
            session_id=str(uuid.uuid4()),
            created_at=time.time(),
            last_used_at=time.time(),
            turn_count=0,
        )
        self._records[key] = rec
        self._persist()
        return rec

    def prune_expired(self) -> int:
        """Remove all expired records. Returns count removed. Caller can
        invoke periodically; not strictly required since `get` prunes on
        access."""
        self._ensure_loaded()
        now = time.time()
        expired = [k for k, r in self._records.items() if r.is_expired(now)]
        for k in expired:
            del self._records[k]
        if expired:
            self._persist()
        return len(expired)

    def all_records(self) -> Iterator[tuple[str, SessionRecord]]:
        """Yield (key, record) pairs. Used by diagnostics / health checks."""
        self._ensure_loaded()
        for k, r in self._records.items():
            yield k, r


# Module-level singleton — the daemon process gets one store.
STORE = SessionStore()
