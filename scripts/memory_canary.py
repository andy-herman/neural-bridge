#!/usr/bin/env python3
"""Read-back canary for the memory stack. Fails loudly when a layer goes quiet.

    python -m scripts.memory_canary [--days N] [--json] [--quiet]

Exit codes: 0 healthy, 1 one or more layers degraded, 2 the canary itself could
not run. Non-zero exit is the point; launchd surfaces it and the Discord notice
puts it somewhere Andy actually looks.

WHY THIS EXISTS

Three memory layers were found broken on 2026-08-01/02, each having failed for
weeks while every log line read healthy:

  - Honcho capture: dead 2026-05-27 to 2026-08-02, failures swallowed at debug
  - Weekly lessons digest: never produced a single file, returned "" every turn
  - Luna's notes: silently discarding the half of the file holding her rules

The common shape is not "an error was thrown and missed". It is that a healthy
system and a dead one produced byte-identical output: nothing. Alert-on-error
cannot catch that, because there is no error. The only thing that distinguishes
them is whether SUCCESS is still happening, so this canary asserts on the
presence of recent successful events rather than the absence of failures.

There are two distinct degraded shapes it names separately, because they need
different repairs:

  SILENT   the store logged nothing at all in the window. Either the code path
           is not running, or it is running and not instrumented. This is the
           shape that hid for ten weeks.
  FAILING  the store logged events but none succeeded. The path runs and is
           reachable; the store itself is broken.

A store that is simply idle because Andy did not talk to any agent is NOT
degraded, and the canary says so rather than crying wolf: when there is no
agent traffic in the window, retrieve-stage stores are reported as IDLE. That
distinction matters here specifically, because this fleet genuinely does sit
dormant for weeks at a time, and a canary that fires every quiet day is a
canary that gets muted and then misses the real outage.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.discord_bot import memory_telemetry as mem  # noqa: E402

# Stores the canary knows about, with the stage that proves the layer is alive.
# `traffic_gated` means the store only produces events when an agent actually
# runs, so silence is expected during a quiet window and is reported as IDLE.
WATCHED: dict[str, dict] = {
    "luna_notes":       {"stage": mem.RETRIEVE, "traffic_gated": True},
    "lessons_digest":   {"stage": mem.RETRIEVE, "traffic_gated": True},
    "echo_voice":       {"stage": mem.RETRIEVE, "traffic_gated": True},
    "honcho_peer_card": {"stage": mem.RETRIEVE, "traffic_gated": True},
    "honcho_capture":   {"stage": mem.WRITE,    "traffic_gated": True},
}

DEFAULT_WINDOW_DAYS = 7

HEALTHY = "healthy"
SILENT = "SILENT"
FAILING = "FAILING"
IDLE = "idle"


def evaluate(summary: dict[str, dict], *, had_traffic: bool,
             watched: dict[str, dict] | None = None) -> dict[str, dict]:
    """Classify each watched store. Pure, so it is testable without a log file.

    Returns {store: {"status", "reason", "ok", "failed"}}.
    """
    watched = watched if watched is not None else WATCHED
    out: dict[str, dict] = {}
    for store, cfg in watched.items():
        row = summary.get(store)
        if not row or row.get("total", 0) == 0:
            if cfg.get("traffic_gated") and not had_traffic:
                out[store] = {"status": IDLE, "ok": 0, "failed": 0,
                              "reason": "no agent traffic in window"}
            else:
                out[store] = {"status": SILENT, "ok": 0, "failed": 0,
                              "reason": "no events logged at all in window"}
            continue
        ok = row.get("ok", 0)
        failed = row.get("failed", 0)
        if ok == 0:
            out[store] = {
                "status": FAILING, "ok": ok, "failed": failed,
                "reason": f"{failed} attempt(s), none succeeded: {row.get('last_detail') or 'no detail'}",
            }
        else:
            out[store] = {"status": HEALTHY, "ok": ok, "failed": failed,
                          "reason": f"{ok} ok / {failed} failed"}
    return out


def had_agent_traffic(summary: dict[str, dict]) -> bool:
    """Did anything at all get recorded this window? If not, the fleet was idle
    rather than broken, and the distinction is what keeps this alertable."""
    return any(row.get("total", 0) > 0 for row in summary.values())


def format_report(results: dict[str, dict], window_days: int) -> str:
    lines = [f"Memory canary, {window_days}-day window:"]
    for store, row in sorted(results.items()):
        marker = {HEALTHY: "ok  ", IDLE: "idle", SILENT: "SILENT", FAILING: "FAIL"}[row["status"]]
        lines.append(f"  [{marker}] {store}: {row['reason']}")
    return "\n".join(lines)


def degraded(results: dict[str, dict]) -> list[str]:
    return [s for s, r in results.items() if r["status"] in (SILENT, FAILING)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Memory stack read-back canary")
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help=f"lookback window (default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--quiet", action="store_true", help="only print when degraded")
    parser.add_argument("--no-notify", action="store_true", help="skip the Discord notice")
    args = parser.parse_args(argv)

    try:
        since = int(time.time()) - args.days * 86400
        events = mem.read_events(since_epoch=since)
        summary = mem.summarize(events)
        results = evaluate(summary, had_traffic=had_agent_traffic(summary))
    except Exception as exc:  # canary infrastructure itself failed
        print(f"memory canary could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    bad = degraded(results)
    report = json.dumps(results, indent=2) if args.json else format_report(results, args.days)

    if bad or not args.quiet:
        print(report)

    if bad and not args.no_notify:
        # Reuse the loop engineer's notifier so this lands in Discord where the
        # rest of the fleet's operational noise goes.
        try:
            from scripts.loop_engineer import notify
            notify.notify(
                "🧠 **memory canary** degraded: " + ", ".join(sorted(bad))
                + "\n```\n" + format_report(results, args.days)[:1500] + "\n```"
            )
        except Exception:
            pass  # a notification failure must not change the exit code

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
