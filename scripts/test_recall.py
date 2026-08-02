"""Unit tests for recall.py chunking and manifest logic.

Stdlib-only. No chromadb import, no model download — only the pure
functions are exercised. Run: `python3 scripts/test_recall.py`
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import recall as R  # noqa: E402


class TestSplitSections(unittest.TestCase):
    def test_preamble_and_headings(self):
        text = "intro line\n\n## First\nalpha\n\n### Sub\nbeta\n\n## Second\ngamma"
        sections = R.split_sections(text)
        self.assertEqual(
            [h for h, _ in sections], ["", "First", "Sub", "Second"]
        )
        self.assertEqual(sections[0][1], "intro line")
        self.assertEqual(sections[3][1], "gamma")

    def test_empty_sections_dropped(self):
        sections = R.split_sections("## A\n\n## B\ncontent")
        self.assertEqual(sections, [("B", "content")])

    def test_no_headings(self):
        sections = R.split_sections("just a note body")
        self.assertEqual(sections, [("", "just a note body")])


class TestChunkText(unittest.TestCase):
    def test_short_body_single_chunk(self):
        self.assertEqual(R.chunk_text("short"), ["short"])

    def test_long_body_overlapping_windows(self):
        body = "x" * 3000
        chunks = R.chunk_text(body, size=1200, overlap=200)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 1200 for c in chunks))
        # windows advance by size - overlap, so full coverage is preserved
        self.assertEqual(sum(len(c) for c in chunks) - 2 * 200, 3000)

    def test_exact_size_no_split(self):
        self.assertEqual(len(R.chunk_text("y" * 1200, size=1200)), 1)


REAL_BODY = "chose worktrees because the auto-reload watcher kept moving HEAD"


class TestChunkFile(unittest.TestCase):
    def test_title_and_heading_prefix(self):
        path = Path("/tmp/2026-08-01.md")
        chunks = R.chunk_file(path, f"## Decisions\n{REAL_BODY}")
        self.assertEqual(len(chunks), 1)
        suffix, text = chunks[0]
        self.assertEqual(suffix, "0")
        self.assertTrue(text.startswith("2026-08-01 — Decisions\n\n"))
        self.assertIn("chose worktrees", text)

    def test_preamble_uses_title_only(self):
        chunks = R.chunk_file(Path("/tmp/note.md"), REAL_BODY)
        self.assertTrue(chunks[0][1].startswith("note\n\n"))

    def test_empty_scaffolding_sections_skipped(self):
        text = "## Open questions\n- —\n\n## Decisions\n" + REAL_BODY
        chunks = R.chunk_file(Path("/tmp/note.md"), text)
        self.assertEqual(len(chunks), 1)
        self.assertIn("Decisions", chunks[0][1])

    def test_repeated_sections_deduplicated(self):
        section = f"## Log\n{REAL_BODY}\n\n"
        chunks = R.chunk_file(Path("/tmp/note.md"), section * 5)
        self.assertEqual(len(chunks), 1)


class TestHasContent(unittest.TestCase):
    def test_dash_placeholder_rejected(self):
        self.assertFalse(R.has_content("- —"))

    def test_real_prose_accepted(self):
        self.assertTrue(R.has_content(REAL_BODY))


class TestDocId(unittest.TestCase):
    def test_stable_and_distinct(self):
        a = R.doc_id(Path("/a.md"), "0")
        self.assertEqual(a, R.doc_id(Path("/a.md"), "0"))
        self.assertNotEqual(a, R.doc_id(Path("/a.md"), "1"))
        self.assertNotEqual(a, R.doc_id(Path("/b.md"), "0"))


class TestManifest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "recall"
            orig_dir, orig_file = R.DATA_DIR, R.MANIFEST_FILE
            R.DATA_DIR = data_dir
            R.MANIFEST_FILE = data_dir / "manifest.json"
            try:
                self.assertEqual(R.load_manifest(), {})
                R.save_manifest({"/x.md": 123})
                self.assertEqual(R.load_manifest(), {"/x.md": 123})
            finally:
                R.DATA_DIR, R.MANIFEST_FILE = orig_dir, orig_file


class TestIterMarkdown(unittest.TestCase):
    def test_excludes_and_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "keep.md").write_text("a")
            (root / "skip.txt").write_text("b")
            (root / ".obsidian").mkdir()
            (root / ".obsidian" / "hidden.md").write_text("c")
            found = [p.name for p in R.iter_markdown(root)]
            self.assertEqual(found, ["keep.md"])

    def test_missing_root_yields_nothing(self):
        self.assertEqual(list(R.iter_markdown(Path("/nonexistent-xyz"))), [])


if __name__ == "__main__":
    unittest.main()
