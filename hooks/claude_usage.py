"""What every fleet `claude -p` call cost, logged by the caller.

Why: since the fleet moved to Andy's API key (PR #208, 2026-10-17) every cost
figure has been an estimate rebuilt from transcripts at list prices. The
transcripts cannot say which job a session was (a check-in, a flush, a Luna
turn) or which route paid for it, and on the proxy route Claude Code priced
the gateway's dotted model ids as a retired model, which inflated the AI
ledger. The caller knows both, and Claude Code reports its own usage and
cost when asked for JSON, so each call writes one line here.

    ~/Library/Application Support/neural-bridge/usage/claude-YYYY-MM.jsonl

One line per call: time, job, agent, route, model, outcome, Claude Code's
cost figure, tokens by kind, turns and duration. Never the prompt or the
reply. The AI ledger (~/Development/ai-ledger) reads these files for its
fleet section. Writing never raises: a call that worked must not fail
because its usage line could not be written.

`run_result()` turns `--output-format json` output back into the
(ok, text, error) shape callers had with `--output-format text`.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import claude_env

USAGE_DIR = Path(os.environ.get("NB_USAGE_DIR") or
                 Path.home() / "Library" / "Application Support" / "neural-bridge" / "usage")

ROUTE_API, ROUTE_PROXY, ROUTE_LOGIN = "api", "proxy", "login"


@dataclass
class CallResult:
    ok: bool
    text: str
    error: str = ""
    meta: dict = field(default_factory=dict)


def route_of(env: Mapping[str, str] | None) -> str:
    """Which route a call's environment takes: the API key, the proxy, or the
    Claude Code login (the Max plan; the loop engineer)."""
    env = env or {}
    if env.get("ANTHROPIC_BASE_URL"):
        return ROUTE_PROXY
    key = env.get("ANTHROPIC_API_KEY")
    if key and key != claude_env.PROXY_PLACEHOLDER_KEY:
        return ROUTE_API
    return ROUTE_LOGIN


def _json_object(stdout: str | None) -> dict:
    """The result object Claude Code printed, or {} when it printed none (a
    missing session prints only to stderr). Only a `"type": "result"` object
    counts: a reply that is itself JSON, from a fake or an older CLI, is text."""
    text = (stdout or "").strip()
    if not text:
        return {}
    for candidate in (text, text.splitlines()[-1]):
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return {}


def run_result(returncode: int, stdout: str | None, stderr: str | None) -> CallResult:
    """(ok, text, error) from a `--output-format json` run. The error keeps the
    shape callers match on: `exit_<code>:<one line>`, where the line is the
    API error, a `error_max_turns` / `error_max_budget_usd` stop, or stderr."""
    meta = _json_object(stdout)
    text = meta.get("result") if isinstance(meta.get("result"), str) else ("" if meta else (stdout or ""))
    failed = returncode != 0 or bool(meta.get("is_error"))
    if not failed:
        return CallResult(True, text, "", meta)
    subtype = str(meta.get("subtype") or "")
    if subtype.startswith("error_") and not text.strip():
        reason = subtype
    else:
        reason = claude_env.error_snippet(text, stderr)
    return CallResult(False, text, f"exit_{returncode or 1}:{reason}", meta)


def _tokens(meta: dict) -> tuple[dict, dict]:
    """(token totals by kind, cost by model) from Claude Code's modelUsage,
    which, unlike the top-level usage, is complete when a cap stops a run."""
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    by_model = {}
    for model, u in (meta.get("modelUsage") or {}).items():
        if not isinstance(u, dict):
            continue
        totals["input"] += int(u.get("inputTokens") or 0)
        totals["output"] += int(u.get("outputTokens") or 0)
        totals["cache_read"] += int(u.get("cacheReadInputTokens") or 0)
        totals["cache_write"] += int(u.get("cacheCreationInputTokens") or 0)
        by_model[model] = round(float(u.get("costUSD") or 0), 6)
    return totals, by_model


def path_for(when: datetime, usage_dir: Path | None = None) -> Path:
    return (usage_dir or USAGE_DIR) / f"claude-{when:%Y-%m}.jsonl"


def record(*, job: str, agent: str | None, route: str, model: str, result: CallResult | None,
           seconds: float, error: str = "", max_budget_usd: float | None = None,
           max_turns: int | None = None, usage_dir: Path | None = None,
           now: datetime | None = None) -> None:
    """Append one call's line. `result` is None when the call never returned
    (a timeout): its cost is then unknown, and the line says so."""
    when = now or datetime.now(timezone.utc)
    meta = result.meta if result else {}
    tokens, by_model = _tokens(meta)
    cost = meta.get("total_cost_usd")
    line = {
        "ts": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "job": job or "other",
        "agent": agent or "",
        "route": route,
        "model": model,
        "ok": bool(result and result.ok),
        "error": (error or (result.error if result else ""))[:160],
        "subtype": str(meta.get("subtype") or ""),
        "cost_usd": round(float(cost), 6) if isinstance(cost, (int, float)) else None,
        "tokens": tokens,
        "models": by_model,
        "turns": int(meta.get("num_turns") or 0),
        "seconds": round(seconds, 1),
        "session_id": str(meta.get("session_id") or ""),
        "max_budget_usd": max_budget_usd,
        "max_turns": max_turns,
    }
    target = path_for(when, usage_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass



def run_logged(args: list[str], *, job: str, agent: str | None, env: Mapping[str, str], timeout: float,
               input: str | None = None, cwd: str | None = None, max_budget_usd: float | None = None,
               max_turns: int | None = None) -> CallResult:
    """Run a `claude -p ... --output-format json` command, write its usage
    line, and return the parsed result. Raises what subprocess.run raises (a
    timeout is logged first), so each caller keeps its own handling."""
    model = args[args.index("--model") + 1] if "--model" in args else ""
    usage = {"job": job, "agent": agent, "route": route_of(env), "model": model,
             "max_budget_usd": max_budget_usd, "max_turns": max_turns}
    started = time.monotonic()
    try:
        proc = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout,
                              stdin=None if input is not None else subprocess.DEVNULL,
                              cwd=cwd, env=dict(env))
    except subprocess.TimeoutExpired:
        record(**usage, result=None, seconds=time.monotonic() - started, error="timeout")
        raise
    result = run_result(proc.returncode, proc.stdout, proc.stderr)
    record(**usage, result=result, seconds=time.monotonic() - started)
    return result
