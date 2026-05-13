#!/usr/bin/env python3
"""Echo weekly profile synthesis (issue: closes the loop after #134's audit).

The `profile_accumulator.py` daemon hook captures every Andy-authored
Discord message into `~/Documents/Luna Master/Andy Profile/raw-conversations.md`
in real time. This script processes that raw corpus on a cron, invokes
`claude -p` with Echo's charter + the new messages since the last run,
and APPENDS quote-grounded observations to the structured profile files
(`voice.md`, `vocabulary.md`, `thinking-patterns.md`, `questions.md`,
`opinions.md`, `examples.md`).

The structured files are the ones that other agents read to mirror Andy's
voice; they were going stale because nothing automated the synthesis step.

Cursor: `.synthesis-cursor.txt` tracks the timestamp of the most recent
raw-conversations.md entry processed. Re-runs only process entries after
that cursor. Idempotent.

Append-only: this script never rewrites existing profile content. Each
addition lands in a dated block with a citation back to the raw message_id.
Refinements to old observations happen in explicit Discord conversations
with Echo, not in this script.

Usage:
  python3 scripts/echo/synthesize_profile.py                  # dry-run
  python3 scripts/echo/synthesize_profile.py --no-dry-run     # actually write
  python3 scripts/echo/synthesize_profile.py --since 2026-05-10  # override cursor
  python3 scripts/echo/synthesize_profile.py --force          # ignore "no new entries" guard
  python3 scripts/echo/synthesize_profile.py --no-discord     # suppress Discord summary post
  python3 scripts/echo/synthesize_profile.py --verbose

Wired as `com.andyherman.neural-bridge.echo-synthesis` launchd agent,
Sundays at 05:00 PT. Logs go to ~/Library/Logs/neural-bridge/echo-synthesis.{stdout,stderr}.log.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hooks import discord_post  # noqa: E402
from scripts.discord_bot.claude_invoke import call_claude_sync  # noqa: E402

VAULT_PROFILE_DIR = Path.home() / "Documents" / "Luna Master" / "Andy Profile"
RAW_CONVERSATIONS = VAULT_PROFILE_DIR / "raw-conversations.md"
CURSOR_PATH = VAULT_PROFILE_DIR / ".synthesis-cursor.txt"
RUN_LOG_DIR = REPO_ROOT / "docs" / "echo-synthesis"

PROFILE_FILES = (
    "voice.md",
    "thinking-patterns.md",
    "vocabulary.md",
    "questions.md",
    "opinions.md",
    "examples.md",
)

PROMPT_PATH = REPO_ROOT / "scripts" / "echo" / "prompts" / "synthesis_v1.md"
CLAUDE_TIMEOUT = 600  # 10 min — long-form synthesis with multiple file outputs

ENTRY_HEADER_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) ", re.MULTILINE)
SECTION_HEADER_RE = re.compile(r"^<<<FILE: (\S+)>>>\s*$", re.MULTILINE)

_logger = logging.getLogger("nb_echo_synthesis")


# ---------- Cursor + corpus slicing ----------


def load_cursor() -> datetime | None:
    """Load the last-processed timestamp from the cursor file, or None on first run."""
    if not CURSOR_PATH.exists():
        return None
    raw = CURSOR_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        _logger.warning("malformed cursor: %r — treating as first run", raw)
        return None


def save_cursor(timestamp: datetime) -> None:
    CURSOR_PATH.write_text(timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8")


def slice_raw_conversations(since: datetime | None) -> tuple[str, datetime | None]:
    """Read raw-conversations.md and return only entries authored after `since`.

    Returns (sliced_text, latest_timestamp_seen). latest_timestamp_seen is None
    if no entries match (caller should treat as no-op).

    The entries are delimited by `### YYYY-MM-DDTHH:MM:SSZ — #channel` headers.
    We walk the headers, find the first one whose timestamp is > since (or all
    of them on first run), and emit everything from that header through the
    end of the file.
    """
    if not RAW_CONVERSATIONS.exists():
        return "", None

    text = RAW_CONVERSATIONS.read_text(encoding="utf-8")
    headers = list(ENTRY_HEADER_RE.finditer(text))
    if not headers:
        return "", None

    # Build (timestamp, start_offset) pairs.
    entries = []
    for m in headers:
        try:
            ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        entries.append((ts, m.start()))

    if not entries:
        return "", None

    if since is None:
        first_idx = 0
    else:
        first_idx = None
        for i, (ts, _) in enumerate(entries):
            if ts > since:
                first_idx = i
                break
        if first_idx is None:
            return "", None

    slice_start = entries[first_idx][1]
    latest_ts = entries[-1][0]
    return text[slice_start:].rstrip() + "\n", latest_ts


# ---------- Profile file loading ----------


def load_existing_profile() -> str:
    """Load the current contents of every structured profile file as a
    single concatenated block for the prompt's `<existing-profile>` slot.

    Files that don't yet exist (first run) are skipped silently; the
    synthesis treats their addition as creating a new file via append.
    """
    sections = []
    for name in PROFILE_FILES:
        path = VAULT_PROFILE_DIR / name
        if path.exists():
            body = path.read_text(encoding="utf-8")
        else:
            body = "(file does not exist yet; synthesis would create it on first non-empty addition)"
        sections.append(f"=== {name} ===\n{body}")
    return "\n\n".join(sections)


# ---------- Prompt build + claude invocation ----------


def build_prompt(new_messages: str, existing_profile: str) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{new_messages}", new_messages).replace(
        "{existing_profile}", existing_profile
    )


def invoke_synthesis(prompt: str) -> tuple[bool, str, str]:
    """Call `claude -p` with the synthesis prompt. Returns (ok, stdout, err)."""
    return call_claude_sync(prompt, timeout=CLAUDE_TIMEOUT)


# ---------- Response parsing + append ----------


@dataclass
class SynthesisOutput:
    """Parsed claude response. additions maps filename to either the
    new-content block (string) or None (for NO-ADDITIONS). trailing_note
    is anything claude wrote after `<<<END>>>` (often a one-line summary
    of why nothing landed)."""
    additions: dict[str, str | None]
    trailing_note: str
    raw_response: str


def parse_response(response: str) -> SynthesisOutput:
    """Split claude's response on the `<<<FILE: ...>>>` markers.

    Tolerant of leading/trailing whitespace, code-fence wrapping, and
    minor formatting drift. Anything claude emits before the first
    `<<<FILE:` is treated as a preamble and dropped (with a log warning
    in verbose mode); anything after the `<<<END>>>` marker is captured
    as `trailing_note`.
    """
    # Strip code fence wrapping if claude added one defensively.
    body = response.strip()
    if body.startswith("```"):
        lines = body.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        body = "\n".join(lines)

    # Split on FILE markers.
    headers = list(SECTION_HEADER_RE.finditer(body))
    if not headers:
        _logger.warning("no <<<FILE:>>> markers found in claude response")
        return SynthesisOutput(additions={}, trailing_note=body, raw_response=response)

    additions: dict[str, str | None] = {}
    for i, h in enumerate(headers):
        filename = h.group(1)
        content_start = h.end()
        if i + 1 < len(headers):
            content_end = headers[i + 1].start()
        else:
            # Last section runs to <<<END>>> or end of body.
            end_marker = body.find("<<<END>>>", content_start)
            content_end = end_marker if end_marker != -1 else len(body)
        chunk = body[content_start:content_end].strip()
        if chunk == "NO-ADDITIONS" or not chunk:
            additions[filename] = None
        else:
            additions[filename] = chunk

    # Extract trailing note after <<<END>>>.
    end_idx = body.find("<<<END>>>")
    trailing_note = ""
    if end_idx != -1:
        trailing_note = body[end_idx + len("<<<END>>>"):].strip()

    return SynthesisOutput(additions=additions, trailing_note=trailing_note, raw_response=response)


def append_to_profile_file(filename: str, content: str, cursor_label: str) -> None:
    """Append a dated synthesis block to the profile file.

    Each appended block is wrapped with a header so future readers (and
    Echo herself) can tell scripted additions apart from interactive ones."""
    path = VAULT_PROFILE_DIR / filename
    header = f"\n\n## Synthesis pass {cursor_label}\n\n"
    payload = header + content.rstrip() + "\n"

    if not path.exists():
        # First-time file creation: write a minimal preamble so the file
        # has a sensible top.
        preamble = f"# {filename.removesuffix('.md').replace('-', ' ').title()}\n\nQuote-grounded observations of Andy's writing, maintained by Echo.\n"
        path.write_text(preamble + payload, encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write(payload)


# ---------- Run log + Discord summary ----------


def write_run_log(
    output: SynthesisOutput, *, started_at: datetime, cursor_advanced_to: datetime, dry_run: bool
) -> Path:
    """Write a per-run log to docs/echo-synthesis/<date>.md so the synthesis
    history is auditable. Includes the raw claude response for replay."""
    RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    fname = started_at.strftime("%Y-%m-%dT%H%M%SZ") + ".md"
    path = RUN_LOG_DIR / fname

    additions_summary_lines = []
    for name in PROFILE_FILES:
        v = output.additions.get(name)
        if v is None:
            additions_summary_lines.append(f"- `{name}`: no additions")
        else:
            additions_summary_lines.append(f"- `{name}`: {len(v.splitlines())} line(s) added")
    additions_summary = "\n".join(additions_summary_lines)

    body = f"""---
