"""Unit tests for handoff_budget.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.handoff_budget import BUDGET, HandoffBudget  # noqa: E402


class TestHandoffBudget(unittest.TestCase):
    def setUp(self):
        self.b = HandoffBudget(max_turns=3)

    def test_starts_at_full_budget(self):
        self.assertEqual(self.b.remaining("c1"), 3)

    def test_consume_decrements(self):
        self.assertTrue(self.b.consume("c1"))
        self.assertEqual(self.b.remaining("c1"), 2)
        self.assertTrue(self.b.consume("c1"))
        self.assertEqual(self.b.remaining("c1"), 1)

    def test_consume_returns_false_at_limit(self):
        for _ in range(3):
            self.assertTrue(self.b.consume("c1"))
        self.assertEqual(self.b.remaining("c1"), 0)
        self.assertFalse(self.b.consume("c1"))
        self.assertFalse(self.b.consume("c1"))

    def test_reset_restores_full_budget(self):
        self.b.consume("c1")
        self.b.consume("c1")
        self.b.reset("c1")
        self.assertEqual(self.b.remaining("c1"), 3)

    def test_per_channel_isolation(self):
        # Channel a's count doesn't affect channel b.
        for _ in range(3):
            self.b.consume("c1")
        self.assertFalse(self.b.consume("c1"))
        self.assertTrue(self.b.consume("c2"))

    def test_default_max_is_5(self):
        # Default singleton uses 5 turns.
        BUDGET.reset_all()
        for _ in range(5):
            self.assertTrue(BUDGET.consume("default-test-channel"))
        self.assertFalse(BUDGET.consume("default-test-channel"))
        BUDGET.reset_all()


if __name__ == "__main__":
    unittest.main(verbosity=2)
