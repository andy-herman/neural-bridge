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
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))
import claude_env  # noqa: E402  (route policy for the LLM check; see hooks/claude_env.py)
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
CONCEPTS_DIR = KNOWLEDGE_DIR / "concepts"
QUARANTINE_DIR = KNOWLEDGE_DIR / "quarantine"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
LINT_DIR = REPO_ROOT / "docs" / "lint"
IMPERATIVE_PROMPT = SCRIPTS_DIR / "prompts" / "lint_imperative_language_v1.md"
AGENTS_MD_FILE = REPO_ROOT / "AGENTS.md"
PLUGIN_AGENTS_DIR = REPO_ROOT / "plugins" / "neural-bridge-core" / "agents"
README_FILE = REPO_ROOT / "README.md"
MARKETPLACE_FILE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_MANIFEST_FILE = REPO_ROOT / "plugins" / "neural-bridge-core" / ".claude-plugin" / "plugin.json"
DECISIONS_DIR = REPO_ROOT / "decisions"
SETTINGS_FILE = REPO_ROOT / ".claude" / "settings.json"
# An ADR still "proposed" after this long is either accepted in practice or dead.
ADR_STALE_DAYS = 60

DEFAULT_MODEL = claude_env.PIPELINE_MODEL  # Copilot proxy; see hooks/claude_env.py
LINT_VERSION = "1.0"
DEFAULT_TIMEOUT = 60

ALL_CHECKS = ("broken-links", "orphans", "frontmatter", "agents-roster", "docs-truth", "imperative-language")
DETERMINISTIC_CHECKS = ("broken-links", "orphans", "frontmatter", "agents-roster", "docs-truth")
LLM_CHECKS = ("imperative-language",)

# Wiki link: [[slug]] or [[slug|display]]
WIKI_LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9\-_/]*)(\|[^\]]+)?\]\]")
# AGENTS.md roster block and its table rows (agent name in backticks, first column)
ROSTER_BLOCK_RE = re.compile(r"<!-- AGENTS-ROSTER:BEGIN.*?-->(.*?)<!-- AGENTS-ROSTER:END -->", re.DOTALL)
ROSTER_ROW_RE = re.compile(r"^\|\s*`([a-z0-9\-]+)`\s*\|", re.MULTILINE)
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


# ---------- check: AGENTS.md roster drift ----------

def check_agents_roster(verbose: bool = False) -> list[Finding]:
    """AGENTS.md roster table must match plugins/neural-bridge-core/agents/*.md.

    The schema doc drifted badly once (2026-05 through 2026-07: claimed 3 agents
    while 14 were defined). This check makes that class of rot impossible to miss.
    """
    findings: list[Finding] = []
    if not PLUGIN_AGENTS_DIR.exists():
        log_line(verbose, "agents-roster: no plugin agents dir, skipping")
        return findings

    on_disk = {p.stem for p in PLUGIN_AGENTS_DIR.glob("*.md") if not p.name.startswith(".")}

    if not AGENTS_MD_FILE.exists():
        findings.append(Finding(
            check="agents-roster",
            severity="HIGH",
            file="AGENTS.md",
            evidence="AGENTS.md is missing entirely",
            suggestion="Restore AGENTS.md with an AGENTS-ROSTER block listing every plugin agent",
        ))
        return findings

    block = ROSTER_BLOCK_RE.search(AGENTS_MD_FILE.read_text(encoding="utf-8"))
    if not block:
        findings.append(Finding(
            check="agents-roster",
            severity="HIGH",
            file="AGENTS.md",
            evidence="no AGENTS-ROSTER:BEGIN/END block found",
            suggestion="Add the roster block markers around the agent table in AGENTS.md",
        ))
        return findings

    in_roster = set(ROSTER_ROW_RE.findall(block.group(1)))
    for name in sorted(on_disk - in_roster):
        findings.append(Finding(
            check="agents-roster",
            severity="HIGH",
            file="AGENTS.md",
            evidence=f"agent `{name}` is defined in plugins/ but missing from the AGENTS.md roster",
            suggestion=f"Add a `{name}` row to the roster table in AGENTS.md",
        ))
    for name in sorted(in_roster - on_disk):
        findings.append(Finding(
            check="agents-roster",
            severity="HIGH",
            file="AGENTS.md",
            evidence=f"roster lists agent `{name}` but plugins/neural-bridge-core/agents/{name}.md does not exist",
            suggestion=f"Remove the `{name}` row or restore the agent definition file",
        ))
    log_line(verbose, f"agents-roster: {len(findings)} findings")
    return findings


# ---------- check 3: frontmatter validity ----------

