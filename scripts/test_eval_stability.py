"""Unit tests for eval_stability.py (pure logic; no claude).

Run: python3 scripts/test_eval_stability.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import eval_stability as st  # noqa: E402


class TestDecision(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(st.decision("PROMOTE"), "PROMOTE")
        self.assertEqual(st.decision("QUARANTINE"), "NOT_PROMOTE")
        self.assertEqual(st.decision("REJECT"), "NOT_PROMOTE")
        self.assertEqual(st.decision("ERROR"), "ERROR")


class TestStability(unittest.TestCase):
    def test_all_same_is_stable(self):
        s = st.stability(["PROMOTE", "PROMOTE", "PROMOTE"])
        self.assertTrue(s["verdict_stable"])
        self.assertTrue(s["decision_stable"])

    def test_verdict_flip_but_decision_stable(self):
        # QUARANTINE vs REJECT differ as verdicts but are both NOT_PROMOTE.
        s = st.stability(["QUARANTINE", "REJECT", "REJECT"])
        self.assertFalse(s["verdict_stable"])
        self.assertTrue(s["decision_stable"])
        self.assertEqual(s["modal_decision"], "NOT_PROMOTE")

    def test_decision_flip(self):
        s = st.stability(["PROMOTE", "REJECT", "PROMOTE"])
        self.assertFalse(s["verdict_stable"])
        self.assertFalse(s["decision_stable"])
        self.assertEqual(s["modal_verdict"], "PROMOTE")


class TestSummarize(unittest.TestCase):
    def test_counts_unstable(self):
        rows = [
            {"case": {"id": "a"},
             "v1": st.stability(["PROMOTE", "REJECT"]),
             "v3": st.stability(["PROMOTE", "PROMOTE"])},
            {"case": {"id": "b"},
             "v1": st.stability(["PROMOTE", "PROMOTE"]),
             "v3": st.stability(["PROMOTE", "PROMOTE"])},
        ]
        s = st.summarize(rows)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["v1_decision_unstable"], 1)  # case a flipped at votes=1
        self.assertEqual(s["v3_decision_unstable"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
