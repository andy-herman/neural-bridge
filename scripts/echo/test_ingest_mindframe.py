"""Unit tests for scripts/echo/ingest_mindframe.py.

Covers the gap senior-pm flagged in #144: cross-machine source path may
not exist on every Mac, author filter is the most likely correctness
pitfall, redacted content must not get double-processed.

Run: `python3 scripts/echo/test_ingest_mindframe.py`
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.echo import ingest_mindframe as im  # noqa: E402


SAMPLE_LOG_GENERAL = """## 2026-05-13T17:33:13.123Z - Andy Herman

let's ship the response cap fix tonight

## 2026-05-13T17:35:01.456Z - Luna Assistant

Acknowledged. Filing the proposal now.

## 2026-05-13T17:36:42.789Z - Andy Herman

yes please commit

## 2026-05-13T17:40:00.000Z - Some Other Person

Quick aside

## 2026-05-13T17:42:15.111Z - andy-herman

What's the ETA on the merge?
"""

# A second file simulating a different channel + edge cases (multi-line content, redacted markers).
SAMPLE_LOG_DM = """## 2026-05-12T22:01:00.000Z - Andy Herman

Multi-line message here.
Spans several lines.

Includes blank lines.

## 2026-05-12T22:05:30.000Z - Andy Herman

