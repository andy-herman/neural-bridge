#!/usr/bin/env python3
"""compile.py — daily-log → concept article promotion (issue #9).

Reads daily-logs/<agent>/*.md, extracts proposed_concepts from each
session block, runs the heavy filing gate (PROMOTE / QUARANTINE /
REJECT) per the memory-poisoning paper, and — for PROMOTE verdicts —
writes a real concept article via a separate `claude -p` call.

Phase A (shipped): filing gate, dry-run default, provenance frontmatter,
quarantine path, run-log to docs/compile/.

Phase B core: rich concept article writer (separate claude call),
never-overwrite history (existing concepts move to
concepts/.history/<slug>/<timestamp>.md), knowledge/log.md and
knowledge/index.md refresh after each live run.

Phase B expansion (this file): connection writer (heuristic: shared
source session_id between two PROMOTE'd candidates produces a
knowledge/connections/<a>--<b>.md file), per-agent containment via
--agent flag, --flush flag for manual single-session flush (folded
from issue #10).

Usage:
  python3 scripts/compile.py                     # dry-run by default
  python3 scripts/compile.py --no-dry-run        # actually write to concepts/
  python3 scripts/compile.py --since 2026-05-08  # only logs modified after
  python3 scripts/compile.py --agent research    # per-agent containment
  python3 scripts/compile.py --no-rich-body      # skip concept writer (cheap)
  python3 scripts/compile.py --no-connections    # skip connection writer
  python3 scripts/compile.py --flush /path/to/transcript.jsonl --agent research
  python3 scripts/compile.py --verbose

Cron-ready. Runs serial (one filing-gate call at a time, one writer call
per PROMOTE) to avoid concurrent SDK pressure.
"""

from __future__ import annotations

import argparse
import json
import os
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
HISTORY_DIR = CONCEPTS_DIR / ".history"
QUARANTINE_DIR = KNOWLEDGE_DIR / "quarantine"
CONNECTIONS_DIR = KNOWLEDGE_DIR / "connections"
WIKI_INDEX = KNOWLEDGE_DIR / "index.md"
WIKI_LOG = KNOWLEDGE_DIR / "log.md"
DRY_RUN_DIR = REPO_ROOT / "docs" / "compile"
COMPILE_STATE_FILE = SCRIPTS_DIR / ".compile_state.json"
FILING_GATE_PROMPT = SCRIPTS_DIR / "prompts" / "filing_gate_v1.md"
CONCEPT_WRITER_PROMPT = SCRIPTS_DIR / "prompts" / "concept_writer_v1.md"
FLUSH_SCRIPT = HOOKS_DIR / "flush.py"

sys.path.insert(0, str(HOOKS_DIR))
import discord_post  # noqa: E402
import schema  # noqa: E402

DEFAULT_MODEL = "claude-sonnet-4-6"
COMPILER_VERSION = "1.2"  # bumped: Phase B expansion (connections, --agent, --flush)
DEFAULT_TIMEOUT = 120
WRITER_TIMEOUT = 240  # concept-writer call is longer-form; give it more time

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


def find_daily_log_files(since: datetime | None = None,
                          agent: str | None = None) -> list[Path]:
    """Return all daily-log files. If `since` is given, filter by mtime.
    If `agent` is given, restrict to daily-logs/<agent>/ (per-agent containment).
    """
    if not DAILY_LOGS_DIR.exists():
        return []
    out: list[Path] = []
    for agent_dir in sorted(DAILY_LOGS_DIR.iterdir()):
        if not agent_dir.is_dir():
            continue
        if agent_dir.name.startswith("_"):
            continue
        if agent is not None and agent_dir.name != agent:
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


def _subprocess_env_for_compile_claude() -> dict[str, str]:
    """Environment for compile.py-spawned `claude -p` subprocesses.

    Sets NB_AGENT=compile so the SessionEnd hook attributes the spawned
    session correctly in daily-logs (instead of falling through to
    `_unattributed`).

    Sets NB_NO_DISCORD=1 so flush.py writes the daily-log entry but
    skips the Discord post — a 70-candidate dry-run otherwise floods
    #neural-bridge-outbound with ~140 flush messages. Audit trail
    (daily-log) is preserved; only the Discord notification is muted.

    Strips NB_DISCORD_WEBHOOK in case it was set in the parent env —
    defense in depth so a webhook URL cannot leak into a child
    process's environment.
    """
    env = {k: v for k, v in os.environ.items() if k != "NB_DISCORD_WEBHOOK"}
    env["NB_AGENT"] = "compile"
    env["NB_NO_DISCORD"] = "1"
    return env


