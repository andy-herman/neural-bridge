"""Unit tests for github_client.py. Subprocess is mocked; no real gh calls."""

from __future__ import annotations

import subprocess as _sp
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.github_client import create_issue_sync  # noqa: E402


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestCreateIssueSync(unittest.TestCase):
    def test_happy_path(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            return_value=_FakeProc(0, "https://github.com/andy-herman/neural-bridge/issues/42\n"),
        ):
            result = create_issue_sync(
                repo="andy-herman/neural-bridge",
                title="t",
                body="b",
                labels=["pm-managed", "agent-ready"],
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.issue_number, 42)
        self.assertIn("/issues/42", result.issue_url)
        self.assertIsNone(result.error)

    def test_nonzero_exit(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            return_value=_FakeProc(1, "", "could not create issue: unauthenticated"),
        ):
            result = create_issue_sync(repo="x/y", title="t", body="b")
        self.assertFalse(result.ok)
        self.assertIsNone(result.issue_number)
        self.assertTrue(result.error.startswith("gh_exit_1"))
        self.assertIn("unauthenticated", result.error)

    def test_empty_url(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            return_value=_FakeProc(0, ""),
        ):
            result = create_issue_sync(repo="x/y", title="t", body="b")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "empty_url_from_gh")

    def test_unparseable_url(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            return_value=_FakeProc(0, "https://example.com/not-an-issue\n"),
        ):
            result = create_issue_sync(repo="x/y", title="t", body="b")
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("could_not_parse_issue_number"))

    def test_timeout(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            side_effect=_sp.TimeoutExpired("gh", 1),
        ):
            result = create_issue_sync(repo="x/y", title="t", body="b", timeout=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")

    def test_gh_missing(self):
        with patch(
            "scripts.discord_bot.github_client.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            result = create_issue_sync(repo="x/y", title="t", body="b")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "gh_cli_not_found")

    def test_labels_passed_through(self):
        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _FakeProc(0, "https://github.com/x/y/issues/1\n")

        with patch("scripts.discord_bot.github_client.subprocess.run", side_effect=fake_run):
            create_issue_sync(
                repo="x/y", title="t", body="b",
                labels=["pm-managed", "agent-ready", "needs-input"],
            )

        args = captured["args"]
        # Each label should appear after a --label flag
        for lab in ("pm-managed", "agent-ready", "needs-input"):
            i = args.index("--label")
            args = args[i + 1:]  # advance past this --label/value pair
            # At least somewhere ahead of us, this label should appear
        # Simpler check: count --label occurrences
        full_args = ["gh", "issue", "create", "--repo", "x/y", "--title", "t", "--body", "b",
                     "--label", "pm-managed", "--label", "agent-ready", "--label", "needs-input"]
        self.assertEqual(captured["args"], full_args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
