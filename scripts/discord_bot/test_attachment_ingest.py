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
    extract_pptx_text,
    extract_xlsx_text,
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


# --------------- extract_pptx_text ---------------


def _make_minimal_pptx(path: Path, slides: list[list[str]]) -> None:
    """Build a minimal .pptx where each slide is a list of paragraph strings.

    Only writes the `ppt/slides/slide*.xml` files. Real .pptx archives have
    a content types + relationships scaffolding, but extract_pptx_text only
    walks the slide files so we can skip the rest.
    """
    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    with zipfile.ZipFile(path, "w") as zf:
        for i, paragraphs in enumerate(slides, 1):
            body = "".join(
                f'<a:p><a:r><a:t>{p}</a:t></a:r></a:p>' for p in paragraphs
            )
            slide_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<p:sld xmlns:p="{p_ns}" xmlns:a="{a_ns}">'
                f'<p:cSld><p:spTree>{body}</p:spTree></p:cSld>'
                '</p:sld>'
            )
            zf.writestr(f"ppt/slides/slide{i}.xml", slide_xml)


class TestExtractPptx(unittest.TestCase):
    def test_extracts_slides_in_order_with_headers(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "deck.pptx"
            _make_minimal_pptx(p, [
                ["Title slide", "Subtitle here"],
                ["Second slide opener", "More detail on slide two"],
                ["Closing thoughts"],
            ])
            out = extract_pptx_text(p)
            self.assertIn("## Slide 1", out)
            self.assertIn("## Slide 2", out)
            self.assertIn("## Slide 3", out)
            self.assertIn("Title slide", out)
            self.assertIn("Subtitle here", out)
            self.assertIn("More detail on slide two", out)
            # Slide order preserved.
            self.assertLess(out.index("Slide 1"), out.index("Slide 2"))
            self.assertLess(out.index("Slide 2"), out.index("Slide 3"))

    def test_slide_with_no_text_renders_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "blank.pptx"
            _make_minimal_pptx(p, [[]])  # one slide, no text
            out = extract_pptx_text(p)
            self.assertIn("## Slide 1", out)
            self.assertIn("no text content", out)

    def test_no_slides_raises_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "empty.pptx"
            # Valid zip but with no slide files inside.
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("ppt/presentation.xml", "<dummy/>")
            with self.assertRaises(ValueError):
                extract_pptx_text(p)

    def test_bad_zip_raises_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "notreallypptx.pptx"
            p.write_bytes(b"not a zip")
            with self.assertRaises(ValueError):
                extract_pptx_text(p)


# --------------- extract_xlsx_text ---------------


def _make_minimal_xlsx(path: Path, sheets: list[list[list[str]]],
                       *, use_shared_strings: bool = True) -> None:
    """Build a minimal .xlsx. `sheets` is a list of sheets; each sheet is a
    list of rows; each row is a list of cell string values.

    If `use_shared_strings`, strings go into xl/sharedStrings.xml and cells
    reference them by index. Otherwise cells use inlineStr.
    """
    s_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared: list[str] = []

    def _shared_idx(s: str) -> int:
        if s not in shared:
            shared.append(s)
        return shared.index(s)

    with zipfile.ZipFile(path, "w") as zf:
        for sheet_i, rows in enumerate(sheets, 1):
            row_xml_parts: list[str] = []
            for row_i, row in enumerate(rows, 1):
                cell_xml_parts: list[str] = []
                for col_i, val in enumerate(row):
                    col_letter = chr(ord("A") + col_i)
                    ref = f'{col_letter}{row_i}'
                    if use_shared_strings:
                        idx = _shared_idx(val)
                        cell_xml_parts.append(
                            f'<c r="{ref}" t="s"><v>{idx}</v></c>'
                        )
                    else:
                        cell_xml_parts.append(
                            f'<c r="{ref}" t="inlineStr"><is><t>{val}</t></is></c>'
                        )
                row_xml_parts.append(
                    f'<row r="{row_i}">{"".join(cell_xml_parts)}</row>'
                )
            sheet_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<worksheet xmlns="{s_ns}"><sheetData>'
                f'{"".join(row_xml_parts)}'
                '</sheetData></worksheet>'
            )
            zf.writestr(f"xl/worksheets/sheet{sheet_i}.xml", sheet_xml)

        if use_shared_strings and shared:
            si_parts = "".join(f"<si><t>{s}</t></si>" for s in shared)
            sst_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<sst xmlns="{s_ns}" count="{len(shared)}" uniqueCount="{len(shared)}">'
                f'{si_parts}'
                '</sst>'
            )
            zf.writestr("xl/sharedStrings.xml", sst_xml)


