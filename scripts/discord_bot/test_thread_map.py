"""Unit tests for thread_map.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.thread_map import ThreadMap  # noqa: E402


class TestThreadMap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "threads.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_file_starts_clean(self):
        m = ThreadMap(path=self.path)
        self.assertEqual(len(m), 0)
        self.assertIsNone(m.get_thread(1))
        self.assertIsNone(m.get_issue("nope"))

    def test_bind_persists_both_directions(self):
        m = ThreadMap(path=self.path)
        m.bind(42, "thread-abc")
        self.assertEqual(m.get_thread(42), "thread-abc")
        self.assertEqual(m.get_issue("thread-abc"), 42)

    def test_persistence_across_instances(self):
        m1 = ThreadMap(path=self.path)
        m1.bind(7, "t7")
        m2 = ThreadMap(path=self.path)
        self.assertEqual(m2.get_thread(7), "t7")
        self.assertEqual(m2.get_issue("t7"), 7)

    def test_rebind_replaces_old_thread(self):
        m = ThreadMap(path=self.path)
        m.bind(1, "old")
        m.bind(1, "new")
        self.assertEqual(m.get_thread(1), "new")
        self.assertIsNone(m.get_issue("old"))

    def test_rebind_replaces_old_issue(self):
        m = ThreadMap(path=self.path)
        m.bind(1, "thread")
        m.bind(2, "thread")
        self.assertEqual(m.get_issue("thread"), 2)
        self.assertIsNone(m.get_thread(1))

    def test_unbind(self):
        m = ThreadMap(path=self.path)
        m.bind(5, "t5")
        m.unbind_issue(5)
        self.assertIsNone(m.get_thread(5))
        self.assertIsNone(m.get_issue("t5"))

    def test_corrupt_file_quarantined_and_starts_fresh(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("not valid json {", encoding="utf-8")
        m = ThreadMap(path=self.path)
        self.assertEqual(len(m), 0)
        # Corrupt file moved aside
        self.assertTrue(self.path.with_suffix(self.path.suffix + ".corrupt").exists())

    def test_atomic_write_no_partial_file(self):
        m = ThreadMap(path=self.path)
        m.bind(1, "t1")
        # The on-disk file is parseable JSON
        import json
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["issue_to_thread"]["1"], "t1")
        self.assertEqual(raw["thread_to_issue"]["t1"], "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
