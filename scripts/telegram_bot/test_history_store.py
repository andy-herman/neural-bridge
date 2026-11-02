"""The Telegram bridges keep their recent history across restarts (2026-10-20)."""

from __future__ import annotations

import os
import stat
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.telegram_bot import history_store  # noqa: E402


class TestHistoryStore(unittest.TestCase):
    def test_round_trip_and_private_file(self):
        history_store.save("luna", {42: [{"author": "Andy", "content": "done"}]})
        self.assertEqual(history_store.load("luna"), {42: [{"author": "Andy", "content": "done"}]})
        mode = stat.S_IMODE(os.stat(history_store._path("luna")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_missing_or_corrupt_is_empty(self):
        self.assertEqual(history_store.load("nobody"), {})
        history_store.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history_store._path("bad").write_text("{not json")
        self.assertEqual(history_store.load("bad"), {})

    def test_bridges_reload_what_they_saved(self):
        try:
            from scripts.telegram_bot import luna_bridge
        except ImportError as exc:
            self.skipTest(f"bridge not importable: {exc}")
        luna_bridge._chat_history.clear()
        luna_bridge._history_loaded = True
        luna_bridge._push_history(7, "Andy", "the venue form is done")
        luna_bridge._push_history(7, "Luna", "Closed it.")
        # A restart: memory is empty, the file is not.
        luna_bridge._chat_history.clear()
        luna_bridge._history_loaded = False
        self.assertEqual([t["content"] for t in luna_bridge._chat_history_for(7)],
                         ["the venue form is done", "Closed it."])


if __name__ == "__main__":
    unittest.main()
