"""Per-stage instrumentation for the memory stack.

Why this exists: on 2026-08-01/02 three separate memory layers were found to
have been broken for weeks while every log line read healthy. Luna's notes
injection was silently discarding the half of the file that held her standing
rules. The weekly lessons digest had never produced a single file and returned
an empty string on every turn since May. The Honcho capture path was dead from
May 27 to Aug 2 with failures swallowed at debug level.

None of those were visible from the outside, because every layer is written to
degrade to `return ""` on any problem. That is the correct local decision (a
missing file should not crash the daemon) and collectively it builds a system
that can lose most of its memory without saying a word.

The fix is not to make the layers throw. It is to make degradation *countable*.
Research on multi-store agent memory converges on the same point: the same
observed failure needs a different repair depending on whether the WRITE, the
RETRIEVE, or the UTILIZE stage broke, so an output-level smoke check cannot
diagnose it. Per-stage records are the minimum viable observability.

Design constraints:
  - Never raises. Telemetry that can break the daemon is worse than none.
  - Append-only JSONL, one line per event, so it is greppable and cheap.
  - No dependencies beyond the stdlib.
  - Records BOTH success and failure. "No failures logged" is not evidence of
    health if nothing is logged at all, which is exactly the trap the Honcho
    path fell into.

Read by scripts/memory_canary.py, which fails loudly when a layer goes quiet.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Stages. A memory system can fail at any of the three, and the repair differs.
WRITE = "write"        # capturing something into a store
RETRIEVE = "retrieve"  # reading it back out of the store
UTILIZE = "utilize"    # actually putting it into a prompt

LOG_PATH = Path.home() / "Library" / "Logs" / "neural-bridge" / "memory-telemetry.jsonl"

# Bound the file so an always-on daemon cannot fill the disk. At roughly 200
# bytes per event and a handful of events per turn, 20MB is many months.
MAX_BYTES = 20 * 1024 * 1024
ENV_DISABLE = "NB_NO_MEMORY_TELEMETRY"
# Set by the telemetry module's OWN tests, which have to exercise recording.
# Everything else running under a test runner is suppressed; see _under_test.
ENV_FORCE = "NB_MEMORY_TELEMETRY_FORCE"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))
    except OSError:
        pass


def _under_test() -> bool:
    """True when running inside a test runner.

    The honcho suite mocks failures with messages like "network blip". Without
    this guard those land in the production telemetry log, and the canary then
    reports a 60% Honcho failure rate that is entirely an artifact of running
    the tests. Telemetry that lies about the system is worse than none, so the
    recorder no-ops under test rather than trusting every test author to
    remember to disable it.
    """
    if os.environ.get(ENV_FORCE) == "1":
        return False
    return "unittest" in sys.modules or "pytest" in sys.modules


def record(
    stage: str,
    store: str,
    *,
    agent_id: str | None = None,
    ok: bool = True,
    chars: int = 0,
    detail: str = "",
) -> None:
    """Append one memory event. Never raises.

    `store` is the memory layer's stable name (e.g. "luna_notes",
    "lessons_digest", "honcho_peer_card"). `ok=False` means the layer did not
    produce what it was asked for, whether that is an error or a legitimately
    empty result: both are worth counting, because a layer that is legitimately
    empty for two months is indistinguishable from a broken one and should be
    surfaced either way. `detail` carries the reason.
    """
    if os.environ.get(ENV_DISABLE) == "1" or _under_test():
        return
    try:
        event = {
            "ts": _utc_iso(),
            "epoch": int(time.time()),
            "stage": stage,
            "store": store,
            "ok": bool(ok),
            "chars": int(chars),
        }
        if agent_id:
            event["agent_id"] = agent_id
        if detail:
            event["detail"] = str(detail)[:300]
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(LOG_PATH)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        # Telemetry must never take down a turn. This is the one place in the
        # codebase where swallowing is correct, because the alternative is that
        # an observability bug becomes an outage.
        return


def read_events(path: Path | None = None, since_epoch: int | None = None) -> list[dict]:
    """Load events, newest last. Malformed lines are skipped, not fatal."""
    target = path or LOG_PATH
    out: list[dict] = []
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if since_epoch is not None and event.get("epoch", 0) < since_epoch:
                    continue
                out.append(event)
    except OSError:
        return []
    return out


def summarize(events: list[dict]) -> dict[str, dict]:
    """Fold events into per-store health: counts, last-seen, last-ok.

    Returns {store: {"total", "ok", "failed", "last_epoch", "last_ok_epoch",
    "last_detail"}}. A store with total>0 but ok==0 is the dangerous shape: it
    is running and producing nothing, which is what a dead capture path looks
    like from the inside.
    """
    summary: dict[str, dict] = {}
    for event in events:
        store = event.get("store")
        if not store:
            continue
        row = summary.setdefault(store, {
            "total": 0, "ok": 0, "failed": 0,
            "last_epoch": 0, "last_ok_epoch": 0, "last_detail": "",
        })
        row["total"] += 1
        epoch = int(event.get("epoch", 0))
        row["last_epoch"] = max(row["last_epoch"], epoch)
        if event.get("ok"):
            row["ok"] += 1
            row["last_ok_epoch"] = max(row["last_ok_epoch"], epoch)
        else:
            row["failed"] += 1
            if event.get("detail"):
                row["last_detail"] = event["detail"]
    return summary
