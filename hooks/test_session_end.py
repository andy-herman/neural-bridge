"""Unit tests for hooks/session_end.py.

The SessionEnd hook launches the whole memory pipeline (agent resolution,
detached flush spawn) and had no tests of its own.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

import session_end  # noqa: E402


class TestResolveAgent(unittest.TestCase):
    def test_payload_agent_type(self):
        with patch.dict(os.environ, {"NB_AGENT": ""}, clear=False):
            self.assertEqual(session_end.resolve_agent({"agent_type": "research"}), "research")

    def test_every_plugin_agent_resolves(self):
        # loid was missing from this file's private list before schema became
        # the single source, so loid sessions were filed as unattributed.
        for agent in sorted(session_end.KNOWN_AGENTS - {session_end.UNATTRIBUTED}):
            with self.subTest(agent=agent), patch.dict(os.environ, {"NB_AGENT": ""}, clear=False):
                self.assertEqual(session_end.resolve_agent({"agent_type": agent}), agent)

    def test_env_var(self):
        with patch.dict(os.environ, {"NB_AGENT": "loid"}, clear=False):
            self.assertEqual(session_end.resolve_agent({}), "loid")

    def test_daemon_stamp_nb_agent_id_is_honoured(self):
        # claude_invoke.py stamps NB_AGENT_ID on every Discord turn. Before
        # 2026-09-22 this hook ignored it, so all Discord work was unattributed
        # and compile.py never ingested it.
        with patch.dict(os.environ, {"NB_AGENT": "", "NB_AGENT_ID": "luna"}, clear=False):
            self.assertEqual(session_end.resolve_agent({"cwd": "/x/neural-bridge"}), "luna")

    def test_nb_agent_overrides_nb_agent_id(self):
        with patch.dict(os.environ, {"NB_AGENT": "research", "NB_AGENT_ID": "luna"}, clear=False):
            self.assertEqual(session_end.resolve_agent({}), "research")

    def test_compile_marker_is_unattributed(self):
        with patch.dict(os.environ, {"NB_AGENT": "compile"}, clear=False):
            self.assertEqual(
                session_end.resolve_agent({"cwd": "/somewhere/neural-bridge"}),
                session_end.UNATTRIBUTED,
            )

    def test_cwd_basename(self):
        with patch.dict(os.environ, {"NB_AGENT": ""}, clear=False):
            self.assertEqual(session_end.resolve_agent({"cwd": "/path/to/social"}), "social")

    def test_unattributed_fallback(self):
        with patch.dict(os.environ, {"NB_AGENT": ""}, clear=False):
            self.assertEqual(
                session_end.resolve_agent({"cwd": "/path/to/anything-else"}),
                session_end.UNATTRIBUTED,
            )


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (session_end.DAILY_LOGS_DIR, session_end.QUEUE_LOG)
        session_end.DAILY_LOGS_DIR = Path(self.tmp.name) / "daily-logs"
        session_end.QUEUE_LOG = session_end.DAILY_LOGS_DIR / "_queue.log"

    def tearDown(self):
        session_end.DAILY_LOGS_DIR, session_end.QUEUE_LOG = self._saved
        self.tmp.cleanup()

    def _run(self, payload) -> int:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        with patch.object(sys, "stdin", io.StringIO(raw)):
            return session_end.main()

    def test_attributed_session_spawns_flush_for_that_agent(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as fh:
            transcript = fh.name
        try:
            payload = {
                "agent_type": "loid",
                "session_id": "abc123",
                "transcript_path": transcript,
                "hook_event_name": "SessionEnd",
            }
            with patch.dict(os.environ, {"NB_AGENT": ""}, clear=False), \
                 patch.object(session_end, "spawn_flush") as spawn:
                self.assertEqual(self._run(payload), 0)
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args.args[0], "loid")
            self.assertIn("flush_spawned", session_end.QUEUE_LOG.read_text(encoding="utf-8"))
        finally:
            os.unlink(transcript)

    def _payload_with_transcript(self, agent_type="luna"):
        fh = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return {"agent_type": agent_type, "session_id": "gate-vote-1",
                "transcript_path": fh.name, "hook_event_name": "SessionEnd"}

    def test_compile_marker_skips_flush_entirely(self):
        # A compile gate vote is a claude -p session like any other, so the
        # hook fires for it. Before 2026-09-23 that spawned a flush model call
        # to summarise one gate vote into _unattributed/, which nothing reads.
        payload = self._payload_with_transcript(agent_type="")
        with patch.dict(os.environ, {"NB_AGENT": "compile", "NB_SKIP_FLUSH": ""}, clear=False), \
             patch.object(session_end, "spawn_flush") as spawn:
            self.assertEqual(self._run(payload), 0)
        spawn.assert_not_called()
        queue = session_end.QUEUE_LOG.read_text(encoding="utf-8")
        self.assertIn("_unattributed gate-vote-1 skipped:compile_session", queue)
        self.assertNotIn("flush_spawned", queue)

    def test_nb_skip_flush_skips_even_an_attributed_session(self):
        # flush.py sets this on its own extraction call: hooks fire for nested
        # claude -p sessions (verified 2026-09-23), so without it a flush
        # would summarise a flush, recursively.
        payload = self._payload_with_transcript(agent_type="luna")
        with patch.dict(os.environ, {"NB_AGENT": "", "NB_SKIP_FLUSH": "1"}, clear=False), \
             patch.object(session_end, "spawn_flush") as spawn:
            self.assertEqual(self._run(payload), 0)
        spawn.assert_not_called()
        self.assertIn("luna gate-vote-1 skipped:NB_SKIP_FLUSH",
                      session_end.QUEUE_LOG.read_text(encoding="utf-8"))

    def test_skip_signals_are_exact(self):
        # Only the documented values opt out; an empty or unrelated value must
        # not silently switch the memory pipeline off.
        self.assertIsNone(session_end.flush_opt_out({}))
        self.assertIsNone(session_end.flush_opt_out({"NB_SKIP_FLUSH": "", "NB_AGENT": "luna"}))
        self.assertIsNone(session_end.flush_opt_out({"NB_SKIP_FLUSH": "0"}))
        self.assertIsNone(session_end.flush_opt_out({"NB_AGENT": "compiler"}))
        for v in ("1", "true", "YES", " on "):
            self.assertEqual(session_end.flush_opt_out({"NB_SKIP_FLUSH": v}), "skipped:NB_SKIP_FLUSH")
        self.assertEqual(session_end.flush_opt_out({"NB_AGENT": "Compile"}), "skipped:compile_session")
        # The generic flag wins when both are present, so the breadcrumb names
        # the mechanism a caller actually set.
        self.assertEqual(session_end.flush_opt_out({"NB_SKIP_FLUSH": "1", "NB_AGENT": "compile"}),
                         "skipped:NB_SKIP_FLUSH")

    def test_missing_transcript_writes_breadcrumb_and_does_not_spawn(self):
        payload = {"agent_type": "research", "session_id": "s1",
                   "transcript_path": "/nonexistent/t.jsonl", "hook_event_name": "SessionEnd"}
        with patch.object(session_end, "spawn_flush") as spawn:
            self.assertEqual(self._run(payload), 0)
        spawn.assert_not_called()
        self.assertIn("failed:transcript_missing", session_end.QUEUE_LOG.read_text(encoding="utf-8"))

    def test_bad_payload_never_fails_the_hook(self):
        with patch.object(session_end, "spawn_flush") as spawn:
            self.assertEqual(self._run("{not json"), 0)
        spawn.assert_not_called()
        self.assertIn("failed:bad_payload", session_end.QUEUE_LOG.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
