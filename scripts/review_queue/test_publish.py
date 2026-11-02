"""Publish items: Sunday prep's post, ready or held a week. The hold runs real
git against a throwaway origin; gh and the outbound guard are fakes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.review_queue import publish  # noqa: E402
from scripts.review_queue import store as st  # noqa: E402
from scripts.review_queue.captures import run_git  # noqa: E402

MONDAY = date(2026, 10, 19)
POST = "src/content/posts/the-queue.mdx"
TEXT = "---\ntitle: The queue\npubDate: 2026-10-19\ndraft: true\n---\nBody.\n"


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = st.Store(self.tmp / "q.db")

    def tearDown(self):
        self._tmp.cleanup()

    def item(self, now=None):
        item, _ = publish.raise_for_week(path=POST, slug="the-queue", title="The queue",
                                         pub_date=date(2026, 10, 19), monday=MONDAY,
                                         store=self.store, now=now)
        return item


class TestProducer(StoreCase):
    def test_one_card_per_post_and_week(self):
        item = self.item()
        self.assertEqual((item.kind, item.source, item.agent), (st.PUBLISH, publish.SOURCE, "content"))
        self.assertEqual(item.title, "Publishes Mon 19 Oct: The queue")
        self.assertTrue(item.ref.endswith(f"/blob/main/{POST}"))
        self.assertEqual(item.payload["publish_at"],
                         int(datetime(2026, 10, 20, 1, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(self.item().id, item.id, "Sunday prep run twice raises one card")

    def test_an_undecided_card_expires_once_the_run_has_gone(self):
        item = self.item(now=publish.publish_time(MONDAY) - 3 * 86400)
        self.assertEqual(publish.expire_stale(self.store, publish.publish_time(MONDAY) - 60), [])
        self.assertEqual(publish.expire_stale(self.store, publish.publish_time(MONDAY)), [item.id])
        self.assertEqual(self.store.get(item.id).state, st.SUPERSEDED)


class TestHeldText(unittest.TestCase):
    def test_only_the_date_moves(self):
        self.assertIn("pubDate: 2026-10-26\n", publish.held_text(TEXT, date(2026, 10, 26)))
        quoted = publish.held_text('pubDate: "2026-10-19T00:00:00Z"\n', date(2026, 10, 26))
        self.assertEqual(quoted, 'pubDate: "2026-10-26T00:00:00Z"\n')
        self.assertIsNone(publish.held_text("title: x\n", date(2026, 10, 26)))


class TestApply(StoreCase):
    def setUp(self):
        super().setUp()
        origin = self.tmp / "origin.git"
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", str(origin))
        seed = self.tmp / "seed"
        sh(self.tmp, "git", "clone", "-q", str(origin), str(seed))
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            sh(seed, "git", "config", k, v)
        (seed / POST).parent.mkdir(parents=True)
        (seed / POST).write_text(TEXT)
        sh(seed, "git", "add", "-A")
        sh(seed, "git", "commit", "-q", "-m", "seed")
        sh(seed, "git", "push", "-q", "origin", "main")
        self.origin = origin
        self.blog = self.tmp / "blog"
        sh(self.tmp, "git", "clone", "-q", str(origin), str(self.blog))
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            sh(self.blog, "git", "config", k, v)
        self.gh_calls: list[list[str]] = []

    def gh(self, args):
        self.gh_calls.append(args)
        if args[:2] == ["pr", "create"]:
            return True, "https://github.com/andy-herman/neural-bridge-blog/pull/80"
        return True, ""

    def decided(self, verb):
        item = self.item()
        self.store.decide(item.id, verb, actor="t")
        return self.store.get(item.id)

    def run_apply(self, item, guard=lambda *a: (True, "")):
        return publish.apply(item, git=run_git, gh=self.gh, guard=guard, blog=self.blog,
                             gh_slug="andy-herman/neural-bridge-blog")

    def test_ready_changes_nothing(self):
        ok, result = self.run_apply(self.decided(st.APPROVE))
        self.assertTrue(ok)
        self.assertIn("publishes", result)
        self.assertEqual(self.gh_calls, [])

    def test_hold_moves_the_pubdate_a_week_through_a_merged_pr(self):
        item = self.decided(st.REJECT)
        ok, result = self.run_apply(item)
        self.assertTrue(ok, result)
        self.assertIn("2026-10-26", result)
        branch = f"queue/{item.id}"
        pushed = sh(self.origin, "git", "show", f"{branch}:{POST}")
        self.assertIn("pubDate: 2026-10-26", pushed)
        self.assertIn("draft: true", pushed, "the post stays a draft")
        message = sh(self.origin, "git", "log", "-1", "--format=%B", branch)
        self.assertIn(f"review queue {item.id}", message)
        for leak in ("Co-Authored-By", "Claude"):
            self.assertNotIn(leak, message)
        self.assertEqual([c[:2] for c in self.gh_calls], [["pr", "create"], ["pr", "merge"]])
        self.assertEqual(sh(self.blog, "git", "worktree", "list").count("\n"), 0,
                         "the throwaway worktree is removed")
        self.assertEqual(sh(self.blog, "git", "status", "--porcelain"), "", "the blog checkout is untouched")

    def test_a_refusing_guard_pushes_nothing(self):
        item = self.decided(st.REJECT)
        ok, result = self.run_apply(item, guard=lambda *a: (False, "marked text"))
        self.assertFalse(ok)
        self.assertIn("nothing pushed", result)
        self.assertEqual(sh(self.origin, "git", "branch", "--list", f"queue/{item.id}"), "")

    def test_a_post_already_moved_or_published_is_left_alone(self):
        seed = self.tmp / "seed"
        (seed / POST).write_text(TEXT.replace("draft: true", "draft: false"))
        sh(seed, "git", "commit", "-q", "-am", "published")
        sh(seed, "git", "push", "-q", "origin", "main")
        ok, result = self.run_apply(self.decided(st.REJECT))
        self.assertTrue(ok)
        self.assertIn("no longer a draft", result)
        self.assertEqual(self.gh_calls, [])


if __name__ == "__main__":
    unittest.main()
