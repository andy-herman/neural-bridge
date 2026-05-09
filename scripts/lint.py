#!/usr/bin/env python3
"""lint.py — V1 weekly health checks for knowledge/ (issue #11).

Runs deterministic checks (broken wiki-links, orphans, missing frontmatter)
and one adversarial LLM check (imperative AI-directed language). Generates
a triage report at docs/lint/<date>.md. Never auto-mutates concepts.

The build plan calls for seven checks total. V1 ships the three
deterministic ones (cheap, always-on) plus one LLM check (the
security-relevant adversarial-language detector). Other LLM checks
(contradictions, source traceability, gap candidates) are scoped for
later PRs once the corpus is large enough to be worth running them.

Usage:
  python3 scripts/lint.py                          # all enabled checks
  python3 scripts/lint.py --check broken-links     # specific check
  python3 scripts/lint.py --no-llm                 # deterministic only
  python3 scripts/lint.py --since 2026-05-01       # only changed-since concepts for LLM
  python3 scripts/lint.py --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
CONCEPTS_DIR = KNOWLEDGE_DIR / "concepts"
QUARANTINE_DIR = KNOWLEDGE_DIR / "quarantine"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
LINT_DIR = REPO_ROOT / "docs" / "lint"
IMPERATIVE_PROMPT = SCRIPTS_DIR / "prompts" / "lint_imperative_language_v1.md"

DEFAULT_MODEL = "claude-sonnet-4-6"
LINT_VERSION = "1.0"
DEFAULT_TIMEOUT = 60

ALL_CHECKS = ("broken-links", "orphans", "frontmatter", "imperative-language")
DETERMINISTIC_CHECKS = ("broken-links", "orphans", "frontmatter")
LLM_CHECKS = ("imperative-language",)

# Wiki link: [[slug]] or [[slug|display]]
WIKI_LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9\-_/]*)(\|[^\]]+)?\]\]")
# YAML frontmatter block at start of file
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

REQUIRED_CONCEPT_FRONTMATTER = {"slug", "verdict", "compiled_at", "compiler_version", "sources"}


# ---------- utilities ----------

def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def log_line(verbose: bool, msg: str) -> None:
    if verbose:
        print(f"[{utc_iso()}] {msg}", file=sys.stderr)


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


# ---------- finding model ----------

@dataclass
class Finding:
    check: str
    severity: str  # HIGH | MEDIUM | LOW
    file: str
    evidence: str
    suggestion: str

    def render_md(self) -> str:
        return (
            f"### {self.severity} | `{self.file}`\n\n"
            f"**Check:** `{self.check}`\n\n"
            f"**Evidence:** {self.evidence}\n\n"
            f"**Suggestion:** {self.suggestion}\n"
        )


# ---------- frontmatter parsing ----------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Empty dict if no frontmatter."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text = m.group(1)
    fm: dict = {}
    for line in fm_text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#") or line.startswith(" "):
            continue  # skip blanks, comments, list-continuation lines
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip()
    return fm, text[m.end():]


def list_concept_files() -> list[Path]:
    if not CONCEPTS_DIR.exists():
        return []
    return sorted(p for p in CONCEPTS_DIR.glob("*.md") if not p.name.startswith("."))


# ---------- check 1: broken wiki-links ----------

def check_broken_links(verbose: bool = False) -> list[Finding]:
    """[[slug]] links that don't resolve to a concept file (or index)."""
    findings: list[Finding] = []
    if not CONCEPTS_DIR.exists():
        return findings

    known_slugs = {p.stem for p in list_concept_files()}
    if INDEX_FILE.exists():
        known_slugs.add("index")

    for path in list_concept_files():
        text = path.read_text(encoding="utf-8")
        for match in WIKI_LINK_RE.finditer(text):
            target = match.group(1).split("/")[-1]
            if target not in known_slugs:
                findings.append(Finding(
                    check="broken-links",
                    severity="MEDIUM",
                    file=str(path.relative_to(REPO_ROOT)),
                    evidence=f"`[[{match.group(1)}]]` does not resolve to a concept",
                    suggestion=f"Either create `knowledge/concepts/{target}.md` or remove the link",
                ))
    log_line(verbose, f"broken-links: {len(findings)} findings")
    return findings