def call_filing_gate(prompt: str, model: str, timeout: int) -> tuple[bool, dict | None, str]:
    """Invoke `claude -p` with the filing gate prompt. Return (ok, parsed, error)."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text", "--model", model],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=_subprocess_env_for_compile_claude(),
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


def write_concept(candidate: ConceptCandidate, gate: dict, dry_run: bool,
                  rendered_text: str | None = None) -> Path:
    """Write a PROMOTE'd concept. In dry-run, write to docs/compile/<date>.md instead.

    If `rendered_text` is provided, use it verbatim (Phase B path: rich article
    body produced by the concept writer). Otherwise fall back to the Phase A
    stub (slug + summary + footer).
    """
    if rendered_text is None:
        rendered_text = stub_concept_article(candidate, gate)

    if dry_run:
        DRY_RUN_DIR.mkdir(parents=True, exist_ok=True)
        target = DRY_RUN_DIR / f"{utc_today()}-PROMOTE-{candidate.slug}.md"
    else:
        CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
        target = CONCEPTS_DIR / f"{candidate.slug}.md"
    target.write_text(rendered_text, encoding="utf-8")
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


# ---------- concept article writer (Phase B core) ----------

def build_concept_writer_prompt(template: str, slug: str, summary: str, agent: str, excerpt: str) -> str:
    return (
        template.replace("{slug}", slug)
        .replace("{summary}", summary)
        .replace("{agent}", agent)
        .replace("{session_excerpt}", excerpt)
    )


def call_concept_writer(prompt: str, model: str, timeout: int) -> tuple[bool, str, str]:
    """Invoke `claude -p` with the concept writer prompt. Return (ok, body, error).

    Body is the article markdown with no frontmatter and no H1 (per the prompt's
    output rules). Caller wraps it with frontmatter + title.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text", "--model", model],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=_subprocess_env_for_compile_claude(),
        )
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except FileNotFoundError:
        return False, "", "claude_cli_not_found"
    if result.returncode != 0:
        snippet = (result.stderr or "")[:200].replace("\n", " ")
        return False, "", f"exit_{result.returncode}:{snippet}"

    body = strip_code_fences(result.stdout)
    # Defensive: strip a leading H1 if the model emitted one despite the rule.
    if body.startswith("# "):
        body = body.split("\n", 1)[1].lstrip("\n") if "\n" in body else ""
    if not body.strip():
        return False, "", "empty_body"
    return True, body.rstrip() + "\n", ""


def render_concept_article(candidate: ConceptCandidate, gate: dict, body: str) -> str:
    """Compose frontmatter + H1 + body. Body is already stripped of leading H1."""
    fm = _frontmatter(candidate, PROMOTE, gate["reason"], gate["checks_triggered"])
    return f"{fm}\n# {candidate.slug}\n\n{body}"


def stub_concept_article(candidate: ConceptCandidate, gate: dict) -> str:
    """Phase A stub. Used when --no-rich-body is set or the writer call fails."""
    fm = _frontmatter(candidate, PROMOTE, gate["reason"], gate["checks_triggered"])
    body = (
        f"# {candidate.slug}\n\n"
        f"{candidate.summary}\n\n"
        f"_Promoted on {utc_iso()} by `compile.py` v{COMPILER_VERSION}._\n"
    )
    return fm + "\n" + body


# ---------- never-overwrite history (Phase B core) ----------

def archive_existing_concept(slug: str, dry_run: bool) -> Path | None:
    """If concepts/<slug>.md exists, move it to concepts/.history/<slug>/<timestamp>.md.

    Returns the archived path, or None if there was nothing to archive.
    In dry-run mode, returns None without touching the filesystem.
    """
    if dry_run:
        return None
    src = CONCEPTS_DIR / f"{slug}.md"
    if not src.exists():
        return None
    dest_dir = HISTORY_DIR / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    dest = dest_dir / f"{timestamp}.md"
    src.rename(dest)
    return dest


# ---------- index.md / log.md refresh (Phase B core) ----------

LOG_DATE_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
INDEX_CONCEPTS_HEADING_RE = re.compile(r"^## Concepts\s*$", re.MULTILINE)


