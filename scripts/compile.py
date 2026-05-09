#!/usr/bin/env python3
"""compile.py — V1 filing gate (issue #9 Phase A, compile component).

Reads daily-logs/<agent>/*.md, extracts proposed_concepts from each
session block, and runs the heavy filing gate (PROMOTE / QUARANTINE /
REJECT) per the memory-poisoning paper. PROMOTE writes to
knowledge/concepts/; QUARANTINE writes to knowledge/quarantine/ with
reason; REJECT logs to the run log only.

Phase B (later PR) adds: two-pass per-agent then cross-agent compile,
rich concept article writer, cross-link writer, never-overwrite history,
log.md / index.md refresh.

Usage:
  python3 scripts/compile.py                     # dry-run by default
  python3 scripts/compile.py --no-dry-run        # actually write to concepts/
  python3 scripts/compile.py --since 2026-05-08  # only logs modified after
  python3 scripts/compile.py --verbose

Cron-ready. Runs serial (one filing-gate call at a time) per the build
plan to avoid concurrent SDK pressure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
HOOKS_DIR = REPO_ROOT / "hooks"
DAILY_LOGS_DIR = REPO_ROOT / "daily-logs"
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
CONCEPTS_DIR = KNOWLEDGE_DIR / "concepts"
QUARANTINE_DIR = KNOWLEDGE_DIR / "quarantine"
DRY_RUN_DIR = REPO_ROOT / "docs" / "compile"
COMPILE_STATE_FILE = SCRIPTS_DIR / ".compile_state.json"
FILING_GATE_PROMPT = SCRIPTS_DIR / "prompts" / "filing_gate_v1.md"

sys.path.insert(0, str(HOOKS_DIR))
import discord_post  # noqa: E402
import schema  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-4-6"
COMPILER_VERSION = "1.0"
DEFAULT_TIMEOUT = 120

PROMOTE = "PROMOTE"
QUARANTINE = "QUARANTINE"
REJECT = "REJECT"
VALID_VERDICTS = {PROMOTE, QUARANTINE, REJECT}


# ---------- utilities ----------

def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def log_line(verbose: bool, msg: str) -> None:
    if verbose:
        print(f"[{utc_iso()}] {msg}", file=sys.stderr)


# ---------- daily-log parsing ----------

@dataclass
class SessionRecord:
    agent: str
    source_log: Path
    session_n: int
    session_id: str
    transcript_sha256: str
    started_at: str
    ended_at: str
    decisions: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    proposed_concepts: list[dict] = field(default_factory=list)


# Parse `## Session N — HH:MM UTC` headings
SESSION_HEADING_RE = re.compile(r"^## Session (\d+) — ", re.MULTILINE)
# Per-session inline YAML block
SESSION_YAML_RE = re.compile(r"```yaml\n(.*?)\n```", re.DOTALL)
# Section headings inside a session block
SECTION_RE = re.compile(r"^### (Decisions|Findings|Open questions|Proposed concepts)\s*$", re.MULTILINE)


def _parse_inline_yaml(text: str) -> dict:
    """Tiny YAML parser for the session block: `key: value` pairs only.

    Uses stdlib only. Sufficient for ADR-007's flat session header schema.
    """
    out: dict = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        out[key] = value
    return out


def _parse_section_bullets(text: str, section: str) -> list[str]:
    """Extract bullets under `### <section>`. Returns [] if section is absent or `- (none)`."""
    pattern = re.compile(
        rf"^### {re.escape(section)}\s*\n+(.*?)(?=^### |\Z)",
        re.DOTALL | re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return []
    body = m.group(1)
    bullets: list[str] = []
    for line in body.splitlines():
        line = line.rstrip()
        if not line.startswith("- "):
            continue
        item = line[2:].strip()
        if not item or item == "(none)":
            continue
        bullets.append(item)
    return bullets


def _parse_proposed_concepts(text: str) -> list[dict]:
    """Concepts are bullets shaped `- <slug>: <summary>` under `### Proposed concepts`."""
    raw = _parse_section_bullets(text, "Proposed concepts")
    out: list[dict] = []
    for item in raw:
        if ":" not in item:
            continue
        slug, _, summary = item.partition(":")
        slug = slug.strip()
        summary = summary.strip()
        if not slug or not summary:
            continue
        out.append({"slug": slug, "summary": summary})
    return out


def parse_daily_log(path: Path) -> list[SessionRecord]:
    """Parse a daily-log file into a list of SessionRecord."""
    text = path.read_text(encoding="utf-8")

    # Strip file-level frontmatter
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5:]

    # Split into per-session chunks by `^## Session N` heading
    matches = list(SESSION_HEADING_RE.finditer(text))
    if not matches:
        return []

    # Determine agent from path: daily-logs/<agent>/YYYY-MM-DD.md
    agent = path.parent.name

    records: list[SessionRecord] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end]
        session_n = int(match.group(1))

        yaml_match = SESSION_YAML_RE.search(block)
        if not yaml_match:
            continue
        meta = _parse_inline_yaml(yaml_match.group(1))

        records.append(
            SessionRecord(
                agent=agent,
                source_log=path,
                session_n=session_n,
                session_id=meta.get("session_id", ""),
                transcript_sha256=meta.get("transcript_sha256", ""),
                started_at=meta.get("started_at", ""),
                ended_at=meta.get("ended_at", ""),
                decisions=_parse_section_bullets(block, "Decisions"),
                findings=_parse_section_bullets(block, "Findings"),
                open_questions=_parse_section_bullets(block, "Open questions"),
                proposed_concepts=_parse_proposed_concepts(block),
            )
        )
    return records


# ---------- candidate extraction ----------

@dataclass
class ConceptCandidate:
    slug: str
    summary: str
    sources: list[dict]  # each: {agent, session_id, transcript_sha256, source_log, session_n}
    excerpt: str  # session content used to ground filing-gate decisions


def session_to_excerpt(rec: SessionRecord, max_chars: int = 4000) -> str:
    """Build a compact text excerpt from a session for the filing gate to read."""
    parts = []
    if rec.decisions:
        parts.append("Decisions:")
        parts.extend(f"- {d}" for d in rec.decisions)
    if rec.findings:
        parts.append("Findings:")
        parts.extend(f"- {f}" for f in rec.findings)
    if rec.open_questions:
        parts.append("Open questions:")
        parts.extend(f"- {q}" for q in rec.open_questions)
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[...truncated]"
    return text


def find_daily_log_files(since: datetime | None = None) -> list[Path]:
    """Return all daily-log files. If `since` is given, filter by mtime."""
    if not DAILY_LOGS_DIR.exists():
        return []
    out: list[Path] = []
    for agent_dir in sorted(DAILY_LOGS_DIR.iterdir()):
        if not agent_dir.is_dir():
            continue
        if agent_dir.name.startswith("_"):
            continue
        for f in sorted(agent_dir.glob("*.md")):
            if since is not None:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < since:
                    continue
            out.append(f)
    return out


def gather_candidates(log_files: list[Path]) -> list[ConceptCandidate]:
    """Walk daily-logs, dedupe candidates by slug, build provenance."""
    by_slug: dict[str, ConceptCandidate] = {}
    for path in log_files:
        for rec in parse_daily_log(path):
            for concept in rec.proposed_concepts:
                slug = concept["slug"]
                summary = concept["summary"]
                source_entry = {
                    "agent": rec.agent,
                    "session_id": rec.session_id,
                    "transcript_sha256": rec.transcript_sha256,
                    "source_log": str(path.relative_to(REPO_ROOT)),
                    "session_n": rec.session_n,
                }
                if slug not in by_slug:
                    by_slug[slug] = ConceptCandidate(
                        slug=slug,
                        summary=summary,
                        sources=[source_entry],
                        excerpt=session_to_excerpt(rec),
                    )
                else:
                    by_slug[slug].sources.append(source_entry)
    return list(by_slug.values())


# ---------- filing gate ----------

def build_filing_gate_prompt(template: str, slug: str, summary: str, agent: str, excerpt: str) -> str:
    return (
        template.replace("{slug}", slug)
        .replace("{summary}", summary)
        .replace("{agent}", agent)
        .replace("{session_excerpt}", excerpt)
    )


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def call_filing_gate(prompt: str, model: str, timeout: int) -> tuple[bool, dict | None, str]:
    """Invoke `claude -p` with the filing gate prompt. Return (ok, parsed, error)."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text", "--model", model],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, None, "timeout"
    except FileNotFoundError:
        return False, None, "claude_cli_not_found"
    if result.returncode != 0:
        snippet = (result.stderr or "")[:200].replace("\n", " ")
        return False, None, f"exit_{result.returncode}:{snippet}"

    text = strip_code_fences(result.stdout)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, None, f"json_decode:{exc.msg}"

    verdict = data.get("verdict")
    if verdict not in VALID_VERDICTS:
        return False, None, f"bad_verdict:{verdict}"
    if not isinstance(data.get("reason"), str):
        return False, None, "missing_reason"
    if not isinstance(data.get("checks_triggered"), list):
        return False, None, "missing_checks_triggered"
    return True, data, ""


