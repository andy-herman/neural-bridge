"""Promotions: the nightly compile, staged, with every new article a card.

Until 2026-10-16 the nightly compile wrote straight into the shared checkout.
Its first promotion through the queue era sat there uncommitted for two days,
blocked auto-reload's next pull, and named a flight, a family member's travel
and a work submission. The outbound guard allowed it (none of that is a marked
phrase); the only thing keeping it off the public repo was that nothing ever
committed it.

Now the nightly job runs this instead of compile.py:

    python -m scripts.review_queue.promotions [compile.py options]

1. A private worktree at .trees/compile-staging, reset to origin/main.
2. compile.py --no-dry-run with NB_COMPILE_KNOWLEDGE_DIR pointing into it, so
   the compile's own logic (gate, writer, history, index, log, connections)
   runs unchanged, on a copy.
3. Every concept article and quarantine file the run added or changed becomes
   a capture item: source compile_staged, its full text in the local payload,
   and a private preview in the vault (Neural Bridge/Wiki staging/<slug>.md)
   to read before deciding. A card carries only the slug and the gate's
   verdict. A later run that restages the same slug supersedes the older card.
4. The staging worktree is reset. Nothing was committed or pushed.

File it (Approve) publishes the article to knowledge/concepts with a
`reviewed:` line, archives the version it replaces under .history/, adds it to
the index and the log, and writes any connection whose other concept is
already on main: one pull request, screened by the outbound guard and merged
at once, the same way captures.py files a quarantined capture. Reject drops
it; nothing ever reached GitHub.
"""

from __future__ import annotations

import hashlib
import os
import subprocess as sp
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import store as st
from .captures import (BASE, GhFn, GitFn, _default_guard, _field, _first_agent, _prepare_worktree,
                       add_to_index, append_log, gh_slug, promote_text, run_gh, run_git,
                       split_frontmatter)

SOURCE = "compile_staged"
STAGING_REL = ".trees/compile-staging"
APPLY_WORKTREE_REL = ".trees/review-queue"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PREVIEW_DIR = Path.home() / "Documents" / "Luna Master" / "Neural Bridge" / "Wiki staging"
PREVIEW_REL = "Neural Bridge/Wiki staging"

CompileFn = Callable[[Path, list[str]], int]


def run_compile(knowledge_dir: Path, args: list[str]) -> int:
    """compile.py, writing into `knowledge_dir`. Its output goes to ours, so
    the launchd log keeps the run summary it always had. Its nightly Discord
    summary is off: the cards are the notice now, and a nightly post that
    fires on quiet nights too is noise (Andy, 2026-10-16)."""
    env = {**os.environ, "NB_COMPILE_KNOWLEDGE_DIR": str(knowledge_dir)}
    return sp.run([sys.executable, str(REPO_ROOT / "scripts" / "compile.py"), "--no-dry-run",
                   "--no-discord", *args], cwd=str(REPO_ROOT), env=env).returncode


def _changed(wt: Path, git: GitFn) -> list[str]:
    ok, out = git(wt, ["status", "--porcelain", "--untracked-files=all", "--", "knowledge"])
    if not ok:
        raise RuntimeError(f"git status in staging failed: {out}")
    # The git helper strips its output, which eats the leading space of a
    # " M path" line, so split on whitespace rather than slicing columns.
    return [line.split(maxsplit=1)[1].strip().strip('"') for line in out.splitlines()
            if len(line.split(maxsplit=1)) == 2]


def _key(path: str, text: str) -> str:
    return hashlib.sha256(f"{path}\n{text}".encode()).hexdigest()[:16]


def preview_path(slug: str, preview_dir: Path | None = None) -> Path:
    return (preview_dir or PREVIEW_DIR) / f"{slug}.md"


def _write_preview(slug: str, text: str, item_id: str, preview_dir: Path | None) -> None:
    target = preview_path(slug, preview_dir)
    if not target.parent.parent.exists():  # no vault on this machine
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"> [!note] Waiting in the review queue (item {item_id}). Nothing here is public yet.\n\n"
                      + text, encoding="utf-8")


