"""Persistent issue↔thread mapping for the Discord bot daemon.

Stores `{issue_number: thread_id, thread_id: issue_number}` so we can
look up the bound Discord thread for a GitHub issue (and vice versa)
across daemon restarts.

JSON sidecar at `~/Library/Application Support/neural-bridge/issue_threads.json`,
written atomically via `os.replace()` from a NamedTemporaryFile so a crash
mid-write never leaves a half-written file.

Pure stdlib. Threadsafe enough for our access pattern (single asyncio loop
calls bind/get serially), no locking.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path.home() / "Library" / "Application Support" / "neural-bridge" / "issue_threads.json"


class ThreadMap:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self._issue_to_thread: dict[str, str] = {}
        self._thread_to_issue: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Corrupt file: rename it for forensics, start fresh
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            self.path.rename(backup)
            return
        i2t = raw.get("issue_to_thread", {})
        t2i = raw.get("thread_to_issue", {})
        if isinstance(i2t, dict):
            self._issue_to_thread = {str(k): str(v) for k, v in i2t.items()}
        if isinstance(t2i, dict):
            self._thread_to_issue = {str(k): str(v) for k, v in t2i.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "issue_to_thread": self._issue_to_thread,
            "thread_to_issue": self._thread_to_issue,
        }
        # Write to a temp file in the same directory, then atomic replace.
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, self.path)

    def bind(self, issue_number: int, thread_id: str) -> None:
        """Bind an issue to a thread (both directions). Persists immediately."""
        issue_key = str(issue_number)
        thread_key = str(thread_id)
        # Drop any prior bindings on either side
        if issue_key in self._issue_to_thread:
            old = self._issue_to_thread.pop(issue_key)
            self._thread_to_issue.pop(old, None)
        if thread_key in self._thread_to_issue:
            old = self._thread_to_issue.pop(thread_key)
            self._issue_to_thread.pop(old, None)
        self._issue_to_thread[issue_key] = thread_key
        self._thread_to_issue[thread_key] = issue_key
        self._save()

    def get_thread(self, issue_number: int) -> Optional[str]:
        return self._issue_to_thread.get(str(issue_number))

    def get_issue(self, thread_id: str) -> Optional[int]:
        v = self._thread_to_issue.get(str(thread_id))
        return int(v) if v is not None else None

    def unbind_issue(self, issue_number: int) -> None:
        issue_key = str(issue_number)
        thread = self._issue_to_thread.pop(issue_key, None)
        if thread is not None:
            self._thread_to_issue.pop(thread, None)
            self._save()

    def __len__(self) -> int:
        return len(self._issue_to_thread)
