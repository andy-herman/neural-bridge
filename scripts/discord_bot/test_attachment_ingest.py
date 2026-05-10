"""Unit tests for attachment_ingest.py — inbound file handling for Echo.

Stdlib-only. No real Discord API calls; attachments are mocked.
Run: `python3 scripts/discord_bot/test_attachment_ingest.py`
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot import attachment_ingest as ai  # noqa: E402
from scripts.discord_bot.attachment_ingest import (  # noqa: E402
    IngestedFile,
    IngestResult,
    extract_docx_text,
    extract_eml_text,
    format_prompt_block,
    ingest_attachments,
    sanitize_filename,
)


# --------------- sanitize_filename ---------------


class TestSanitizeFilename(unittest.TestCase):
    def test_keeps_safe_name(self):
        self.assertEqual(sanitize_filename("notes.txt"), "notes.txt")

    def test_replaces_spaces(self):
        self.assertEqual(sanitize_filename("my notes.txt"), "my-notes.txt")

    def test_strips_path_components(self):
        # Whatever path-y prefix the client sent, we keep only the basename.
        self.assertEqual(sanitize_filename("/etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("../../foo.txt"), "foo.txt")

    def test_strips_unicode_garbage(self):
        out = sanitize_filename("résumé 📄.docx")
        self.assertTrue(out.endswith(".docx"))
        self.assertNotIn(" ", out)

    def test_falls_back_when_empty(self):
        self.assertEqual(sanitize_filename(""), "dropped-file")
        self.assertEqual(sanitize_filename("..."), "dropped-file")

    def test_caps_length_keeps_suffix(self):
        out = sanitize_filename("x" * 300 + ".docx", max_len=40)
        self.assertLessEqual(len(out), 40)
        self.assertTrue(out.endswith(".docx"))


# --------------- extract_docx_text ---------------


def _make_minimal_docx(path: Path, paragraphs: list[str]) -> None:
    """Build a minimal .docx that extract_docx_text can read."""
    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>'
        for p in paragraphs
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{w_ns}">'
        f"<w:body>{body}</w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", document_xml)


class TestExtractDocx(unittest.TestCase):
    def test_extracts_paragraphs_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.docx"
            _make_minimal_docx(p, ["Hello world.", "Second paragraph.", "Third."])
            out = extract_docx_text(p)
            self.assertIn("Hello world.", out)
            self.assertIn("Second paragraph.", out)
            self.assertIn("Third.", out)
            # Paragraph order preserved.
            self.assertLess(out.index("Hello"), out.index("Second"))

    def test_bad_zip_raises_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "notreallydocx.docx"
            p.write_bytes(b"not a zip file at all")
            with self.assertRaises(ValueError):
                extract_docx_text(p)


# --------------- extract_eml_text ---------------


class TestExtractEml(unittest.TestCase):
    def test_extracts_plain_text_body_and_headers(self):
        eml = (
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Lunch Thursday\r\n"
            "Date: Thu, 1 May 2026 10:00:00 +0000\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Hey Bob, are you free for lunch?\r\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "msg.eml"
            p.write_text(eml, encoding="utf-8")
            out = extract_eml_text(p)
            self.assertIn("From: alice@example.com", out)
            self.assertIn("Subject: Lunch Thursday", out)
            self.assertIn("Hey Bob, are you free for lunch?", out)

    def test_extracts_html_body_stripped(self):
        eml = (
            "From: alice@example.com\r\n"
            "Subject: Hi\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: text/html; charset="utf-8"\r\n'
            "\r\n"
            "<p>Hello <b>world</b>.</p><br><p>Second line.</p>\r\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "msg.eml"
            p.write_text(eml, encoding="utf-8")
            out = extract_eml_text(p)
            self.assertIn("Hello world.", out)
            self.assertIn("Second line.", out)
            self.assertNotIn("<p>", out)
            self.assertNotIn("<b>", out)


# --------------- ingest_attachments (end-to-end with mocked discord) ---------------


class FakeAttachment:
    """Mocks the minimal surface of discord.Attachment that ingest uses."""

    def __init__(self, filename: str, payload: bytes):
        self.filename = filename
        self.size = len(payload)
        self._payload = payload

    async def save(self, fp):
        # Discord-py accepts a path-like or a file-like. We only use the path-like flavor.
        Path(fp).write_bytes(self._payload)


class FakeMessage:
    def __init__(self, attachments):
        self.attachments = attachments


def _run(coro):
    return asyncio.run(coro)


class TestIngestAttachments(unittest.TestCase):
    def setUp(self):
        # Redirect dropped-files dir to a tempdir so tests don't pollute the vault.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir = ai.DROPPED_FILES_DIR
        ai.DROPPED_FILES_DIR = Path(self._tmp.name) / "dropped-files"

    def tearDown(self):
        ai.DROPPED_FILES_DIR = self._orig_dir
        self._tmp.cleanup()

    def test_empty_attachments_returns_empty_result(self):
        result = _run(ingest_attachments(FakeMessage([]), agent_id="echo"))
        self.assertEqual(result.ingested, [])
        self.assertEqual(result.rejected, [])
        self.assertFalse(result.over_cap)

    def test_txt_file_saved_as_is(self):
        att = FakeAttachment("notes.txt", b"Hello there, this is a note.\n")
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="echo"))
        self.assertEqual(len(result.ingested), 1)
        self.assertEqual(len(result.rejected), 0)
        ing = result.ingested[0]
        self.assertEqual(ing.original_filename, "notes.txt")
        self.assertTrue(ing.saved_path.exists())
        self.assertIsNone(ing.sidecar_text_path)
        self.assertEqual(ing.saved_path.read_text(), "Hello there, this is a note.\n")

    def test_docx_file_creates_sidecar(self):
        with tempfile.TemporaryDirectory() as src_dir:
            src = Path(src_dir) / "src.docx"
            _make_minimal_docx(src, ["Alpha.", "Beta gamma."])
            payload = src.read_bytes()

        att = FakeAttachment("report.docx", payload)
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="echo"))
        self.assertEqual(len(result.ingested), 1)
        ing = result.ingested[0]
        self.assertTrue(ing.saved_path.exists())
        self.assertIsNotNone(ing.sidecar_text_path)
        self.assertTrue(ing.sidecar_text_path.exists())
        sidecar_text = ing.sidecar_text_path.read_text()
        self.assertIn("Alpha.", sidecar_text)
        self.assertIn("Beta gamma.", sidecar_text)

    def test_eml_file_creates_sidecar(self):
        eml = (
            b"From: a@x.com\r\n"
            b"Subject: Test\r\n"
            b"\r\n"
            b"Body text here.\r\n"
        )
        att = FakeAttachment("note.eml", eml)
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="echo"))
        self.assertEqual(len(result.ingested), 1)
        ing = result.ingested[0]
        self.assertIsNotNone(ing.sidecar_text_path)
        self.assertIn("Body text here.", ing.sidecar_text_path.read_text())

    def test_unsupported_extension_rejected(self):
        att = FakeAttachment("evil.exe", b"MZ\x00\x00")
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="echo"))
        self.assertEqual(len(result.ingested), 0)
        self.assertEqual(len(result.rejected), 1)
        name, reason = result.rejected[0]
        self.assertEqual(name, "evil.exe")
        self.assertIn("unsupported_extension", reason)

    def test_oversize_rejected(self):
        # Construct an attachment whose claimed size is over the cap. We don't
        # need actual bytes — the size check happens before download.
        att = FakeAttachment("huge.txt", b"")
        att.size = ai.MAX_FILE_BYTES + 1
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="echo"))
        self.assertEqual(len(result.ingested), 0)
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("too_large", result.rejected[0][1])

    def test_over_cap_flag_set(self):
        # 6 attachments → first 5 processed, over_cap True.
        atts = [FakeAttachment(f"f{i}.txt", b"x") for i in range(6)]
        result = _run(ingest_attachments(FakeMessage(atts), agent_id="echo"))
        self.assertTrue(result.over_cap)
        self.assertEqual(len(result.ingested), 5)

    def test_name_collision_disambiguated(self):
        att1 = FakeAttachment("dup.txt", b"first")
        att2 = FakeAttachment("dup.txt", b"second")
        result = _run(ingest_attachments(FakeMessage([att1, att2]), agent_id="echo"))
        self.assertEqual(len(result.ingested), 2)
        paths = {i.saved_path for i in result.ingested}
        self.assertEqual(len(paths), 2)  # different paths despite same filename


# --------------- format_prompt_block ---------------


class TestFormatPromptBlock(unittest.TestCase):
    def test_empty_result_returns_empty_string(self):
        self.assertEqual(format_prompt_block(IngestResult()), "")

    def test_renders_files_with_sidecar_priority(self):
        result = IngestResult(
            ingested=[
                IngestedFile(
                    original_filename="email.eml",
                    saved_path=Path("/tmp/email.eml"),
                    sidecar_text_path=Path("/tmp/email.eml.txt"),
                    size_bytes=1234,
                ),
                IngestedFile(
                    original_filename="plain.txt",
                    saved_path=Path("/tmp/plain.txt"),
                    sidecar_text_path=None,
                    size_bytes=42,
                ),
            ]
        )
        out = format_prompt_block(result)
        self.assertIn("Andy dropped files", out)
        # Sidecar path appears as the "Read:" path for the .eml.
        self.assertIn("`/tmp/email.eml.txt`", out)
        # Original appears as a sub-bullet.
        self.assertIn("Original (binary)", out)
        # Plain .txt uses its own path as "Read:".
        self.assertIn("`/tmp/plain.txt`", out)


if __name__ == "__main__":
    unittest.main()