# ---------- output writing ----------

def _frontmatter(
    candidate: ConceptCandidate,
    verdict: str,
    reason: str,
    checks_triggered: list[str],
) -> str:
    sources_yaml = "\n".join(
        f"  - agent: {s['agent']}\n"
        f"    session_id: {s['session_id']}\n"
        f"    transcript_sha256: {s['transcript_sha256']}\n"
        f"    source_log: {s['source_log']}\n"
        f"    session_n: {s['session_n']}"
        for s in candidate.sources
    )
    checks_yaml = (
        "[]" if not checks_triggered
        else "[" + ", ".join(c for c in checks_triggered) + "]"
    )
    return (
        "---\n"
        f"slug: {candidate.slug}\n"
        f"verdict: {verdict}\n"
        f"reason: {reason}\n"
        f"checks_triggered: {checks_yaml}\n"
        f"compiled_at: {utc_iso()}\n"
        f'compiler_version: "{COMPILER_VERSION}"\n'
        "sources:\n"
        f"{sources_yaml}\n"
        "---\n"
    )


def write_concept(candidate: ConceptCandidate, gate: dict, dry_run: bool) -> Path:
    """Write a PROMOTE'd concept. In dry-run, write to docs/compile/<date>.md instead."""
    body = (
        f"# {candidate.slug}\n\n"
        f"{candidate.summary}\n\n"
        f"_Promoted on {utc_iso()} by `compile.py` v{COMPILER_VERSION}._\n"
    )
    fm = _frontmatter(candidate, PROMOTE, gate["reason"], gate["checks_triggered"])
    text = fm + "\n" + body

    if dry_run:
        DRY_RUN_DIR.mkdir(parents=True, exist_ok=True)
        target = DRY_RUN_DIR / f"{utc_today()}-PROMOTE-{candidate.slug}.md"
    else:
        CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
        target = CONCEPTS_DIR / f"{candidate.slug}.md"
    target.write_text(text, encoding="utf-8")
    return target