def _drop_preview(slug: str, preview_dir: Path | None) -> None:
    try:
        preview_path(slug, preview_dir).unlink()
    except OSError:
        pass


def stage(store: st.Store, repo: Path = REPO_ROOT, compile_args: list[str] | None = None, *,
          git: GitFn = run_git, compile_fn: CompileFn = run_compile,
          preview_dir: Path | None = None) -> tuple[list[st.Item], int]:
    """One nightly run. Returns (items created, compile's exit code)."""
    wt = repo / STAGING_REL
    err = _prepare_worktree(repo, wt, git)
    if err:
        raise RuntimeError(err)
    try:
        rc = compile_fn(wt / "knowledge", list(compile_args or []))
        changed = _changed(wt, git)
        connections = {p: (wt / p).read_text(encoding="utf-8") for p in changed
                       if p.startswith("knowledge/connections/") and (wt / p).is_file()}
        articles = [p for p in changed if p.endswith(".md") and (wt / p).is_file()
                    and (p.startswith("knowledge/quarantine/")
                         or (p.startswith("knowledge/concepts/") and "/.history/" not in p))]
        created = []
        for path in articles:
            text = (wt / path).read_text(encoding="utf-8")
            fm, _ = split_frontmatter(text)
            slug = _field(fm, "slug") or Path(path).stem
            promoted = path.startswith("knowledge/concepts/")
            item, is_new = store.raise_item(
                source=SOURCE, key=_key(path, text), kind=st.CAPTURE,
                title=f"{'New wiki concept' if promoted else 'Quarantined'}: {slug}",
                detail=(f"{'Promoted' if promoted else 'Held'} by the filing gate. "
                        f"Read it in the vault: {PREVIEW_REL}/{slug}.md"),
                agent=_first_agent(fm),
                payload={"slug": slug, "verdict": "PROMOTE" if promoted else "QUARANTINE",
                         "path": path, "text": text,
                         "connections": {p: t for p, t in connections.items()
                                         if slug in Path(p).stem.split("--")}},
                unique=True)
            if not is_new:
                continue
            for older in store.items(states=(st.OPEN, st.SNOOZED), kinds=(st.CAPTURE,)):
                if older.source == SOURCE and older.id != item.id and older.payload.get("slug") == slug:
                    store.clear(source=SOURCE, key=older.key)
            _write_preview(slug, text, item.id, preview_dir)
            created.append(item)
        return created, rc
    finally:
        git(wt, ["reset", "-q", "--hard", f"origin/{BASE}"])
        git(wt, ["clean", "-fdq"])


def _reviewed(text: str, item_id: str, when: str) -> str:
    """A promoted article with Andy's review stamped into its frontmatter."""
    fm, body = split_frontmatter(text)
    out, stamped = [], False
    for line in fm:
        if line.startswith("reviewed:"):
            continue
        out.append(line)
        if line.startswith("verdict:") and not stamped:
            out.append(f'reviewed: "approved by Andy in the review queue (item {item_id}, {when})"')
            stamped = True
    if not stamped:
        out.append(f'reviewed: "approved by Andy in the review queue (item {item_id}, {when})"')
    return "---\n" + "\n".join(out) + "\n---\n" + body


