"""Tests for github_client.edit_issue_body."""

from __future__ import annotations

import subprocess as _sp
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.github_client import edit_issue_body_sync  # noqa: E402


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestEditIssueBodySync(unittest.TestCase):
    def test_happy_path(self):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["input"] = kwargs.get("input")
            return _FakeProc(0)

        with patch("scripts.discord_bot.github_client.subprocess.run", side_effect=fake_run):
            result = edit_issue_body_sync(
                repo="x/y", issue_number=42, new_body="new body content",
            )
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        # Verify gh args
        self.assertEqual(
            captured["args"],
            ["gh", "issue", "edit", "42", "--repo", "x/y", "--body-file", "-"],
        )
        # Body comes via stdin (input=)
        self.assertEqual(captured["input"], "new body content")

    def test_failure_returns_error(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            return_value=_FakeProc(1, "", "permission denied"),
        ):
            result = edit_issue_body_sync(repo="x/y", issue_number=42, new_body="x")
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("gh_exit_1"))
        self.assertIn("permission denied", result.error)

    def test_timeout(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            side_effect=_sp.TimeoutExpired("gh", 1),
        ):
            result = edit_issue_body_sync(repo="x/y", issue_number=42, new_body="x", timeout=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")

    def test_gh_missing(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            result = edit_issue_body_sync(repo="x/y", issue_number=42, new_body="x")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "gh_cli_not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
