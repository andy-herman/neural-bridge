"""Tests for github_client.close_issue (added in PR-K)."""

from __future__ import annotations

import subprocess as _sp
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.github_client import close_issue_sync  # noqa: E402


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestCloseIssueSync(unittest.TestCase):
    def test_close_without_comment(self):
        captured = []

        def fake_run(args, **kwargs):
            captured.append(args)
            return _FakeProc(0)

        with patch("scripts.discord_bot.github_client.subprocess.run", side_effect=fake_run):
            result = close_issue_sync(repo="x/y", issue_number=42)
        self.assertTrue(result.ok)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0], ["gh", "issue", "close", "42", "--repo", "x/y"])

    def test_close_with_comment_makes_two_calls(self):
        captured = []

        def fake_run(args, **kwargs):
            captured.append(args)
            return _FakeProc(0)

        with patch("scripts.discord_bot.github_client.subprocess.run", side_effect=fake_run):
            result = close_issue_sync(repo="x/y", issue_number=42, comment="bye")
        self.assertTrue(result.ok)
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0][:5], ["gh", "issue", "comment", "42", "--repo"])
        self.assertEqual(captured[1], ["gh", "issue", "close", "42", "--repo", "x/y"])

    def test_close_failed(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            return_value=_FakeProc(1, "", "no permission"),
        ):
            result = close_issue_sync(repo="x/y", issue_number=42)
        self.assertFalse(result.ok)
        self.assertIn("gh_exit_1", result.error)

    def test_comment_failed_short_circuits(self):
        captured = []

        def fake_run(args, **kwargs):
            captured.append(args)
            # Fail on the comment call
            return _FakeProc(1, "", "rate limited")

        with patch("scripts.discord_bot.github_client.subprocess.run", side_effect=fake_run):
            result = close_issue_sync(repo="x/y", issue_number=42, comment="hello")
        self.assertFalse(result.ok)
        self.assertIn("comment_exit_1", result.error)
        self.assertEqual(len(captured), 1)  # close was never attempted

    def test_timeout(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            side_effect=_sp.TimeoutExpired("gh", 1),
        ):
            result = close_issue_sync(repo="x/y", issue_number=42)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