class TestExtractXlsx(unittest.TestCase):
    def test_extracts_sheets_in_order_with_shared_strings(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.xlsx"
            _make_minimal_xlsx(p, [
                [["Name", "Role"], ["Andy", "Senior Manager"]],
                [["Team", "Headcount"], ["GRC", "12"]],
            ])
            out = extract_xlsx_text(p)
            self.assertIn("## Sheet 1", out)
            self.assertIn("## Sheet 2", out)
            self.assertIn("Andy", out)
            self.assertIn("Senior Manager", out)
            self.assertIn("GRC", out)
            # Tab-separated rows on the same line.
            self.assertIn("Name\tRole", out)
            self.assertIn("Andy\tSenior Manager", out)

    def test_inline_strings_work_when_no_shared_table(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "inline.xlsx"
            _make_minimal_xlsx(p, [[["Inline", "values"]]],
                               use_shared_strings=False)
            out = extract_xlsx_text(p)
            self.assertIn("Inline", out)
            self.assertIn("values", out)

    def test_no_sheets_raises_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "empty.xlsx"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("xl/workbook.xml", "<dummy/>")
            with self.assertRaises(ValueError):
                extract_xlsx_text(p)

    def test_bad_zip_raises_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "notreallyxlsx.xlsx"
            p.write_bytes(b"not a zip")
            with self.assertRaises(ValueError):
                extract_xlsx_text(p)


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
        # Redirect dropped-files dirs to a tempdir so tests don't pollute the vault.
        # We override both Echo's and Luna's drop dirs because tests below exercise
        # both agents.
        self._tmp = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmp.name)
        self._orig_dir = ai.DROPPED_FILES_DIR
        self._orig_map = dict(ai.DROPPED_FILES_DIR_PER_AGENT)
        ai.DROPPED_FILES_DIR = tmp_root / "echo-dropped"
        ai.DROPPED_FILES_DIR_PER_AGENT["echo"] = tmp_root / "echo-dropped"
        ai.DROPPED_FILES_DIR_PER_AGENT["luna"] = tmp_root / "luna-dropped"

    def tearDown(self):
        ai.DROPPED_FILES_DIR = self._orig_dir
        ai.DROPPED_FILES_DIR_PER_AGENT.clear()
        ai.DROPPED_FILES_DIR_PER_AGENT.update(self._orig_map)
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

    def test_pptx_file_creates_sidecar(self):
        with tempfile.TemporaryDirectory() as src_dir:
            src = Path(src_dir) / "src.pptx"
            _make_minimal_pptx(src, [["First slide line"], ["Second slide line"]])
            payload = src.read_bytes()

        att = FakeAttachment("deck.pptx", payload)
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="luna"))
        self.assertEqual(len(result.ingested), 1)
        ing = result.ingested[0]
        self.assertIsNotNone(ing.sidecar_text_path)
        self.assertTrue(ing.sidecar_text_path.exists())
        sidecar_text = ing.sidecar_text_path.read_text()
        self.assertIn("Slide 1", sidecar_text)
        self.assertIn("First slide line", sidecar_text)
        self.assertIn("Second slide line", sidecar_text)

    def test_xlsx_file_creates_sidecar(self):
        with tempfile.TemporaryDirectory() as src_dir:
            src = Path(src_dir) / "src.xlsx"
            _make_minimal_xlsx(src, [[["Header"], ["Value"]]])
            payload = src.read_bytes()

        att = FakeAttachment("data.xlsx", payload)
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="luna"))
        self.assertEqual(len(result.ingested), 1)
        ing = result.ingested[0]
        self.assertIsNotNone(ing.sidecar_text_path)
        self.assertTrue(ing.sidecar_text_path.exists())
        sidecar_text = ing.sidecar_text_path.read_text()
        self.assertIn("Sheet 1", sidecar_text)
        self.assertIn("Header", sidecar_text)
        self.assertIn("Value", sidecar_text)

    def test_pptx_accepted_by_echo_too(self):
        with tempfile.TemporaryDirectory() as src_dir:
            src = Path(src_dir) / "src.pptx"
            _make_minimal_pptx(src, [["echo slide content"]])
            payload = src.read_bytes()
        att = FakeAttachment("deck.pptx", payload)
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="echo"))
        self.assertEqual(len(result.ingested), 1)
        self.assertEqual(len(result.rejected), 0)

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

    # ---- per-agent routing ----

    def test_luna_accepts_png_no_sidecar(self):
        # 8-byte PNG signature is enough; we don't validate the rest.
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        att = FakeAttachment("screenshot.png", png_bytes)
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="luna"))
        self.assertEqual(len(result.ingested), 1)
        self.assertEqual(len(result.rejected), 0)
        ing = result.ingested[0]
        self.assertEqual(ing.original_filename, "screenshot.png")
        self.assertTrue(ing.saved_path.exists())
        # Images get no sidecar — the Read tool handles them as multimodal input.
        self.assertIsNone(ing.sidecar_text_path)
        # And the file landed in Luna's drop dir, not Echo's.
        self.assertIn("luna-dropped", str(ing.saved_path))

    def test_echo_rejects_png(self):
        # Echo's allowlist is docs-only; an image is `unsupported_extension`.
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        att = FakeAttachment("screenshot.png", png_bytes)
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="echo"))
        self.assertEqual(len(result.ingested), 0)
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("unsupported_extension", result.rejected[0][1])

    def test_luna_still_accepts_docs(self):
        # Luna's allowlist is the union of docs + images.
        att = FakeAttachment("notes.txt", b"hello\n")
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="luna"))
        self.assertEqual(len(result.ingested), 1)
        self.assertIn("luna-dropped", str(result.ingested[0].saved_path))

    def test_unwired_agent_rejects_everything(self):
        # An agent not in ALLOWED_EXTENSIONS_PER_AGENT has every attachment
        # rejected with `agent_not_wired_for_ingest`. handlers.py shouldn't
        # call us in this case, but the function must not crash if it happens.
        att = FakeAttachment("anything.txt", b"data")
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="content"))
        self.assertEqual(len(result.ingested), 0)
        self.assertEqual(len(result.rejected), 1)
        self.assertEqual(result.rejected[0][1], "agent_not_wired_for_ingest")

    def test_image_filename_sanitized_extension_preserved(self):
        # Filenames with spaces/unicode still keep the image extension so
        # the Read tool recognizes them as images.
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        att = FakeAttachment("My Cool 📸.png", png_bytes)
        result = _run(ingest_attachments(FakeMessage([att]), agent_id="luna"))
        self.assertEqual(len(result.ingested), 1)
        self.assertTrue(str(result.ingested[0].saved_path).endswith(".png"))


# --------------- per-agent accessors ---------------


class TestPerAgentAccessors(unittest.TestCase):
    def test_allowed_extensions_for_echo(self):
        from scripts.discord_bot.attachment_ingest import allowed_extensions_for
        exts = allowed_extensions_for("echo")
        self.assertIn(".txt", exts)
        self.assertIn(".docx", exts)
        self.assertNotIn(".png", exts)

    def test_allowed_extensions_for_luna(self):
        from scripts.discord_bot.attachment_ingest import allowed_extensions_for
        exts = allowed_extensions_for("luna")
        self.assertIn(".txt", exts)
        self.assertIn(".png", exts)
        self.assertIn(".jpg", exts)
        self.assertIn(".heic", exts)

    def test_allowed_extensions_for_unknown_agent_empty(self):
        from scripts.discord_bot.attachment_ingest import allowed_extensions_for
        self.assertEqual(allowed_extensions_for("some-future-agent"), set())

    def test_dropped_files_dir_routing(self):
        from scripts.discord_bot.attachment_ingest import dropped_files_dir_for
        self.assertIn("Andy Profile", str(dropped_files_dir_for("echo")))
        self.assertIn("Luna", str(dropped_files_dir_for("luna")))


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
