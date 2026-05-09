"""GitHub issue creation via the gh CLI.

Shells out to `gh` rather than reimplementing the REST client, because:
1. `gh` is already authenticated on Andy's Mac with the right scopes.
2. We don't introduce a new HTTP/auth path for the daemon.
3. The CLI handles token refresh and rate limit reporting.

For PR-I-A only `create_issue` is needed. Hand-off / status transition
helpers (add_label, comment, close) ship in PR-K.

`gh issue create` returns the new issue's URL on stdout. We parse the
trailing `/<number>` to extract the issue number.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass

DEFAULT_TIMEOUT = 30
ISSUE_URL_RE = re.compile(r"/issues/(\d+)\s*$")


@dataclass
class CreateIssueResult:
    ok: bool
    issue_number: int | None
    issue_url: str | None
    error: str | None


def create_issue_sync(
    *,
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> CreateIssueResult:
    """Synchronous gh issue create. Returns parsed result; never raises."""
    args = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels or []:
        args.extend(["--label", label])

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return CreateIssueResult(ok=False, issue_number=None, issue_url=None, error="timeout")
    except FileNotFoundError:
        return CreateIssueResult(ok=False, issue_number=None, issue_url=None, error="gh_cli_not_found")

    if proc.returncode != 0:
        snippet = (proc.stderr or "")[:300].replace("\n", " ").strip()
        return CreateIssueResult(
            ok=False, issue_number=None, issue_url=None,
            error=f"gh_exit_{proc.returncode}: {snippet}",
        )

    url = (proc.stdout or "").strip()
    if not url:
        return CreateIssueResult(ok=False, issue_number=None, issue_url=None, error="empty_url_from_gh")

    m = ISSUE_URL_RE.search(url)
    if not m:
        return CreateIssueResult(
            ok=False, issue_number=None, issue_url=url,
            error=f"could_not_parse_issue_number: {url[:200]}",
        )
    return CreateIssueResult(ok=True, issue_number=int(m.group(1)), issue_url=url, error=None)


async def create_issue(
    *,
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> CreateIssueResult:
    """Async wrapper for use inside discord.py event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: create_issue_sync(repo=repo, title=title, body=body, labels=labels, timeout=timeout),
    )


@dataclass
class CloseIssueResult:
    ok: bool
    error: str | None


def close_issue_sync(
    *,
    repo: str,
    issue_number: int,
    comment: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> CloseIssueResult:
    """Synchronous gh issue close. Optionally adds a closing comment."""
    if comment:
        comment_args = ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body", comment]
        try:
            proc = subprocess.run(
                comment_args,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return CloseIssueResult(ok=False, error=f"comment_{type(exc).__name__}")
        if proc.returncode != 0:
            snippet = (proc.stderr or "")[:300].replace("\n", " ").strip()
            return CloseIssueResult(ok=False, error=f"comment_exit_{proc.returncode}: {snippet}")

    args = ["gh", "issue", "close", str(issue_number), "--repo", repo]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return CloseIssueResult(ok=False, error="timeout")
    except FileNotFoundError:
        return CloseIssueResult(ok=False, error="gh_cli_not_found")
    if proc.returncode != 0:
        snippet = (proc.stderr or "")[:300].replace("\n", " ").strip()
        return CloseIssueResult(ok=False, error=f"gh_exit_{proc.returncode}: {snippet}")
    return CloseIssueResult(ok=True, error=None)


async def close_issue(
    *,
    repo: str,
    issue_number: int,
    comment: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> CloseIssueResult:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: close_issue_sync(repo=repo, issue_number=issue_number, comment=comment, timeout=timeout),
    )
