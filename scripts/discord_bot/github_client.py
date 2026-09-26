"""GitHub issue creation via the gh CLI.

Shells out to `gh` rather than reimplementing the REST client, because:
1. `gh` is already authenticated on Andy's Mac with the right scopes.
2. We don't introduce a new HTTP/auth path for the daemon.
3. The CLI handles token refresh and rate limit reporting.

For PR-I-A only `create_issue` is needed. Hand-off / status transition
helpers (add_label, comment, close) ship in PR-K.

`gh issue create` returns the new issue's URL on stdout. We parse the
trailing `/<number>` to extract the issue number.

Every call that publishes text (issue title and body, comments, closing
comments, body edits) first passes scripts/outbound_guard.py. The repos are
public and agents read the vault, so text from a marked note is refused before
gh runs, and the guard fails closed. The refusal carries counts only.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass

from scripts import outbound_guard

DEFAULT_TIMEOUT = 30
ISSUE_URL_RE = re.compile(r"/issues/(\d+)\s*$")


def _guard(text: str, surface: str) -> str | None:
    """None when the outbound guard clears `text`, else its counts-only reason."""
    verdict = outbound_guard.check(text, surface=surface)
    return None if verdict.allowed else verdict.describe()


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
    blocked = _guard(f"{title}\n\n{body}", "github:create_issue")
    if blocked:
        return CreateIssueResult(ok=False, issue_number=None, issue_url=None, error=blocked)
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
    """Synchronous gh issue close. Optionally adds a closing comment.

    A closing comment the outbound guard refuses stops the whole action: the
    issue is neither commented on nor closed.
    """
    if comment:
        blocked = _guard(comment, "github:close_comment")
        if blocked:
            return CloseIssueResult(ok=False, error=blocked)
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


@dataclass
class CommentIssueResult:
    ok: bool
    error: str | None


def comment_issue_sync(
    *,
    repo: str,
    issue_number: int,
    body: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> CommentIssueResult:
    """Post a comment on an issue. Uses --body-file - via stdin to avoid argv length limits."""
    blocked = _guard(body, "github:comment")
    if blocked:
        return CommentIssueResult(ok=False, error=blocked)
    args = ["gh", "issue", "comment", str(issue_number), "--repo", repo, "--body-file", "-"]
    try:
        proc = subprocess.run(
            args,
            input=body,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CommentIssueResult(ok=False, error="timeout")
    except FileNotFoundError:
        return CommentIssueResult(ok=False, error="gh_cli_not_found")
    if proc.returncode != 0:
        snippet = (proc.stderr or "")[:300].replace("\n", " ").strip()
        return CommentIssueResult(ok=False, error=f"gh_exit_{proc.returncode}: {snippet}")
    return CommentIssueResult(ok=True, error=None)


async def comment_issue(
    *,
    repo: str,
    issue_number: int,
    body: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> CommentIssueResult:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: comment_issue_sync(repo=repo, issue_number=issue_number, body=body, timeout=timeout),
    )


@dataclass
class EditIssueBodyResult:
    ok: bool
    error: str | None


def edit_issue_body_sync(
    *,
    repo: str,
    issue_number: int,
    new_body: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> EditIssueBodyResult:
    """Replace the issue body via gh issue edit --body-file (uses stdin to
    avoid argv length issues for long bodies)."""
    blocked = _guard(new_body, "github:edit_issue_body")
    if blocked:
        return EditIssueBodyResult(ok=False, error=blocked)
    args = ["gh", "issue", "edit", str(issue_number), "--repo", repo, "--body-file", "-"]
    try:
        proc = subprocess.run(
            args,
            input=new_body,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return EditIssueBodyResult(ok=False, error="timeout")
    except FileNotFoundError:
        return EditIssueBodyResult(ok=False, error="gh_cli_not_found")
    if proc.returncode != 0:
        snippet = (proc.stderr or "")[:300].replace("\n", " ").strip()
        return EditIssueBodyResult(ok=False, error=f"gh_exit_{proc.returncode}: {snippet}")
    return EditIssueBodyResult(ok=True, error=None)


async def edit_issue_body(
    *,
    repo: str,
    issue_number: int,
    new_body: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> EditIssueBodyResult:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: edit_issue_body_sync(repo=repo, issue_number=issue_number, new_body=new_body, timeout=timeout),
    )