# ---------- check 2: orphans ----------

def check_orphans(verbose: bool = False) -> list[Finding]:
    """Concepts that no other concept (or index.md) links to."""
    findings: list[Finding] = []
    if not CONCEPTS_DIR.exists():
        return findings

    incoming: dict[str, set[str]] = {p.stem: set() for p in list_concept_files()}

    sources = list_concept_files()
    if INDEX_FILE.exists():
        sources.append(INDEX_FILE)

    for path in sources:
        text = path.read_text(encoding="utf-8")
        source_slug = path.stem
        for match in WIKI_LINK_RE.finditer(text):
            target = match.group(1).split("/")[-1]
            if target in incoming and target != source_slug:
                incoming[target].add(source_slug)

    for slug, ins in incoming.items():
        if not ins:
            findings.append(Finding(
                check="orphans",
                severity="LOW",
                file=str((CONCEPTS_DIR / f"{slug}.md").relative_to(REPO_ROOT)),
                evidence="no incoming wiki-links from other concepts or index.md",
                suggestion=f"Add a `[[{slug}]]` reference from a related concept or `knowledge/index.md`, or close-out and quarantine",
            ))
    log_line(verbose, f"orphans: {len(findings)} findings")
    return findings


# ---------- check 3: frontmatter validity ----------

def check_frontmatter(verbose: bool = False) -> list[Finding]:
    """Required fields present, slug matches filename."""
    findings: list[Finding] = []
    for path in list_concept_files():
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        rel = str(path.relative_to(REPO_ROOT))

        if not fm:
            findings.append(Finding(
                check="frontmatter",
                severity="HIGH",
                file=rel,
                evidence="no YAML frontmatter at start of file",
                suggestion="Re-run compile.py to regenerate, or add frontmatter manually with required fields",
            ))
            continue

        missing = REQUIRED_CONCEPT_FRONTMATTER - set(fm.keys())
        if missing:
            findings.append(Finding(
                check="frontmatter",
                severity="HIGH",
                file=rel,
                evidence=f"missing frontmatter keys: {sorted(missing)}",
                suggestion="Re-run compile.py to regenerate, or fill in required keys manually",
            ))
            continue

        if fm.get("slug") and fm["slug"] != path.stem:
            findings.append(Finding(
                check="frontmatter",
                severity="MEDIUM",
                file=rel,
                evidence=f"frontmatter slug `{fm['slug']}` does not match filename `{path.stem}`",
                suggestion="Rename the file to match the slug, or update the frontmatter",
            ))

        if fm.get("verdict") and fm["verdict"] not in ("PROMOTE", "QUARANTINE"):
            findings.append(Finding(
                check="frontmatter",
                severity="MEDIUM",
                file=rel,
                evidence=f"unexpected verdict in concepts/: `{fm['verdict']}`",
                suggestion="Concepts should only carry verdict=PROMOTE; QUARANTINE belongs in knowledge/quarantine/",
            ))

    log_line(verbose, f"frontmatter: {len(findings)} findings")
    return findings


# ---------- check 4: imperative AI-directed language (LLM) ----------

def call_imperative_check(prompt: str, model: str, timeout: int) -> tuple[bool, dict | None, str]:
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

    if "finding" not in data or not isinstance(data["finding"], bool):
        return False, None, "missing_finding"
    if data["finding"]:
        if data.get("severity") not in ("HIGH", "MEDIUM", "LOW"):
            return False, None, f"bad_severity:{data.get('severity')}"
        if not isinstance(data.get("evidence"), list):
            return False, None, "evidence_not_list"
    return True, data, ""