def append_to_log(run_summary: str, action_lines: list[str], dry_run: bool) -> None:
    """Append a dated section to knowledge/log.md.

    If today's date already has a section, append bullets under it.
    Otherwise add a new `## YYYY-MM-DD` block at the bottom.
    Skipped entirely in dry-run.
    """
    if dry_run:
        return
    if not WIKI_LOG.exists():
        return  # log.md is hand-curated; respect its absence
    today = utc_today()
    text = WIKI_LOG.read_text(encoding="utf-8")

    new_bullets = ["- " + run_summary] + [f"  {line}" for line in action_lines]

    headings = list(LOG_DATE_HEADING_RE.finditer(text))
    if headings and headings[-1].group(1) == today:
        # Append under existing today section.
        body = text.rstrip() + "\n" + "\n".join(new_bullets) + "\n"
    else:
        # New dated block.
        body = text.rstrip() + f"\n\n## {today}\n\n" + "\n".join(new_bullets) + "\n"
    WIKI_LOG.write_text(body, encoding="utf-8")


def refresh_index(promoted_slugs: list[str], dry_run: bool) -> None:
    """Add new promoted slugs to the `## Concepts` section of knowledge/index.md.

    Idempotent: existing entries are not duplicated. The placeholder
    `_None yet..._` line is replaced on first promotion. Skipped in dry-run.
    """
    if dry_run or not promoted_slugs:
        return
    if not WIKI_INDEX.exists():
        return
    text = WIKI_INDEX.read_text(encoding="utf-8")

    m = INDEX_CONCEPTS_HEADING_RE.search(text)
    if not m:
        return  # Index doesn't have a Concepts section; respect that

    # Find the bounds of the Concepts section: from after the heading to the
    # next `^## ` heading (or EOF).
    section_start = m.end()
    next_heading = re.search(r"^## ", text[section_start:], re.MULTILINE)
    section_end = section_start + next_heading.start() if next_heading else len(text)
    section_body = text[section_start:section_end]

    # Existing concept slugs in the section (from `- [[slug]]` lines).
    existing = set(re.findall(r"^- \[\[([^\]]+)\]\]", section_body, re.MULTILINE))

    new_slugs = [s for s in promoted_slugs if s not in existing]
    if not new_slugs:
        return

    new_lines = [f"- [[{s}]]" for s in sorted(set(existing) | set(new_slugs))]
    rebuilt_section = "\n\n" + "\n".join(new_lines) + "\n\n"

    new_text = text[:section_start] + rebuilt_section + text[section_end:]
    WIKI_INDEX.write_text(new_text, encoding="utf-8")


# ---------- connection writer (Phase B expansion) ----------

@dataclass
class Connection:
    slug_a: str  # always the alphabetically first slug
    slug_b: str  # always the alphabetically second slug
    shared_session_ids: list[str]
    shared_sources: list[dict]


def find_shared_session_pairs(candidates: list[ConceptCandidate]) -> list[Connection]:
    """Heuristic: two concepts are connected if they share a source session_id.

    A concept's source list is a list of dicts with `session_id`. If concept A
    and concept B both list the same session_id in their sources, they emerged
    from the same work and are likely related.

    Returns Connection records with slugs alphabetized for stable filenames.
    Self-pairs and pairs with no shared session are excluded.
    """
    out: list[Connection] = []
    for i, a in enumerate(candidates):
        a_sids = {s["session_id"] for s in a.sources if s.get("session_id")}
        if not a_sids:
            continue
        for b in candidates[i + 1:]:
            b_sids = {s["session_id"] for s in b.sources if s.get("session_id")}
            shared = sorted(a_sids & b_sids)
            if not shared:
                continue
            slug_a, slug_b = sorted([a.slug, b.slug])
            shared_sources = [
                s for s in a.sources + b.sources
                if s.get("session_id") in shared
            ]
            out.append(Connection(
                slug_a=slug_a, slug_b=slug_b,
                shared_session_ids=shared,
                shared_sources=shared_sources,
            ))
    return out