# ---------- docs-truth ----------
#
# The docs drifted from the code twice (2026-07 and 2026-09 truth passes): agent
# counts written as prose, a plugin version stated in three places, ADRs left
# "proposed" for four months, and hook commands that only resolve from the repo
# root. Each sub-check below is one of those exact failures, made mechanical.

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}
# "nine .md plugin files", "3 specialist agents", "fourteen specialists",
# "nine specialist .md definitions", "14 defined". Case-insensitive.
AGENT_COUNT_RE = re.compile(
    r"\b(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s+"
    r"(?:specialist(?:s|\s+agents|\s+\.md\s+definitions)|specialized\s+agents|agents\b|"
    r"\.md\s+plugin\s+files|defined\b)",
    re.IGNORECASE,
)
# Counts that are not about the roster (e.g. "5 cross-agent turns") are the
# reason this regex is anchored on roster nouns; "agents" alone is still broad,
# so only lines that also mention the plugin, the roster, or "specialist" count.
_ROSTER_CONTEXT_RE = re.compile(r"plugin|specialist|roster|defined|\.md", re.IGNORECASE)
_HOOK_PATH_RE = re.compile(r"(?:\"\$CLAUDE_PROJECT_DIR\"?/|\$CLAUDE_PROJECT_DIR/)?([\w./-]+\.py)")
ADR_STATUS_RE = re.compile(r"^status:\s*(\w+)", re.MULTILINE)
ADR_CREATED_RE = re.compile(r"^created:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)


def _count_from_token(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUMBER_WORDS[tok.lower()]


def check_docs_truth(verbose: bool = False, today: datetime | None = None) -> list[Finding]:
    """Docs must match the code they describe. Four deterministic sub-checks."""
    findings: list[Finding] = []
    today = today or datetime.now(timezone.utc)

    # 1. Agent counts stated in prose must equal the plugin's agent files.
    if PLUGIN_AGENTS_DIR.exists():
        on_disk = len([p for p in PLUGIN_AGENTS_DIR.glob("*.md") if not p.name.startswith(".")])
        for doc in (README_FILE, AGENTS_MD_FILE):
            if not doc.exists():
                continue
            for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
                if not _ROSTER_CONTEXT_RE.search(line):
                    continue
                for m in AGENT_COUNT_RE.finditer(line):
                    stated = _count_from_token(m.group(1))
                    if stated != on_disk:
                        findings.append(Finding(
                            check="docs-truth", severity="HIGH",
                            file=f"{doc.name}:{lineno}",
                            evidence=f"says \"{m.group(0)}\" but plugins/neural-bridge-core/agents/ holds {on_disk}",
                            suggestion=f"Change the count to {on_disk} or stop stating it in prose",
                        ))
        log_line(verbose, f"docs-truth: agent-count phrases checked against {on_disk} on disk")

    # 2. The plugin version must agree between plugin.json and marketplace.json.
    try:
        manifest = json.loads(PLUGIN_MANIFEST_FILE.read_text(encoding="utf-8"))
        market = json.loads(MARKETPLACE_FILE.read_text(encoding="utf-8"))
        entry = next((p for p in market.get("plugins", []) if p.get("name") == manifest.get("name")), None)
        if entry is None:
            findings.append(Finding(
                check="docs-truth", severity="HIGH", file=str(MARKETPLACE_FILE.relative_to(REPO_ROOT)),
                evidence=f"no plugin entry named {manifest.get('name')!r}",
                suggestion="Add the plugin to marketplace.json or fix the name",
            ))
        elif entry.get("version") != manifest.get("version"):
            findings.append(Finding(
                check="docs-truth", severity="HIGH", file=str(MARKETPLACE_FILE.relative_to(REPO_ROOT)),
                evidence=f"marketplace says {entry.get('version')} but plugin.json says {manifest.get('version')}",
                suggestion="Bump both in the same commit",
            ))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        findings.append(Finding(
            check="docs-truth", severity="MEDIUM", file=".claude-plugin/marketplace.json",
            evidence=f"could not compare plugin versions: {type(exc).__name__}: {exc}",
            suggestion="Ensure both manifests exist and are valid JSON",
        ))

    # 3. ADRs left "proposed" past the stale window need a decision.
    if DECISIONS_DIR.exists():
        for adr in sorted(DECISIONS_DIR.glob("*.md")):
            text = adr.read_text(encoding="utf-8")
            sm = ADR_STATUS_RE.search(text)
            cm = ADR_CREATED_RE.search(text)
            if not sm or sm.group(1).lower() != "proposed" or not cm:
                continue
            created = datetime.strptime(cm.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            age = (today - created).days
            if age > ADR_STALE_DAYS:
                findings.append(Finding(
                    check="docs-truth", severity="MEDIUM", file=str(adr.relative_to(REPO_ROOT)),
                    evidence=f"status: proposed for {age} days (created {cm.group(1)})",
                    suggestion="Set status to accepted, rejected, or superseded; a proposal this old is a decision in practice",
                ))

    # 4. Hook commands must resolve from any working directory.
    if SETTINGS_FILE.exists():
        try:
            settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            settings = {}
        for event, groups in (settings.get("hooks") or {}).items():
            for group in groups or []:
                for hook in group.get("hooks") or []:
                    cmd = hook.get("command") or ""
                    if hook.get("type") != "command" or not cmd:
                        continue
                    for m in _HOOK_PATH_RE.finditer(cmd):
                        rel = m.group(1)
                        if rel.startswith("/"):
                            continue
                        anchored = "$CLAUDE_PROJECT_DIR" in cmd
                        if not anchored:
                            findings.append(Finding(
                                check="docs-truth", severity="HIGH", file=".claude/settings.json",
                                evidence=f"{event} hook command is cwd-relative: {cmd!r}",
                                suggestion='Anchor it: python3 "$CLAUDE_PROJECT_DIR/<path>" so a stray cd cannot disable every hook',
                            ))
                        if not (REPO_ROOT / rel).exists():
                            findings.append(Finding(
                                check="docs-truth", severity="HIGH", file=".claude/settings.json",
                                evidence=f"{event} hook references {rel}, which does not exist",
                                suggestion="Restore the script or remove the hook entry",
                            ))
    return findings


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
            # The copilot-api proxy, never an inherited base URL or API key.
            env=claude_env.proxy_env(os.environ),
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
    if "agents-roster" in checks:
        findings.extend(check_agents_roster(verbose=args.verbose))
    if "docs-truth" in checks:
        findings.extend(check_docs_truth(verbose=args.verbose))
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