type: echo-synthesis-run
started_at: {started_at.strftime('%Y-%m-%dT%H:%M:%SZ')}
cursor_advanced_to: {cursor_advanced_to.strftime('%Y-%m-%dT%H:%M:%SZ')}
dry_run: {str(dry_run).lower()}
---

# Echo synthesis run {started_at.strftime('%Y-%m-%d %H:%M:%SZ')}

## Additions summary

{additions_summary}

{('## Trailing note\n\n' + output.trailing_note) if output.trailing_note else ''}

## Raw claude response

<details>
<summary>Click to expand</summary>

```
{output.raw_response}
```

</details>
"""
    path.write_text(body, encoding="utf-8")
    return path


def post_discord_summary(output: SynthesisOutput, *, dry_run: bool) -> bool:
    if dry_run:
        return False
    nonzero = [(name, v) for name, v in output.additions.items() if v]
    if not nonzero:
        msg = (
            "🪞 **Echo synthesis pass — no new observations**\n\n"
            "Processed the latest window of Discord activity; nothing in the new messages "
            "supported a quote-grounded addition to the structured profile this run.\n\n"
            f"_{output.trailing_note}_" if output.trailing_note else ""
        )
    else:
        lines = [f"🪞 **Echo synthesis pass — {len(nonzero)} file(s) updated**", ""]
        for name, content in nonzero:
            lines.append(f"• `{name}`: {len(content.splitlines())} new lines")
        lines.append("")
        lines.append("Profile files refreshed in `~/Documents/Luna Master/Andy Profile/`.")
        msg = "\n".join(lines)
    return discord_post.send(msg)


# ---------- Main ----------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Echo weekly profile synthesis pass.")
    parser.add_argument("--no-dry-run", action="store_true", help="Actually write to profile files + post to Discord. Default is dry-run.")
    parser.add_argument("--since", type=str, default=None, help="Override cursor with an ISO timestamp (YYYY-MM-DDTHH:MM:SSZ).")
    parser.add_argument("--force", action="store_true", help="Run even if there are no new entries since the cursor.")
    parser.add_argument("--no-discord", action="store_true", help="Skip the Discord summary post.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    dry_run = not args.no_dry_run
    started_at = datetime.now(timezone.utc)

    # 1. Resolve cursor.
    if args.since:
        try:
            cursor = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError:
            _logger.error("invalid --since timestamp: %r", args.since)
            return 2
        _logger.info("using --since override: %s", cursor.isoformat())
    else:
        cursor = load_cursor()
        if cursor:
            _logger.info("cursor: %s (last synthesis processed up to here)", cursor.isoformat())
        else:
            _logger.info("no cursor yet (first run); processing entire raw-conversations.md")

    # 2. Slice raw conversations.
    new_messages, latest_ts = slice_raw_conversations(cursor)
    if not new_messages or latest_ts is None:
        if args.force:
            _logger.info("no new entries since cursor, but --force is set; running anyway with empty corpus")
            new_messages = "(no new messages in this window)"
            latest_ts = cursor or started_at
        else:
            _logger.info("no new entries since cursor — exiting (use --force to run anyway)")
            return 0

    _logger.info("new corpus size: %d chars; latest entry: %s", len(new_messages), latest_ts.isoformat())

    # 3. Load existing profile + build prompt.
    existing = load_existing_profile()
    prompt = build_prompt(new_messages=new_messages, existing_profile=existing)
    _logger.info("synthesis prompt: %d chars total", len(prompt))

    if dry_run:
        _logger.info("DRY-RUN: would invoke claude -p with %d-char prompt and write to %d profile files", len(prompt), len(PROFILE_FILES))
        return 0

    # 4. Invoke claude.
    ok, response, err = invoke_synthesis(prompt)
    if not ok:
        _logger.error("claude invocation failed: %s", err)
        if not args.no_discord:
            discord_post.send(f"⚠️ Echo synthesis pass failed at claude invocation: `{err}`")
        return 1

    _logger.info("claude returned %d chars", len(response))

    # 5. Parse + apply.
    output = parse_response(response)
    cursor_label = started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    additions_applied = 0
    for name in PROFILE_FILES:
        v = output.additions.get(name)
        if v:
            append_to_profile_file(name, v, cursor_label=cursor_label)
            additions_applied += 1
            _logger.info("appended to %s (%d lines)", name, len(v.splitlines()))

    # 6. Update cursor + run log.
    save_cursor(latest_ts)
    log_path = write_run_log(output, started_at=started_at, cursor_advanced_to=latest_ts, dry_run=dry_run)
    _logger.info("run log written: %s", log_path)

    # 7. Discord summary.
    if not args.no_discord:
        posted = post_discord_summary(output, dry_run=dry_run)
        if not posted:
            _logger.warning("discord summary post returned False (webhook missing or HTTP error)")

    _logger.info("done. %d profile files updated; cursor advanced to %s", additions_applied, latest_ts.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