def render_connection(conn: Connection) -> str:
    """Compose the connection markdown file."""
    sids_yaml = "[" + ", ".join(conn.shared_session_ids) + "]"
    sources_yaml = "\n".join(
        f"  - agent: {s['agent']}\n"
        f"    session_id: {s['session_id']}\n"
        f"    source_log: {s['source_log']}"
        for s in conn.shared_sources
    )
    fm = (
        "---\n"
        "type: connection\n"
        f"discovered_via: shared-session\n"
        f"concepts: [{conn.slug_a}, {conn.slug_b}]\n"
        f"shared_session_ids: {sids_yaml}\n"
        f"created_at: {utc_iso()}\n"
        f'compiler_version: "{COMPILER_VERSION}"\n'
        "shared_sources:\n"
        f"{sources_yaml}\n"
        "---\n\n"
    )
    body = (
        f"# [[{conn.slug_a}]] ↔ [[{conn.slug_b}]]\n\n"
        f"Both concepts emerged from the same source session(s): "
        f"{', '.join(f'`{s}`' for s in conn.shared_session_ids)}. "
        f"Shared origin suggests they cover related ideas from the same exploration.\n\n"
        f"## Why these are linked\n\n"
        f"Discovered via the `shared-session` heuristic: a candidate-pair where both "
        f"concepts cite at least one source session_id in common after the filing "
        f"gate has independently approved each one.\n\n"
        f"## Source sessions\n\n"
    )
    body += "\n".join(
        f"- `{s['session_id']}` (agent: `{s['agent']}`, log: `{s['source_log']}`)"
        for s in conn.shared_sources
    ) + "\n\n"
    body += "## Related\n\n"
    body += f"- [[{conn.slug_a}]]\n- [[{conn.slug_b}]]\n"
    return fm + body


def write_connection(conn: Connection, dry_run: bool) -> Path | None:
    """Write a connection file. Returns the path written, or None if skipped.

    Idempotent: if the target file exists, returns None without overwriting
    (connections accumulate; we never replace an existing one).
    Skipped entirely in dry-run.
    """
    if dry_run:
        return None
    target = CONNECTIONS_DIR / f"{conn.slug_a}--{conn.slug_b}.md"
    if target.exists():
        return None  # idempotent
    CONNECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(render_connection(conn), encoding="utf-8")
    return target


# ---------- state ----------

def read_compile_state() -> dict:
    if COMPILE_STATE_FILE.exists():
        return json.loads(COMPILE_STATE_FILE.read_text(encoding="utf-8"))
    return {"last_run_at": None, "compiled_concepts": {}}


