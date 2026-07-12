"""Thin git shell-out + pure diff parsers shared by worktree/verify/pr.

`_git` mirrors the idiom in discord_bot/pr_proposals.py: capture output, never
raise, return (ok, text). The parsers are pure so the size/integrity gates can
be unit-tested without a real repo.
"""

from __future__ import annotations

import subprocess as sp
from pathlib import Path


def git(cwd: Path, args: list[str], timeout: int = 120) -> tuple[bool, str]:
    """Run `git <args>` in `cwd`. Returns (ok, stdout-or-error). Never raises."""
    try:
        proc = sp.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=sp.DEVNULL,
        )
    except (sp.TimeoutExpired, FileNotFoundError) as exc:
        return False, type(exc).__name__
    if proc.returncode != 0:
        snippet = (proc.stderr or proc.stdout or "")[:300].replace("\n", " ")
        return False, f"git_exit_{proc.returncode}: {snippet}"
    return True, proc.stdout.strip()


def parse_numstat(output: str) -> tuple[int, int, int]:
    """Parse `git diff --numstat` into (files, insertions, deletions).

    Each line is `<added>\\t<deleted>\\t<path>`. Binary files show `-` for the
    counts; those contribute to the file count but not the line totals.
    """
    files = 0
    insertions = 0
    deletions = 0
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        added, deleted = parts[0], parts[1]
        if added != "-":
            try:
                insertions += int(added)
            except ValueError:
                pass
        if deleted != "-":
            try:
                deletions += int(deleted)
            except ValueError:
                pass
    return files, insertions, deletions


def parse_name_status(output: str) -> list[tuple[str, str]]:
    """Parse `git diff --name-status` into [(status, path), ...].

    Status is a single letter: A(dded) M(odified) D(eleted) R(enamed) etc.
    For renames (`R100\\told\\tnew`) we report the NEW path with status 'R'.
    """
    out: list[tuple[str, str]] = []
    for line in output.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1].upper()
        path = parts[-1]  # new path for renames, the path otherwise
        out.append((status, path))
    return out
