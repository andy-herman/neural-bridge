"""Unit tests for pr_proposals.py — proposal validation, approval text
matching, store TTL behavior. Stdlib-only.

End-to-end git/gh execution is not tested here — that requires a real
repo + network. The execute_proposal() boundary uses _git/_gh
subprocess wrappers that can be mocked in integration tests later.

Run: `python3 scripts/discord_bot/test_pr_proposals.py`
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot import pr_proposals as pp  # noqa: E402
from scripts.discord_bot import repos as repos_mod  # noqa: E402
from scripts.discord_bot.pr_proposals import (  # noqa: E402
    PRProposal,
    ProposalStore,
    format_preview,
    is_approval_text,
    is_cancel_text,
    validate_open_pr_action,
)


# ---------- approval / cancel text matching ----------

class TestApprovalText(unittest.TestCase):
    def test_plain_approve(self):
        ok, pid = is_approval_text("approve")
        self.assertTrue(ok)
        self.assertIsNone(pid)

    def test_approve_with_id(self):
        ok, pid = is_approval_text("approve a1b2c3d4")
        self.assertTrue(ok)
        self.assertEqual(pid, "a1b2c3d4")

    def test_ship_it(self):
        ok, _ = is_approval_text("ship it")
        self.assertTrue(ok)

    def test_go_ahead(self):
        ok, _ = is_approval_text("go ahead")
        self.assertTrue(ok)
        ok, _ = is_approval_text("go  ahead")
        self.assertTrue(ok)

    def test_lgtm(self):
        ok, _ = is_approval_text("LGTM")
        self.assertTrue(ok)
        ok, _ = is_approval_text("lgtm")
        self.assertTrue(ok)

    def test_do_it(self):
        ok, _ = is_approval_text("do it")
        self.assertTrue(ok)

    def test_plain_yes_does_not_approve(self):
        """Conservative matching — plain `yes` is too ambient to count."""
        ok, _ = is_approval_text("yes")
        self.assertFalse(ok)

    def test_unrelated_text_does_not_approve(self):
        for text in ("can you check this", "approve this draft please", "approved by me yesterday"):
            ok, _ = is_approval_text(text)
            self.assertFalse(ok, f"unexpectedly matched: {text!r}")

    def test_empty_string(self):
        self.assertEqual(is_approval_text(""), (False, None))


class TestCancelText(unittest.TestCase):
    def test_plain_cancel(self):
        ok, _ = is_cancel_text("cancel")
        self.assertTrue(ok)

    def test_drop_it(self):
        ok, _ = is_cancel_text("drop it")
        self.assertTrue(ok)
        ok, _ = is_cancel_text("drop")
        self.assertTrue(ok)

    def test_nevermind(self):
        ok, _ = is_cancel_text("nevermind")
        self.assertTrue(ok)
        ok, _ = is_cancel_text("never mind")
        self.assertTrue(ok)

    def test_cancel_with_id(self):
        ok, pid = is_cancel_text("cancel f00fba12")
        self.assertTrue(ok)
        self.assertEqual(pid, "f00fba12")

    def test_plain_no_does_not_cancel(self):
        ok, _ = is_cancel_text("no")
        self.assertFalse(ok)


# ---------- validate_open_pr_action ----------

class TestValidate(unittest.TestCase):
    def setUp(self):
        # Point one of the registry repos at a tempdir that exists, so the
        # local_path check passes. We restore in tearDown.
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name)
        self._orig = repos_mod.REPOS["neural-bridge-blog"]
        repos_mod.REPOS["neural-bridge-blog"] = repos_mod.Repo(
            repo_id="neural-bridge-blog",
            gh_slug="andy-herman/neural-bridge-blog",
            local_path=self._tmp_path,
            default_branch="main",
        )

    def tearDown(self):
        repos_mod.REPOS["neural-bridge-blog"] = self._orig
        self._tmp.cleanup()

    def _good_action(self, **over) -> dict:
        base = {
            "action": "open_pr_with_changes",
            "repo": "neural-bridge-blog",
            "branch": "luna/test",
            "commit_message": "fix: test",
            "pr_title": "fix: test",
            "pr_body": "body",
            "files": [{"path": "src/foo.mdx", "content": "hello"}],
        }
        base.update(over)
        return base

    def test_ok_minimal(self):
        v = validate_open_pr_action(self._good_action(), agent_id="luna", channel_id=42)
        self.assertTrue(v.ok, v.error)
        self.assertEqual(v.proposal.repo.repo_id, "neural-bridge-blog")
        self.assertEqual(v.proposal.agent_id, "luna")
        self.assertEqual(v.proposal.channel_id, 42)
        self.assertEqual(len(v.proposal.files), 1)
        self.assertEqual(v.proposal.files[0], ("src/foo.mdx", "hello"))

    def test_unknown_repo(self):
        v = validate_open_pr_action(
            self._good_action(repo="huskyhub"), agent_id="luna", channel_id=42,
        )
        self.assertFalse(v.ok)
        self.assertIn("unknown repo", v.error)

    def test_agent_not_in_allowlist(self):
        v = validate_open_pr_action(
            self._good_action(), agent_id="echo", channel_id=42,
        )
        self.assertFalse(v.ok)
        self.assertIn("not in the push allowlist", v.error)

    def test_branch_equal_to_default_rejected(self):
        v = validate_open_pr_action(
            self._good_action(branch="main"), agent_id="luna", channel_id=42,
        )
        self.assertFalse(v.ok)
        self.assertIn("must not equal default branch", v.error)

    def test_branch_with_invalid_chars(self):
        for bad in ("luna/test space", "luna/test;rm -rf", "$(pwd)", "luna/../etc"):
            v = validate_open_pr_action(
                self._good_action(branch=bad), agent_id="luna", channel_id=42,
            )
            self.assertFalse(v.ok, f"branch={bad!r} unexpectedly accepted")

    def test_path_traversal_blocked(self):
        for bad in ("../etc/passwd", "src/../../etc", "/etc/passwd", "src\\foo"):
            files = [{"path": bad, "content": "x"}]
            v = validate_open_pr_action(
                self._good_action(files=files), agent_id="luna", channel_id=42,
            )
            self.assertFalse(v.ok, f"path={bad!r} unexpectedly accepted")

    def test_files_must_be_nonempty(self):
        v = validate_open_pr_action(
            self._good_action(files=[]), agent_id="luna", channel_id=42,
        )
        self.assertFalse(v.ok)
        self.assertIn("non-empty list", v.error)

    def test_too_many_files(self):
        files = [{"path": f"src/f{i}.mdx", "content": "x"} for i in range(pp.MAX_FILES_PER_PR + 1)]
        v = validate_open_pr_action(
            self._good_action(files=files), agent_id="luna", channel_id=42,
        )
        self.assertFalse(v.ok)
        self.assertIn("too many files", v.error)

    def test_file_too_large(self):
        files = [{"path": "src/big.mdx", "content": "x" * (pp.MAX_FILE_BYTES + 1)}]
        v = validate_open_pr_action(
            self._good_action(files=files), agent_id="luna", channel_id=42,
        )
        self.assertFalse(v.ok)
        self.assertIn("bytes; cap is", v.error)

    def test_total_payload_cap(self):
        # Each file just under MAX_FILE_BYTES, enough files that total exceeds
        # MAX_TOTAL_BYTES. MAX_FILE_BYTES=200_000, MAX_TOTAL_BYTES=800_000,
        # so 5 files at 190_000 = 950_000 bytes total.
        per_file = pp.MAX_FILE_BYTES - 10_000
        n = (pp.MAX_TOTAL_BYTES // per_file) + 1  # one more than fits
        files = [{"path": f"src/f{i}.mdx", "content": "x" * per_file} for i in range(n)]
        v = validate_open_pr_action(
            self._good_action(files=files), agent_id="luna", channel_id=42,
        )
        self.assertFalse(v.ok)
        self.assertIn("total payload", v.error)

    def test_duplicate_paths_rejected(self):
        files = [
            {"path": "src/foo.mdx", "content": "a"},
            {"path": "src/foo.mdx", "content": "b"},
        ]
        v = validate_open_pr_action(
            self._good_action(files=files), agent_id="luna", channel_id=42,
        )
        self.assertFalse(v.ok)
        self.assertIn("duplicated", v.error)

    def test_missing_repo_field(self):
        bad = self._good_action()
        del bad["repo"]
        v = validate_open_pr_action(bad, agent_id="luna", channel_id=42)
        self.assertFalse(v.ok)


# ---------- ProposalStore ----------

class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = ProposalStore()

    def _mk(self, *, channel_id=1, agent_id="luna", created_at=None):
        repo = repos_mod.REPOS["neural-bridge-blog"]
        return PRProposal(
            proposal_id=pp._new_id(),
            agent_id=agent_id,
            channel_id=channel_id,
            repo=repo,
            branch="luna/x",
            files=[("a.txt", "x")],
            commit_message="m",
            pr_title="t",
            pr_body="b",
            created_at=created_at if created_at is not None else time.time(),
        )

    def test_stage_and_get(self):
        p = self._mk()
        pid = self.store.stage(p)
        self.assertEqual(self.store.get(pid).proposal_id, pid)

    def test_peek_returns_most_recent(self):
        p1 = self._mk(created_at=time.time() - 100)
        p2 = self._mk(created_at=time.time() - 50)
        self.store.stage(p1)
        self.store.stage(p2)
        peeked = self.store.peek_for_channel_agent(1, "luna")
        self.assertEqual(peeked.proposal_id, p2.proposal_id)

    def test_pop_removes(self):
        p = self._mk()
        pid = self.store.stage(p)
        popped = self.store.pop(pid)
        self.assertEqual(popped.proposal_id, pid)
        self.assertIsNone(self.store.get(pid))

    def test_expired_proposals_pruned_on_access(self):
        p = self._mk(created_at=time.time() - pp.TTL_SECONDS - 1)
        pid = self.store.stage(p)
        # First access prunes — expired proposal disappears.
        self.assertIsNone(self.store.get(pid))

    def test_channel_isolation(self):
        p1 = self._mk(channel_id=1)
        p2 = self._mk(channel_id=2)
        self.store.stage(p1)
        self.store.stage(p2)
        self.assertEqual(self.store.peek_for_channel_agent(1, "luna").channel_id, 1)
        self.assertEqual(self.store.peek_for_channel_agent(2, "luna").channel_id, 2)

    def test_agent_isolation(self):
        p1 = self._mk(agent_id="luna")
        p2 = self._mk(agent_id="content")
        self.store.stage(p1)
        self.store.stage(p2)
        self.assertEqual(self.store.peek_for_channel_agent(1, "luna").agent_id, "luna")
        self.assertEqual(self.store.peek_for_channel_agent(1, "content").agent_id, "content")


# ---------- format_preview ----------

class TestFormatPreview(unittest.TestCase):
    def test_contains_key_fields(self):
        repo = repos_mod.REPOS["neural-bridge-blog"]
        p = PRProposal(
            proposal_id="abc12345",
            agent_id="luna",
            channel_id=1,
            repo=repo,
            branch="luna/fix-typo",
            files=[("src/about.mdx", "x" * 100)],
            commit_message="m",
            pr_title="fix: typo",
            pr_body="b",
        )
        out = format_preview(p)
        self.assertIn("luna", out)
        self.assertIn("luna/fix-typo", out)
        self.assertIn("fix: typo", out)
        self.assertIn("src/about.mdx", out)
        self.assertIn("abc12345", out)
        self.assertIn("approve abc12345", out)
        self.assertIn("cancel abc12345", out)


# ---------- repos.py basics ----------

class TestRepos(unittest.TestCase):
    def test_agent_can_push(self):
        self.assertTrue(repos_mod.agent_can_push_to("luna", "neural-bridge-blog"))
        self.assertFalse(repos_mod.agent_can_push_to("luna", "neural-bridge"))
        self.assertFalse(repos_mod.agent_can_push_to("echo", "neural-bridge-blog"))
        self.assertFalse(repos_mod.agent_can_push_to("nonexistent", "neural-bridge-blog"))

    def test_pushable_repos_for_unknown_agent_empty(self):
        self.assertEqual(repos_mod.pushable_repos_for("nonexistent"), set())

    def test_repo_for_unknown(self):
        self.assertIsNone(repos_mod.repo_for("nope"))


if __name__ == "__main__":
    unittest.main()