def write_compile_state(state: dict) -> None:
    COMPILE_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ---------- --flush delegate (Phase B expansion, folded from issue #10) ----------

def _run_flush_delegate(transcript_path: str, agent: str, verbose: bool) -> int:
    """Manual single-session flush. Delegates to hooks/flush.py.

    The flush script normally runs as a detached subprocess from session_end.py.
    --flush mode runs it synchronously in the foreground for a single transcript,
    so the user can backfill or replay one session without the nightly batch.

    Returns flush.py's exit code passed through.
    """
    if not Path(transcript_path).exists():
        print(f"error: transcript not found at {transcript_path}", file=sys.stderr)
        return 1
    if not FLUSH_SCRIPT.exists():
        print(f"error: flush.py not found at {FLUSH_SCRIPT}", file=sys.stderr)
        return 1
    log_line(verbose, f"flush delegate: agent={agent} transcript={transcript_path}")
    try:
        result = subprocess.run(
            [sys.executable, str(FLUSH_SCRIPT),
             "--transcript", transcript_path,
             "--agent", agent],
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("error: python interpreter not found", file=sys.stderr)
        return 1
    return result.returncode


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
    parser.add_argument(
        "--no-rich-body",
        action="store_true",
        help="Skip the concept-writer claude call; PROMOTE'd concepts get the Phase A stub.",
    )
    parser.add_argument(
        "--agent",
        default=None,
        help="Per-agent containment: only process daily-logs for this agent (e.g., research). "
        "Useful for spot-debugging or when one agent's logs are suspect and you don't want "
        "their candidates influencing the cross-agent merge.",
    )
    parser.add_argument(
        "--no-connections",
        action="store_true",
        help="Skip writing connection files for shared-session candidate pairs.",
    )
    parser.add_argument(
        "--flush",
        default=None,
        metavar="TRANSCRIPT_PATH",
        help="One-shot manual flush mode (folded from issue #10). Delegates to hooks/flush.py "
        "for a single transcript and exits. Requires --agent to be set.",
    )
    args = parser.parse_args()

    # --flush: short-circuit to manual flush mode and return.
    if args.flush:
        if not args.agent:
            print("error: --flush requires --agent <agent-id>", file=sys.stderr)
            return 1
        return _run_flush_delegate(args.flush, args.agent, args.verbose)

    if not FILING_GATE_PROMPT.exists():
        print(f"error: filing gate prompt missing at {FILING_GATE_PROMPT}", file=sys.stderr)
        return 1
    if not args.no_rich_body and not CONCEPT_WRITER_PROMPT.exists():
        print(f"error: concept writer prompt missing at {CONCEPT_WRITER_PROMPT} "
              f"(use --no-rich-body to fall back to Phase A stub)", file=sys.stderr)
        return 1

    state = read_compile_state()
    since: datetime | None = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif state.get("last_run_at"):
        since = datetime.strptime(state["last_run_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    log_files = find_daily_log_files(since=since, agent=args.agent)
    log_line(args.verbose, f"found {len(log_files)} daily-log files (since={since}, agent={args.agent or 'ALL'})")

    candidates = gather_candidates(log_files)
    log_line(args.verbose, f"gathered {len(candidates)} unique candidates")

    if not candidates:
        log_line(args.verbose, "no candidates; exiting clean")
        state["last_run_at"] = utc_iso()
        write_compile_state(state)
        return 0

    template = FILING_GATE_PROMPT.read_text(encoding="utf-8")
    writer_template = (
        CONCEPT_WRITER_PROMPT.read_text(encoding="utf-8")
        if not args.no_rich_body else None
    )

    counts = {PROMOTE: 0, QUARANTINE: 0, REJECT: 0, "errors": 0,
              "skipped_already_compiled": 0, "writer_failures": 0,
              "archived": 0, "connections_written": 0}
    promoted_slugs: list[str] = []
    promoted_candidates: list[ConceptCandidate] = []  # for connection writer
    run_log_lines: list[str] = [
        f"# Compile run — {utc_iso()}", "",
        f"Dry run: {args.dry_run}",
        f"Agent scope: {args.agent or 'ALL'}",
        "",
    ]

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
            # Phase B: call the concept writer for a real article body. Falls
            # back to the Phase A stub if --no-rich-body is set or the writer
            # call fails for any reason.
            rendered: str | None = None
            if writer_template is not None:
                writer_prompt = build_concept_writer_prompt(
                    writer_template, cand.slug, cand.summary,
                    cand.sources[0]["agent"], cand.excerpt,
                )
                wok, body, werr = call_concept_writer(writer_prompt, args.model, WRITER_TIMEOUT)
                if wok:
                    rendered = render_concept_article(cand, gate, body)
                else:
                    log_line(args.verbose, f"writer ERROR for {cand.slug}: {werr} (falling back to stub)")
                    counts["writer_failures"] += 1

            # Phase B: never-overwrite. If a concept already exists, archive
            # its current version to .history before writing the new one.
            archived = archive_existing_concept(cand.slug, dry_run=args.dry_run)
            if archived is not None:
                counts["archived"] += 1
                run_log_lines.append(f"- ARCHIVE {cand.slug} -> {archived.relative_to(REPO_ROOT)}")

            target = write_concept(cand, gate, dry_run=args.dry_run, rendered_text=rendered)
            counts[PROMOTE] += 1
            promoted_slugs.append(cand.slug)
            promoted_candidates.append(cand)
            tag = "PROMOTE-rich" if rendered else "PROMOTE-stub"
            run_log_lines.append(f"- {tag} {cand.slug} -> {target.relative_to(REPO_ROOT)}")
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

    # Phase B expansion: connection writer.
    # For PROMOTE'd candidates only, find pairs that share a source session_id
    # and write a connection file at knowledge/connections/<slug-a>--<slug-b>.md.
    if not args.no_connections and len(promoted_candidates) >= 2:
        connections = find_shared_session_pairs(promoted_candidates)
        for conn in connections:
            target = write_connection(conn, dry_run=args.dry_run)
            if target is not None:
                counts["connections_written"] += 1
                run_log_lines.append(
                    f"- CONNECTION {conn.slug_a} ↔ {conn.slug_b} -> {target.relative_to(REPO_ROOT)}"
                )
            elif args.dry_run:
                # Surface dry-run intent.
                run_log_lines.append(
                    f"- CONNECTION (dry-run) {conn.slug_a} ↔ {conn.slug_b} via {conn.shared_session_ids}"
                )

    # Phase B: refresh log.md and index.md (live mode only).
    summary = (
        f"compile complete: PROMOTE={counts[PROMOTE]} QUARANTINE={counts[QUARANTINE]} "
        f"REJECT={counts[REJECT]} errors={counts['errors']} skipped={counts['skipped_already_compiled']}"
    )
    if counts["archived"] or counts["writer_failures"]:
        summary += f" archived={counts['archived']} writer_failures={counts['writer_failures']}"
    if counts["connections_written"]:
        summary += f" connections={counts['connections_written']}"

    log_action_lines = [line for line in run_log_lines if line.startswith("- ")]
    append_to_log(summary, log_action_lines, dry_run=args.dry_run)
    refresh_index(promoted_slugs, dry_run=args.dry_run)

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
