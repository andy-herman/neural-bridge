"""Unit tests for scripts/echo/synthesize_profile.py.

Covers:
- Cursor load/save round-trip
- raw-conversations.md slicing by timestamp (first run, mid-cursor, past-cursor)
- Response parser (clean output, code-fence wrapped, NO-ADDITIONS handling, trailing note)
- append_to_profile_file (existing file vs new file creation)

Run: `python3 scripts/echo/test_synthesize_profile.py`
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.echo import synthesize_profile as sp  # noqa: E402


SAMPLE_RAW = """### 2026-05-10T19:57:19Z — #content-backlog
<!-- message_id: 1503123793093333172 -->

> hey @content any thoughts on this?

### 2026-05-11T20:14:03Z — #general
<!-- message_id: 1503128005063479348 -->

> can you introduce yourself like me?

### 2026-05-12T20:26:14Z — #general
<!-- message_id: 1503131073239060581 -->

> shipping the new charter changes today

### 2026-05-13T05:08:39Z — #neural-bridge
<!-- message_id: 1503987319265300603 -->

> Yes please commit
"""


# ---------- Cursor ----------


class TestCursor(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.patcher = mock.patch.object(sp, "CURSOR_PATH", self.tmpdir / ".cursor")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_first_run_returns_none(self):
        self.assertIsNone(sp.load_cursor())

    def test_save_and_load_round_trip(self):
        ts = datetime(2026, 5, 12, 20, 26, 14, tzinfo=timezone.utc)
        sp.save_cursor(ts)
        loaded = sp.load_cursor()
        self.assertEqual(loaded, ts)

    def test_malformed_cursor_treated_as_first_run(self):
        sp.CURSOR_PATH.write_text("not-a-timestamp\n", encoding="utf-8")
        self.assertIsNone(sp.load_cursor())


# ---------- Slicing raw-conversations.md ----------


class TestSlicing(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.raw_path = self.tmpdir / "raw.md"
        self.raw_path.write_text(SAMPLE_RAW, encoding="utf-8")
        self.patcher = mock.patch.object(sp, "RAW_CONVERSATIONS", self.raw_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.raw_path.unlink(missing_ok=True)
        self.tmpdir.rmdir()

    def test_first_run_returns_all_entries(self):
        sliced, latest = sp.slice_raw_conversations(since=None)
        self.assertIn("2026-05-10T19:57:19Z", sliced)
        self.assertIn("2026-05-13T05:08:39Z", sliced)
        self.assertEqual(latest, datetime(2026, 5, 13, 5, 8, 39, tzinfo=timezone.utc))

    def test_cursor_mid_corpus_returns_only_newer(self):
        since = datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc)
        sliced, latest = sp.slice_raw_conversations(since=since)
        self.assertNotIn("2026-05-10T19:57:19Z", sliced)
        self.assertIn("2026-05-11T20:14:03Z", sliced)
        self.assertIn("2026-05-13T05:08:39Z", sliced)
        self.assertEqual(latest, datetime(2026, 5, 13, 5, 8, 39, tzinfo=timezone.utc))

    def test_cursor_past_corpus_returns_empty(self):
        since = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        sliced, latest = sp.slice_raw_conversations(since=since)
        self.assertEqual(sliced, "")
        self.assertIsNone(latest)

    def test_missing_raw_conversations_returns_empty(self):
        self.raw_path.unlink()
        sliced, latest = sp.slice_raw_conversations(since=None)
        self.assertEqual(sliced, "")
        self.assertIsNone(latest)

    def test_cursor_exactly_matching_entry_excludes_that_entry(self):
        # Cursor at the most recent entry's timestamp should return nothing
        # new (we use > not >=).
        since = datetime(2026, 5, 13, 5, 8, 39, tzinfo=timezone.utc)
        sliced, latest = sp.slice_raw_conversations(since=since)
        self.assertEqual(sliced, "")
        self.assertIsNone(latest)


# ---------- Response parser ----------


class TestParseResponse(unittest.TestCase):
    def test_clean_output_all_sections(self):
        response = """<<<FILE: voice.md>>>
**Observation: short imperative closes** (recurring)

He ends decision messages with two-word imperatives.

Citations:
- "Yes please commit" (raw-conversations.md message_id: 1503987319265300603)

<<<FILE: thinking-patterns.md>>>
NO-ADDITIONS
<<<FILE: vocabulary.md>>>
NO-ADDITIONS
<<<FILE: questions.md>>>
NO-ADDITIONS
<<<FILE: opinions.md>>>
NO-ADDITIONS
<<<FILE: examples.md>>>
NO-ADDITIONS
<<<END>>>
Window contained mostly approval shorthand.
"""
        out = sp.parse_response(response)
        self.assertIsNotNone(out.additions.get("voice.md"))
        self.assertIsNone(out.additions.get("thinking-patterns.md"))
        self.assertIsNone(out.additions.get("vocabulary.md"))
        self.assertEqual(out.trailing_note, "Window contained mostly approval shorthand.")
        self.assertIn("short imperative closes", out.additions["voice.md"])

    def test_code_fence_wrapping_stripped(self):
        response = """```
<<<FILE: voice.md>>>
NO-ADDITIONS
<<<FILE: thinking-patterns.md>>>
NO-ADDITIONS
<<<FILE: vocabulary.md>>>
NO-ADDITIONS
<<<FILE: questions.md>>>
NO-ADDITIONS
<<<FILE: opinions.md>>>
NO-ADDITIONS
<<<FILE: examples.md>>>
NO-ADDITIONS
<<<END>>>
```"""
        out = sp.parse_response(response)
        for name in sp.PROFILE_FILES:
            self.assertIsNone(out.additions.get(name))

    def test_missing_markers_returns_empty_additions(self):
        out = sp.parse_response("no markers at all in this response")
        self.assertEqual(out.additions, {})
        self.assertIn("no markers", out.trailing_note)


# ---------- Append to profile file ----------


class TestAppendToProfile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.patcher = mock.patch.object(sp, "VAULT_PROFILE_DIR", self.tmpdir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for p in self.tmpdir.glob("*"):
            p.unlink()
        self.tmpdir.rmdir()

    def test_append_to_existing_file_preserves_prior_content(self):
        path = self.tmpdir / "voice.md"
        path.write_text("# Voice\n\nExisting observation.\n", encoding="utf-8")
        sp.append_to_profile_file("voice.md", "New observation block.", cursor_label="2026-05-13T05:00:00Z")
        body = path.read_text(encoding="utf-8")
        self.assertIn("Existing observation.", body)
        self.assertIn("## Synthesis pass 2026-05-13T05:00:00Z", body)
        self.assertIn("New observation block.", body)
        # Existing content comes first; addition is appended.
        self.assertLess(body.find("Existing observation."), body.find("New observation block."))

    def test_first_time_file_creation_writes_preamble(self):
        sp.append_to_profile_file("vocabulary.md", "First addition.", cursor_label="2026-05-13T05:00:00Z")
        path = self.tmpdir / "vocabulary.md"
        self.assertTrue(path.exists())
        body = path.read_text(encoding="utf-8")
        self.assertIn("# Vocabulary", body)
        self.assertIn("First addition.", body)


if __name__ == "__main__":
    unittest.main()
