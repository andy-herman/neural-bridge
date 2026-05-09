"""Unit tests for flush.py and schema.py.

Stdlib-only. No real Claude CLI calls — the subprocess is mocked.
Run: `python3 hooks/test_flush.py` (from repo root or anywhere).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

import flush  # noqa: E402
import schema  # noqa: E402


# ---------- schema.py ----------

class TestSchema(unittest.TestCase):
    def _valid(self, **overrides):
        base = {
            "decisions": ["Chose X"],
            "findings": ["Found Y"],
            "open_questions": ["What about Z?"],
            "proposed_concepts": [{"slug": "filing-gate", "summary": "PROMOTE/QUARANTINE/REJECT"}],
        }
        base.update(overrides)
        return base

    def test_valid_full(self):
        ok, err = schema.validate_flush_output(self._valid())
        self.assertTrue(ok, err)

    def test_valid_all_empty(self):
        ok, err = schema.validate_flush_output(self._valid(
            decisions=[], findings=[], open_questions=[], proposed_concepts=[]
        ))
        self.assertTrue(ok, err)

    def test_not_a_dict(self):
        ok, err = schema.validate_flush_output(["not", "a", "dict"])
        self.assertFalse(ok)
        self.assertIn("not a JSON object", err)

    def test_missing_key(self):
        bad = self._valid()
        del bad["decisions"]
        ok, err = schema.validate_flush_output(bad)
        self.assertFalse(ok)
        self.assertIn("missing", err)

    def test_extra_key(self):
        bad = self._valid()
        bad["bonus"] = "extra"
        ok, err = schema.validate_flush_output(bad)
        self.assertFalse(ok)
        self.assertIn("extra", err)

    def test_decisions_not_list(self):
        ok, err = schema.validate_flush_output(self._valid(decisions="not a list"))
        self.assertFalse(ok)
        self.assertIn("not a list", err)

    def test_decisions_contains_non_string(self):
        ok, err = schema.validate_flush_output(self._valid(decisions=["ok", 42]))
        self.assertFalse(ok)
        self.assertIn("[1]", err)

    def test_decisions_empty_string(self):
        ok, err = schema.validate_flush_output(self._valid(decisions=["", "ok"]))
        self.assertFalse(ok)
        self.assertIn("empty or whitespace", err)

    def test_concept_wrong_keys(self):
        ok, err = schema.validate_flush_output(self._valid(
            proposed_concepts=[{"slug": "ok", "summary": "ok", "extra": "no"}]
        ))
        self.assertFalse(ok)
        self.assertIn("slug,summary", err)

    def test_concept_slug_not_kebab(self):
        ok, err = schema.validate_flush_output(self._valid(
            proposed_concepts=[{"slug": "Not Kebab", "summary": "x"}]
        ))
        self.assertFalse(ok)
        self.assertIn("kebab-case", err)

    def test_concept_slug_valid_with_digits(self):
        ok, err = schema.validate_flush_output(self._valid(
            proposed_concepts=[{"slug": "cve-2026-12345", "summary": "x"}]
        ))
        self.assertTrue(ok, err)

    def test_concept_empty_summary(self):
        ok, err = schema.validate_flush_output(self._valid(
            proposed_concepts=[{"slug": "ok", "summary": "   "}]
        ))
        self.assertFalse(ok)

    def test_is_empty_session_true(self):
        self.assertTrue(schema.is_empty_session({
            "decisions": [], "findings": [], "open_questions": [], "proposed_concepts": []
        }))

    def test_is_empty_session_false(self):
        self.assertFalse(schema.is_empty_session({
            "decisions": ["x"], "findings": [], "open_questions": [], "proposed_concepts": []
        }))


# ---------- flush.py: parse_response ----------

class TestParseResponse(unittest.TestCase):
    def test_clean_json(self):
        s = '{"decisions":[],"findings":[],"open_questions":[],"proposed_concepts":[]}'
        ok, data, err = flush.parse_response(s)
        self.assertTrue(ok, err)
        self.assertEqual(data["decisions"], [])

    def test_code_fenced_json(self):
        s = '```json\n{"decisions":[],"findings":[],"open_questions":[],"proposed_concepts":[]}\n```'
        ok, data, err = flush.parse_response(s)
        self.assertTrue(ok, err)

    def test_code_fenced_no_lang(self):
        s = '```\n{"decisions":[],"findings":[],"open_questions":[],"proposed_concepts":[]}\n```'
        ok, data, err = flush.parse_response(s)
        self.assertTrue(ok, err)

    def test_invalid_json(self):
        ok, data, err = flush.parse_response("not json at all")
        self.assertFalse(ok)
        self.assertTrue(err.startswith("json_decode"))

    def test_schema_invalid(self):
        ok, data, err = flush.parse_response('{"decisions": []}')
        self.assertFalse(ok)
        self.assertTrue(err.startswith("schema"))


# ---------- flush.py: render & block building ----------

class TestRendering(unittest.TestCase):
    def test_render_section_empty(self):
        out = flush.render_section("Decisions", [])
        self.assertEqual(out, ["### Decisions", "", "- (none)", ""])

    def test_render_section_strings(self):
        out = flush.render_section("Findings", ["a", "b"])
        self.assertEqual(out, ["### Findings", "", "- a", "- b", ""])

    def test_render_section_concepts(self):
        out = flush.render_section("Proposed concepts", [{"slug": "x", "summary": "y"}])
        self.assertEqual(out, ["### Proposed concepts", "", "- x: y", ""])

    def test_build_session_block_shape(self):
        data = {
            "decisions": ["d1"],
            "findings": ["f1"],
            "open_questions": ["q1"],
            "proposed_concepts": [{"slug": "c1", "summary": "summary one"}],
        }
        block = flush.build_session_block(
            data=data,
            session_id="sess-x",
            transcript_path="/tmp/t.jsonl",
            transcript_hash="abc123",
            hook_event="SessionEnd",
            session_n=1,
            started_at="2026-05-09T07:00:00Z",
            ended_at="2026-05-09T07:01:00Z",
        )
        self.assertIn("## Session 1 —", block)
        self.assertIn("session_id: sess-x", block)
        self.assertIn("transcript_sha256: abc123", block)
        self.assertIn("hook_event: SessionEnd", block)
        self.assertIn("- d1", block)
        self.assertIn("- c1: summary one", block)


# ---------- flush.py: append_session + count_existing_sessions ----------

class TestAppendSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig_dir = flush.DAILY_LOGS_DIR
        self._orig_queue = flush.QUEUE_LOG
        flush.DAILY_LOGS_DIR = self.tmp_path / "daily-logs"
        flush.QUEUE_LOG = flush.DAILY_LOGS_DIR / "_queue.log"

    def tearDown(self):
        flush.DAILY_LOGS_DIR = self._orig_dir
        flush.QUEUE_LOG = self._orig_queue
        self.tmp.cleanup()

    def _block(self, n: int) -> str:
        return flush.build_session_block(
            data={"decisions": [f"d{n}"], "findings": [], "open_questions": [], "proposed_concepts": []},
            session_id=f"sess-{n}",
            transcript_path="/tmp/t.jsonl",
            transcript_hash="hash",
            hook_event="SessionEnd",
            session_n=n,
            started_at="2026-05-09T07:00:00Z",
            ended_at="2026-05-09T07:01:00Z",
        )

    def test_first_session_creates_file_with_frontmatter(self):
        log_file = flush.append_session("research", self._block(1), session_n=1)
        text = log_file.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\ntype: daily-log\n"))
        self.assertIn("agent: research", text)
        self.assertIn("session_count: 1", text)
        self.assertIn("## Session 1 —", text)

    def test_second_session_appends_separator_and_increments(self):
        flush.append_session("research", self._block(1), session_n=1)
        flush.append_session("research", self._block(2), session_n=2)
        log_file = flush.DAILY_LOGS_DIR / "research" / f"{flush.utc_today()}.md"
        text = log_file.read_text(encoding="utf-8")
        self.assertIn("session_count: 2", text)
        self.assertNotIn("session_count: 1", text)
        self.assertIn("\n---\n\n## Session 2 —", text)
        self.assertEqual(flush.count_existing_sessions(log_file), 2)

    def test_count_existing_sessions_no_file(self):
        log_file = flush.DAILY_LOGS_DIR / "research" / "1900-01-01.md"
        self.assertEqual(flush.count_existing_sessions(log_file), 0)


# ---------- flush.py: write_failed + write_queue ----------

class TestFailureAndQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig_dir = flush.DAILY_LOGS_DIR
        self._orig_queue = flush.QUEUE_LOG
        flush.DAILY_LOGS_DIR = self.tmp_path / "daily-logs"
        flush.QUEUE_LOG = flush.DAILY_LOGS_DIR / "_queue.log"

    def tearDown(self):
        flush.DAILY_LOGS_DIR = self._orig_dir
        flush.QUEUE_LOG = self._orig_queue
        self.tmp.cleanup()

    def test_write_failed(self):
        flush.write_failed("research", "sess-bad", "raw model output", "json_decode:bad token")
        failed = flush.DAILY_LOGS_DIR / "research" / "_failed" / "sess-bad.txt"
        self.assertTrue(failed.exists())
        self.assertIn("raw model output", failed.read_text(encoding="utf-8"))
        self.assertIn("json_decode:bad token", failed.read_text(encoding="utf-8"))

    def test_write_queue(self):
        flush.write_queue("research", "sess-1", "flushed")
        text = flush.QUEUE_LOG.read_text(encoding="utf-8")
        self.assertIn("research sess-1 flushed", text)


# ---------- flush.py: main() with mocked subprocess ----------

class TestMainWithMockedSubprocess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._orig_dir = flush.DAILY_LOGS_DIR
        self._orig_queue = flush.QUEUE_LOG
        flush.DAILY_LOGS_DIR = self.tmp_path / "daily-logs"
        flush.QUEUE_LOG = flush.DAILY_LOGS_DIR / "_queue.log"
        self.transcript = self.tmp_path / "transcript.jsonl"
        self.transcript.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

    def tearDown(self):
        flush.DAILY_LOGS_DIR = self._orig_dir
        flush.QUEUE_LOG = self._orig_queue
        self.tmp.cleanup()

    def _run_main(self, argv: list[str]) -> int:
        with patch.object(sys, "argv", ["flush.py", *argv]):
            return flush.main()

    def _mock_claude(self, stdout_text: str, returncode: int = 0):
        class _Result:
            stdout = stdout_text
            stderr = ""
        _Result.returncode = returncode

        def runner(*args, **kwargs):
            return _Result()
        return runner

    def test_happy_path_writes_session_block(self):
        valid_json = json.dumps({
            "decisions": ["Chose X"],
            "findings": ["Found Y"],
            "open_questions": [],
            "proposed_concepts": [],
        })
        with patch("flush.subprocess.run", side_effect=self._mock_claude(valid_json)):
            rc = self._run_main([
                "--agent", "research",
                "--session-id", "sess-happy",
                "--transcript", str(self.transcript),
            ])
        self.assertEqual(rc, 0)
        log_file = flush.DAILY_LOGS_DIR / "research" / f"{flush.utc_today()}.md"
        self.assertTrue(log_file.exists())
        self.assertIn("Chose X", log_file.read_text(encoding="utf-8"))
        self.assertIn("research sess-happy flushed", flush.QUEUE_LOG.read_text(encoding="utf-8"))

    def test_empty_session_skipped(self):
        empty_json = json.dumps({
            "decisions": [], "findings": [], "open_questions": [], "proposed_concepts": []
        })
        with patch("flush.subprocess.run", side_effect=self._mock_claude(empty_json)):
            rc = self._run_main([
                "--agent", "research",
                "--session-id", "sess-empty",
                "--transcript", str(self.transcript),
            ])
        self.assertEqual(rc, 0)
        log_file = flush.DAILY_LOGS_DIR / "research" / f"{flush.utc_today()}.md"
        self.assertFalse(log_file.exists())
        self.assertIn("research sess-empty skipped:empty", flush.QUEUE_LOG.read_text(encoding="utf-8"))

    def test_parse_failure_writes_failed_after_retry(self):
        with patch("flush.subprocess.run", side_effect=self._mock_claude("not json at all")):
            rc = self._run_main([
                "--agent", "research",
                "--session-id", "sess-bad",
                "--transcript", str(self.transcript),
            ])
        self.assertEqual(rc, 0)
        failed = flush.DAILY_LOGS_DIR / "research" / "_failed" / "sess-bad.txt"
        self.assertTrue(failed.exists())
        queue = flush.QUEUE_LOG.read_text(encoding="utf-8")
        self.assertIn("research sess-bad failed:json_decode", queue)

    def test_transcript_missing(self):
        rc = self._run_main([
            "--agent", "research",
            "--session-id", "sess-no-tx",
            "--transcript", str(self.tmp_path / "does_not_exist.jsonl"),
        ])
        self.assertEqual(rc, 0)
        self.assertIn(
            "research sess-no-tx failed:transcript_missing",
            flush.QUEUE_LOG.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
