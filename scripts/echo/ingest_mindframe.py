"""Echo ingestion: MindFrame Discord conversation logs.

Reads MindFrame's local Discord conversation logs (written by
`mindframe/src/bot/conversation-log.ts` and pre-redacted via
`mindframe/src/security/redaction.ts`) and appends Andy-authored turns to
`~/Documents/Luna Master/Andy Profile/mindframe-conversations.md`.

MindFrame layout (source: mindframe/src/bot/conversation-log.ts):
  <mindframe-repo>/logs/conversations/YYYY-MM-DD/<channel-slug>-<channel-id>.md

Per-file turn format:
  ## 2026-05-13T17:33:13.123Z - <author display name>

  <message content, max 12000 chars, already redacted upstream>

Andy's voice profile benefits from his work-side conversations as well as
his personal-substrate ones; this script gives Echo a third corpus source
alongside `raw-conversations.md` (Neural Bridge Discord) and
`claude-transcripts.md` (Claude Code sessions).

## Cross-machine reality

MindFrame is Windows-deployed per its README. The `logs/` directory in
that repo is gitignored. So unless Andy syncs the conversation logs from
the Windows machine to this Mac (Dropbox, OneDrive, Syncthing, rsync over
Tailscale, etc.), the source path won't exist locally. This script
no-ops cleanly when that's the case so the daily cron doesn't error
every day until the sync is wired.

Configurable source via `NB_MINDFRAME_LOGS_DIR` env var; default is
`~/Development/mindframe/logs/conversations`.

## Filter rules

- Only files under `YYYY-MM-DD/` subdirectories of the resolved source
- Per file, only `## <ISO timestamp> - <author>` turn headers
- Author allowlist filter: only Andy-authored turns get appended; turns
  from bots, other humans, or unknown authors are counted and logged
  but skipped. Allowlist is configurable via
  `.mindframe-author-allowlist.txt` in the vault profile dir.
- Empty content is skipped
- Redaction happens upstream in MindFrame; do NOT add another redaction
  pass here (would double-process and risk corrupting markers)

## Usage

  python -m scripts.echo.ingest_mindframe              # actual ingest
  python -m scripts.echo.ingest_mindframe --dry-run    # plan only
  python -m scripts.echo.ingest_mindframe --verbose    # per-day/per-file counts
  NB_MINDFRAME_LOGS_DIR=/path/to/logs python -m scripts.echo.ingest_mindframe
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VAULT_PROFILE_DIR = Path.home() / "Documents" / "Luna Master" / "Andy Profile"
OUTPUT_PATH = VAULT_PROFILE_DIR / "mindframe-conversations.md"
DEDUPE_SIDECAR = VAULT_PROFILE_DIR / ".mindframe-ingested.txt"
ALLOWLIST_PATH = VAULT_PROFILE_DIR / ".mindframe-author-allowlist.txt"

ENV_VAR_OVERRIDE = "NB_MINDFRAME_LOGS_DIR"
DEFAULT_LOGS_DIR = Path.home() / "Development" / "mindframe" / "logs" / "conversations"

# Match MindFrame's turn headers: ## <ISO timestamp> - <author>
# The ISO timestamp has optional fractional seconds (Discord adds them).
TURN_HEADER_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s*-\s*(.+?)\s*$",
    re.MULTILINE,
)

# Daily directory pattern: YYYY-MM-DD
DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Default allowlist seed. Andy's MindFrame Discord display name may differ;
# the implementer should spot-check after first install. The file at
# ALLOWLIST_PATH is the source of truth at runtime; this is just the seed.
DEFAULT_ALLOWLIST_SEED = [
    "Andy Herman",
    "andy-herman",
    "andyherman",
    "Andy",
]

# Cap any single turn at this length; longer pastes get a truncation marker.
MAX_CONTENT_CHARS = 6000


@dataclass
class Turn:
    """One conversation turn extracted from a MindFrame log file."""
    timestamp: str           # ISO string as-written by MindFrame
    author: str              # display name string
    channel_label: str       # e.g., "general" (slug before the channel-id)
    content: str             # already-redacted message body
    source_path: Path        # absolute path to the source log file
    source_line: int         # 1-based line number where the header was found

    def dedupe_key(self) -> str:
        """File-path + line-number granularity, matching the existing
        ingest_claude_transcripts.py convention."""
        return f"{self.source_path.name}:{self.source_line}"


# ---------- Source resolution ----------


def resolve_logs_dir() -> Path:
    """Resolve the MindFrame logs source path. Env var wins; otherwise default."""
    raw = os.environ.get(ENV_VAR_OVERRIDE)
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_LOGS_DIR


# ---------- Author allowlist ----------


def load_allowlist() -> set[str]:
    """Load allowed author names or seed it on first run.

    Matching is case-insensitive after normalization. The file at
    ALLOWLIST_PATH is the runtime source of truth; the seed gets written
    once and Andy can edit freely afterward.
    """
    if not ALLOWLIST_PATH.exists():
        VAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        body = (
            "# Echo MindFrame ingestion: author allowlist.\n"
            "# One display name per line (case-insensitive). Andy-authored turns\n"
            "# pass the filter; all other authors are skipped (and counted in the\n"
            "# run log). Lines starting with # are comments. Edit freely.\n"
            "#\n"
            "# Default allowlist (verify against actual MindFrame log samples):\n"
            + "\n".join(DEFAULT_ALLOWLIST_SEED)
            + "\n"
        )
        ALLOWLIST_PATH.write_text(body, encoding="utf-8")
        print(f"seeded MindFrame author allowlist at {ALLOWLIST_PATH}", file=sys.stderr)

    lines = ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines()
    return {line.strip().lower() for line in lines if line.strip() and not line.startswith("#")}


def author_in_allowlist(author: str, allowlist: set[str]) -> bool:
    return author.strip().lower() in allowlist


# ---------- Dedupe sidecar ----------


def load_dedupe_sidecar() -> set[str]:
    if not DEDUPE_SIDECAR.exists():
        return set()
    return {
        line.strip()
        for line in DEDUPE_SIDECAR.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_dedupe_sidecar(keys: set[str]) -> None:
    VAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    DEDUPE_SIDECAR.write_text("\n".join(sorted(keys)) + "\n", encoding="utf-8")


# ---------- Log file parsing ----------


def extract_channel_label(filename: str) -> str:
    """`logs/conversations/2026-05-13/general-1503151234567890.md` -> `general`.

    MindFrame's path format is `<channel-slug>-<channel-id>.md` where the
    channel-id is a Discord snowflake (16-20 digits). Strip the trailing
    `-<snowflake>` to get the channel slug.
    """
    stem = Path(filename).stem
    m = re.match(r"^(.*?)-(\d{16,20})$", stem)
    if m:
        return m.group(1) or "unknown"
    return stem


def extract_turns_from_file(
    log_file: Path, allowlist: set[str], seen: set[str]
) -> tuple[list[Turn], list[Turn]]:
    """Parse one MindFrame log file. Returns (kept_turns, skipped_turns).

    kept_turns are Andy-authored and not previously deduped. skipped_turns
    represent author-filter rejects (logged for visibility, not appended).
    """
    text = log_file.read_text(encoding="utf-8")
    matches = list(TURN_HEADER_RE.finditer(text))
    if not matches:
        return [], []

    channel_label = extract_channel_label(log_file.name)
    kept: list[Turn] = []
    skipped: list[Turn] = []

    for i, m in enumerate(matches):
        timestamp = m.group(1)
        author = m.group(2)
        line_no = text[: m.start()].count("\n") + 1

        content_start = m.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[content_start:content_end].strip()

        if not content:
            continue

        if len(content) > MAX_CONTENT_CHARS:
            content = content[: MAX_CONTENT_CHARS - 20].rstrip() + "\n... truncated ..."

        turn = Turn(
            timestamp=timestamp,
            author=author,
            channel_label=channel_label,
            content=content,
            source_path=log_file,
            source_line=line_no,
        )

        if turn.dedupe_key() in seen:
            continue

        if author_in_allowlist(author, allowlist):
            kept.append(turn)
        else:
            skipped.append(turn)

    return kept, skipped


def walk_source(logs_dir: Path) -> list[Path]:
    """Return all `YYYY-MM-DD/*.md` files under logs_dir, sorted (oldest first)."""
    out: list[Path] = []
    for day_dir in sorted(logs_dir.iterdir()):
        if not day_dir.is_dir() or not DAY_DIR_RE.match(day_dir.name):
            continue
        for log_file in sorted(day_dir.iterdir()):
            if log_file.is_file() and log_file.suffix == ".md":
                out.append(log_file)
    return out


# ---------- Output ----------


def normalize_timestamp_to_iso(ts: str) -> str:
    """MindFrame writes fractional seconds (`...T17:33:13.123Z`); strip them
    for consistency with `raw-conversations.md` headers which use plain
    second-precision."""
    # Match the date prefix up to the seconds, drop fractional + Z.
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", ts)
    if m:
        return m.group(1) + "Z"
    return ts


def format_turn_block(turn: Turn) -> str:
    """Match the `raw-conversations.md` format so synthesize_profile.py can
    eventually read mindframe-conversations.md the same way it reads the
    Neural Bridge captures."""
    iso = normalize_timestamp_to_iso(turn.timestamp)
    body = "\n".join(f"> {line}" for line in turn.content.splitlines())
    return (
        f"### {iso} — #{turn.channel_label}\n"
        f"<!-- mindframe source: {turn.source_path.name}:{turn.source_line} -->\n\n"
        f"{body}\n"
    )


def append_turns(turns: list[Turn]) -> None:
    VAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    if not OUTPUT_PATH.exists():
        preamble = (
            "# MindFrame conversations\n\n"
            "Andy-authored Discord turns ingested from MindFrame (Andy's work-side\n"
            "control plane). Pre-redacted upstream by `redactSensitiveContent` in\n"
            "the MindFrame bot. Captured by `scripts/echo/ingest_mindframe.py`.\n"
        )
        OUTPUT_PATH.write_text(preamble + "\n", encoding="utf-8")
    with OUTPUT_PATH.open("a", encoding="utf-8") as f:
        for t in turns:
            f.write("\n" + format_turn_block(t))


# ---------- Main ----------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest MindFrame Discord conversation logs into Echo's corpus.")
    parser.add_argument("--dry-run", action="store_true", help="Plan only; don't write to mindframe-conversations.md or dedupe sidecar.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Per-day/per-file counts and progress.")
    args = parser.parse_args(argv)

    logs_dir = resolve_logs_dir()
    if not logs_dir.exists():
        # Clean no-op so the daily cron doesn't error before sync is wired.
        print(
            f"MindFrame logs dir not present at {logs_dir}. "
            f"Configure {ENV_VAR_OVERRIDE} env var or sync the logs to disk. "
            f"Exiting cleanly.",
            file=sys.stderr,
        )
        return 0

    print(f"source: {logs_dir}", file=sys.stderr)

    allowlist = load_allowlist()
    if not allowlist:
        print(f"error: allowlist at {ALLOWLIST_PATH} is empty. Add at least one display name.", file=sys.stderr)
        return 1
    if args.verbose:
        print(f"author allowlist ({len(allowlist)} name(s)): {sorted(allowlist)}", file=sys.stderr)

    seen = load_dedupe_sidecar()
    print(f"dedupe sidecar: {len(seen)} previously-ingested key(s)", file=sys.stderr)

    files = walk_source(logs_dir)
    if not files:
        print("no .md log files found in source", file=sys.stderr)
        return 0

    new_turns: list[Turn] = []
    skipped_turns: list[Turn] = []
    for log_file in files:
        kept, skipped = extract_turns_from_file(log_file, allowlist, seen)
        new_turns.extend(kept)
        skipped_turns.extend(skipped)
        if args.verbose:
            print(f"  {log_file.name}: kept {len(kept)}, skipped {len(skipped)}", file=sys.stderr)

    print(f"total: kept {len(new_turns)} Andy-authored turn(s); skipped {len(skipped_turns)} other-author turn(s)", file=sys.stderr)

    if skipped_turns and args.verbose:
        # Surface unknown-author drift so Andy can spot display-name changes.
        from collections import Counter
        author_counts = Counter(t.author for t in skipped_turns)
        print("  skipped-author breakdown:", file=sys.stderr)
        for author, count in author_counts.most_common(10):
            print(f"    {count:>4}  {author!r}", file=sys.stderr)

    if not new_turns:
        print("nothing new to ingest", file=sys.stderr)
        return 0

    if args.dry_run:
        print(f"--dry-run: would append {len(new_turns)} turn(s) to {OUTPUT_PATH}", file=sys.stderr)
        return 0

    append_turns(new_turns)
    new_keys = {t.dedupe_key() for t in new_turns}
    save_dedupe_sidecar(seen | new_keys)
    print(f"appended {len(new_turns)} turn(s) to {OUTPUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
