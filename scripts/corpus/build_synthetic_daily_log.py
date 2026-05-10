"""Bulk-import corpus concept proposals into a synthetic teaching-prep daily-log.

The Stage 2/3 corpus dossiers in
  ~/Documents/Luna Master/Neural Bridge/Corpus/INFO 310A/{lectures,labs}/*.md
contain `- \`concept proposal: <slug> — <summary>\`` lines under their
"Concept proposals" sections. Those proposals were never routed through
the normal SessionEnd → flush.py → daily-logs flow, so compile.py
(filing gate + concept writer) has no way to see them.

This script extracts every concept proposal across all corpus dossiers
and writes them as a synthetic daily-log at:
  daily-logs/teaching-prep/<today>.md

…with a properly-formed ADR-007 schema header + one synthetic session block
+ all proposals listed under `### Proposed concepts`. compile.py can then
read this file like any other daily-log and run the filing gate against
each candidate.

The synthetic session metadata uses a stable session_id derived from
content hash so that re-runs are idempotent (same content → same id).

Usage:
  python3 scripts/corpus/build_synthetic_daily_log.py
  python3 scripts/corpus/build_synthetic_daily_log.py --dry-run  # print, don't write

This is a one-shot bulk-import tool, not part of normal compile flow.
After this synthetic log is consumed and concepts are promoted, the
file should be archived (rename to `.archived`) so compile.py doesn't
re-process it on subsequent runs.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CORPUS_ROOT = Path.home() / "Documents" / "Luna Master" / "Neural Bridge" / "Corpus" / "INFO 310A"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DAILY_LOG_DIR = REPO_ROOT / "daily-logs" / "teaching-prep"

# Match: - `concept proposal: <slug> — <summary>`
# The em-dash separator can be either an actual em-dash (—) or a hyphen (-) in case
# of variation. Be liberal in what we accept.
PROPOSAL_RE = re.compile(
    r"^-\s*`concept proposal:\s*([a-z0-9][a-z0-9-]*)\s*[—-]\s*(.+?)\s*`\s*$",
    re.MULTILINE,
)


def extract_proposals_from_file(path: Path) -> list[tuple[str, str, Path]]:
    """Returns list of (slug, summary, source_file) tuples."""
    text = path.read_text(encoding="utf-8")
    matches = PROPOSAL_RE.findall(text)
    return [(slug, summary, path) for slug, summary in matches]


def collect_all_proposals() -> list[tuple[str, str, Path]]:
    out: list[tuple[str, str, Path]] = []
    for sub in ("lectures", "labs"):
        d = CORPUS_ROOT / sub
        if not d.exists():
            print(f"WARN: {d} not found", file=sys.stderr)
            continue
        for f in sorted(d.glob("*.md")):
            out.extend(extract_proposals_from_file(f))
    # Dedupe by slug (last summary wins) — corpus dossiers may repeat slugs
    # across files when the same pattern recurs.
    seen: dict[str, tuple[str, Path]] = {}
    for slug, summary, source in out:
        seen[slug] = (summary, source)
    return [(slug, summary, source) for slug, (summary, source) in seen.items()]


def build_daily_log(proposals: list[tuple[str, str, Path]]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Stable session_id from content hash so re-runs are idempotent.
    content_hash = hashlib.sha256(
        "\n".join(f"{s}:{u}" for s, u, _ in proposals).encode("utf-8")
    ).hexdigest()
    session_id = f"{content_hash[:8]}-{content_hash[8:12]}-{content_hash[12:16]}-{content_hash[16:20]}-{content_hash[20:32]}"
    transcript_sha = content_hash

    lines: list[str] = []
    # Frontmatter
    lines.append("---")
    lines.append("type: daily-log")
    lines.append("agent: teaching-prep")
    lines.append(f"date: {today}")
    lines.append('schema_version: "1.0"')
    lines.append("session_count: 1")
    lines.append(f"last_flushed_at: {timestamp}")
    lines.append("---")
    lines.append("")
    lines.append(
        "<!-- Synthetic daily-log built from INFO 310A corpus dossier concept proposals. "
        "See scripts/corpus/build_synthetic_daily_log.py. After compile.py has consumed "
        "this file, archive it (rename .archived) to prevent re-processing. -->"
    )
    lines.append("")
    # Session block
    lines.append(f"## Session 1 — {timestamp[11:16]} UTC")
    lines.append("")
    lines.append("```yaml")
    lines.append(f"session_id: {session_id}")
    lines.append(
        "transcript_path: synthetic://corpus-bulk-import/info-310a-stage-2-3"
    )
    lines.append(f"transcript_sha256: {transcript_sha}")
    lines.append(f"started_at: {timestamp}")
    lines.append(f"ended_at: {timestamp}")
    lines.append('flush_version: "1.0"')
    lines.append("hook_event: SyntheticBulkImport")
    lines.append("```")
    lines.append("")
    lines.append("### Decisions")
    lines.append("")
    lines.append(
        "- Stage 2/3 calibration pass on INFO 310A produced ~70 cross-cutting "
        "concept candidates worth filing-gate evaluation."
    )
    lines.append("")
    lines.append("### Findings")
    lines.append("")
    lines.append(
        f"- {len(proposals)} unique concept proposals extracted across "
        f"{len({p[2].name for p in proposals})} corpus dossier files (lectures + labs)."
    )
    lines.append("")
    lines.append("### Open questions")
    lines.append("")
    lines.append("- (none — defer to filing-gate verdicts)")
    lines.append("")
    lines.append("### Proposed concepts")
    lines.append("")
    for slug, summary, _source in sorted(proposals):
        # Strip trailing periods/whitespace and any backticks
        clean_summary = summary.strip().rstrip(".").rstrip()
        lines.append(f"- {slug}: {clean_summary}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print to stdout, don't write")
    ap.add_argument(
        "--allowlist",
        type=Path,
        help="Path to a text file with one slug per line. If set, only proposals "
             "with matching slugs are included. Used to feed compile.py only the "
             "filing-gate-passing candidates from a prior dry-run.",
    )
    args = ap.parse_args()

    proposals = collect_all_proposals()
    if not proposals:
        print("No proposals found in corpus dossiers.", file=sys.stderr)
        return 1

    if args.allowlist:
        allowed = {
            line.strip()
            for line in args.allowlist.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        before = len(proposals)
        proposals = [(s, u, src) for s, u, src in proposals if s in allowed]
        print(
            f"Allowlist filter: {before} → {len(proposals)} proposals "
            f"(allowlist had {len(allowed)} slugs).",
            file=sys.stderr,
        )

    content = build_daily_log(proposals)

    if args.dry_run:
        print(content)
        print(
            f"\n=== Would write {len(proposals)} proposals to daily-logs/teaching-prep/<today>.md ===",
            file=sys.stderr,
        )
        return 0

    DAILY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target = DAILY_LOG_DIR / f"{today}.md"
    target.write_text(content, encoding="utf-8")
    print(f"Wrote {len(proposals)} proposals to {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
