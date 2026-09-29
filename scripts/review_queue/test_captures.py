"""Captures: quarantine files become queue items, and Andy's decision becomes
a merged pull request. Run against a throwaway repo with a bare origin; gh
and the outbound guard are stubbed, git is real."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.review_queue import captures  # noqa: E402
from scripts.review_queue import store as st  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

QUARANTINED = """---
slug: idor-still-a01
verdict: QUARANTINE
reason: summary asserts facts the excerpt does not support
checks_triggered: [untraceable-claims]
compiled_at: 2026-05-10T15:23:46Z
compiler_version: "1.2"
sources:
  - agent: teaching-prep
    session_id: s-1
---

# idor-still-a01

**Quarantined** for human review.

**Reason:** summary asserts facts the excerpt does not support

**Checks triggered:** untraceable-claims

## Proposed summary

Broken access control stays first; the lab teaches the canonical example

_Quarantined on 2026-05-10T15:23:46Z by `compile.py` v1.2._
"""

INDEX = """# Wiki Index

## Concepts

- [[zeta]]
- [[alpha]]

## Per-agent memory

table
"""

LOG = """# Wiki Activity Log

## 2026-05-10

- compile complete
"""

WHEN = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class TestPureEdits(unittest.TestCase):
    def test_promote_rewrites_verdict_and_keeps_the_summary(self):
        out = captures.promote_text(QUARANTINED, item_id="abc123", when="2026-09-29T12:00:00Z")
        fm, body = captures.split_frontmatter(out)
        self.assertIn("verdict: PROMOTE", fm)
        self.assertNotIn("verdict: QUARANTINE", fm)
        self.assertTrue(any(l.startswith("reviewed:") and "abc123" in l for l in fm))
        self.assertIn("  - agent: teaching-prep", fm, "sources survive")
        self.assertIn("# idor-still-a01", body)
        self.assertIn("Broken access control stays first", body)
        self.assertNotIn("**Quarantined**", body)
        self.assertIn("untraceable-claims", body)

    def test_index_gains_the_slug_sorted_and_only_once(self):
        once = captures.add_to_index(INDEX, "middle")
        self.assertIn("- [[alpha]]\n- [[middle]]\n- [[zeta]]", once)
        self.assertEqual(captures.add_to_index(once, "middle"), once)
        self.assertIn("## Per-agent memory", once)

    def test_log_appends_under_today_or_opens_a_new_day(self):
        same = captures.append_log(LOG, "- x", "2026-05-10")
        self.assertEqual(same.count("## 2026-05-10"), 1)
        self.assertTrue(same.endswith("- compile complete\n- x\n"))
        new = captures.append_log(LOG, "- x", "2026-09-29")
        self.assertTrue(new.endswith("## 2026-09-29\n\n- x\n"))


@unittest.skipUnless(shutil.which("git"), "needs git")
class TestWithRepo(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, GIT_ENV)
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.repo = root / "neural-bridge"
        self._git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self._git(root, "clone", "-q", str(self.origin), str(self.seed))
        self._write(self.seed, "knowledge/quarantine/idor-still-a01.md", QUARANTINED)
        self._write(self.seed, "knowledge/index.md", INDEX)
        self._write(self.seed, "knowledge/log.md", LOG)
        self._write(self.seed, ".gitignore", ".trees/\n")
        self._git(self.seed, "add", "-A")
        self._git(self.seed, "commit", "-q", "-m", "seed")
        self._git(self.seed, "push", "-q", "origin", "main")
        self._git(root, "clone", "-q", str(self.origin), str(self.repo))
        self.store = st.Store(root / "q.db")
        self.gh_calls: list[list[str]] = []

    def tearDown(self):
        self._tmp.cleanup()
        self._env.stop()

    # -- helpers -----------------------------------------------------------

    def _git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()

    def _write(self, clone: Path, rel: str, text: str) -> None:
        (clone / rel).parent.mkdir(parents=True, exist_ok=True)
        (clone / rel).write_text(text)

    def gh(self, args: list[str]) -> tuple[bool, str]:
        """pr list finds nothing, pr create returns a URL, pr merge fast-forwards
        origin's main to the pushed branch, as a merge would."""
        self.gh_calls.append(args)
        if args[:2] == ["pr", "list"]:
            return True, ""
        if args[:2] == ["pr", "create"]:
            self.branch = args[args.index("--head") + 1]
            return True, "https://github.com/example/nb/pull/9"
        if args[:2] == ["pr", "merge"]:
            self._git(self.origin, "update-ref", "refs/heads/main", f"refs/heads/{self.branch}")
            return True, ""
        return False, "unexpected gh call"

    @staticmethod
    def allow(wt, git, surface, extra):
        return True, "allowed"

    def queued(self, verb: str) -> st.Item:
        captures.sync(self.store, self.repo)
        item = self.store.items(kinds=(st.CAPTURE,))[0]
        self.store.decide(item.id, verb, actor="t")
        return self.store.get(item.id)

    def origin_file(self, rel: str) -> str | None:
        try:
            return self._git(self.origin, "show", f"main:{rel}")
        except subprocess.CalledProcessError:
            return None

    # -- producer ------------------------------------------------------------

    def test_scan_offers_each_committed_quarantine_file(self):
        (entry,) = captures.scan(self.repo)
        blob = self._git(self.repo, "rev-parse", "HEAD:knowledge/quarantine/idor-still-a01.md")
        head = self._git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(entry["key"], f"idor-still-a01@{blob[:12]}")
        self.assertEqual(entry["title"], "idor-still-a01")
        self.assertEqual(entry["agent"], "teaching-prep")
        self.assertIn("untraceable-claims", entry["detail"])
        self.assertIn("2026-05-10", entry["detail"])
        self.assertTrue(entry["ref"].endswith(f"/blob/{head}/knowledge/quarantine/idor-still-a01.md"))

    def test_uncommitted_quarantine_files_are_not_offered(self):
        self._write(self.repo, "knowledge/quarantine/local-only.md", QUARANTINED)
        self.assertEqual([e["title"] for e in captures.scan(self.repo)], ["idor-still-a01"])

    def test_sync_is_idempotent(self):
        created, _ = captures.sync(self.store, self.repo)
        again, _ = captures.sync(self.store, self.repo)
        self.assertEqual((len(created), len(again)), (1, 0))

    # -- applier ---------------------------------------------------------------

    def test_approve_files_the_concept_through_a_merged_pr(self):
        item = self.queued(st.APPROVE)
        ok, result = captures.apply(item, repo=self.repo, gh=self.gh, guard=self.allow, now=WHEN)
        self.assertTrue(ok, result)
        self.assertEqual(result, "https://github.com/example/nb/pull/9")
        concept = self.origin_file("knowledge/concepts/idor-still-a01.md")
        self.assertIn("verdict: PROMOTE", concept)
        self.assertIn(f"item {item.id}", concept)
        self.assertIsNone(self.origin_file("knowledge/quarantine/idor-still-a01.md"))
        self.assertIn("- [[idor-still-a01]]", self.origin_file("knowledge/index.md"))
        self.assertIn(f"promoted `idor-still-a01` from quarantine after human review (item {item.id})",
                      self.origin_file("knowledge/log.md"))
        subject = self._git(self.origin, "log", "-1", "--format=%s", "main")
        self.assertEqual(subject, f"wiki: promote idor-still-a01 from quarantine (review queue {item.id})")
        self.assertEqual([c[:2] for c in self.gh_calls],
                         [["pr", "list"], ["pr", "create"], ["pr", "merge"]])

    def test_the_shared_checkout_is_never_touched(self):
        head = self._git(self.repo, "rev-parse", "HEAD")
        item = self.queued(st.APPROVE)
        captures.apply(item, repo=self.repo, gh=self.gh, guard=self.allow, now=WHEN)
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self._git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self._git(self.repo, "status", "--porcelain"), "")
        wt = self.repo / captures.WORKTREE_REL
        self.assertEqual(self._git(wt, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD", "left detached")
        self.assertNotIn("queue/", self._git(self.repo, "branch", "--list"))

    def test_reject_deletes_it_from_quarantine(self):
        item = self.queued(st.REJECT)
        ok, result = captures.apply(item, repo=self.repo, gh=self.gh, guard=self.allow, now=WHEN)
        self.assertTrue(ok, result)
        self.assertIsNone(self.origin_file("knowledge/quarantine/idor-still-a01.md"))
        self.assertIsNone(self.origin_file("knowledge/concepts/idor-still-a01.md"))
        self.assertIn("rejected quarantined `idor-still-a01`", self.origin_file("knowledge/log.md"))

    def test_applying_twice_finds_the_first_merge(self):
        item = self.queued(st.APPROVE)
        captures.apply(item, repo=self.repo, gh=self.gh, guard=self.allow, now=WHEN)
        ok, result = captures.apply(item, repo=self.repo, gh=self.gh, guard=self.allow, now=WHEN)
        self.assertTrue(ok)
        self.assertTrue(result.startswith("already on main"), result)

    def test_existing_concept_is_never_overwritten(self):
        self._write(self.seed, "knowledge/concepts/idor-still-a01.md", "hand written\n")
        self._git(self.seed, "add", "-A")
        self._git(self.seed, "commit", "-q", "-m", "concept")
        self._git(self.seed, "push", "-q", "origin", "main")
        item = self.queued(st.APPROVE)
        ok, result = captures.apply(item, repo=self.repo, gh=self.gh, guard=self.allow, now=WHEN)
        self.assertFalse(ok)
        self.assertIn("already exists", result)
        self.assertEqual(self.origin_file("knowledge/concepts/idor-still-a01.md"), "hand written")
        self.assertEqual(self.gh_calls, [])

    def test_a_file_changed_since_the_card_was_sent_is_not_applied(self):
        item = self.queued(st.APPROVE)
        self._write(self.seed, "knowledge/quarantine/idor-still-a01.md", QUARANTINED + "\nedited\n")
        self._git(self.seed, "commit", "-qam", "edit")
        self._git(self.seed, "push", "-q", "origin", "main")
        ok, result = captures.apply(item, repo=self.repo, gh=self.gh, guard=self.allow, now=WHEN)
        self.assertFalse(ok)
        self.assertIn("changed on main", result)

    def test_guard_refusal_pushes_nothing(self):
        item = self.queued(st.APPROVE)
        ok, result = captures.apply(item, repo=self.repo, gh=self.gh, now=WHEN,
                                    guard=lambda *a: (False, "marked text"))
        self.assertFalse(ok)
        self.assertIn("outbound guard refused", result)
        branches = self._git(self.origin, "branch", "--list")
        self.assertNotIn("queue/", branches)
        self.assertEqual(self.gh_calls, [])

    def test_unmerged_pr_is_reported_with_its_url(self):
        item = self.queued(st.APPROVE)

        def gh(args):
            if args[:2] == ["pr", "merge"]:
                return False, "merge blocked"
            return self.gh(args)

        ok, result = captures.apply(item, repo=self.repo, gh=gh, guard=self.allow, now=WHEN)
        self.assertFalse(ok)
        self.assertIn("https://github.com/example/nb/pull/9", result)

    def test_a_file_already_gone_counts_as_done(self):
        item = self.queued(st.APPROVE)
        self._git(self.seed, "rm", "-q", "knowledge/quarantine/idor-still-a01.md")
        self._git(self.seed, "commit", "-qm", "moved by hand")
        self._git(self.seed, "push", "-q", "origin", "main")
        ok, result = captures.apply(item, repo=self.repo, gh=self.gh, guard=self.allow, now=WHEN)
        self.assertTrue(ok)
        self.assertIn("already left quarantine", result)

    def test_only_approve_and_reject_apply(self):
        item = self.queued(st.ACKNOWLEDGE)
        ok, result = captures.apply(item, repo=self.repo, gh=self.gh, guard=self.allow)
        self.assertFalse(ok)
        self.assertIn("approve or reject", result)


if __name__ == "__main__":
    unittest.main()
