#!/usr/bin/env python3
"""Em-dash sweep for agent-governing markdown. Six rules in specificity order:

1. `[Link](url) — desc` after a markdown link, colon
2. Bullet-list / YAML-scalar first-dash that introduces explanation, colon
3. Beat-pause before short final lowercase phrase, period plus capitalized
4. Body dash + uppercase continuation, period
5. Body dash + lowercase continuation, comma
6. Numeric range N–M between digits, hyphen

Usage:
    strip-em-dashes-charters.py [ROOT] [--recursive] [--exclude NAME ...]

ROOT defaults to the Neural Bridge agent charters. Pass a different root to
sweep another agent's governing files, e.g. Yor's brain at
`~/Documents/Luna Master/Agents/Hermes`.

Why this matters beyond style: Andy's no-em-dash rule applies to what these
agents WRITE, and the rule is stated inside the very documents they read. A
charter that bans em-dashes while using nine of them is a context that
contradicts its own instruction. `--exclude` exists for the one legitimate
case: the line that states the rule has to be able to show the character.
"""
from pathlib import Path
import argparse
import re

DEFAULT_ROOT = Path("/Users/andyherman/Development/neural-bridge/plugins/neural-bridge-core/agents")

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
    parser = argparse.ArgumentParser(description="Strip em-dashes from agent markdown.")
    parser.add_argument("root", nargs="?", default=str(DEFAULT_ROOT),
                        help="directory to sweep (default: NB agent charters)")
    parser.add_argument("--recursive", action="store_true",
                        help="include markdown in subdirectories")
    parser.add_argument("--exclude", nargs="*", default=[],
                        help="file names to skip (e.g. the file that states the rule)")
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    excluded = set(args.exclude)

    total = 0
    files_touched = 0
    paths = sorted(root.rglob("*.md") if args.recursive else root.glob("*.md"))
    for path in paths:
        if path.name in excluded:
            print(f"  (skipped {path.name})")
            continue
        text = path.read_text(encoding="utf-8")
        if "—" not in text and "–" not in text:
            continue
        new_text, n = process(text)
        leftover_em = new_text.count("—")
        leftover_en = new_text.count("–")
        if leftover_em or leftover_en:
            print(f"  ! {path.relative_to(root)}: {leftover_em} em + {leftover_en} en remain after sweep")
        if n > 0:
            path.write_text(new_text, encoding="utf-8")
            files_touched += 1
            total += n
            print(f"  {path.relative_to(root)}: {n}")
    print(f"\nDone. {total} replacement(s) across {files_touched} file(s).")


if __name__ == "__main__":
    main()