def write_quarantine(candidate: ConceptCandidate, gate: dict, dry_run: bool) -> Path:
    """Write a QUARANTINE'd concept for human review."""
    body = (
        f"# {candidate.slug}\n\n"
        f"**Quarantined** for human review.\n\n"
        f"**Reason:** {gate['reason']}\n\n"
        f"**Checks triggered:** {', '.join(gate['checks_triggered']) or 'none'}\n\n"
        f"## Proposed summary\n\n"
        f"{candidate.summary}\n\n"
        f"_Quarantined on {utc_iso()} by `compile.py` v{COMPILER_VERSION}._\n"
    )
    fm = _frontmatter(candidate, QUARANTINE, gate["reason"], gate["checks_triggered"])
    text = fm + "\n" + body

    if dry_run:
        DRY_RUN_DIR.mkdir(parents=True, exist_ok=True)
        target = DRY_RUN_DIR / f"{utc_today()}-QUARANTINE-{candidate.slug}.md"
    else:
        QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        target = QUARANTINE_DIR / f"{candidate.slug}.md"
    target.write_text(text, encoding="utf-8")
    return target


# ---------- state ----------

def read_compile_state() -> dict:
    if COMPILE_STATE_FILE.exists():
        return json.loads(COMPILE_STATE_FILE.read_text(encoding="utf-8"))
    return {"last_run_at": None, "compiled_concepts": {}}


