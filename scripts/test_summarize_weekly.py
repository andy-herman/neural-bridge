"""Unit tests for scripts/summarize_weekly.py.

Stdlib-only. The claude subprocess call is mocked — we don't shell out
to the real model from unit tests.

Run: `python3 scripts/test_summarize_weekly.py`
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import summarize_weekly as sw  # noqa: E402
from scripts.discord_bot import conversation_log as cl  # noqa: E402


class TestIsoWeek(unittest.TestCase):
    def test_known_dates(self):
        self.assertEqual(sw._iso_week(date(2026, 1, 5)), "2026-W02")
        self.assertEqual(sw._iso_week(date(2026, 5, 11)), "2026-W20")
        self.assertEqual(sw._iso_week(date(2026, 12, 31)), "2026-W53")


class TestDiscoverAgents(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cl = cl.AGENTS_BASE
        cl.AGENTS_BASE = Path(self._tmp.name) / "Agents"
        sw.AGENTS_BASE = cl.AGENTS_BASE  # share

    def tearDown(self):
        cl.AGENTS_BASE = self._orig_cl
        sw.AGENTS_BASE = self._orig_cl
        self._tmp.cleanup()

    def test_no_agents_dir_returns_empty(self):
        self.assertEqual(sw._discover_agents(), [])

    def test_finds_agents_with_conversations_subdir(self):
        (cl.AGENTS_BASE / "luna" / "conversations").mkdir(parents=True)
        (cl.AGENTS_BASE / "echo" / "conversations").mkdir(parents=True)
        # An agent dir without a conversations subdir should be excluded.
        (cl.AGENTS_BASE / "research").mkdir(parents=True)
        agents = sw._discover_agents()
        self.assertEqual(sorted(agents), ["echo", "luna"])


class TestGatherRecentTurns(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cl = cl.AGENTS_BASE
        cl.AGENTS_BASE = Path(self._tmp.name) / "Agents"
        sw.AGENTS_BASE = cl.AGENTS_BASE

    def tearDown(self):
        cl.AGENTS_BASE = self._orig_cl
        sw.AGENTS_BASE = self._orig_cl
        self._tmp.cleanup()

    def _put_log(self, agent_id: str, month: str, channel: str, content: str, mtime: float | None = None):
        path = cl.AGENTS_BASE / agent_id / "conversations" / month / f"{channel}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mtime is not None:
            import os
            os.utime(path, (mtime, mtime))
        return path

    def test_returns_empty_when_no_logs(self):
        self.assertEqual(sw._gather_recent_turns("luna", datetime.now(timezone.utc)), "")

    def test_includes_files_newer_than_cutoff(self):
        now = time.time()
        self._put_log("luna", "2026-05", "neural-bridge", "fresh content here", mtime=now)
        cutoff = datetime.fromtimestamp(now - 3600, tz=timezone.utc)
        result = sw._gather_recent_turns("luna", cutoff)
        self.assertIn("fresh content here", result)
        self.assertIn("FILE: luna/conversations/2026-05/neural-bridge.md", result)

    def test_excludes_files_older_than_cutoff(self):
        old_mtime = time.time() - 30 * 24 * 3600  # 30 days ago
        self._put_log("luna", "2026-04", "neural-bridge", "old content", mtime=old_mtime)
        cutoff = datetime.fromtimestamp(time.time() - 7 * 24 * 3600, tz=timezone.utc)
        result = sw._gather_recent_turns("luna", cutoff)
        self.assertNotIn("old content", result)

    def test_truncates_to_max_chars(self):
        # Write a file deliberately oversized.
        big = "x" * (sw.MAX_RAW_CONTENT_CHARS + 50_000)
        self._put_log("luna", "2026-05", "big", big, mtime=time.time())
        cutoff = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc)
        result = sw._gather_recent_turns("luna", cutoff)
        self.assertLessEqual(len(result), sw.MAX_RAW_CONTENT_CHARS + 200)  # cap + truncation marker
        self.assertIn("older turns truncated", result)


class TestBuildPrompt(unittest.TestCase):
    def test_substitutes_variables_and_appends_data_block(self):
        prompt = sw._build_prompt("luna", "2026-W20", "raw turn content")
        self.assertIn("luna", prompt)
        self.assertIn("2026-W20", prompt)
        self.assertIn("<conversation-data>", prompt)
        self.assertIn("raw turn content", prompt)
        self.assertIn("</conversation-data>", prompt)


class TestWriteDigest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cl = cl.AGENTS_BASE
        cl.AGENTS_BASE = Path(self._tmp.name) / "Agents"
        sw.AGENTS_BASE = cl.AGENTS_BASE

    def tearDown(self):
        cl.AGENTS_BASE = self._orig_cl
        sw.AGENTS_BASE = self._orig_cl
        self._tmp.cleanup()

    def test_writes_to_per_agent_lessons_learned_dir(self):
        path = sw._write_digest("luna", "2026-W20", "# Lessons\n\n- preference A\n- decision B")
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "2026-W20.md")
        self.assertIn("luna/lessons-learned", str(path))
        content = path.read_text(encoding="utf-8")
        self.assertIn("preference A", content)

    def test_overwrites_existing_same_week(self):
        sw._write_digest("luna", "2026-W20", "v1")
        sw._write_digest("luna", "2026-W20", "v2")
        path = cl.AGENTS_BASE / "luna" / "lessons-learned" / "2026-W20.md"
        self.assertEqual(path.read_text(encoding="utf-8").strip(), "v2")


class TestSummarizeOneAgent(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cl = cl.AGENTS_BASE
        cl.AGENTS_BASE = Path(self._tmp.name) / "Agents"
        sw.AGENTS_BASE = cl.AGENTS_BASE

    def tearDown(self):
        cl.AGENTS_BASE = self._orig_cl
        sw.AGENTS_BASE = self._orig_cl
        self._tmp.cleanup()

    def _put_log(self, agent_id, content, mtime=None):
        path = cl.AGENTS_BASE / agent_id / "conversations" / "2026-05" / "neural-bridge.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mtime is not None:
            import os
            os.utime(path, (mtime, mtime))

    def test_skips_when_no_recent_activity(self):
        # Put an OLD log file so the gather returns empty.
        self._put_log("luna", "old content", mtime=time.time() - 30 * 24 * 3600)
        ok, line = sw.summarize_one_agent("luna", today=date(2026, 5, 11))
        self.assertTrue(ok)
        self.assertIn("no conversation activity", line)

    @mock.patch.object(sw, "_run_claude")
    def test_writes_digest_when_claude_succeeds(self, mock_claude):
        mock_claude.return_value = (True, "# Lessons\n\n- preference X", "")
        self._put_log("luna", "## 2026-05-10 12:00 — Andy\n\nhi luna", mtime=time.time())
        ok, line = sw.summarize_one_agent("luna", today=date(2026, 5, 11))
        self.assertTrue(ok)
        self.assertIn("wrote", line)
        digest = cl.AGENTS_BASE / "luna" / "lessons-learned" / "2026-W20.md"
        self.assertTrue(digest.exists())
        self.assertIn("preference X", digest.read_text(encoding="utf-8"))

    @mock.patch.object(sw, "_run_claude")
    def test_reports_failure_when_claude_errors(self, mock_claude):
        mock_claude.return_value = (False, "", "exit_1: bad")
        self._put_log("luna", "## turn\n", mtime=time.time())
        ok, line = sw.summarize_one_agent("luna", today=date(2026, 5, 11))
        self.assertFalse(ok)
        self.assertIn("claude failed", line)

    @mock.patch.object(sw, "_run_claude")
    def test_reports_failure_when_claude_returns_empty(self, mock_claude):
        mock_claude.return_value = (True, "   \n  ", "")
        self._put_log("luna", "## turn\n", mtime=time.time())
        ok, line = sw.summarize_one_agent("luna", today=date(2026, 5, 11))
        self.assertFalse(ok)
        self.assertIn("empty output", line)


if __name__ == "__main__":
    unittest.main()