def apply(item: st.Item, *, repo: Path = REPO_ROOT, git: GitFn = run_git, gh: GhFn = run_gh,
          guard: Callable[[Path, GitFn, str, str], tuple[bool, str]] | None = None,
          now: datetime | None = None, preview_dir: Path | None = None) -> tuple[bool, str]:
    """File or drop one staged article."""
    p = item.payload
    slug, text = str(p.get("slug") or ""), str(p.get("text") or "")
    if not slug or not text:
        return False, "the item carries no article"
    if item.verb == st.REJECT:
        _drop_preview(slug, preview_dir)
        return True, "discarded; nothing reached the wiki"
    if item.verb != st.APPROVE:
        return False, f"staged articles take approve or reject, not {item.verb!r}"

    guard = guard or _default_guard
    wt = repo / APPLY_WORKTREE_REL
    err = _prepare_worktree(repo, wt, git)
    if err:
        return False, err
    marker = f"review queue {item.id}"
    ok, found = git(wt, ["log", f"origin/{BASE}", "-1", "--format=%h", "--fixed-strings", f"--grep={marker}"])
    if ok and found:
        _drop_preview(slug, preview_dir)
        return True, f"already on main as {found}"

    when_dt = now or datetime.now(timezone.utc)
    when = when_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    branch = f"queue/{item.id}"
    ok, err = git(wt, ["checkout", "-q", "-B", branch])
    if not ok:
        return False, f"could not create {branch}: {err}"
    try:
        article = (_reviewed(text, item.id, when) if p.get("verdict") == "PROMOTE"
                   else promote_text(text, item_id=item.id, when=when))
        concept = f"knowledge/concepts/{slug}.md"
        to_add = [concept]
        if (wt / concept).exists():
            archived = f"knowledge/concepts/.history/{slug}/{when_dt.strftime('%Y-%m-%dT%H-%M-%SZ')}.md"
            (wt / archived).parent.mkdir(parents=True, exist_ok=True)
            ok, err = git(wt, ["mv", concept, archived])
            if not ok:
                return False, f"could not archive the current {slug}: {err}"
            to_add.append(archived)
        (wt / concept).parent.mkdir(parents=True, exist_ok=True)
        (wt / concept).write_text(article, encoding="utf-8")
        index = wt / "knowledge" / "index.md"
        if index.exists():
            index.write_text(add_to_index(index.read_text(encoding="utf-8"), slug), encoding="utf-8")
            to_add.append("knowledge/index.md")
        for path, conn in (p.get("connections") or {}).items():
            other = next((s for s in Path(path).stem.split("--") if s != slug), "")
            if other and (wt / "knowledge" / "concepts" / f"{other}.md").exists():
                (wt / path).parent.mkdir(parents=True, exist_ok=True)
                (wt / path).write_text(conn, encoding="utf-8")
                to_add.append(path)
        log = wt / "knowledge" / "log.md"
        if log.exists():
            log.write_text(append_log(log.read_text(encoding="utf-8"),
                                      f"- review queue: filed `{slug}` from the nightly compile after human "
                                      f"review (item {item.id})", when_dt.strftime("%Y-%m-%d")),
                           encoding="utf-8")
            to_add.append("knowledge/log.md")
        for rel in to_add:
            ok, err = git(wt, ["add", "--", rel])
            if not ok:
                return False, f"git add {rel} failed: {err}"
        ok, staged = git(wt, ["diff", "--cached", "--name-only"])
        paths = [x for x in staged.splitlines() if x.strip()] if ok else []
        if not paths or any(not x.startswith("knowledge/") for x in paths):
            return False, f"refusing: the change must stay inside knowledge/ (got {paths or 'nothing'})"

        title = f"wiki: file {slug} from the nightly compile ({marker})"
        body = (f"Approved by Andy in the review queue (item `{item.id}`) after reading it in the vault.\n\n"
                f"- the filing gate's verdict: {p.get('verdict')}\n"
                "- Opened and merged by the review queue applier (scripts/review_queue/promotions.py).")
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
        _drop_preview(slug, preview_dir)
        return True, url
    finally:
        git(wt, ["checkout", "-q", "--detach", "-f", f"origin/{BASE}"])
        git(wt, ["branch", "-q", "-D", branch])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    store = st.Store()
    created, rc = stage(store, compile_args=args)
    print(f"review queue: {len(created)} staged article(s) queued"
          + (f": {', '.join(i.payload['slug'] for i in created)}" if created else ""))
    return rc


if __name__ == "__main__":
    sys.exit(main())
