"""The Discord daemon's GitHub paths run through the outbound guard.

Synthetic controls only (scripts/outbound_guard_testing.py): the repo is
public, so no real marked text or marking phrase appears here. gh and git are
mocked; nothing leaves the machine.

Run: python3 scripts/discord_bot/test_outbound_guard_wiring.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts import outbound_guard as og  # noqa: E402
from scripts.discord_bot import agent_builder as ab  # noqa: E402
from scripts.discord_bot import github_client as gc  # noqa: E402
from scripts.discord_bot import pr_proposals as pp  # noqa: E402
from scripts.discord_bot import repos as repos_mod  # noqa: E402
from scripts.outbound_guard_testing import (  # noqa: E402
    FILLER, MARKING_PHRASE, SECRET_WORDS, SyntheticGuard, marked_excerpt, planted,
)

LEAK = planted(marked_excerpt(20))
_GUARD = SyntheticGuard()


def setUpModule():
    _GUARD.install()


def tearDownModule():
    _GUARD.remove()


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _no_subprocess():
    return mock.patch.object(gc.subprocess, "run", side_effect=AssertionError("gh must not run"))


class TestGithubClient(unittest.TestCase):
    """Every gh call that publishes text is refused before gh runs."""

    def assertRefused(self, result):
        self.assertFalse(result.ok)
        self.assertIn("outbound guard blocked", result.error)
        for word in SECRET_WORDS:
            self.assertNotIn(word, result.error.lower())

    def test_create_issue_screens_title_and_body(self):
        with _no_subprocess():
            for title, body in ((LEAK, "fine"), ("fine", LEAK), ("fine", f"cc {MARKING_PHRASE}")):
                with self.subTest(title=title[:20], body=body[:20]):
                    self.assertRefused(gc.create_issue_sync(repo="x/y", title=title, body=body))

    def test_comment(self):
        with _no_subprocess():
            self.assertRefused(gc.comment_issue_sync(repo="x/y", issue_number=7, body=LEAK))

    def test_close_with_a_marked_comment_neither_comments_nor_closes(self):
        with _no_subprocess():
            self.assertRefused(gc.close_issue_sync(repo="x/y", issue_number=7, comment=LEAK))

    def test_close_without_comment_publishes_no_text(self):
        with mock.patch.object(gc.subprocess, "run", return_value=_Proc()) as run:
            self.assertTrue(gc.close_issue_sync(repo="x/y", issue_number=7).ok)
        run.assert_called_once()

    def test_edit_issue_body(self):
        with _no_subprocess():
            self.assertRefused(gc.edit_issue_body_sync(repo="x/y", issue_number=7, new_body=LEAK))

    def test_async_wrappers_are_covered(self):
        with _no_subprocess():
            self.assertRefused(asyncio.run(gc.comment_issue(repo="x/y", issue_number=7, body=LEAK)))
            self.assertRefused(asyncio.run(gc.create_issue(repo="x/y", title="t", body=LEAK)))

    def test_clean_text_reaches_gh(self):
        with mock.patch.object(gc.subprocess, "run",
                               return_value=_Proc(stdout="https://github.com/x/y/issues/12\n")) as run:
            r = gc.create_issue_sync(repo="x/y", title="Baking notes", body=FILLER)
        self.assertTrue(r.ok)
        self.assertEqual(r.issue_number, 12)
        run.assert_called_once()

    def test_unusable_guard_refuses_even_clean_text(self):
        with mock.patch.dict(os.environ, {og.ENV_DIR: str(_GUARD.root / "empty")}), _no_subprocess():
            r = gc.comment_issue_sync(repo="x/y", issue_number=7, body="thanks, merged")
        self.assertFalse(r.ok)
        self.assertIn("no-index", r.error)

    def test_every_check_is_audited_with_its_surface(self):
        before = len(_GUARD.audit_records())
        with _no_subprocess():
            gc.comment_issue_sync(repo="x/y", issue_number=7, body=LEAK)
        record = _GUARD.audit_records()[before]
        self.assertEqual((record["surface"], record["allowed"]), ("github:comment", False))


class TestPRProposals(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = repos_mod.REPOS["neural-bridge-blog"]
        repos_mod.REPOS["neural-bridge-blog"] = repos_mod.Repo(
            repo_id="neural-bridge-blog", gh_slug="andy-herman/neural-bridge-blog",
            local_path=Path(self._tmp.name), default_branch="main",
        )

    def tearDown(self):
        repos_mod.REPOS["neural-bridge-blog"] = self._orig
        self._tmp.cleanup()

    def _action(self, **over) -> dict:
        action = {"action": "open_pr_with_changes", "repo": "neural-bridge-blog", "branch": "luna/notes",
                  "commit_message": "docs: notes", "pr_title": "docs: notes", "pr_body": "body",
                  "files": [{"path": "src/notes.md", "content": FILLER}]}
        action.update(over)
        return action

    def _proposal(self, content: str = "hello") -> pp.PRProposal:
        return pp.PRProposal(proposal_id="abc12345", agent_id="luna", channel_id=1,
                             repo=repos_mod.REPOS["neural-bridge-blog"], branch="luna/notes",
                             files=[("src/notes.md", content)], commit_message="docs: notes",
                             pr_title="docs: notes", pr_body="body")

    def test_staging_refuses_marked_text_anywhere_in_the_proposal(self):
        for over in ({"files": [{"path": "src/notes.md", "content": LEAK}]},
                     {"pr_body": LEAK}, {"commit_message": LEAK}, {"pr_title": f"re {MARKING_PHRASE}"},
                     {"branch": "luna/zephyrine-internal-only"}):
            with self.subTest(field=next(iter(over))):
                v = pp.validate_open_pr_action(self._action(**over), agent_id="luna", channel_id=1)
                self.assertFalse(v.ok)
                self.assertIn("outbound guard blocked", v.error)

    def test_staging_accepts_clean_proposal(self):
        v = pp.validate_open_pr_action(self._action(), agent_id="luna", channel_id=1)
        self.assertTrue(v.ok, v.error)

    def test_execution_rescreens_before_touching_the_working_tree(self):
        # e.g. the index gained a newly marked note while the proposal waited for approval
        with mock.patch.object(pp, "_git") as git, mock.patch.object(pp, "_gh") as gh:
            result = pp.execute_proposal(self._proposal(LEAK))
        self.assertFalse(result.ok)
        self.assertIn("outbound guard blocked", result.error)
        git.assert_not_called()
        gh.assert_not_called()

    def test_push_is_screened_after_commit_and_refused(self):
        calls = []

        def fake_git(_cwd, args, timeout=60):
            calls.append(args)
            if args[0] == "show-ref":
                return False, "no such ref"
            if args[0] == "log" and "-p" in args:  # an unexpected added line in the unpushed commits
                return True, "diff --git a/x b/x\n+++ b/x\n" + "\n".join(f"+{w}" for w in LEAK.split(". "))
            return True, ""

        with mock.patch.object(pp, "_git", side_effect=fake_git), \
             mock.patch.object(pp, "_gh", side_effect=AssertionError("no PR may open")):
            result = pp.execute_proposal(self._proposal())
        self.assertFalse(result.ok)
        self.assertIn("nothing pushed", result.error)
        self.assertNotIn("push", [a[0] for a in calls])
        self.assertIn(["commit", "-m", "docs: notes", "--", "src/notes.md"], calls)
        self.assertEqual(calls[-1], ["checkout", "main"])


class TestPRProposalAgainstARealRepo(unittest.TestCase):
    """execute_proposal end to end on a temporary repo and remote; only gh is mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.remote, self.work = root / "remote.git", root / "work"
        self._git(root, "-c", "init.defaultBranch=main", "init", "--bare", str(self.remote))
        self._git(root, "-c", "init.defaultBranch=main", "init", str(self.work))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
            self._git(self.work, "config", key, value)
        self._git(self.work, "remote", "add", "origin", str(self.remote))
        (self.work / "README.md").write_text("hello\n", encoding="utf-8")
        self._git(self.work, "add", "README.md")
        self._git(self.work, "commit", "-m", "initial")
        self._git(self.work, "push", "-u", "origin", "main")
        self._orig = repos_mod.REPOS["neural-bridge-blog"]
        repos_mod.REPOS["neural-bridge-blog"] = repos_mod.Repo(
            repo_id="neural-bridge-blog", gh_slug="andy-herman/neural-bridge-blog",
            local_path=self.work, default_branch="main",
        )

    def tearDown(self):
        repos_mod.REPOS["neural-bridge-blog"] = self._orig
        self._tmp.cleanup()

    @staticmethod
    def _git(cwd, *args):
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def _proposal(self, branch):
        return pp.PRProposal(proposal_id="e2e00001", agent_id="luna", channel_id=1,
                             repo=repos_mod.REPOS["neural-bridge-blog"], branch=branch,
                             files=[("src/notes.md", FILLER)], commit_message="docs: notes",
                             pr_title="docs: notes", pr_body="body")

    def test_clean_proposal_pushes_only_its_own_paths(self):
        (self.work / "private.txt").write_text("a parallel terminal's staged work\n", encoding="utf-8")
        self._git(self.work, "add", "private.txt")
        with mock.patch.object(pp, "_gh", return_value=(True, "https://github.com/x/y/pull/1")):
            result = pp.execute_proposal(self._proposal("luna/e2e"))
        self.assertTrue(result.ok, result.error)
        pushed = self._git(self.remote, "ls-tree", "-r", "--name-only", "luna/e2e").split()
        self.assertEqual(sorted(pushed), ["README.md", "src/notes.md"])
        self.assertIn("A  private.txt", self._git(self.work, "status", "--short"))  # still staged, not pushed
        self.assertEqual(self._git(self.work, "branch", "--show-current").strip(), "main")

    def test_unpushed_local_commit_with_marked_text_stops_the_push(self):
        (self.work / "local.md").write_text(LEAK + "\n", encoding="utf-8")
        self._git(self.work, "add", "local.md")
        self._git(self.work, "commit", "-m", "local work, never pushed")
        with mock.patch.object(pp, "_gh", side_effect=AssertionError("no PR may open")):
            result = pp.execute_proposal(self._proposal("luna/e2e-blocked"))
        self.assertFalse(result.ok)
        self.assertIn("nothing pushed", result.error)
        self.assertNotIn("luna/e2e-blocked", self._git(self.remote, "branch", "--list"))
        self.assertEqual(self._git(self.work, "branch", "--show-current").strip(), "main")


