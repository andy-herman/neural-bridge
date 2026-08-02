"""Unit tests for inflight.py restart-recovery tracking.

Stdlib-only. Run: `python3 scripts/discord_bot/test_inflight.py`
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.discord_bot import inflight  # noqa: E402


class TestInflight(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = inflight.STATE_FILE
        inflight.STATE_FILE = Path(self._tmp.name) / ".inflight_mentions.json"

    def tearDown(self):
        inflight.STATE_FILE = self._orig
        self._tmp.cleanup()

    def test_register_clear_leaves_nothing(self):
        token = inflight.register("luna", 123, "456")
        self.assertTrue(token)
        inflight.clear(token)
        self.assertEqual(inflight.drain(), [])

    def test_unclear_entry_survives_to_drain(self):
        inflight.register("automation-engineer", 999, "111")
        left = inflight.drain()
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["agent_id"], "automation-engineer")
        self.assertEqual(left[0]["channel_id"], 999)
        self.assertIn("started_at", left[0])

    def test_drain_empties_file(self):
        inflight.register("luna", 1, "2")
        inflight.drain()
        self.assertEqual(inflight.drain(), [])

    def test_clear_unknown_token_noop(self):
        inflight.clear("nonexistent")
        self.assertEqual(inflight.drain(), [])

    def test_corrupt_state_file_recovers(self):
        inflight.STATE_FILE.write_text("{not json")
        token = inflight.register("luna", 5, "6")
        self.assertTrue(token)
        self.assertEqual(len(inflight.drain()), 1)


if __name__ == "__main__":
    unittest.main()
