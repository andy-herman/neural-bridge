#!/usr/bin/env python3
"""eval_stability.py - measure filing-gate verdict stability across repeats.

The calibration showed votes=3 is more precise than votes=1 on borderline cases.
The hypothesis is that a single pass WAVERS on hard cases while a 3-vote majority
is more STABLE. This quantifies it: each case is run `--repeats` times at votes=1
and at votes=3, and we report how often the verdict (and the promote/not-promote
decision) flips across repeats. If votes=3 flips less than votes=1, the ensemble's
value is stability, as claimed.

Usage:
  python3 scripts/eval_stability.py --repeats 3
  python3 scripts/eval_stability.py --repeats 3 --borderline-only
  python3 scripts/eval_stability.py --repeats 2 --mock     # wiring self-test, no claude

Needs the `claude` CLI unless --mock.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import compile as cmp  # noqa: E402
import eval_filing_gate as ev  # noqa: E402

BORDERLINE_PREFIX = "borderline-"


def decision(verdict: str) -> str:
    """Collapse a verdict to the binary that actually matters: admit or not."""
    if verdict == cmp.PROMOTE:
        return "PROMOTE"
    if verdict in (cmp.QUARANTINE, cmp.REJECT):
        return "NOT_PROMOTE"
    return "ERROR"


def stability(verdicts: list[str]) -> dict:
    """Stability summary for one case at one vote mode."""
    vc = Counter(verdicts)
    modal_verdict, _ = vc.most_common(1)[0]
    dc = Counter(decision(v) for v in verdicts)
    modal_decision, _ = dc.most_common(1)[0]
    return {
        "verdicts": verdicts,
        "verdict_stable": len(vc) == 1,
        "decision_stable": len(dc) == 1,
        "modal_verdict": modal_verdict,
        "modal_decision": modal_decision,
    }


def _sample(case, template, model, timeout, votes, mock_kind) -> tuple[str, str]:
    """Return (verdict, error). verdict is 'ERROR' when the gate call failed."""
    if mock_kind:
        ok, gate, err = ev._mock_gate(mock_kind)(case)
    else:
        prompt = cmp.build_filing_gate_prompt(
            template, case["slug"], case["summary"], case.get("agent", "research"), case["excerpt"])
        ok, gate, err = cmp.call_filing_gate_voted(prompt, model, timeout, votes=votes)
    return (gate["verdict"] if (ok and gate) else "ERROR"), err


def run(cases, *, repeats, template="", model=None, timeout=None, mock_kind=None):
    model = model or cmp.DEFAULT_MODEL
    timeout = timeout or cmp.DEFAULT_TIMEOUT
    rows = []
    for case in cases:
        v1 = [_sample(case, template, model, timeout, 1, mock_kind)[0] for _ in range(repeats)]
        v3 = [_sample(case, template, model, timeout, 3, mock_kind)[0] for _ in range(repeats)]
        rows.append({"case": case, "v1": stability(v1), "v3": stability(v3)})
    return rows


def summarize(rows) -> dict:
    def unstable(mode, key):
        return sum(1 for r in rows if not r[mode][key])
    return {
        "total": len(rows),
        "v1_verdict_unstable": unstable("v1", "verdict_stable"),
        "v3_verdict_unstable": unstable("v3", "verdict_stable"),
        "v1_decision_unstable": unstable("v1", "decision_stable"),
        "v3_decision_unstable": unstable("v3", "decision_stable"),
    }


def _print_rows(rows) -> None:
    for r in rows:
        flip = ""
        if not r["v1"]["verdict_stable"] or not r["v3"]["verdict_stable"]:
            flip = "  <- FLIP"
        print(f"  {r['case']['id']:<32} v1={r['v1']['verdicts']} v3={r['v3']['verdicts']}{flip}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure filing-gate verdict stability across repeats")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model", default=cmp.DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=cmp.DEFAULT_TIMEOUT)
    parser.add_argument("--borderline-only", action="store_true")
    parser.add_argument("--mock", nargs="?", const="ideal",
                        choices=["ideal", "naive", "reject_all"], default=None)
    parser.add_argument("--cases", default=str(ev.CASES_FILE))
    args = parser.parse_args()

    cases = ev.load_cases(Path(args.cases))
    if args.borderline_only:
        cases = [c for c in cases if c["id"].startswith(BORDERLINE_PREFIX)]

    template = ""
    if not args.mock:
        if not cmp.FILING_GATE_PROMPT.exists():
            print(f"error: filing gate prompt missing at {cmp.FILING_GATE_PROMPT}", file=sys.stderr)
            return 2
        template = cmp.FILING_GATE_PROMPT.read_text(encoding="utf-8")

    where = f"mock={args.mock}" if args.mock else f"model={args.model}"
    print(f"Stability: {len(cases)} cases x {args.repeats} repeats | {where}\n")

    # Preflight: one probe call so a broken environment (e.g. an expired CLI
    # login returning 401) aborts in a single call instead of grinding through
    # every case. The first stability run wasted a long batch exactly this way.
    if not args.mock and cases:
        _, probe_err = _sample(cases[0], template, args.model, args.timeout, 1, None)
        if probe_err:
            print(f"error: probe call failed ({probe_err}). The nested `claude -p` is not "
                  f"working (commonly an expired CLI login -> 401). Re-authenticate the "
                  f"Claude CLI, then retry.", file=sys.stderr)
            return 3

    rows = run(cases, repeats=args.repeats, template=template, model=args.model,
               timeout=args.timeout, mock_kind=args.mock)
    _print_rows(rows)
    s = summarize(rows)
    print(f"\nsummary ({s['total']} cases, {args.repeats} repeats each)")
    print(f"  verdict-level flips:   votes=1 {s['v1_verdict_unstable']}   votes=3 {s['v3_verdict_unstable']}")
    print(f"  decision-level flips:  votes=1 {s['v1_decision_unstable']}   votes=3 {s['v3_decision_unstable']}")
    print("  (decision = PROMOTE vs NOT_PROMOTE; fewer flips = more stable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
