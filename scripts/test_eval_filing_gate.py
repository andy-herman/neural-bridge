"""Unit tests for eval_filing_gate.py. Stdlib-only; no real claude calls.

Run: python3 scripts/test_eval_filing_gate.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import eval_filing_gate as ev  # noqa: E402


class TestScoreCase(unittest.TestCase):
    def test_benign_promoted_passes(self):
        self.assertTrue(ev.score_case("PROMOTE", "PROMOTE")["passed"])

    def test_benign_blocked_fails(self):
        r = ev.score_case("PROMOTE", "QUARANTINE")
        self.assertFalse(r["passed"])
        self.assertEqual(r["error"], "benign_blocked")

    def test_attack_admitted_fails(self):
        r = ev.score_case("NOT_PROMOTE", "PROMOTE")
        self.assertFalse(r["passed"])
        self.assertEqual(r["error"], "false_admit")

    def test_attack_rejected_or_quarantined_passes(self):
        self.assertTrue(ev.score_case("NOT_PROMOTE", "REJECT")["passed"])
        self.assertTrue(ev.score_case("NOT_PROMOTE", "QUARANTINE")["passed"])

    def test_gate_error(self):
        self.assertEqual(ev.score_case("PROMOTE", "ERROR")["error"], "gate_error")


class TestLoadCases(unittest.TestCase):
    def test_dataset_is_wellformed(self):
        cases = ev.load_cases()
        self.assertGreaterEqual(len(cases), 8)
        ids = [c["id"] for c in cases]
        self.assertEqual(len(ids), len(set(ids)))  # ids are unique
        for c in cases:
            self.assertIn(c["expect"], ev.VALID_EXPECT)
            self.assertTrue(c["slug"] and c["summary"] and c["excerpt"])

    def test_has_both_benign_and_attacks(self):
        cases = ev.load_cases()
        expects = {c["expect"] for c in cases}
        self.assertIn("PROMOTE", expects)
        self.assertIn("NOT_PROMOTE", expects)
        self.assertTrue(any(c["label"] in ev.SECURITY_LABELS for c in cases))


class TestEvaluateMock(unittest.TestCase):
    def test_naive_mock_admits_every_attack(self):
        cases = ev.load_cases()
        _, s = ev.evaluate(cases, mock_kind="naive")
        not_promote = sum(1 for c in cases if c["expect"] == "NOT_PROMOTE")
        self.assertEqual(s["false_admit"], not_promote)
        attacks = sum(1 for c in cases if c["label"] in ev.SECURITY_LABELS)
        self.assertEqual(s["attack_admitted"], attacks)

    def test_ideal_mock_passes_everything(self):
        cases = ev.load_cases()
        _, s = ev.evaluate(cases, mock_kind="ideal")
        self.assertEqual(s["passed"], s["total"])
        self.assertEqual(s["attack_admitted"], 0)

    def test_reject_all_blocks_benign_but_admits_nothing(self):
        cases = ev.load_cases()
        _, s = ev.evaluate(cases, mock_kind="reject_all")
        benign = sum(1 for c in cases if c["expect"] == "PROMOTE")
        self.assertEqual(s["benign_blocked"], benign)
        self.assertEqual(s["attack_admitted"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
