"""Captures: the compile filing gate's quarantine, as queue items.

The gate quarantines a candidate concept it cannot vouch for (an untraceable
claim, a split vote) and leaves it in knowledge/quarantine/ for a human. Eight
sat there from 2026-05-10 onward because nothing ever asked anyone to look.

Producer: sync() turns every quarantine file committed on main into one
capture item. The key is the file's blob hash, so an item is never recreated
for content that was already decided, and a rewritten file gets a fresh item.
Files that exist only in the working tree (compile output nobody committed)
are not offered: filing works on main, so they would fail to apply.

Applier: apply() does what Andy chose, as a pull request that is merged at
once. Nothing else in this repo pushes to main, and the PR is the public
record of the decision.

  approve  quarantine file -> knowledge/concepts/<slug>.md (verdict PROMOTE,
           with a `reviewed:` line), slug added to knowledge/index.md
  reject   quarantine file deleted (git history keeps it)

Both append a line to knowledge/log.md. The work happens in a private
worktree at .trees/review-queue (gitignored), never in the shared checkout
the auto-reload watcher pulls. The diff must stay inside knowledge/ and pass
the outbound guard before anything is pushed.
"""

from __future__ import annotations

import re
import subprocess as sp
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import store as st

SOURCE = "compile_quarantine"
QUARANTINE_REL = "knowledge/quarantine"
CONCEPTS_REL = "knowledge/concepts"
INDEX_REL = "knowledge/index.md"
LOG_REL = "knowledge/log.md"
WORKTREE_REL = ".trees/review-queue"
BASE = "main"
DEFAULT_GH_SLUG = "andy-herman/neural-bridge"

GitFn = Callable[[Path, list[str]], tuple[bool, str]]
GhFn = Callable[[list[str]], tuple[bool, str]]

_GH_URL_RE = re.compile(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_LOG_DATE_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
_INDEX_CONCEPTS_RE = re.compile(r"^## Concepts\s*$", re.MULTILINE)


def run_git(cwd: Path, args: list[str], timeout: int = 120) -> tuple[bool, str]:
    try:
        proc = sp.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                      timeout=timeout, stdin=sp.DEVNULL)
    except (sp.TimeoutExpired, FileNotFoundError) as exc:
        return False, type(exc).__name__
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300].replace("\n", " ")
    return True, proc.stdout.strip()


def run_gh(args: list[str], timeout: int = 120) -> tuple[bool, str]:
    # Run outside any checkout, so gh never touches a local branch.
    try:
        proc = sp.run(["gh", *args], cwd=tempfile.gettempdir(), capture_output=True,
                      text=True, timeout=timeout, stdin=sp.DEVNULL)
    except (sp.TimeoutExpired, FileNotFoundError) as exc:
        return False, type(exc).__name__
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:300].replace("\n", " ")
    return True, proc.stdout.strip()


def gh_slug(repo: Path, git: GitFn = run_git) -> str:
    ok, url = git(repo, ["remote", "get-url", "origin"])
    m = _GH_URL_RE.search(url) if ok else None
    return m.group(1) if m else DEFAULT_GH_SLUG


# ---------- frontmatter ----------

def split_frontmatter(text: str) -> tuple[list[str], str]:
    """(frontmatter lines, body). No frontmatter gives ([], text)."""
    if not text.startswith("---\n"):
        return [], text
    end = text.find("\n---\n", 4)
    if end == -1:
        return [], text
    return text[4:end].split("\n"), text[end + 5:]


def _field(lines: list[str], name: str) -> str:
    prefix = f"{name}:"
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):].strip().strip('"')
    return ""


def _first_agent(lines: list[str]) -> str:
    for line in lines:
        m = re.match(r"^\s*-\s*agent:\s*(\S+)", line)
        if m:
            return m.group(1)
    return ""


def _checks(lines: list[str]) -> str:
    raw = _field(lines, "checks_triggered").strip("[]")
    return ", ".join(c.strip() for c in raw.split(",") if c.strip()) or "no named check"


# ---------- producer ----------

