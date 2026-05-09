"""Unit tests for hooks/session_start.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

import session_start  # noqa: E402


class TestResolveAgent(unittest.TestCase):
    def test_payload_agent_type(self):
        with patch.dict(os.environ, {"NB_AGENT": ""}, clear=False):
            agent = session_start.resolve_agent({"agent_type": "research"})
        self.assertEqual(agent, "research")

    def test_env_var(self):
        with patch.dict(os.environ, {"NB_AGENT": "content"}, clear=False):
            agent = session_start.resolve_agent({})
        self.assertEqual(agent, "content")

    def test_cwd_basename(self):
        with patch.dict(os.environ, {"NB_AGENT": ""}, clear=False):
            agent = session_start.resolve_agent({"cwd": "/path/to/social"})
        self.assertEqual(agent, "social")

    def test_unattributed_fallback(self):
        with patch.dict(os.environ, {"NB_AGENT": ""}, clear=False):
            agent = session_start.resolve_agent({"cwd": "/path/to/anything-else"})
        self.assertEqual(agent, session_start.UNATTRIBUTED)

    def test_payload_overrides_env(self):
        with patch.dict(os.environ, {"NB_AGENT": "research"}, clear=False):
            agent = session_start.resolve_agent({"agent_type": "content"})
        self.assertEqual(agent, "content")


class TestReadCapped(unittest.TestCase):
    def test_short_returned_intact(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("hello world")
            path = Path(f.name)
        try:
            self.assertEqual(session_start.read_capped(path, 100), "hello world")
        finally:
            path.unlink()

    def test_long_truncated_with_ellipsis(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("x" * 1000)
            path = Path(f.name)
        try:
            out = session_start.read_capped(path, 100)
            self.assertEqual(len(out), 100)
            self.assertTrue(out.endswith("…"))
        finally:
            path.unlink()

    def test_missing_file_returns_empty(self):
        self.assertEqual(session_start.read_capped(Path("/does/not/exist.md"), 100), "")


class TestRecentFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_dir(self):
        self.assertEqual(session_start.recent_files(self.dir, limit=5), [])

    def test_missing_dir(self):
        self.assertEqual(session_start.recent_files(self.dir / "nope", limit=5), [])

    def test_orders_by_mtime_desc(self):
        import time
        a = self.dir / "a.md"
        b = self.dir / "b.md"
        c = self.dir / "c.md"
        a.write_text("a")
        time.sleep(0.01)
        b.write_text("b")
        time.sleep(0.01)
        c.write_text("c")
        files = session_start.recent_files(self.dir, limit=2)
        self.assertEqual([f.name for f in files], ["c.md", "b.md"])

    def test_skips_hidden_files(self):
        (self.dir / ".hidden.md").write_text("x")
        (self.dir / "a.md").write_text("y")
        files = session_start.recent_files(self.dir, limit=5)
        self.assertEqual([f.name for f in files], ["a.md"])


class TestBuildContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        # Patch the module's path constants
        self._saved = (
            session_start.REPO_ROOT,
            session_start.KNOWLEDGE_DIR,
            session_start.INDEX_FILE,
            session_start.AGENTS_DIR,
            session_start.DAILY_LOGS_DIR,
            session_start.QUEUE_LOG,
        )
        session_start.REPO_ROOT = self.repo
        session_start.KNOWLEDGE_DIR = self.repo / "knowledge"
        session_start.INDEX_FILE = session_start.KNOWLEDGE_DIR / "index.md"
        session_start.AGENTS_DIR = session_start.KNOWLEDGE_DIR / "agents"
        session_start.DAILY_LOGS_DIR = self.repo / "daily-logs"
        session_start.QUEUE_LOG = session_start.DAILY_LOGS_DIR / "_queue.log"

    def tearDown(self):
        (
            session_start.REPO_ROOT,
            session_start.KNOWLEDGE_DIR,
            session_start.INDEX_FILE,
            session_start.AGENTS_DIR,
            session_start.DAILY_LOGS_DIR,
            session_start.QUEUE_LOG,
        ) = self._saved
        self.tmp.cleanup()

    def test_includes_index_when_present(self):
        session_start.KNOWLEDGE_DIR.mkdir()
        session_start.INDEX_FILE.write_text("# Wiki index\n\nThis is the index.")
        out = session_start.build_context("research", budget=4000)
        self.assertIn("Wiki index", out)
        self.assertIn("This is the index.", out)

    def test_no_index_no_problem(self):
        out = session_start.build_context("research", budget=4000)
        self.assertIn("Neural Bridge SessionStart context", out)
        self.assertIn("end SessionStart context", out)

    def test_includes_agent_session_notes(self):
        agent_dir = session_start.AGENTS_DIR / "research"
        agent_dir.mkdir(parents=True)
        (agent_dir / "2026-05-09-prior-work.md").write_text("Prior research findings.")
        out = session_start.build_context("research", budget=4000)
        self.assertIn("Recent research session notes", out)
        self.assertIn("Prior research findings.", out)

    def test_includes_daily_logs(self):
        log_dir = session_start.DAILY_LOGS_DIR / "research"
        log_dir.mkdir(parents=True)
        (log_dir / "2026-05-09.md").write_text("Daily log entry.")
        out = session_start.build_context("research", budget=4000)
        self.assertIn("Recent research daily logs", out)
        self.assertIn("Daily log entry.", out)

    def test_unattributed_yields_minimal_block(self):
        # Even unattributed gets the index if present
        session_start.KNOWLEDGE_DIR.mkdir()
        session_start.INDEX_FILE.write_text("Index.")
        out = session_start.build_context(session_start.UNATTRIBUTED, budget=4000)
        self.assertIn("Index.", out)
        self.assertNotIn("session notes", out)
        self.assertNotIn("daily logs", out)


class TestMainEndToEnd(unittest.TestCase):
    """Run session_start.py as a subprocess with a real stdin payload."""

    def test_unattributed_session_skipped(self):
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "session_start.py")],
            input="{}",
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "NB_AGENT": ""},
        )
        self.assertEqual(proc.returncode, 0)
        # Unattributed sessions write nothing to stdout
        self.assertEqual(proc.stdout.strip(), "")

    def test_known_agent_emits_block(self):
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "session_start.py")],
            input=json.dumps({"agent_type": "research"}),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0)
        # The current repo's index.md exists, so an agent-attributed run
        # should produce at least the wrapper comments.
        self.assertIn("SessionStart context", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
