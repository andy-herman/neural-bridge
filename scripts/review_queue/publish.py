"""Publish items: the week's blog post, held or let go from Andy's phone.

The blog publishes by date. Its scheduled-publish workflow runs at Tuesday
01:00 UTC (Monday evening in Seattle) and flips the oldest post with
`draft: true` and `pubDate` on or before that day to published. Sunday prep
(scripts/publish/prepare_week.py) picks the same post, writes its LinkedIn and
X drafts to the vault, and posts a Discord brief. Until slice D, skipping a
week meant editing the post's frontmatter by hand.

Producer. Sunday prep raises one publish item for the post it picked.

Ready (Approve) records that Andy has seen it; the workflow publishes as
planned. Hold a week (Reject) moves the post's pubDate seven days past this
Monday through a pull request on the blog, merged at once. The post stays a
draft, so nothing becomes visible. The work happens in a throwaway worktree at
origin/main, never in the blog's own checkout, and the push passes the
outbound guard first.

An undecided item expires once the workflow has run: by then the post is
published, and the card has nothing left to decide.
"""

from __future__ import annotations

import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import store as st
from .captures import GhFn, GitFn, _default_guard, run_gh, run_git

SOURCE = "publish_prep"
REPO_ID = "neural-bridge-blog"
BASE = "main"
HOLD_DAYS = 7

_PUBDATE_RE = re.compile(r"(?m)^(pubDate:\s*['\"]?)(\d{4}-\d{2}-\d{2})")
_DRAFT_RE = re.compile(r"(?m)^draft:\s*true\s*$")


def publish_time(monday: date) -> int:
    """When the workflow that publishes `monday`'s post runs: Tuesday 01:00 UTC."""
    tuesday = monday + timedelta(days=1)
    return int(datetime(tuesday.year, tuesday.month, tuesday.day, 1, tzinfo=timezone.utc).timestamp())


def raise_for_week(*, path: str, slug: str, title: str, pub_date: date, monday: date,
                   store: st.Store | None = None, now: int | None = None) -> tuple[st.Item, bool]:
    """Queue this week's post. `path` is relative to the blog repo."""
    from scripts.discord_bot.repos import repo_for

    store = store or st.Store()
    repo = repo_for(REPO_ID)
    ref = f"https://github.com/{repo.gh_slug}/blob/{BASE}/{path}" if repo else ""
    return store.raise_item(
        source=SOURCE, key=f"{slug}@{monday.isoformat()}", kind=st.PUBLISH,
        title=f"Publishes {monday.strftime('%a %d %b')}: {title}"[:200],
        detail=f"LinkedIn and X drafts are in the vault, Drafts/scheduled/{monday.isoformat()}.",
        ref=ref, agent="content",
        payload={"path": path, "slug": slug, "pub_date": pub_date.isoformat(),
                 "monday": monday.isoformat(), "publish_at": publish_time(monday)},
        unique=True, now=now)


def expire_stale(store: st.Store, now: int) -> list[str]:
    """An undecided publish item whose workflow has already run."""
    return [i.id for i in store.items(states=(st.OPEN, st.SNOOZED), kinds=(st.PUBLISH,))
            if now >= int(i.payload.get("publish_at") or 0)
            and store.expire(i.id, actor="queue", reason="the Monday publish run has passed", now=now)]


def held_text(text: str, new_date: date) -> str | None:
    """The post with its pubDate moved to `new_date`, or None if it has no
    pubDate. Keeps whatever followed the date (a time, a closing quote)."""
    if not _PUBDATE_RE.search(text):
        return None
    return _PUBDATE_RE.sub(lambda m: m.group(1) + new_date.isoformat(), text, count=1)


def apply(item: st.Item, *, git: GitFn = run_git, gh: GhFn = run_gh,
          guard: Callable[[Path, GitFn, str, str], tuple[bool, str]] | None = None,
          blog: Path | None = None, gh_slug: str | None = None) -> tuple[bool, str]:
    """Applier: Ready changes nothing; Hold a week moves the pubDate."""
    p = item.payload
    try:
        monday = date.fromisoformat(str(p["monday"]))
        pub_date = date.fromisoformat(str(p["pub_date"]))
        path, slug = str(p["path"]), str(p["slug"])
    except (KeyError, ValueError) as exc:
        return False, f"unreadable publish item: {exc}"
    if item.verb == st.APPROVE:
        return True, f"ready; it publishes {monday.strftime('%a %d %b')} in the evening"
    if item.verb != st.REJECT:
        return False, f"publish items take ready or hold, not {item.verb!r}"
    if blog is None or gh_slug is None:
        from scripts.discord_bot.repos import repo_for
        repo = repo_for(REPO_ID)
        if repo is None:
            return False, f"unknown repo {REPO_ID}"
        blog, gh_slug = blog or repo.local_path, gh_slug or repo.gh_slug
    guard = guard or _default_guard
    new_date = max(pub_date, monday) + timedelta(days=HOLD_DAYS)

    ok, err = git(blog, ["fetch", "-q", "origin", BASE])
    if not ok:
        return False, f"git fetch failed: {err}"
    wt = Path(tempfile.mkdtemp(prefix="nb-publish-hold-")) / "blog"
    ok, err = git(blog, ["worktree", "add", "-q", "--detach", str(wt), f"origin/{BASE}"])
    if not ok:
        return False, f"could not create a worktree: {err}"
    branch = f"queue/{item.id}"
    try:
        post = wt / path
        if not post.exists():
            return False, f"{path} is not on {BASE}"
        text = post.read_text(encoding="utf-8")
        if not _DRAFT_RE.search(text):
            return True, "it is no longer a draft on main; nothing to hold"
        current = _PUBDATE_RE.search(text)
        if current and current.group(2) != pub_date.isoformat():
            return True, f"its pubDate is already {current.group(2)} on main"
        held = held_text(text, new_date)
        if held is None:
            return False, f"{path} has no pubDate to move"
        post.write_text(held, encoding="utf-8")
        title = f"content: hold {slug} a week, to {new_date.isoformat()} (review queue {item.id})"
        body = (f"Held by Andy in the review queue (item `{item.id}`): `pubDate` "
                f"{pub_date.isoformat()} -> {new_date.isoformat()}. The post stays a draft.")
        for args in (["checkout", "-q", "-B", branch], ["add", "--", path],
                     ["commit", "-q", "-m", title, "-m", "Decided in the review queue."]):
            ok, err = git(wt, args)
            if not ok:
                return False, f"git {args[0]} failed: {err}"
        allowed, why = guard(wt, git, f"github:push:{gh_slug}", f"{title}\n{body}")
        if not allowed:
            return False, f"outbound guard refused, nothing pushed: {why}"
        ok, err = git(wt, ["push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}"])
        if not ok:
            return False, f"git push failed: {err}"
        ok, out = gh(["pr", "create", "--repo", gh_slug, "--base", BASE, "--head", branch,
                      "--title", title, "--body", body])
        if not ok:
            return False, f"branch {branch} pushed, but gh pr create failed: {out}"
        url = out.strip().splitlines()[-1]
        ok, out = gh(["pr", "merge", url, "--squash", "--delete-branch"])
        if not ok:
            return False, f"PR open but not merged ({url}): {out}"
        return True, f"held to {new_date.isoformat()}: {url}"
    finally:
        git(blog, ["worktree", "remove", "--force", str(wt)])
        git(blog, ["branch", "-q", "-D", branch])