def write_compile_state(state: dict) -> None:
    COMPILE_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Compile daily-logs into concept articles via filing gate")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Default True for the first two weeks (per ADR-007). Use --no-dry-run to write to concepts/.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--since",
        help="Only process daily-log files modified at/after this UTC date (YYYY-MM-DD). "
        "If omitted, uses last_run_at from .compile_state.json, or all logs if first run.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--no-discord",
        action="store_true",
        help="Skip the Discord outbound push of the run summary.",
    )
    args = parser.parse_args()

    if not FILING_GATE_PROMPT.exists():
        print(f"error: filing gate prompt missing at {FILING_GATE_PROMPT}", file=sys.stderr)
        return 1

    state = read_compile_state()
    since: datetime | None = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif state.get("last_run_at"):
        since = datetime.strptime(state["last_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    log_files = find_daily_log_files(since=since)
    log_line(args.verbose, f"found {len(log_files)} daily-log files (since={since})")

    candidates = gather_candidates(log_files)
    log_line(args.verbose, f"gathered {len(candidates)} unique candidates")

    if not candidates:
        log_line(args.verbose, "no candidates; exiting clean")
        state["last_run_at"] = utc_iso()
        write_compile_state(state)
        return 0

    template = FILING_GATE_PROMPT.read_text(encoding="utf-8")

    counts = {PROMOTE: 0, QUARANTINE: 0, REJECT: 0, "errors": 0, "skipped_already_compiled": 0}
    run_log_lines: list[str] = [f"# Compile run — {utc_iso()}", "", f"Dry run: {args.dry_run}", ""]

    for cand in candidates:
        if cand.slug in state["compiled_concepts"] and not args.dry_run:
            log_line(args.verbose, f"skip already-compiled: {cand.slug}")
            counts["skipped_already_compiled"] += 1
            run_log_lines.append(f"- SKIP {cand.slug} (already compiled)")
            continue

        prompt = build_filing_gate_prompt(template, cand.slug, cand.summary, cand.sources[0]["agent"], cand.excerpt)
        ok, gate, err = call_filing_gate(prompt, args.model, args.timeout)
        if not ok:
            log_line(args.verbose, f"filing gate ERROR for {cand.slug}: {err}")
            counts["errors"] += 1
            run_log_lines.append(f"- ERROR {cand.slug}: {err}")
            continue

        verdict = gate["verdict"]
        log_line(args.verbose, f"{verdict} {cand.slug}: {gate['reason']}")

        if verdict == PROMOTE:
            target = write_concept(cand, gate, dry_run=args.dry_run)
            counts[PROMOTE] += 1
            run_log_lines.append(f"- PROMOTE {cand.slug} -> {target.relative_to(REPO_ROOT)}")
            if not args.dry_run:
                state["compiled_concepts"][cand.slug] = {
                    "compiled_at": utc_iso(),
                    "verdict": PROMOTE,
                    "sources": cand.sources,
                }
        elif verdict == QUARANTINE:
            target = write_quarantine(cand, gate, dry_run=args.dry_run)
            counts[QUARANTINE] += 1
            run_log_lines.append(f"- QUARANTINE {cand.slug} -> {target.relative_to(REPO_ROOT)} ({gate['reason']})")
            if not args.dry_run:
                state["compiled_concepts"][cand.slug] = {
                    "compiled_at": utc_iso(),
                    "verdict": QUARANTINE,
                    "reason": gate["reason"],
                    "sources": cand.sources,
                }
        else:  # REJECT
            counts[REJECT] += 1
            run_log_lines.append(f"- REJECT {cand.slug}: {gate['reason']} (checks: {', '.join(gate['checks_triggered'])})")

    state["last_run_at"] = utc_iso()
    if not args.dry_run:
        write_compile_state(state)

    summary = (
        f"compile complete: PROMOTE={counts[PROMOTE]} QUARANTINE={counts[QUARANTINE]} "
        f"REJECT={counts[REJECT]} errors={counts['errors']} skipped={counts['skipped_already_compiled']}"
    )
    print(summary)

    if not args.no_discord:
        mode = "dry-run" if args.dry_run else "live"
        header = f"**Compile run** | {utc_today()} | {mode}"
        body_lines = [
            header,
            "",
            f"`PROMOTE={counts[PROMOTE]}` `QUARANTINE={counts[QUARANTINE]}` `REJECT={counts[REJECT]}` "
            f"`errors={counts['errors']}` `skipped={counts['skipped_already_compiled']}`",
        ]
        # First 10 actions for visibility; rest live in the run log on disk.
        action_lines = [line for line in run_log_lines if line.startswith("- ")][:10]
        if action_lines:
            body_lines.append("")
            body_lines.extend(action_lines)
        if len([line for line in run_log_lines if line.startswith("- ")]) > 10:
            body_lines.append(f"_…and more. See `{run_log_path.relative_to(REPO_ROOT)}` for full log._")
        message = discord_post.truncate_for_discord("\n".join(body_lines))
        discord_post.send(message)

    # Always write a run log to docs/compile/
    DRY_RUN_DIR.mkdir(parents=True, exist_ok=True)
    run_log_lines.append("")
    run_log_lines.append(f"## Summary\n\n{summary}\n")
    suffix = "-dry-run" if args.dry_run else ""
    run_log_path = DRY_RUN_DIR / f"{utc_today()}-compile-run{suffix}.md"
    run_log_path.write_text("\n".join(run_log_lines), encoding="utf-8")
    log_line(args.verbose, f"run log: {run_log_path.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
