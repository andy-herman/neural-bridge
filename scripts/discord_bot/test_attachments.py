"""Unit tests for attachments.py — extract, validate, and batch.

Stdlib-only. No real Discord API calls.
Run: `python3 scripts/discord_bot/test_attachments.py`
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.attachments import (  # noqa: E402
    MAX_ATTACHMENTS_PER_MESSAGE,
    extract_attachments,
    validate_attachment_batch,
    validate_path,
)


# ============================================================================
# extract_attachments
# ============================================================================


class TestExtractAttachments(unittest.TestCase):
    def test_no_block_returns_text_unchanged(self):
        result = extract_attachments("Just a normal response.")
        self.assertEqual(result.visible_response, "Just a normal response.")
        self.assertIsNone(result.paths)
        self.assertIsNone(result.parse_error)

    def test_extracts_single_path(self):
        text = (
            "Here's the file you asked for.\n\n"
            "```attachments\n"
            '["/Users/andyherman/Desktop/foo.pptx"]\n'
            "```"
        )
        result = extract_attachments(text)
        self.assertEqual(result.paths, ["/Users/andyherman/Desktop/foo.pptx"])
        self.assertEqual(result.visible_response, "Here's the file you asked for.")
        self.assertIsNone(result.parse_error)

    def test_extracts_multiple_paths(self):
        text = (
            "Body.\n\n"
            "```attachments\n"
            '["/a.pdf", "/b.pdf"]\n'
            "```"
        )
        result = extract_attachments(text)
        self.assertEqual(result.paths, ["/a.pdf", "/b.pdf"])

    def test_strips_block_from_response(self):
        text = "Body.\n\n```attachments\n[]\n```\n\nMore body."
        result = extract_attachments(text)
        self.assertNotIn("attachments", result.visible_response)
        self.assertIn("Body.", result.visible_response)
        self.assertIn("More body.", result.visible_response)

    def test_singular_attachment_label_also_works(self):
        text = "Body.\n\n```attachment\n[\"/foo\"]\n```"
        result = extract_attachments(text)
        self.assertEqual(result.paths, ["/foo"])

    def test_malformed_json_sets_parse_error(self):
        text = "Body.\n\n```attachments\n[unclosed string\n```"
        result = extract_attachments(text)
        self.assertIsNone(result.paths)
        self.assertIsNotNone(result.parse_error)
        self.assertEqual(result.visible_response, text)  # not stripped on error

    def test_non_array_rejects(self):
        text = "Body.\n\n```attachments\n{\"path\": \"/foo\"}\n```"
        result = extract_attachments(text)
        self.assertIsNone(result.paths)
        self.assertIn("array", result.parse_error)

    def test_non_string_paths_reject(self):
        text = "Body.\n\n```attachments\n[\"/foo\", 123]\n```"
        result = extract_attachments(text)
        self.assertIsNone(result.paths)
        self.assertIn("string", result.parse_error)


# ============================================================================
# validate_path
# ============================================================================


class TestValidatePath(unittest.TestCase):
    def setUp(self):
        # Sandbox under a temp dir; treat it as $HOME for the duration.
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_file(self, relpath: str, size: int = 100) -> Path:
        p = self.home / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p

    def test_valid_file_passes(self):
        f = self._make_file("Documents/foo.pdf", size=1024)
        result = validate_path(str(f), home=self.home)
        self.assertTrue(result.ok, result.error)

    def test_relative_path_rejected(self):
        result = validate_path("Documents/foo.pdf", home=self.home)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "path_must_be_absolute")

    def test_nonexistent_rejected(self):
        result = validate_path(str(self.home / "nonexistent.pdf"), home=self.home)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "file_not_found")

    def test_outside_home_rejected(self):
        # Use /etc/hosts which exists on macOS/Linux and is outside any tmp home
        if not Path("/etc/hosts").exists():
            self.skipTest("no /etc/hosts on this platform")
        result = validate_path("/etc/hosts", home=self.home)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "outside_home_directory")

    def test_directory_rejected(self):
        d = self.home / "Documents"
        d.mkdir()
        result = validate_path(str(d), home=self.home)
        self.assertFalse(result.ok)
        # Either resolves to dir (not_a_regular_file) or path_resolution succeeds; both are rejection paths
        self.assertIn(result.error, ("not_a_regular_file", "file_not_found"))

    def test_empty_file_rejected(self):
        f = self._make_file("Documents/empty.txt", size=0)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "empty_file")

    def test_oversize_rejected(self):
        f = self._make_file("Documents/big.bin", size=1024)
        result = validate_path(str(f), home=self.home, max_bytes=512)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("too_large:"))

    def test_ssh_dir_rejected(self):
        f = self._make_file(".ssh/id_rsa", size=100)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        # Either deny via dir prefix or filename pattern — both are correct rejections
        self.assertTrue(
            result.error.startswith("sensitive_dir:.ssh") or
            result.error.startswith("sensitive_filename:id_rsa"),
            f"unexpected error: {result.error}",
        )

    def test_aws_credentials_rejected(self):
        f = self._make_file(".aws/credentials", size=100)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("sensitive_dir:.aws"))

    def test_gnupg_rejected(self):
        f = self._make_file(".gnupg/secring.gpg", size=100)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("sensitive_dir:.gnupg"))

    def test_dot_env_rejected(self):
        f = self._make_file("Development/myapp/.env", size=100)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("sensitive_filename:.env"))

    def test_dot_env_local_rejected(self):
        f = self._make_file("Development/myapp/.env.local", size=100)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("sensitive_filename:"))

    def test_pem_rejected(self):
        f = self._make_file("Documents/cert.pem", size=100)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("sensitive_filename:cert.pem"))

    def test_key_rejected(self):
        f = self._make_file("Documents/private.key", size=100)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("sensitive_filename:private.key"))

    def test_git_anywhere_rejected(self):
        f = self._make_file("Development/myrepo/.git/config", size=100)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "sensitive_dir:.git")

    def test_zsh_history_rejected(self):
        f = self._make_file(".zsh_history", size=100)
        result = validate_path(str(f), home=self.home)
        self.assertFalse(result.ok)
        self.assertTrue(result.error.startswith("sensitive_filename:.zsh_history"))

    def test_symlink_escape_rejected(self):
        # Create a symlink under home pointing to /etc/hosts. After resolve(),
        # the target is /etc/hosts which is outside home.
        if not Path("/etc/hosts").exists():
            self.skipTest("no /etc/hosts on this platform")
        link = self.home / "Documents" / "sneaky.txt"
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("/etc/hosts", link)
        result = validate_path(str(link), home=self.home)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "outside_home_directory")

    def test_empty_string_rejected(self):
        result = validate_path("", home=self.home)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "empty_or_non_string_path")


# ============================================================================
# validate_attachment_batch
# ============================================================================


class TestValidateAttachmentBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        # Don't actually use Andy's HOME; we patch validate_path-based logic
        # implicitly by relying on the absolute path being under tempfile's
        # home (which validate_path doesn't enforce since it uses HOME constant).

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_input_returns_empty(self):
        result = validate_attachment_batch([])
        self.assertEqual(result.valid_paths, [])
        self.assertEqual(result.errors, [])
        self.assertFalse(result.over_cap)

    def test_over_cap_sets_flag_and_truncates(self):
        # Generate MAX+2 garbage paths (will all be rejected, but the cap
        # check happens before validation).
        paths = [f"/nonexistent/{i}.txt" for i in range(MAX_ATTACHMENTS_PER_MESSAGE + 2)]
        result = validate_attachment_batch(paths)
        self.assertTrue(result.over_cap)
        # Only MAX paths considered
        self.assertEqual(len(result.errors), MAX_ATTACHMENTS_PER_MESSAGE)

    def test_collects_errors_per_path(self):
        paths = ["/nonexistent.txt", "relative.txt"]
        result = validate_attachment_batch(paths)
        self.assertEqual(len(result.errors), 2)
        # First reason is file_not_found, second is path_must_be_absolute
        self.assertEqual(result.errors[0][1], "file_not_found")
        self.assertEqual(result.errors[1][1], "path_must_be_absolute")


if __name__ == "__main__":
    unittest.main(verbosity=2)