def _agent_action(**over) -> dict:
    action = {"agent_id": "zz-guard-test", "display_name": "Guard Test",
              "description": "Exercises the outbound guard wiring.", "color": "cyan",
              "tools": ["Read"], "model": "sonnet",
              "body": "You are a test agent that exists only to exercise the create_agent path. " * 3}
    action.update(over)
    return action


class TestAgentBuilder(unittest.TestCase):
    CONSTANTS = ("AGENTS_DIR", "SESSION_END", "SCHEMA_PY", "MARKETPLACE_JSON", "PLUGIN_JSON", "AGENTS_JSON")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._saved = {name: getattr(ab, name) for name in self.CONSTANTS}
        ab.AGENTS_DIR = root / "agents"
        ab.AGENTS_DIR.mkdir()
        ab.SESSION_END = root / "session_end.py"
        ab.SCHEMA_PY = root / "schema.py"
        for path in (ab.SESSION_END, ab.SCHEMA_PY):
            path.write_text('KNOWN_AGENTS = {\n    "research",\n}\n', encoding="utf-8")
        ab.MARKETPLACE_JSON = root / "marketplace.json"
        ab.MARKETPLACE_JSON.write_text(json.dumps({"plugins": [{"version": "1.2.0"}]}), encoding="utf-8")
        ab.PLUGIN_JSON = root / "plugin.json"
        ab.PLUGIN_JSON.write_text(json.dumps({"version": "1.2.0"}), encoding="utf-8")
        ab.AGENTS_JSON = root / "agents.json"
        ab.AGENTS_JSON.write_text(json.dumps({"agents": []}), encoding="utf-8")

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(ab, name, value)
        self._tmp.cleanup()

    def test_marked_fields_are_refused_before_any_change(self):
        for over in ({"body": "Background for this agent: " + LEAK},
                     {"description": f"Handles {MARKING_PHRASE} requests"},
                     {"display_name": "Zephyrine Internal Only Desk"}):
            with self.subTest(field=next(iter(over))):
                with mock.patch.object(ab, "_git") as git, mock.patch.object(ab, "_gh") as gh:
                    result = ab.execute_create_agent(_agent_action(**over), "x/y")
                self.assertFalse(result.ok)
                self.assertIn("outbound guard blocked", result.error)
                git.assert_not_called()
                gh.assert_not_called()
                self.assertEqual(list(ab.AGENTS_DIR.iterdir()), [])

    def test_only_the_files_it_wrote_are_staged(self):
        calls = []

        def fake_git(args, cwd=None, timeout=30):
            calls.append(args)
            return True, ""

        with mock.patch.object(ab, "_git", side_effect=fake_git), \
             mock.patch.object(ab, "_gh", return_value=(True, "https://github.com/x/y/pull/9")):
            result = ab.execute_create_agent(_agent_action(), "x/y")
        self.assertTrue(result.ok, result.error)
        paths = [str(ab.AGENTS_DIR / "zz-guard-test.md"), str(ab.SESSION_END), str(ab.SCHEMA_PY),
                 str(ab.MARKETPLACE_JSON), str(ab.PLUGIN_JSON)]
        self.assertEqual([a for a in calls if a[0] == "add"], [["add", "--", *paths]])
        commit = next(a for a in calls if a[0] == "commit")
        self.assertEqual(commit[3:], ["--", *paths])  # only these paths, whatever else is staged
        self.assertIn(["push", "-u", "origin", "feat/agent-zz-guard-test"], calls)

    def test_push_is_screened_after_commit_and_refused(self):
        calls = []

        def fake_git(args, cwd=None, timeout=30):
            calls.append(args)
            if args[0] == "log" and "-p" in args:  # e.g. a local unpushed commit carrying marked text
                return True, "diff --git a/x b/x\n+++ b/x\n+" + LEAK
            return True, ""

        with mock.patch.object(ab, "_git", side_effect=fake_git), \
             mock.patch.object(ab, "_gh", side_effect=AssertionError("no PR may open")):
            result = ab.execute_create_agent(_agent_action(), "x/y")
        self.assertFalse(result.ok)
        self.assertIn("nothing pushed", result.error)
        self.assertNotIn("push", [a[0] for a in calls])
        self.assertEqual(calls[-1], ["checkout", "main"])


