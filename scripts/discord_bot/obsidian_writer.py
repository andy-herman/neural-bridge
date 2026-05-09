"""Mirror Neural Bridge issues into the Obsidian vault.

Per-issue note at:
  <vault-root>/Neural Bridge/Kanban/Issues/Issue <N> - <safe-title>.md

The writer:
- Sanitizes the filename (illegal chars + length cap)
- Resolves the target path within the vault root (no path traversal)
- Writes atomically via tempfile + os.replace()
- Updates an existing note if it exists rather than overwriting blindly:
  the body's "Status" section is amended; everything else stays.

Pattern (not code) lifted from
agent-kanban-orchestrator/src/runner/obsidian-writer.ts: the
`safeMarkdownFileName` and `resolveWithinVault` traversal guards in
particular.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_VAULT_ROOT = Path.home() / "Documents" / "Luna Master"
KANBAN_SUBPATH = Path("Neural Bridge") / "Kanban" / "Issues"

# Filename sanitization: replace anything that's not a safe Mac/cross-platform
# filename char with a hyphen. Keep alnum, space, hyphen, underscore, period.
UNSAFE_RE = re.compile(r"[^\w\-. ]+", re.UNICODE)
COLLAPSE_DASH_RE = re.compile(r"-{2,}")
MAX_FILENAME_LEN = 100  # excluding extension


def safe_markdown_file_name(title: str, *, issue_number: int) -> str:
    """Build a safe filename like 'Issue 12 - Some Title.md'."""
    sanitized = UNSAFE_RE.sub("-", title.strip())
    sanitized = COLLAPSE_DASH_RE.sub("-", sanitized).strip("-. ")
    if not sanitized:
        sanitized = "untitled"
    base = f"Issue {issue_number} - {sanitized}"
    if len(base) > MAX_FILENAME_LEN:
        base = base[:MAX_FILENAME_LEN].rstrip("-. ")
    return base + ".md"


def resolve_within_vault(vault_root: Path, target: Path) -> Path:
    """Resolve `target` under `vault_root`. Raises if it escapes the root.

    Defends against path traversal via `..` or absolute paths in the
    issue title or sanitized filename.
    """
    vault_root = vault_root.resolve()
    resolved = (vault_root / target).resolve()
    try:
        resolved.relative_to(vault_root)
    except ValueError as exc:
        raise ValueError(f"target escapes vault root: {target} -> {resolved}") from exc
    return resolved


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_initial_note(
    *,
    issue_number: int,
    title: str,
    issue_url: str,
    source_request: str,
    closure_criteria: Optional[str],
    initial_owner: str,
    discord_thread_url: Optional[str],
) -> str:
    now = utc_iso()
    closure_md = closure_criteria or "_(not captured during intake; senior-pm to follow up)_"
    thread_md = f"- Discord thread: {discord_thread_url}" if discord_thread_url else ""
    return (
        f"---\n"
        f"type: kanban-issue\n"
        f"project: Neural Bridge\n"
        f"source: github-issue\n"
        f"source_issue: {issue_number}\n"
        f"source_url: {issue_url}\n"
        f"initial_owner: {initial_owner}\n"
        f"status: open\n"
        f"created: {now}\n"
        f"updated: {now}\n"
        f"tags: [neural-bridge, kanban, issue-{issue_number}]\n"
        f"---\n\n"
        f"# Issue {issue_number} - {title}\n\n"
        f"## Source request\n\n"
        f"> {source_request}\n\n"
        f"## Closure criteria\n\n"
        f"{closure_md}\n\n"
        f"## Routing\n\n"
        f"- Initial owner: **{initial_owner}**\n"
        f"- Recommended specialist: _(senior-pm to assign)_\n"
        f"{thread_md}\n\n"
        f"## Status\n\n"
        f"- {now} — issue opened\n"
    )


def append_status_line(note_text: str, line: str) -> str:
    """Append a bullet to the `## Status` section. If the section doesn't
    exist, create one at the end. Updates the `updated:` frontmatter.
    """
    now = utc_iso()
    bullet = f"- {now} — {line}\n"

    # Update frontmatter `updated:` field
    note_text = re.sub(
        r"^updated: .*$",
        f"updated: {now}",
        note_text,
        count=1,
        flags=re.MULTILINE,
    )

    # If `## Status` exists, append at end of section
    status_match = re.search(r"^## Status\s*\n", note_text, re.MULTILINE)
    if status_match:
        # Find next ^## or end of file
        start = status_match.end()
        next_h2 = re.search(r"^## ", note_text[start:], re.MULTILINE)
        if next_h2:
            insert_at = start + next_h2.start()
            return note_text[:insert_at].rstrip() + "\n" + bullet + "\n" + note_text[insert_at:]
        else:
            return note_text.rstrip() + "\n" + bullet
    else:
        return note_text.rstrip() + "\n\n## Status\n\n" + bullet


def update_status_to(note_text: str, status: str) -> str:
    """Update the frontmatter `status:` field."""
    return re.sub(
        r"^status: .*$",
        f"status: {status}",
        note_text,
        count=1,
        flags=re.MULTILINE,
    )


class ObsidianWriter:
    def __init__(self, vault_root: Path = DEFAULT_VAULT_ROOT):
        self.vault_root = vault_root.resolve()

    def _path_for(self, issue_number: int, title: str) -> Path:
        filename = safe_markdown_file_name(title, issue_number=issue_number)
        rel = KANBAN_SUBPATH / filename
        return resolve_within_vault(self.vault_root, rel)

    def _existing_path_for(self, issue_number: int) -> Optional[Path]:
        """Find an existing note for this issue regardless of title drift."""
        target_dir = self.vault_root / KANBAN_SUBPATH
        if not target_dir.exists():
            return None
        prefix = f"Issue {issue_number} - "
        for p in target_dir.glob("*.md"):
            if p.name.startswith(prefix):
                return p
        return None

    def write_initial_note(
        self,
        *,
        issue_number: int,
        title: str,
        issue_url: str,
        source_request: str,
        closure_criteria: Optional[str],
        initial_owner: str = "senior-pm",
        discord_thread_url: Optional[str] = None,
    ) -> Path:
        """Create a new note. If a note exists already (e.g., re-run), skip
        and return the existing path — never overwrite a manually-edited note.
        """
        existing = self._existing_path_for(issue_number)
        if existing is not None:
            return existing

        target = self._path_for(issue_number, title)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = render_initial_note(
            issue_number=issue_number,
            title=title,
            issue_url=issue_url,
            source_request=source_request,
            closure_criteria=closure_criteria,
            initial_owner=initial_owner,
            discord_thread_url=discord_thread_url,
        )
        _atomic_write(target, body)
        return target

    def append_status(self, *, issue_number: int, line: str, new_status: Optional[str] = None) -> Optional[Path]:
        """Append a Status bullet to the existing note. Optionally update
        the frontmatter status: field. Returns the note path, or None if
        no existing note for this issue.
        """
        path = self._existing_path_for(issue_number)
        if path is None:
            return None
        text = path.read_text(encoding="utf-8")
        text = append_status_line(text, line)
        if new_status is not None:
            text = update_status_to(text, new_status)
        _atomic_write(path, text)
        return path


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, path)
