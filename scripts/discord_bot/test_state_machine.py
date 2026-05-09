"""Unit tests for state_machine.py. Subprocess is mocked."""

from __future__ import annotations

import asyncio
import subprocess as _sp
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.state_machine import (  # noqa: E402
    STATE_LABELS,
    add_label_sync,
    apply_labels,
    is_canonical_transition,
    is_state_label,
    remove_label_sync,
)


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestSyncLabelOps(unittest.TestCase):
    def test_add_label_success(self):
        with patch(
            "scripts.discord_bot.state_machine.subprocess.run",
            return_value=_FakeProc(0),
        ) as m:
            result = add_label_sync(repo="x/y", issue_number=42, label="agent-ready")
        self.assertTrue(result.ok)
        args = m.call_args[0][0]
        self.assertIn("--add-label", args)
        self.assertIn("agent-ready", args)

    def test_remove_label_success(self):
        with patch(
            "scripts.discord_bot.state_machine.subprocess.run",
            return_value=_FakeProc(0),
        ) as m:
            result = remove_label_sync(repo="x/y", issue_number=42, label="agent-inbox")
        self.assertTrue(result.ok)
        args = m.call_args[0][0]
        self.assertIn("--remove-label", args)

    def test_failure_returns_error(self):
        with patch(
            "scripts.discord_bot.state_machine.subprocess.run",
            return_value=_FakeProc(1, "", "label not found"),
        ):
            result = add_label_sync(repo="x/y", issue_number=42, label="x")
        self.assertFalse(result.ok)
        self.assertIn("gh_exit_1", result.error)

    def test_timeout(self):
        with patch(
            "scripts.discord_bot.state_machine.subprocess.run",
            side_effect=_sp.TimeoutExpired("gh", 1),
        ):
            result = add_label_sync(repo="x/y", issue_number=42, label="x", timeout=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")


class TestApplyLabels(unittest.TestCase):
    def test_apply_serial(self):
        captured = []

        def fake_run(args, **kwargs):
            captured.append(args)
            return _FakeProc(0)

        with patch("scripts.discord_bot.state_machine.subprocess.run", side_effect=fake_run):
            applied, failures = asyncio.run(
                apply_labels(repo="x/y", issue_number=42, add=["a", "b"], remove=["c"])
            )
        self.assertEqual(applied, ["-c", "+a", "+b"])
        self.assertEqual(failures, [])
        # Removes go before adds
        self.assertIn("--remove-label", captured[0])
        self.assertIn("--add-label", captured[1])

    def test_partial_failure_continues(self):
        results = [_FakeProc(0), _FakeProc(1, "", "no such label"), _FakeProc(0)]
        index = {"i": 0}

        def fake_run(args, **kwargs):
            r = results[index["i"]]
            index["i"] += 1
            return r

        with patch("scripts.discord_bot.state_machine.subprocess.run", side_effect=fake_run):
            applied, failures = asyncio.run(
                apply_labels(repo="x/y", issue_number=1, add=["a", "b"], remove=["c"])
            )
        self.assertIn("-c", applied)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], "+a")
        self.assertIn("+b", applied)


class TestStateValidation(unittest.TestCase):
    def test_state_labels_known(self):
        for state in STATE_LABELS:
            self.assertTrue(is_state_label(state))

    def test_state_labels_rejects_unknown(self):
        self.assertFalse(is_state_label("not-a-state"))
        self.assertFalse(is_state_label("squad:senior-pm"))

    def test_canonical_transitions(self):
        self.assertTrue(is_canonical_transition("agent-inbox", "agent-ready"))
        self.assertTrue(is_canonical_transition("agent-ready", "agent-running"))
        self.assertTrue(is_canonical_transition("agent-running", "agent-review"))
        self.assertTrue(is_canonical_transition("agent-review", "agent-done"))
        # Backward jumps are not canonical
        self.assertFalse(is_canonical_transition("agent-done", "agent-running"))
        self.assertFalse(is_canonical_transition("agent-ready", "agent-inbox"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
