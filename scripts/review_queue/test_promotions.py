"""The nightly compile, staged: articles become cards with private previews,
and nothing reaches the shared checkout or GitHub until one is filed. Real
git against a throwaway origin; the compile itself, gh and the outbound guard
are fakes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.review_queue import promotions as pm  # noqa: E402
from scripts.review_queue import render  # noqa: E402
from scripts.review_queue import store as st  # noqa: E402
from scripts.review_queue.captures import run_git  # noqa: E402

INDEX = "# Index\n\n## Concepts\n\n- [[existing-concept]]\n"
LOG = "# Log\n\n## 2026-10-01\n\n- seeded\n"
EXISTING = "---\nslug: existing-concept\nverdict: PROMOTE\n---\n\n# existing-concept\n\nOld text.\n"


def concept(slug: str, body: str = "A lesson.", verdict: str = "PROMOTE") -> str:
    return (f"---\nslug: {slug}\nverdict: {verdict}\nreason: 3-vote gate\nsources:\n  - agent: luna\n"
            f"    session_id: s1\n---\n\n# {slug}\n\n{body}\n")


QUARANTINED = ("---\nslug: held-concept\nverdict: QUARANTINE\nchecks_triggered: [untraceable]\n"
               "sources:\n  - agent: research\n---\n\n## Proposed summary\n\nA held idea.\n")


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.origin = self.tmp / "origin.git"
        sh(self.tmp, "git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.tmp / "repo"
        sh(self.tmp, "git", "clone", "-q", str(self.origin), str(self.repo))
        for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
            sh(self.repo, "git", "config", k, v)
        k = self.repo / "knowledge"
        (k / "concepts").mkdir(parents=True)
        (k / "index.md").write_text(INDEX)
        (k / "log.md").write_text(LOG)
        (k / "concepts" / "existing-concept.md").write_text(EXISTING)
        (self.repo / ".gitignore").write_text(".trees/\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "seed")
        sh(self.repo, "git", "push", "-q", "origin", "main")
        self.store = st.Store(self.tmp / "q.db")
        self.vault = self.tmp / "vault" / "Neural Bridge"
        self.vault.mkdir(parents=True)
        self.previews = self.vault / "Wiki staging"
        self.gh_calls: list[list[str]] = []

    def tearDown(self):
        self._tmp.cleanup()

    def gh(self, args):
        self.gh_calls.append(args)
        if args[:2] == ["pr", "create"]:
            return True, "https://github.com/andy-herman/neural-bridge/pull/300"
        return True, ""

    def compile_writes(self, files: dict[str, str]):
        def fake(knowledge: Path, args):
            for rel, text in files.items():
                (knowledge / rel).parent.mkdir(parents=True, exist_ok=True)
                (knowledge / rel).write_text(text)
            return 0
        return fake

    def stage(self, files):
        return pm.stage(self.store, self.repo, git=run_git, compile_fn=self.compile_writes(files),
                        preview_dir=self.previews)

    def staging_clean(self) -> bool:
        return sh(self.repo / pm.STAGING_REL, "git", "status", "--porcelain") == ""


class TestStage(Case):
    def test_articles_become_cards_with_previews_and_nothing_else_changes(self):
        created, rc = self.stage({
            "concepts/fresh-lesson.md": concept("fresh-lesson"),
            "quarantine/held-concept.md": QUARANTINED,
            "connections/existing-concept--fresh-lesson.md": "linked",
            "index.md": INDEX + "- [[fresh-lesson]]\n",
            "log.md": LOG + "\n## 2026-10-16\n\n- compile complete\n",
        })
        self.assertEqual(rc, 0)
        by_slug = {i.payload["slug"]: i for i in created}
        self.assertEqual(set(by_slug), {"fresh-lesson", "held-concept"})
        fresh = by_slug["fresh-lesson"]
        self.assertEqual((fresh.kind, fresh.source, fresh.agent), (st.CAPTURE, pm.SOURCE, "luna"))
        self.assertEqual(fresh.title, "New wiki concept: fresh-lesson")
        self.assertIn("Wiki staging/fresh-lesson.md", fresh.detail)
        self.assertEqual(fresh.ref, "", "nothing to link to: it is not on GitHub")
        self.assertEqual(list(fresh.payload["connections"]), ["knowledge/connections/existing-concept--fresh-lesson.md"])
        self.assertEqual(by_slug["held-concept"].payload["verdict"], "QUARANTINE")
        self.assertIn("A lesson.", (self.previews / "fresh-lesson.md").read_text())
        self.assertTrue(self.staging_clean(), "the staging copy is reset after the run")
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "", "the shared checkout is untouched")
        self.assertEqual(sh(self.origin, "git", "branch", "--list"), "* main", "nothing pushed")

    def test_a_restaged_slug_supersedes_its_older_card(self):
        [first], _ = self.stage({"concepts/fresh-lesson.md": concept("fresh-lesson", "v1")})
        [second], _ = self.stage({"concepts/fresh-lesson.md": concept("fresh-lesson", "v2")})
        self.assertEqual(self.store.get(first.id).state, st.SUPERSEDED)
        self.assertEqual(self.store.get(second.id).state, st.OPEN)
        self.assertEqual(self.stage({"concepts/fresh-lesson.md": concept("fresh-lesson", "v2")})[0], [],
                         "the same text staged again raises nothing")

    def test_a_quiet_night_raises_nothing(self):
        self.assertEqual(self.stage({})[0], [])


class TestApply(Case):
    NOW = datetime(2026, 10, 16, 3, 5, tzinfo=timezone.utc)

    def decided(self, files, verb):
        created, _ = self.stage(files)
        item = created[0]
        self.store.decide(item.id, verb, actor="t")
        return self.store.get(item.id)

    def run_apply(self, item, guard=lambda *a: (True, "")):
        return pm.apply(item, repo=self.repo, git=run_git, gh=self.gh, guard=guard, now=self.NOW,
                        preview_dir=self.previews)

    def test_filing_publishes_the_article_with_its_index_log_and_connection(self):
        item = self.decided({"concepts/fresh-lesson.md": concept("fresh-lesson"),
                             "connections/existing-concept--fresh-lesson.md": "linked",
                             "connections/fresh-lesson--not-on-main.md": "dangling"}, st.APPROVE)
        ok, result = self.run_apply(item)
        self.assertTrue(ok, result)
        self.assertEqual(result, "https://github.com/andy-herman/neural-bridge/pull/300")
        branch = f"queue/{item.id}"
        article = sh(self.origin, "git", "show", f"{branch}:knowledge/concepts/fresh-lesson.md")
        self.assertIn(f"approved by Andy in the review queue (item {item.id}", article)
        self.assertIn("A lesson.", article)
        self.assertIn("[[fresh-lesson]]", sh(self.origin, "git", "show", f"{branch}:knowledge/index.md"))
        self.assertIn(f"item {item.id}", sh(self.origin, "git", "show", f"{branch}:knowledge/log.md"))
        files = sh(self.origin, "git", "ls-tree", "-r", "--name-only", branch)
        self.assertIn("knowledge/connections/existing-concept--fresh-lesson.md", files)
        self.assertNotIn("not-on-main", files, "a connection to a concept that is not on main waits")
        message = sh(self.origin, "git", "log", "-1", "--format=%B", branch)
        for leak in ("Co-Authored-By", "Claude"):
            self.assertNotIn(leak, message)
        self.assertEqual([c[:2] for c in self.gh_calls][-2:], [["pr", "create"], ["pr", "merge"]])
        self.assertFalse((self.previews / "fresh-lesson.md").exists(), "the preview goes once it is filed")
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")

    def test_a_repromotion_archives_the_version_it_replaces(self):
        item = self.decided({"concepts/existing-concept.md": concept("existing-concept", "New text.")},
                            st.APPROVE)
        ok, result = self.run_apply(item)
        self.assertTrue(ok, result)
        files = sh(self.origin, "git", "ls-tree", "-r", "--name-only", f"queue/{item.id}")
        self.assertIn("knowledge/concepts/.history/existing-concept/2026-10-16T03-05-00Z.md", files)
        self.assertIn("New text.", sh(self.origin, "git", "show",
                                      f"queue/{item.id}:knowledge/concepts/existing-concept.md"))

    def test_a_quarantined_article_is_promoted_when_filed(self):
        item = self.decided({"quarantine/held-concept.md": QUARANTINED}, st.APPROVE)
        ok, result = self.run_apply(item)
        self.assertTrue(ok, result)
        article = sh(self.origin, "git", "show", f"queue/{item.id}:knowledge/concepts/held-concept.md")
        self.assertIn("verdict: PROMOTE", article)
        self.assertIn("A held idea.", article)

    def test_dropping_or_a_refusing_guard_publishes_nothing(self):
        item = self.decided({"concepts/fresh-lesson.md": concept("fresh-lesson")}, st.REJECT)
        self.assertEqual(self.run_apply(item), (True, "discarded; nothing reached the wiki"))
        self.assertFalse((self.previews / "fresh-lesson.md").exists())
        item = self.decided({"concepts/other-lesson.md": concept("other-lesson")}, st.APPROVE)
        ok, result = self.run_apply(item, guard=lambda *a: (False, "marked text"))
        self.assertFalse(ok)
        self.assertIn("nothing pushed", result)
        self.assertEqual(sh(self.origin, "git", "branch", "--list"), "* main")
        self.assertEqual(self.gh_calls, [])


class TestCards(Case):
    def test_staged_confirm_text_says_it_publishes(self):
        [item], _ = self.stage({"concepts/fresh-lesson.md": concept("fresh-lesson")})
        self.assertIn("publishes it to the public repo", render.confirm_text(item, st.APPROVE))
        self.assertIn("Nothing has been published", render.confirm_text(item, st.REJECT))


class TestCompileOverride(unittest.TestCase):
    def test_compile_writes_where_the_staging_env_points(self):
        out = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, 'scripts'); import compile as c; "
                                   "print(c.CONCEPTS_DIR, c.WIKI_INDEX)"],
            cwd=REPO_ROOT, capture_output=True, text=True,
            env={"NB_COMPILE_KNOWLEDGE_DIR": "/tmp/staged/knowledge", "PATH": "/usr/bin:/bin",
                 "HOME": str(Path.home())})
        self.assertEqual(out.stdout.split(), ["/tmp/staged/knowledge/concepts", "/tmp/staged/knowledge/index.md"],
                         out.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
