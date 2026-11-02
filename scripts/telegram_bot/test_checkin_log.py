"""Luna's chat sees her own check-ins (checkin_log.py)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.telegram_bot import checkin_log  # noqa: E402

CHAT = 42


class TestCheckinLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "luna-checkins.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_then_since_filters_by_chat_and_time(self):
        checkin_log.record(CHAT, "morning", "old one", now=100, path=self.path)
        checkin_log.record(CHAT, "evening", "new one", now=200, path=self.path)
        checkin_log.record(7, "evening", "someone else", now=300, path=self.path)
        got = checkin_log.since(CHAT, 150, path=self.path)
        self.assertEqual([e["text"] for e in got], ["new one"])
        self.assertEqual(got[0]["kind"], "evening")

    def test_keeps_only_the_newest(self):
        for i in range(checkin_log.KEEP + 5):
            checkin_log.record(CHAT, "morning", f"m{i}", now=i, path=self.path)
        got = checkin_log.since(CHAT, -1, path=self.path)
        self.assertEqual(len(got), checkin_log.KEEP)
        self.assertEqual(got[-1]["text"], f"m{checkin_log.KEEP + 4}")

    def test_missing_or_corrupt_file_is_empty_not_an_error(self):
        self.assertEqual(checkin_log.since(CHAT, 0, path=self.path), [])
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json\n{\"ts\": 5, \"chat_id\": 42, \"text\": \"ok\"}\n")
        self.assertEqual([e["text"] for e in checkin_log.since(CHAT, 0, path=self.path)], ["ok"])

    def test_unwritable_location_does_not_raise(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        checkin_log.record(CHAT, "morning", "hi", path=blocker / "nested.jsonl")


class TestBridgeAbsorbsCheckins(unittest.TestCase):
    def setUp(self):
        try:
            from scripts.telegram_bot import luna_bridge
        except ImportError as exc:  # python-telegram-bot not installed
            self.skipTest(f"bridge not importable: {exc}")
        self.bridge = luna_bridge
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "luna-checkins.jsonl"
        self.patch = mock.patch.object(checkin_log, "LOG_PATH", path)
        self.patch.start()
        self.bridge._chat_history.clear()
        self.bridge._checkins_seen.clear()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_unseen_checkin_becomes_lunas_turn_once(self):
        checkin_log.record(CHAT, "morning", "Two cards are past due.", now=1000)
        self.assertEqual(self.bridge._absorb_checkins(CHAT, now=2000), 1)
        hist = self.bridge._chat_history_for(CHAT)
        self.assertEqual(hist[-1]["author"], "Luna")
        self.assertIn("scheduled morning check-in", hist[-1]["content"])
        self.assertIn("Two cards are past due.", hist[-1]["content"])
        # The next message does not repeat it.
        self.assertEqual(self.bridge._absorb_checkins(CHAT, now=2100), 0)
        self.assertEqual(len(self.bridge._chat_history_for(CHAT)), 1)

    def test_first_message_after_restart_looks_back_a_bounded_window(self):
        now = 10 * 24 * 3600
        checkin_log.record(CHAT, "morning", "too old", now=now - checkin_log.LOOKBACK_SECONDS - 60)
        checkin_log.record(CHAT, "evening", "recent", now=now - 3600)
        self.assertEqual(self.bridge._absorb_checkins(CHAT, now=now), 1)
        self.assertIn("recent", self.bridge._chat_history_for(CHAT)[0]["content"])


if __name__ == "__main__":
    unittest.main()
