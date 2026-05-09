#!/usr/bin/env python3
"""flush.py — V1 implementation (issue #9 Phase A).

Invoked by hooks/session_end.py with --agent, --session-id, --transcript,
--hook-event. Calls `claude -p` with the flush prompt + transcript and
appends a structured session block to daily-logs/<agent>/YYYY-MM-DD.md
per ADR-007.

Light filing gate only: the prompt explicitly frames transcript content
as data-not-instructions, and provenance frontmatter (session_id,
transcript_sha256) is mandatory so a poisoned log is traceable. The
heavy filing gate (PROMOTE/QUARANTINE/REJECT) is compile.py's job and
ships in a later PR.

Tracks: issue #9. Replaces the stub from PR #26.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOKS_DIR.parent
DAILY_LOGS_DIR = REPO_ROOT / "daily-logs"
QUEUE_LOG = DAILY_LOGS_DIR / "_queue.log"
PROMPT_TEMPLATE = HOOKS_DIR / "prompts" / "flush_v1.md"

sys.path.insert(0, str(HOOKS_DIR))
import schema  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-4-7"
FLUSH_VERSION = "1.0"
DEFAULT_TIMEOUT = 300


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def utc_hhmm() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")


def write_queue(agent: str, session_id: str, status: str) -> None:
    DAILY_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{utc_iso()} {agent} {session_id} {status}\n"
    with QUEUE_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def transcript_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def build_prompt(template: str, agent: str, session_id: str, hook_event: str, transcript: str) -> str:
    return (
        template.replace("{agent}", agent)
        .replace("{session_id}", session_id)
        .replace("{hook_event}", hook_event)
        .replace("{transcript}", transcript)
    )


def call_claude(prompt: str, model: str, timeout: int) -> tuple[bool, str, str]:
    """Invoke `claude -p`. Return (ok, stdout, error_reason)."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text", "--model", model],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except FileNotFoundError:
        return False, "", "claude_cli_not_found"
    if result.returncode != 0:
        snippet = (result.stderr or "")[:200].replace("\n", " ")
        return False, result.stdout, f"exit_{result.returncode}:{snippet}"
    return True, result.stdout, ""


