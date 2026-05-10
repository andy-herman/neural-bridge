"""Extract huskyhub labs into the vault corpus.

Source:  ~/Development/huskyhub/labs/week-NN/README.md (+ any other artifacts)
Target:  ~/Documents/Luna Master/Neural Bridge/Corpus/INFO 310A/labs/week-NN-<slug>.md

Same pattern as extract_lectures.py — two sections:
  (A) Full extract — README verbatim, refreshed on every run
  (B) Code review notes — preserved across runs (populated by automation-engineer
      and professor agents)

Slug derived from the title in the README (`# Week N Lab — <Title>`).

Usage:
    ~/Development/neural-bridge/.venv/bin/python scripts/corpus/extract_labs.py
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

HUSKYHUB_LABS = Path.home() / "Development" / "huskyhub" / "labs"
VAULT_LABS_DIR = (
    Path.home() / "Documents" / "Luna Master" / "Neural Bridge" / "Corpus" / "INFO 310A" / "labs"
)

TITLE_RE = re.compile(r"^#\s*Week\s+(\d+)\s+Lab\s*[—\-]+\s*(.+?)\s*$", re.MULTILINE)
LECTURE_LINE_RE = re.compile(r"^\*\*Lecture:\*\*\s*(.+?)\s*$", re.MULTILINE)

SECTION_A_HEADER = "## (A) Full extract"
SECTION_B_HEADER = "## (B) Code review and student-friendliness notes"


def slugify(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def render_section_b_placeholder() -> str:
    return (
        f"{SECTION_B_HEADER}\n\n"
        f"> Populated by `automation-engineer` (code review) and `teaching-prep` (alignment).\n"
        f"> Re-extraction does NOT overwrite this section.\n"
        f"> Last updated: _(not yet populated)_\n\n"
        f"### Code review (automation-engineer)\n\n"
        f"_Anything in the lab repo that's confusing, fragile, or would trip up a terminal-novice student._\n\n"
        f"### Alignment with the lecture (teaching-prep)\n\n"
        f"_Where the lab assumes concepts that aren't in the corresponding lecture (or vice versa)._\n\n"
        f"### Common student confusions\n\n"
        f"_Recurring sticking points from prior cohorts. Drawn from the terminal-novice handbook plus per-lab specifics._\n\n"
        f"### Real-world story hooks\n\n"
        f"_What recent breach or incident this lab maps to in the real world._\n"
    )


def split_existing(text: str) -> str:
    """Return the existing Section B (incl. header), or empty string if none."""
    idx = text.find(SECTION_B_HEADER)
    return text[idx:] if idx != -1 else ""


def extract_lab(week_dir: Path) -> Path | None:
    readme = week_dir / "README.md"
    if not readme.exists():
        print(f"  MISS {week_dir.name} — no README.md", file=sys.stderr)
        return None
    raw = readme.read_text(encoding="utf-8")

    # Parse week number + title from the H1
    title_match = TITLE_RE.search(raw)
    if not title_match:
        print(f"  FAIL {week_dir.name} — could not parse '# Week N Lab — Title' from README", file=sys.stderr)
        return None
    week_n = int(title_match.group(1))
    title = title_match.group(2).strip()

    # Parse the lecture-mapping line (optional)
    lecture_match = LECTURE_LINE_RE.search(raw)
    lecture_mapping = lecture_match.group(1).strip() if lecture_match else "_(not specified in README)_"

    # Inventory other artifacts in the week dir
    extras = sorted([p.name for p in week_dir.iterdir() if p.name != "README.md"])

    slug = slugify(title)
    target = VAULT_LABS_DIR / f"week-{week_n:02d}-{slug}.md"
    today = date.today().isoformat()

    frontmatter = (
        "---\n"
        f"type: lab-corpus\n"
        f"week: {week_n}\n"
        f"slug: week-{week_n:02d}-{slug}\n"
        f"title: {title!r}\n"
        f"source_repo: andy-herman/huskyhub\n"
        f"source_path: labs/{week_dir.name}\n"
        f"source_extracted: {today}\n"
        f"corresponding_lectures: {lecture_mapping!r}\n"
        f"extra_artifacts: {extras}\n"
        "---\n"
    )
    heading = f"\n# Week {week_n} Lab — {title}\n\n"
    section_a = (
        f"{SECTION_A_HEADER}\n\n"
        f"> Extracted from `huskyhub/labs/{week_dir.name}/README.md` on {today}. "
        f"Re-run `scripts/corpus/extract_labs.py` to refresh from the source repo.\n\n"
        f"{raw}\n"
    )
    if extras:
        section_a += f"\n### Other artifacts in this lab directory\n\n"
        for e in extras:
            section_a += f"- `{e}`\n"

    # Preserve existing Section B if present
    if target.exists():
        section_b = split_existing(target.read_text(encoding="utf-8"))
        if not section_b.strip():
            section_b = render_section_b_placeholder()
    else:
        section_b = render_section_b_placeholder()

    out = frontmatter + heading + section_a + "\n---\n\n" + section_b
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(out, encoding="utf-8")
    return target


def main() -> int:
    if not HUSKYHUB_LABS.exists():
        print(f"ERROR: {HUSKYHUB_LABS} not found. Clone andy-herman/huskyhub first.", file=sys.stderr)
        return 1
    week_dirs = sorted([p for p in HUSKYHUB_LABS.iterdir() if p.is_dir() and p.name.startswith("week-")])
    if not week_dirs:
        print(f"ERROR: no week-* directories under {HUSKYHUB_LABS}", file=sys.stderr)
        return 1

    for week_dir in week_dirs:
        target = extract_lab(week_dir)
        if target:
            print(f"  ok  {week_dir.name}  ->  {target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