def check_imperative_language(
    since: datetime | None,
    model: str,
    timeout: int,
    verbose: bool = False,
) -> list[Finding]:
    findings: list[Finding] = []
    if not IMPERATIVE_PROMPT.exists():
        return findings
    template = IMPERATIVE_PROMPT.read_text(encoding="utf-8")

    for path in list_concept_files():
        if since is not None:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < since:
                continue

        text = path.read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        slug = path.stem

        prompt = template.replace("{slug}", slug).replace("{concept_text}", body)
        ok, data, err = call_imperative_check(prompt, model, timeout)
        if not ok:
            log_line(verbose, f"imperative-language ERROR for {slug}: {err}")
            findings.append(Finding(
                check="imperative-language",
                severity="LOW",
                file=str(path.relative_to(REPO_ROOT)),
                evidence=f"lint check failed: {err}",
                suggestion="Re-run lint, or check `claude` availability and model permissions",
            ))
            continue

        if not data["finding"]:
            log_line(verbose, f"imperative-language CLEAN: {slug}")
            continue

        evidence_str = "; ".join(f'"{e}"' for e in data["evidence"][:3])
        findings.append(Finding(
            check="imperative-language",
            severity=data["severity"],
            file=str(path.relative_to(REPO_ROOT)),
            evidence=f"{data.get('reason', 'imperative AI-directed language')}: {evidence_str}",
            suggestion=(
                "Rewrite the imperative phrasing as a description, "
                "or move the article to `knowledge/quarantine/` if the imperative is intentional"
            ),
        ))
        log_line(verbose, f"imperative-language {data['severity']}: {slug}")

    return findings


# ---------- report ----------

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def render_report(findings: list[Finding], checks_run: list[str], dry_run_note: str = "") -> str:
    findings_sorted = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 99), f.check, f.file))
    by_severity: dict[str, list[Finding]] = {"HIGH": [], "MEDIUM": [], "LOW": []}
    for f in findings_sorted:
        by_severity.setdefault(f.severity, []).append(f)

    lines = [
        f"# Lint report — {utc_today()}",
        "",
        f"_Generated {utc_iso()} by `lint.py` v{LINT_VERSION}._",
        "",
        f"**Checks run:** {', '.join(f'`{c}`' for c in checks_run)}",
        "",
        f"**Total findings:** {len(findings_sorted)} "
        f"(HIGH: {len(by_severity['HIGH'])}, "
        f"MEDIUM: {len(by_severity['MEDIUM'])}, "
        f"LOW: {len(by_severity['LOW'])})",
    ]
    if dry_run_note:
        lines.append("")
        lines.append(dry_run_note)

    if not findings_sorted:
        lines.extend(["", "Clean. No findings."])
        return "\n".join(lines) + "\n"

    for severity in ("HIGH", "MEDIUM", "LOW"):
        items = by_severity.get(severity, [])
        if not items:
            continue
        lines.extend(["", f"## {severity}", ""])
        for f in items:
            lines.append(f.render_md())

    return "\n".join(lines) + "\n"


# ---------- main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly health checks on knowledge/concepts/")
    parser.add_argument(
        "--check",
        action="append",
        choices=ALL_CHECKS,
        help=f"Run a specific check (repeatable). Default: all of {', '.join(ALL_CHECKS)}.",
    )
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM-based checks (only deterministic)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--since", help="Only LLM-check concepts modified at/after this UTC date (YYYY-MM-DD)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    checks = args.check or list(ALL_CHECKS)
    if args.no_llm:
        checks = [c for c in checks if c not in LLM_CHECKS]

    if not CONCEPTS_DIR.exists():
        print("knowledge/concepts/ does not exist — nothing to lint.", file=sys.stderr)
        # Still write a report so the run is observable.
        LINT_DIR.mkdir(parents=True, exist_ok=True)
        report = render_report([], checks, dry_run_note="_No `knowledge/concepts/` directory yet._")
        (LINT_DIR / f"{utc_today()}.md").write_text(report, encoding="utf-8")
        return 0

    since: datetime | None = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    findings: list[Finding] = []
    if "broken-links" in checks:
        findings.extend(check_broken_links(verbose=args.verbose))
    if "orphans" in checks:
        findings.extend(check_orphans(verbose=args.verbose))
    if "frontmatter" in checks:
        findings.extend(check_frontmatter(verbose=args.verbose))
    if "imperative-language" in checks:
        findings.extend(check_imperative_language(since, args.model, args.timeout, verbose=args.verbose))

    LINT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = LINT_DIR / f"{utc_today()}.md"
    report_path.write_text(render_report(findings, checks), encoding="utf-8")

    by_sev: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    print(
        f"lint complete: {len(findings)} findings "
        f"(HIGH={by_sev['HIGH']}, MEDIUM={by_sev['MEDIUM']}, LOW={by_sev['LOW']}) "
        f"-> {report_path.relative_to(REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