def strip_code_fences(text: str) -> str:
    """Strip a single leading/trailing ``` block if present."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_response(stdout: str) -> tuple[bool, dict | None, str]:
    """Parse claude -p stdout as flush JSON. Return (ok, data, reason)."""
    text = strip_code_fences(stdout)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, None, f"json_decode:{exc.msg}"
    ok, err = schema.validate_flush_output(data)
    if not ok:
        return False, None, f"schema:{err}"
    return True, data, ""


def render_section(header: str, items: list) -> list[str]:
    out = [f"### {header}", ""]
    if not items:
        out.append("- (none)")
    else:
        if header == "Proposed concepts":
            for c in items:
                out.append(f"- {c['slug']}: {c['summary']}")
        else:
            for it in items:
                out.append(f"- {it}")
    out.append("")
    return out


def build_session_block(
    data: dict,
    session_id: str,
    transcript_path: str,
    transcript_hash: str,
    hook_event: str,
    session_n: int,
    started_at: str,
    ended_at: str,
) -> str:
    lines: list[str] = [
        f"## Session {session_n} — {utc_hhmm()} UTC",
        "",
        "```yaml",
        f"session_id: {session_id}",
        f"transcript_path: {transcript_path}",
        f"transcript_sha256: {transcript_hash}",
        f"started_at: {started_at}",
        f"ended_at: {ended_at}",
        f'flush_version: "{FLUSH_VERSION}"',
        f"hook_event: {hook_event}",
        "```",
        "",
    ]
    lines.extend(render_section("Decisions", data["decisions"]))
    lines.extend(render_section("Findings", data["findings"]))
    lines.extend(render_section("Open questions", data["open_questions"]))
    lines.extend(render_section("Proposed concepts", data["proposed_concepts"]))
    return "\n".join(lines).rstrip() + "\n"


def count_existing_sessions(log_file: Path) -> int:
    if not log_file.exists():
        return 0
    text = log_file.read_text(encoding="utf-8")
    return sum(1 for _ in re.finditer(r"^## Session \d+", text, flags=re.MULTILINE))


def append_session(agent: str, block: str, session_n: int) -> Path:
    today = utc_today()
    agent_dir = DAILY_LOGS_DIR / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    log_file = agent_dir / f"{today}.md"
    now_iso = utc_iso()

    if not log_file.exists():
        frontmatter = (
            "---\n"
            "type: daily-log\n"
            f"agent: {agent}\n"
            f"date: {today}\n"
            f'schema_version: "{schema.SCHEMA_VERSION}"\n'
            f"session_count: {session_n}\n"
            f"last_flushed_at: {now_iso}\n"
            "---\n\n"
        )
        log_file.write_text(frontmatter + block, encoding="utf-8")
        return log_file

    existing = log_file.read_text(encoding="utf-8")
    existing = re.sub(
        r"^session_count: \d+$",
        f"session_count: {session_n}",
        existing,
        count=1,
        flags=re.MULTILINE,
    )
    existing = re.sub(
        r"^last_flushed_at: .*$",
        f"last_flushed_at: {now_iso}",
        existing,
        count=1,
        flags=re.MULTILINE,
    )
    log_file.write_text(existing.rstrip() + "\n\n---\n\n" + block, encoding="utf-8")
    return log_file


def write_failed(agent: str, session_id: str, raw_output: str, reason: str) -> None:
    failed_dir = DAILY_LOGS_DIR / agent / "_failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    failed_file = failed_dir / f"{session_id}.txt"
    failed_file.write_text(
        f"# Flush failed: {reason}\n# {utc_iso()}\n\n{raw_output}\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Flush a session transcript to a daily log")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--hook-event", default="SessionEnd")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    transcript_path = Path(args.transcript)
    if not transcript_path.exists():
        write_queue(args.agent, args.session_id, "failed:transcript_missing")
        return 0
    if not PROMPT_TEMPLATE.exists():
        write_queue(args.agent, args.session_id, "failed:prompt_template_missing")
        return 0

    started_at = utc_iso()

    try:
        transcript_text = transcript_path.read_text(encoding="utf-8")
        transcript_hash = transcript_sha256(transcript_path)
    except OSError as exc:
        write_queue(args.agent, args.session_id, f"failed:read_transcript_{type(exc).__name__}")
        return 0

    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    prompt = build_prompt(template, args.agent, args.session_id, args.hook_event, transcript_text)

    parsed: dict | None = None
    last_raw = ""
    last_err = ""
    for _ in range(2):
        ok, stdout, err = call_claude(prompt, args.model, args.timeout)
        if not ok:
            last_err = err or "subprocess_failure"
            last_raw = stdout
            continue
        parse_ok, data, parse_err = parse_response(stdout)
        if parse_ok:
            parsed = data
            break
        last_err = parse_err
        last_raw = stdout

    if parsed is None:
        write_failed(args.agent, args.session_id, last_raw, last_err)
        prefix = last_err.split(":", 1)[0] if last_err else "unknown"
        write_queue(args.agent, args.session_id, f"failed:{prefix}")
        return 0

    if schema.is_empty_session(parsed):
        write_queue(args.agent, args.session_id, "skipped:empty")
        return 0

    ended_at = utc_iso()
    today = utc_today()
    log_file = DAILY_LOGS_DIR / args.agent / f"{today}.md"
    session_n = count_existing_sessions(log_file) + 1

    block = build_session_block(
        data=parsed,
        session_id=args.session_id,
        transcript_path=str(transcript_path),
        transcript_hash=transcript_hash,
        hook_event=args.hook_event,
        session_n=session_n,
        started_at=started_at,
        ended_at=ended_at,
    )
    append_session(args.agent, block, session_n)
    write_queue(args.agent, args.session_id, "flushed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
