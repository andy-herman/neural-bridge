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

Three distinct degraded shapes, named separately because the repair differs:

  SILENT    the store logged nothing at all in the window. Either the code path
            is not running, or it is running and not instrumented. This is the
            shape that hid for ten weeks.
  FAILING   the store logged events but none succeeded. The path runs and is
            reachable; the store itself is broken.
  DEGRADED  the store succeeds sometimes, below the success-rate floor. The
            first version of this canary missed this entirely: it only checked
            ok == 0, so a layer failing 60% of its writes reported healthy
            because one write landed. Partial degradation is the commoner real
            shape and it is what precedes total failure.

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
# lessons_digest was removed 2026-08-16. It resolved for 1 request in 7 over
# its instrumented life and is retired; see docs/MEMORY_CONSOLIDATION.md.
WATCHED: dict[str, dict] = {
    "luna_notes":       {"stage": mem.RETRIEVE, "traffic_gated": True},
    "luna_live_state":  {"stage": mem.RETRIEVE, "traffic_gated": True},
    "echo_voice":       {"stage": mem.RETRIEVE, "traffic_gated": True},
    "honcho_peer_card": {"stage": mem.RETRIEVE, "traffic_gated": True},
    "honcho_capture":   {"stage": mem.WRITE,    "traffic_gated": True},
    # Layer 4 (the wiki) was uninstrumented until 2026-09-22, which is how a
    # 135-day gap in concept promotion went unnoticed. wiki_recall is the read
    # side (one UTILIZE per agent turn); flush_daily_log is the write side per
    # session; compile_concepts is one event per live nightly run and is NOT
    # traffic gated, because launchd runs it whether or not anyone talked to
    # an agent: silence there means the job is not running.
    "wiki_recall":      {"stage": mem.UTILIZE,  "traffic_gated": True},
    "flush_daily_log":  {"stage": mem.WRITE,    "traffic_gated": True},
    "compile_concepts": {"stage": mem.WRITE,    "traffic_gated": False},
    # The progress log (MEMORY_CONSOLIDATION Step 1): read on every mention,
    # written by flush at session close. A missing file records ok/chars=0, so
    # this row measures whether the path runs, not whether every agent has one.
    "progress_log":     {"stage": mem.RETRIEVE, "traffic_gated": True},
}

DEFAULT_WINDOW_DAYS = 7

HEALTHY = "healthy"
SILENT = "SILENT"
FAILING = "FAILING"
DEGRADED = "DEGRADED"
IDLE = "idle"

# A store that succeeds sometimes is not healthy. The first version of this
# canary only flagged total failure (ok == 0), which meant a layer failing 60%
# of its writes reported "ok" because one write landed. Partial degradation is
# the more common real shape, so anything below this success rate is called out.
MIN_SUCCESS_RATE = 0.75
# Below this many events the rate is noise, so only total failure is reported.
RATE_MIN_SAMPLES = 4


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
        total = ok + failed
        if ok == 0:
            out[store] = {
                "status": FAILING, "ok": ok, "failed": failed,
                "reason": f"{failed} attempt(s), none succeeded: {row.get('last_detail') or 'no detail'}",
            }
        elif total >= RATE_MIN_SAMPLES and (ok / total) < MIN_SUCCESS_RATE:
            rate = ok / total
            out[store] = {
                "status": DEGRADED, "ok": ok, "failed": failed,
                "reason": (f"{ok}/{total} succeeded ({rate:.0%}, below "
                           f"{MIN_SUCCESS_RATE:.0%}): {row.get('last_detail') or 'no detail'}"),
            }
        else:
            out[store] = {"status": HEALTHY, "ok": ok, "failed": failed,
                          "reason": f"{ok} ok / {failed} failed"}
    return out


def had_agent_traffic(summary: dict[str, dict],
                      watched: dict[str, dict] | None = None) -> bool:
    """Did any agent-driven store record anything this window? If not, the
    fleet was idle rather than broken, and the distinction is what keeps this
    alertable.

    Stores that run on a schedule (traffic_gated False, e.g. the nightly
    compile) are excluded: a compile that ran at 03:00 in a week when nobody
    talked to an agent is not traffic, and counting it would turn every idle
    store into a SILENT alarm on quiet weeks.
    """
    watched = watched if watched is not None else WATCHED
    return any(
        row.get("total", 0) > 0
        for store, row in summary.items()
        if watched.get(store, {}).get("traffic_gated", True)
    )


def format_report(results: dict[str, dict], window_days: int,
                  events: list[dict] | None = None) -> str:
    lines = [f"Memory canary, {window_days}-day window:"]
    for store, row in sorted(results.items()):
        marker = {HEALTHY: "ok  ", IDLE: "idle", SILENT: "SILENT",
                  FAILING: "FAIL", DEGRADED: "DEGR"}[row["status"]]
        lines.append(f"  [{marker}] {store}: {row['reason']}")
    if events is not None:
        # Informational, never affects the exit code: whether the wiki is
        # alive is health; whether it is useful is this line.
        try:
            hooks_dir = str(REPO_ROOT / "hooks")
            if hooks_dir not in sys.path:
                sys.path.insert(0, hooks_dir)
            import wiki_recall
            lines.append(wiki_recall.format_grounding(
                wiki_recall.grounding_summary(events), window_days))
        except Exception as exc:
            lines.append(f"Wiki grounding: unavailable ({type(exc).__name__})")
    return "\n".join(lines)