[REDACTED bearer token]: lorem ipsum
"""


class TempVaultFixture(unittest.TestCase):
    """Base class that patches module-level paths to a tmpdir for isolation."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.vault_dir = self.tmpdir / "vault"
        self.vault_dir.mkdir()
        self.logs_dir = self.tmpdir / "logs"

        self.patches = [
            mock.patch.object(im, "VAULT_PROFILE_DIR", self.vault_dir),
            mock.patch.object(im, "OUTPUT_PATH", self.vault_dir / "mindframe-conversations.md"),
            mock.patch.object(im, "DEDUPE_SIDECAR", self.vault_dir / ".mindframe-ingested.txt"),
            mock.patch.object(im, "ALLOWLIST_PATH", self.vault_dir / ".mindframe-author-allowlist.txt"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        # Clean tmpdir
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write_log(self, day: str, channel_slug: str, channel_id: str, content: str) -> Path:
        day_dir = self.logs_dir / day
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{channel_slug}-{channel_id}.md"
        path.write_text(content, encoding="utf-8")
        return path

    def run_main(self, args=None, env=None):
        """Helper: invoke main() with NB_MINDFRAME_LOGS_DIR pointed at the test logs dir."""
        env_to_set = {im.ENV_VAR_OVERRIDE: str(self.logs_dir)}
        if env:
            env_to_set.update(env)
        with mock.patch.dict(os.environ, env_to_set, clear=False):
            return im.main(args or [])


# ---------- Source resolution ----------


class TestSourceResolution(TempVaultFixture):
    def test_env_var_overrides_default(self):
        with mock.patch.dict(os.environ, {im.ENV_VAR_OVERRIDE: "/some/custom/path"}, clear=False):
            resolved = im.resolve_logs_dir()
        self.assertEqual(str(resolved), "/some/custom/path")

    def test_default_when_env_var_unset(self):
        env = {k: v for k, v in os.environ.items() if k != im.ENV_VAR_OVERRIDE}
        with mock.patch.dict(os.environ, env, clear=True):
            resolved = im.resolve_logs_dir()
        self.assertEqual(resolved, im.DEFAULT_LOGS_DIR)


# ---------- No-op on missing source (the linchpin) ----------


class TestMissingSourceCleanExit(TempVaultFixture):
    def test_main_exits_zero_when_logs_dir_missing(self):
        # logs_dir is set but the dir doesn't exist (we never created it).
        rc = self.run_main([])
        self.assertEqual(rc, 0)
        # Nothing should have been written.
        self.assertFalse((self.vault_dir / "mindframe-conversations.md").exists())
        self.assertFalse((self.vault_dir / ".mindframe-ingested.txt").exists())


# ---------- Author allowlist filter ----------


class TestAuthorAllowlist(TempVaultFixture):
    def test_allowlist_seeded_on_first_run(self):
        im.load_allowlist()
        self.assertTrue((self.vault_dir / ".mindframe-author-allowlist.txt").exists())

    def test_allowlist_match_is_case_insensitive(self):
        (self.vault_dir / ".mindframe-author-allowlist.txt").write_text("Andy Herman\nandy-herman\n", encoding="utf-8")
        allowlist = im.load_allowlist()
        self.assertTrue(im.author_in_allowlist("Andy Herman", allowlist))
        self.assertTrue(im.author_in_allowlist("ANDY HERMAN", allowlist))
        self.assertTrue(im.author_in_allowlist("andy herman", allowlist))
        self.assertTrue(im.author_in_allowlist("andy-herman", allowlist))
        self.assertFalse(im.author_in_allowlist("Luna Assistant", allowlist))
        self.assertFalse(im.author_in_allowlist("Some Other Person", allowlist))


# ---------- Channel label extraction ----------


class TestChannelLabel(unittest.TestCase):
    def test_strips_trailing_channel_id(self):
        self.assertEqual(im.extract_channel_label("general-1503151234567890.md"), "general")
        self.assertEqual(im.extract_channel_label("squad-discuss-1234567890123456.md"), "squad-discuss")

    def test_falls_back_to_stem_when_no_snowflake_suffix(self):
        self.assertEqual(im.extract_channel_label("dm.md"), "dm")
        self.assertEqual(im.extract_channel_label("weird-name.md"), "weird-name")


# ---------- File parsing ----------


class TestExtractTurns(TempVaultFixture):
    def setUp(self):
        super().setUp()
        (self.vault_dir / ".mindframe-author-allowlist.txt").write_text("Andy Herman\nandy-herman\n", encoding="utf-8")

    def test_kept_vs_skipped_split(self):
        path = self.write_log("2026-05-13", "general", "1503151234567890", SAMPLE_LOG_GENERAL)
        allowlist = im.load_allowlist()
        kept, skipped = im.extract_turns_from_file(path, allowlist, seen=set())
        kept_authors = sorted(t.author for t in kept)
        skipped_authors = sorted(t.author for t in skipped)
        self.assertEqual(kept_authors, ["Andy Herman", "Andy Herman", "andy-herman"])
        self.assertEqual(skipped_authors, ["Luna Assistant", "Some Other Person"])

    def test_dedupe_blocks_seen_keys(self):
        path = self.write_log("2026-05-13", "general", "1503151234567890", SAMPLE_LOG_GENERAL)
        allowlist = im.load_allowlist()
        kept_first, _ = im.extract_turns_from_file(path, allowlist, seen=set())
        # Simulate sidecar containing all keys we just extracted.
        seen = {t.dedupe_key() for t in kept_first}
        kept_second, _ = im.extract_turns_from_file(path, allowlist, seen=seen)
        self.assertEqual(kept_second, [])

    def test_multiline_content_preserved(self):
        path = self.write_log("2026-05-12", "dm", "9999999999999999", SAMPLE_LOG_DM)
        allowlist = im.load_allowlist()
        kept, _ = im.extract_turns_from_file(path, allowlist, seen=set())
        self.assertEqual(len(kept), 2)
        # First turn has multi-line content with blank lines preserved
        self.assertIn("Multi-line message here.", kept[0].content)
        self.assertIn("Spans several lines.", kept[0].content)
        self.assertIn("Includes blank lines.", kept[0].content)

    def test_redacted_content_not_double_processed(self):
        """If MindFrame wrote `[REDACTED bearer token]`, we preserve it
        verbatim. No second-pass redaction here."""
        path = self.write_log("2026-05-12", "dm", "9999999999999999", SAMPLE_LOG_DM)
        allowlist = im.load_allowlist()
        kept, _ = im.extract_turns_from_file(path, allowlist, seen=set())
        # Second turn contains the redacted marker
        self.assertIn("[REDACTED bearer token]", kept[1].content)


# ---------- Output formatting + append ----------


class TestOutput(TempVaultFixture):
    def test_format_turn_block_matches_raw_conversations_shape(self):
        """The output format should match raw-conversations.md so the future
        synthesis pass can read both files with the same parser."""
        turn = im.Turn(
            timestamp="2026-05-13T17:33:13.123Z",
            author="Andy Herman",
            channel_label="general",
            content="ship the cap fix",
            source_path=Path("general-1503151234567890.md"),
            source_line=1,
        )
        block = im.format_turn_block(turn)
        self.assertIn("### 2026-05-13T17:33:13Z — #general", block)  # fractional seconds dropped
        self.assertIn("<!-- mindframe source: general-1503151234567890.md:1 -->", block)
        self.assertIn("> ship the cap fix", block)

    def test_first_time_output_creates_with_preamble(self):
        turn = im.Turn(
            timestamp="2026-05-13T17:33:13Z",
            author="Andy Herman",
            channel_label="general",
            content="hello",
            source_path=Path("general-x.md"),
            source_line=1,
        )
        im.append_turns([turn])
        body = im.OUTPUT_PATH.read_text(encoding="utf-8")
        self.assertIn("# MindFrame conversations", body)
        self.assertIn("> hello", body)


# ---------- End-to-end main() ----------


class TestEndToEnd(TempVaultFixture):
    def setUp(self):
        super().setUp()
        # Seed allowlist
        (self.vault_dir / ".mindframe-author-allowlist.txt").write_text("Andy Herman\nandy-herman\n", encoding="utf-8")

    def test_full_run_writes_output_and_advances_sidecar(self):
        self.write_log("2026-05-13", "general", "1503151234567890", SAMPLE_LOG_GENERAL)
        self.write_log("2026-05-12", "dm", "9999999999999999", SAMPLE_LOG_DM)

        rc = self.run_main([])
        self.assertEqual(rc, 0)

        output = im.OUTPUT_PATH.read_text(encoding="utf-8")
        self.assertIn("> let's ship the response cap fix tonight", output)
        self.assertIn("> yes please commit", output)
        self.assertIn("> Multi-line message here.", output)
        # Non-Andy authors should NOT appear in the output body
        self.assertNotIn("Acknowledged. Filing the proposal", output)
        self.assertNotIn("Quick aside", output)

        sidecar = im.DEDUPE_SIDECAR.read_text(encoding="utf-8")
        # Should have 5 dedupe keys (3 Andy turns in general + 2 in dm)
        self.assertEqual(len([l for l in sidecar.splitlines() if l.strip()]), 5)

    def test_second_run_is_noop_after_first(self):
        self.write_log("2026-05-13", "general", "1503151234567890", SAMPLE_LOG_GENERAL)

        rc1 = self.run_main([])
        self.assertEqual(rc1, 0)
        output_after_first = im.OUTPUT_PATH.read_text(encoding="utf-8")

        rc2 = self.run_main([])
        self.assertEqual(rc2, 0)
        output_after_second = im.OUTPUT_PATH.read_text(encoding="utf-8")

        # File contents should be unchanged on the second run
        self.assertEqual(output_after_first, output_after_second)

    def test_dry_run_does_not_write(self):
        self.write_log("2026-05-13", "general", "1503151234567890", SAMPLE_LOG_GENERAL)

        rc = self.run_main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(im.OUTPUT_PATH.exists())
        self.assertFalse(im.DEDUPE_SIDECAR.exists())


if __name__ == "__main__":
    unittest.main()
