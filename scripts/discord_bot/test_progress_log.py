"""Unit tests for progress_log.py, the narrative half of the note store."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot import progress_log as pl  # noqa: E402

WHEN = datetime(2026, 9, 22, 14, 5, tzinfo=timezone.utc)


class _Vault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agents = Path(self.tmp.name) / "Agents"
        self.agents.mkdir()

    def tearDown(self):
        self.tmp.cleanup()


class TestRenderEntry(unittest.TestCase):
    def test_renders_dated_heading_and_only_nonempty_sections(self):
        text = pl.render_entry(session_id="abcdef123456", decisions=["Chose X"],
                               findings=[], open_questions=["Y?"], source="SessionEnd", when=WHEN)
        self.assertTrue(text.startswith("## 2026-09-22 14:05Z session abcdef12 (SessionEnd)\n"))
        self.assertIn("**Decided**\n- Chose X", text)
        self.assertIn("**Open**\n- Y?", text)
        self.assertNotIn("**Found**", text)
        self.assertTrue(text.endswith("\n"))

    def test_nothing_to_say_renders_nothing(self):
        self.assertEqual(pl.render_entry(session_id="s", decisions=[], findings=[" "], open_questions=[]), "")

    def test_oversized_entry_is_truncated_with_marker(self):
        text = pl.render_entry(session_id="s", decisions=["x" * 5000], findings=[], open_questions=[])
        self.assertLessEqual(len(text), pl.MAX_ENTRY_CHARS)
        self.assertIn("(entry truncated)", text)


class TestAppend(_Vault):
    def test_creates_file_with_header_then_appends(self):
        e1 = pl.render_entry(session_id="s1", decisions=["a"], findings=[], open_questions=[], when=WHEN)
        e2 = pl.render_entry(session_id="s2", decisions=["b"], findings=[], open_questions=[], when=WHEN)
        self.assertEqual(pl.append_entry("research", e1, agents_base=self.agents), (True, "appended"))
        self.assertEqual(pl.append_entry("research", e2, agents_base=self.agents), (True, "appended"))
        text = pl.progress_path("research", self.agents).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# research progress log\n"))
        self.assertEqual(text.count("## 2026-09-22"), 2)
        self.assertLess(text.index("session s1"), text.index("session s2"))

    def test_refuses_to_invent_a_vault(self):
        ok, reason = pl.append_entry("research", "## 2026-09-22 00:00Z session x\n\n- a\n",
                                     agents_base=self.agents / "nope")
        self.assertFalse(ok)
        self.assertEqual(reason, "vault absent")
        self.assertFalse((self.agents / "nope").exists())

    def test_empty_entry_is_refused(self):
        self.assertEqual(pl.append_entry("research", "  ", agents_base=self.agents), (False, "empty entry"))
        self.assertFalse(pl.progress_path("research", self.agents).exists())


class TestReadRecent(_Vault):
    def _seed(self, n: int, size: int = 40) -> None:
        for i in range(n):
            e = pl.render_entry(session_id=f"s{i:02d}", decisions=[f"d{i} " + "x" * size],
                                findings=[], open_questions=[], when=WHEN)
            pl.append_entry("luna", e, agents_base=self.agents)

    def test_missing_and_empty_are_distinguished(self):
        self.assertEqual(pl.read_recent("luna", agents_base=self.agents), ("", "missing"))
        pl.progress_path("luna", self.agents).parent.mkdir()
        pl.progress_path("luna", self.agents).write_text("# luna progress log\n\nheader only\n", encoding="utf-8")
        self.assertEqual(pl.read_recent("luna", agents_base=self.agents), ("", "empty"))

    def test_returns_newest_entries_that_fit_oldest_first(self):
        self._seed(10)
        text, status = pl.read_recent("luna", max_chars=400, agents_base=self.agents)
        self.assertEqual(status, "ok")
        self.assertLessEqual(len(text), 401)
        self.assertNotIn("session s00", text)
        self.assertIn("session s09", text)
        # Chronological order inside the block.
        kept = [l for l in text.splitlines() if l.startswith("## ")]
        self.assertEqual(kept, sorted(kept))

    def test_whole_entries_only_unless_newest_alone_is_too_big(self):
        self._seed(3, size=40)
        pl.append_entry("luna", pl.render_entry(session_id="big", decisions=["y" * 1200],
                                                findings=[], open_questions=[], when=WHEN),
                        agents_base=self.agents)
        text, status = pl.read_recent("luna", max_chars=300, agents_base=self.agents)
        self.assertEqual(status, "ok")
        self.assertIn("session big", text)
        self.assertIn("(entry truncated)", text)
        self.assertNotIn("session s02", text)

    def test_render_block_frames_as_own_memory(self):
        block = pl.render_block("luna", "## 2026-09-22 00:00Z session x\n\n- a\n")
        self.assertIn("Agents/luna/progress.md", block)
        self.assertIn("<progress-log>", block)
        self.assertTrue(block.endswith("</progress-log>\n\n"))


if __name__ == "__main__":
    unittest.main()
