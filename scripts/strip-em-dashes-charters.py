#!/usr/bin/env python3
"""Em-dash sweep adapted from /tmp/strip-em-dashes-v2.py for the agent
charter directory. Same six rules in the same specificity order; only
the target path changes.

1. `[Link](url) — desc` after a markdown link, colon
2. Bullet-list / YAML-scalar first-dash that introduces explanation, colon
3. Beat-pause before short final lowercase phrase, period plus capitalized
4. Body dash + uppercase continuation, period
5. Body dash + lowercase continuation, comma
6. Numeric range N–M between digits, hyphen
"""
from pathlib import Path
import re

ROOT = Path("/Users/andyherman/Development/neural-bridge/plugins/neural-bridge-core/agents")

LINK_DASH = re.compile(r"\) [—–] ")

BULLET_DASH = re.compile(
    r"(?m)^(\s*[-*] |role_tagline:\s+|does_not_own:\s+|description:\s+)([A-Z][^—\n:]*?) [—–] "
)

BEAT_PAUSE = re.compile(r" [—–] ([a-z][a-zA-Z ]{0,15}\.)(?=\s|$)")
DASH_UPPER = re.compile(r" [—–] (?=[A-Z])")
DASH_OTHER = re.compile(r" [—–] ")
NUMERIC_RANGE = re.compile(r"(\d)–(\d)")


def beat_pause_sub(match: re.Match) -> str:
    phrase = match.group(1)
    return f". {phrase[0].upper()}{phrase[1:]}"


def process(text: str) -> tuple[str, int]:
    count = 0
    new = text
    for pattern, replacement in [
        (LINK_DASH, "): "),
        (BULLET_DASH, r"\1\2: "),
        (BEAT_PAUSE, beat_pause_sub),
        (DASH_UPPER, ". "),
        (DASH_OTHER, ", "),
        (NUMERIC_RANGE, r"\1-\2"),
    ]:
        new, n = pattern.subn(replacement, new)
        count += n
    return new, count


def main() -> None:
    total = 0
    files_touched = 0
    for path in sorted(ROOT.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if "—" not in text and "–" not in text:
            continue
        new_text, n = process(text)
        leftover_em = new_text.count("—")
        leftover_en = new_text.count("–")
        if leftover_em or leftover_en:
            print(f"  ! {path.name}: {leftover_em} em + {leftover_en} en remain after sweep")
        if n > 0:
            path.write_text(new_text, encoding="utf-8")
            files_touched += 1
            total += n
            print(f"  {path.name}: {n}")
    print(f"\nDone. {total} replacement(s) across {files_touched} file(s).")


if __name__ == "__main__":
    main()