# ---------- consolidation decision gates ----------
#
# docs/MEMORY_CONSOLIDATION.md, Step 0: each retire/keep call names the
# telemetry query that decides it. Those queries lived in prose only, so the
# gates stayed unanswered for six weeks. This makes each one a single command:
#   python -m scripts.memory_canary --gates --days 30

G3_KEEP_RATE = 0.90   # peer card must be non-empty at least this often to stay injected


def gates(events: list[dict]) -> dict[str, dict]:
    """Answer gates G2, G3, G4 from raw events. Pure, so it is testable."""
    def _sel(store: str, stage: str) -> list[dict]:
        return [e for e in events if e.get("store") == store and e.get("stage") == stage]

    # G2: echo_voice retrieves by agent. Keep for 3 agents or 1?
    # Reads logged before 2026-09-25 carry no agent. They are shown but never
    # counted as a vote: 37 of them once read as "keep for all three".
    echo = _sel("echo_voice", mem.RETRIEVE)
    by_agent: dict[str, int] = {}
    for e in echo:
        by_agent[e.get("agent_id") or "?"] = by_agent.get(e.get("agent_id") or "?", 0) + 1
    attributed = {a for a in by_agent if a != "?"}
    g2 = {"question": "keep echo_voice injected for content/social, or luna only?",
          "by_agent": by_agent,
          "answer": ("no data" if not echo else
                     "undecided: no read carries an agent yet" if not attributed else
                     "luna only" if attributed <= {"luna"} else "keep for all three")}

    # G3: honcho_peer_card non-empty rate. Keep as an injected layer?
    card = _sel("honcho_peer_card", mem.RETRIEVE)
    nonempty = sum(1 for e in card if e.get("ok") and int(e.get("chars", 0)) > 0)
    rate = (nonempty / len(card)) if card else 0.0
    g3 = {"question": f"keep honcho peer card injected (needs non-empty >= {G3_KEEP_RATE:.0%})?",
          "retrieves": len(card), "nonempty": nonempty, "rate": rate,
          "answer": ("no data" if not card else
                     "keep injected" if rate >= G3_KEEP_RATE else "demote to retrieval-only")}

    # G4: is the repo wiki alive? Reads since the read loop shipped, and the
    # last live compile.
    reads = _sel("wiki_recall", mem.UTILIZE)
    grounded = sum(1 for e in reads if e.get("ok") and int(e.get("chars", 0)) > 0)
    compiles = _sel("compile_concepts", mem.WRITE)
    last_compile = max((int(e.get("epoch", 0)) for e in compiles), default=0)
    # "Agents ran under the code that reads the wiki": mention.py logs a
    # progress_log retrieve on every turn, right beside the wiki read, and both
    # shipped together. Older traffic (honcho, luna_notes) predates the read
    # path, so counting it would call the wiki dead for turns that could never
    # have read it.
    turns = _sel("progress_log", mem.RETRIEVE)
    g4 = {"question": "is knowledge/concepts read at all, and does compile still run?",
          "reads": len(reads), "grounded": grounded, "compile_runs": len(compiles),
          "last_compile_epoch": last_compile,
          "answer": (("dead: agents ran but never read the wiki, and compile never ran" if turns else
                       "undecided: no agent turns and no compile runs in the window")
                      if not reads and not compiles else
                     "alive: read but compile not running" if reads and not compiles else
                     "alive: compiling but never read" if compiles and not reads else
                     "alive")}

    # Progress log adoption: how many agents have a log that was actually read.
    prog = [e for e in _sel("progress_log", mem.RETRIEVE) if e.get("ok") and int(e.get("chars", 0)) > 0]
    agents_with_log = sorted({e.get("agent_id") or "?" for e in prog})
    p1 = {"question": "which agents have a progress.md that is being injected?",
          "agents": agents_with_log,
          "answer": ", ".join(agents_with_log) if agents_with_log else "none yet"}

    return {"G2": g2, "G3": g3, "G4": g4, "progress_log": p1}


def format_gates(results: dict[str, dict], window_days: int) -> str:
    lines = [f"Consolidation gates, {window_days}-day window:"]
    for key, row in results.items():
        lines.append(f"  {key}: {row['question']}")
        facts = {k: v for k, v in row.items() if k not in ("question", "answer")}
        if facts:
            lines.append("      " + ", ".join(f"{k}={v}" for k, v in facts.items()))
        lines.append(f"      -> {row['answer']}")
    return "\n".join(lines)


def degraded(results: dict[str, dict]) -> list[str]:
    return [s for s, r in results.items() if r["status"] in (SILENT, FAILING, DEGRADED)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Memory stack read-back canary")
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help=f"lookback window (default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--quiet", action="store_true", help="only print when degraded")
    parser.add_argument("--no-notify", action="store_true", help="skip the Discord notice")
    parser.add_argument("--gates", action="store_true",
                        help="answer the MEMORY_CONSOLIDATION decision gates instead of the health check")
    args = parser.parse_args(argv)

    try:
        since = int(time.time()) - args.days * 86400
        events = mem.read_events(since_epoch=since)
        if args.gates:
            answers = gates(events)
            print(json.dumps(answers, indent=2) if args.json else format_gates(answers, args.days))
            return 0
        summary = mem.summarize(events)
        results = evaluate(summary, had_traffic=had_agent_traffic(summary))
    except Exception as exc:  # canary infrastructure itself failed
        print(f"memory canary could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    bad = degraded(results)
    report = json.dumps(results, indent=2) if args.json else format_report(results, args.days, events)

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