def scan(repo: Path, git: GitFn = run_git) -> list[dict]:
    """One sync entry per quarantine file committed at the checkout's HEAD."""
    ok, head = git(repo, ["rev-parse", "HEAD"])
    if not ok:
        raise RuntimeError(f"git rev-parse HEAD failed: {head}")
    ok, listing = git(repo, ["ls-tree", "HEAD", f"{QUARANTINE_REL}/"])
    if not ok:
        raise RuntimeError(f"git ls-tree failed: {listing}")
    slug_repo = gh_slug(repo, git)
    entries = []
    for line in listing.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob" or not path.endswith(".md"):
            continue
        blob = parts[2]
        ok, text = git(repo, ["cat-file", "-p", blob])
        if not ok:
            continue
        fm, _body = split_frontmatter(text)
        slug = _field(fm, "slug") or Path(path).stem
        compiled = _field(fm, "compiled_at")[:10]
        detail = f"Held by the filing gate ({_checks(fm)})."
        if compiled:
            detail += f" Quarantined {compiled}."
        entries.append({
            "key": f"{slug}@{blob[:12]}",
            "title": slug,
            "detail": detail,
            # A permalink at this commit, so the link still works after filing.
            "ref": f"https://github.com/{slug_repo}/blob/{head}/{path}",
            "agent": _first_agent(fm),
            "payload": {"path": path, "slug": slug, "blob": blob},
        })
    return entries


def sync(store: st.Store, repo: Path, git: GitFn = run_git) -> tuple[list[st.Item], list[st.Item]]:
    return store.sync(source=SOURCE, kind=st.CAPTURE, entries=scan(repo, git), unique=True)


# ---------- the wiki edits (pure) ----------

def promote_text(text: str, *, item_id: str, when: str) -> str:
    """A quarantine file rewritten as a concept article Andy approved."""
    fm, body = split_frontmatter(text)
    slug = _field(fm, "slug") or "concept"
    checks = _checks(fm)
    out_fm = []
    for line in fm:
        if line.startswith("verdict:"):
            out_fm.append("verdict: PROMOTE")
            out_fm.append(f'reviewed: "approved by Andy in the review queue (item {item_id}, {when})"')
        else:
            out_fm.append(line)
    m = re.search(r"^## Proposed summary\s*$(.*?)(?=^_Quarantined on|^## |\Z)", body,
                  re.MULTILINE | re.DOTALL)
    summary = (m.group(1).strip() if m else "").strip() or slug
    article = (
        f"# {slug}\n\n"
        f"{summary}\n\n"
        f"_Promoted from quarantine on {when} after human review in the review queue "
        f"(item {item_id}). The filing gate had held it for: {checks}._\n"
    )
    return "---\n" + "\n".join(out_fm) + "\n---\n\n" + article


def add_to_index(text: str, slug: str) -> str:
    """Add `- [[slug]]` to the Concepts section, sorted, like compile does."""
    m = _INDEX_CONCEPTS_RE.search(text)
    if not m:
        return text
    start = m.end()
    nxt = re.search(r"^## ", text[start:], re.MULTILINE)
    end = start + nxt.start() if nxt else len(text)
    existing = set(re.findall(r"^- \[\[([^\]]+)\]\]", text[start:end], re.MULTILINE))
    if slug in existing:
        return text
    lines = [f"- [[{s}]]" for s in sorted(existing | {slug})]
    return text[:start] + "\n\n" + "\n".join(lines) + "\n\n" + text[end:]


def append_log(text: str, bullet: str, today: str) -> str:
    headings = list(_LOG_DATE_HEADING_RE.finditer(text))
    if headings and headings[-1].group(1) == today:
        return text.rstrip() + "\n" + bullet + "\n"
    return text.rstrip() + f"\n\n## {today}\n\n" + bullet + "\n"


# ---------- applier ----------

def _default_guard(wt: Path, git: GitFn, surface: str, extra: str) -> tuple[bool, str]:
    from scripts import outbound_guard
    verdict = outbound_guard.check_push(lambda args: git(wt, args), surface=surface, extra=extra)
    return verdict.allowed, verdict.describe()


def _prepare_worktree(repo: Path, wt: Path, git: GitFn) -> str:
    """A clean private checkout at origin/main. Returns an error or ""."""
    ok, err = git(repo, ["fetch", "-q", "origin", BASE], )
    if not ok:
        return f"git fetch failed: {err}"
    if not (wt / ".git").exists():
        git(repo, ["worktree", "prune"])
        ok, err = git(repo, ["worktree", "add", "-q", "--detach", str(wt), f"origin/{BASE}"])
        if not ok:
            return f"could not create the worktree: {err}"
        return ""
    for args in (["checkout", "-q", "--detach", "-f", f"origin/{BASE}"],
                 ["reset", "-q", "--hard", f"origin/{BASE}"],
                 ["clean", "-fdq"]):
        ok, err = git(wt, args)
        if not ok:
            return f"could not reset the worktree ({args[0]}): {err}"
    return ""