class TestHandlerReplies(unittest.TestCase):
    def test_refused_issue_title_is_not_echoed_to_discord(self):
        from types import SimpleNamespace
        from scripts.discord_bot import handlers

        action = {"action": "create_issue", "title": marked_excerpt(20), "body": "see title"}
        with _no_subprocess():
            results, _ = asyncio.run(handlers._execute_action_batch(
                [action], SimpleNamespace(default_repo="x/y"), agent_id="research", channel_id=1))
        self.assertEqual(len(results), 1)
        self.assertIn("outbound guard blocked", results[0])
        for word in SECRET_WORDS + ("routing", "pilot"):
            self.assertNotIn(word, results[0].lower())


class TestDaemonRefreshLoop(unittest.TestCase):
    def test_refreshes_at_start_and_reports_failures(self):
        from scripts.discord_bot import main as daemon

        async def scenario(refresh):
            task = asyncio.create_task(daemon.outbound_guard_refresh_loop(interval=3600))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if refresh.called:
                    break
            await asyncio.sleep(0.05)  # let the loop log before it sleeps
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with mock.patch.object(daemon.outbound_guard, "refresh",
                               return_value=(False, "vault not found")) as refresh, \
             mock.patch.object(daemon, "log") as log, \
             mock.patch.object(daemon, "fleet_log_event") as fleet:
            asyncio.run(scenario(refresh))
        refresh.assert_called_once()
        log.assert_any_call("outbound guard refresh FAILED: vault not found")
        fleet.assert_called_once_with("outbound guard refresh failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
