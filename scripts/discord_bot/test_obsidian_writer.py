"""Unit tests for obsidian_writer.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.obsidian_writer import (  # noqa: E402
    KANBAN_SUBPATH,
    ObsidianWriter,
    append_status_line,
    render_initial_note,
    resolve_within_vault,
    safe_markdown_file_name,
    update_status_to,
)


class TestSafeFilename(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(
            safe_markdown_file_name("Hello World", issue_number=12),
            "Issue 12 - Hello World.md",
        )

    def test_strips_unsafe_chars(self):
        self.assertEqual(
            safe_markdown_file_name("Why/does:this*break?", issue_number=1),
            "Issue 1 - Why-does-this-break.md",
        )

    def test_collapses_dashes(self):
        self.assertEqual(
            safe_markdown_file_name("a / / / b", issue_number=2),
            "Issue 2 - a - - - b.md",
        )

    def test_empty_falls_back_to_untitled(self):
        self.assertEqual(
            safe_markdown_file_name("???", issue_number=3),
            "Issue 3 - untitled.md",
        )

    def test_truncates_long_titles(self):
        long_title = "x" * 500
        out = safe_markdown_file_name(long_title, issue_number=1)
        self.assertTrue(out.endswith(".md"))
        self.assertLessEqual(len(out), 100 + len(".md"))


class TestResolveWithinVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal(self):
        out = resolve_within_vault(self.root, Path("Neural Bridge/Kanban/Issues/x.md"))
        self.assertTrue(str(out).startswith(str(self.root.resolve())))

    def test_blocks_traversal(self):
        with self.assertRaises(ValueError):
            resolve_within_vault(self.root, Path("../../../etc/passwd"))

    def test_blocks_absolute_escape(self):
        with self.assertRaises(ValueError):
            resolve_within_vault(self.root, Path("/etc/passwd"))


class TestRenderNote(unittest.TestCase):
    def test_includes_required_sections(self):
        body = render_initial_note(
            issue_number=42,
            title="Build dashboard",
            issue_url="https://github.com/x/y/issues/42",
            source_request="I want a dashboard.",
            closure_criteria="When users see X.",
            initial_owner="senior-pm",
            discord_thread_url="https://discord.com/x",
        )
        self.assertIn("type: kanban-issue", body)
        self.assertIn("source_issue: 42", body)
        self.assertIn("# Issue 42 - Build dashboard", body)
        self.assertIn("> I want a dashboard.", body)
        self.assertIn("When users see X.", body)
        self.assertIn("Initial owner: **senior-pm**", body)
        self.assertIn("https://discord.com/x", body)

    def test_handles_missing_closure(self):
        body = render_initial_note(
            issue_number=1, title="t", issue_url="u", source_request="s",
            closure_criteria=None, initial_owner="senior-pm", discord_thread_url=None,
        )
        self.assertIn("not captured", body)


class TestStatusOps(unittest.TestCase):
    def test_appends_to_existing_status(self):
        original = "## Status\n\n- 2026-05-09T08:00:00Z — opened\n"
        out = append_status_line(original, "handed off to research")
        self.assertIn("opened", out)
        self.assertIn("handed off to research", out)

    def test_creates_status_when_missing(self):
        original = "# title\n\nbody\n"
        out = append_status_line(original, "first event")
        self.assertIn("## Status", out)
        self.assertIn("first event", out)

    def test_updates_frontmatter_updated_field(self):
        original = "---\nupdated: 2026-01-01T00:00:00Z\n---\n\nbody"
        out = append_status_line(original, "event")
        self.assertIn("updated: 2026-", out)
        self.assertNotIn("updated: 2026-01-01T00:00:00Z", out)

    def test_update_status_to(self):
        original = "---\nstatus: open\n---\n\nbody"
        out = update_status_to(original, "closed")
        self.assertIn("status: closed", out)
        self.assertNotIn("status: open", out)


class TestObsidianWriterIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        self.writer = ObsidianWriter(vault_root=self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_initial_creates_file(self):
        path = self.writer.write_initial_note(
            issue_number=7,
            title="Test Issue",
            issue_url="https://github.com/x/y/issues/7",
            source_request="do a thing",
            closure_criteria="thing is done",
            discord_thread_url="https://discord.com/x",
        )
        self.assertTrue(path.exists())
        self.assertTrue(path.name.startswith("Issue 7 - "))
        self.assertIn(str(KANBAN_SUBPATH), str(path))

    def test_write_initial_is_idempotent(self):
        p1 = self.writer.write_initial_note(
            issue_number=1, title="A", issue_url="u",
            source_request="r", closure_criteria=None,
        )
        p2 = self.writer.write_initial_note(
            issue_number=1, title="A different title",
            issue_url="u", source_request="r", closure_criteria=None,
        )
        self.assertEqual(p1, p2)

    def test_append_status_returns_none_when_missing(self):
        result = self.writer.append_status(issue_number=999, line="x")
        self.assertIsNone(result)

    def test_append_status_after_create(self):
        self.writer.write_initial_note(
            issue_number=5, title="A", issue_url="u",
            source_request="r", closure_criteria="c",
        )
        path = self.writer.append_status(
            issue_number=5, line="closed via Discord", new_status="closed",
        )
        self.assertIsNotNone(path)
        text = path.read_text(encoding="utf-8")
        self.assertIn("closed via Discord", text)
        self.assertIn("status: closed", text)

    def test_write_creates_directory_structure(self):
        self.assertFalse((self.vault / KANBAN_SUBPATH).exists())
        self.writer.write_initial_note(
            issue_number=1, title="A", issue_url="u",
            source_request="r", closure_criteria=None,
        )
        self.assertTrue((self.vault / KANBAN_SUBPATH).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
