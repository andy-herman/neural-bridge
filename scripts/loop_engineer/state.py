"""Tiny on-disk daily counter so the per-day issue ceiling survives restarts.

Stored as `{"date": "YYYY-MM-DD", "count": N}` in the loop's state dir. A new
date resets the count. Kept deliberately minimal — one integer that a runaway
loop cannot exceed even across launchd restarts.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_FILENAME = "daily_count.json"


def _path(state_dir: Path) -> Path:
    return state_dir / _FILENAME


def _today() -> str:
    return date.today().isoformat()


def load_count(state_dir: Path) -> int:
    """Return today's processed count (0 if no file or a stale date)."""
    p = _path(state_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict) or data.get("date") != _today():
        return 0
    count = data.get("count")
    return count if isinstance(count, int) and count >= 0 else 0


def bump_count(state_dir: Path) -> int:
    """Increment today's count and persist. Returns the new value."""
    state_dir.mkdir(parents=True, exist_ok=True)
    current = load_count(state_dir)
    new = current + 1
    _path(state_dir).write_text(
        json.dumps({"date": _today(), "count": new}), encoding="utf-8"
    )
    return new