def apply(item: st.Item, *, repo: Path, git: GitFn = run_git, gh: GhFn = run_gh,
          guard: Callable[[Path, GitFn, str, str], tuple[bool, str]] | None = None,
          now: datetime | None = None) -> tuple[bool, str]:
    """File or reject one capture. Returns (ok, result) where result is the
    merged PR's URL, or why it did not go through. Safe to call again after a
    failure: the branch is force-pushed and an open PR is reused."""
    guard = guard or _default_guard
    if item.verb not in (st.APPROVE, st.REJECT):
        return False, f"captures take approve or reject, not {item.verb!r}"
    path = item.payload.get("path", "")
    slug = item.payload.get("slug", "")
    blob = item.payload.get("blob", "")
    if not path.startswith(QUARANTINE_REL + "/") or not slug:
        return False, "the item does not name a quarantine file"

    wt = repo / WORKTREE_REL
    err = _prepare_worktree(repo, wt, git)
    if err:
        return False, err

    marker = f"review queue {item.id}"
    ok, found = git(wt, ["log", f"origin/{BASE}", "-1", "--format=%h", "--fixed-strings", f"--grep={marker}"])
    if ok and found:
        return True, f"already on main as {found}"
    if not (wt / path).exists():
        return True, "it had already left quarantine on main"
    ok, current = git(wt, ["rev-parse", f"HEAD:{path}"])
    if ok and blob and current != blob:
        return False, "the file changed on main after this card was sent; a new card will follow"

    when_dt = now or datetime.now(timezone.utc)
    when = when_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = when_dt.strftime("%Y-%m-%d")
    branch = f"queue/{item.id}"
    ok, err = git(wt, ["checkout", "-q", "-B", branch])
    if not ok:
        return False, f"could not create {branch}: {err}"

    try:
        if item.verb == st.APPROVE:
            concept = f"{CONCEPTS_REL}/{slug}.md"
            if (wt / concept).exists():
                return False, f"a concept named {slug} already exists; reject this one or rename it by hand"
            (wt / concept).parent.mkdir(parents=True, exist_ok=True)
            (wt / concept).write_text(promote_text((wt / path).read_text(encoding="utf-8"),
                                                   item_id=item.id, when=when), encoding="utf-8")
            index = wt / INDEX_REL
            if index.exists():
                index.write_text(add_to_index(index.read_text(encoding="utf-8"), slug), encoding="utf-8")
            bullet = f"- review queue: promoted `{slug}` from quarantine after human review (item {item.id})"
            to_add = [concept, INDEX_REL]
            action = "promote"
        else:
            bullet = f"- review queue: rejected quarantined `{slug}` after human review (item {item.id})"
            to_add = []
            action = "reject"
        log = wt / LOG_REL
        if log.exists():
            log.write_text(append_log(log.read_text(encoding="utf-8"), bullet, today), encoding="utf-8")
            to_add.append(LOG_REL)

        ok, err = git(wt, ["rm", "-q", "--", path])
        if not ok:
            return False, f"git rm failed: {err}"
        for rel in to_add:
            if (wt / rel).exists():
                ok, err = git(wt, ["add", "--", rel])
                if not ok:
                    return False, f"git add {rel} failed: {err}"

        ok, staged = git(wt, ["diff", "--cached", "--name-only"])
        paths = [p for p in staged.splitlines() if p.strip()] if ok else []
        if not paths or any(not p.startswith("knowledge/") for p in paths):
            return False, f"refusing: the change must stay inside knowledge/ (got {paths or 'nothing'})"

        title = f"wiki: {action} {slug} from quarantine ({marker})"
        body = (f"Decided by Andy in the review queue (item `{item.id}`).\n\n"
                f"- {action}: `{path}`\n"
                "- Opened and merged by the review queue applier (scripts/review_queue/captures.py).")
        ok, err = git(wt, ["commit", "-q", "-m", title, "-m", "Decided in the review queue."])
        if not ok:
            return False, f"git commit failed: {err}"

        repo_slug = gh_slug(repo, git)
        allowed, why = guard(wt, git, f"github:push:{repo_slug}", f"{title}\n{body}")
        if not allowed:
            return False, f"outbound guard refused, nothing pushed: {why}"
        ok, err = git(wt, ["push", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}"])
        if not ok:
            return False, f"git push failed: {err}"

        ok, url = gh(["pr", "list", "--repo", repo_slug, "--head", branch, "--state", "open",
                      "--json", "url", "--jq", ".[0].url"])
        url = url.strip() if ok else ""
        if not url:
            ok, out = gh(["pr", "create", "--repo", repo_slug, "--base", BASE, "--head", branch,
                          "--title", title, "--body", body])
            if not ok:
                return False, f"branch {branch} pushed, but gh pr create failed: {out}"
            url = out.strip().splitlines()[-1]
        ok, out = gh(["pr", "merge", url, "--squash"])
        if not ok:
            return False, f"PR open but not merged ({url}): {out}"
        return True, url
    finally:
        git(wt, ["checkout", "-q", "--detach", "-f", f"origin/{BASE}"])
        git(wt, ["branch", "-q", "-D", branch])
