"""Tests for scripts/fleet_code.py, the loader for FLEET_CODE.md."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.fleet_code import TITLE, FleetCodeError, load_fleet_code  # noqa: E402


class TestLoadFleetCode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "FLEET_CODE.md"

    def _write(self, text: str, newline: str = "\n") -> None:
        self.path.write_bytes(text.replace("\n", newline).encode("utf-8"))

    def test_real_file_loads(self):
        body = load_fleet_code()
        self.assertTrue(body.startswith(TITLE))
        self.assertIn("## 2. Hard limits", body)

    def test_human_header_is_dropped(self):
        self._write("# Header for people\n\nStatus line.\n\n---\n\n# Fleet code\n\nRule one.\n")
        self.assertEqual(load_fleet_code(self.path), "# Fleet code\n\nRule one.")

    def test_crlf_file_loads(self):
        self._write("# Header\n\n---\n\n# Fleet code\n\nRule one.\n", newline="\r\n")
        self.assertEqual(load_fleet_code(self.path), "# Fleet code\n\nRule one.")

    def test_only_the_first_separator_splits(self):
        self._write("# Header\n\n---\n\n# Fleet code\n\nA\n\n---\n\nB\n")
        body = load_fleet_code(self.path)
        self.assertIn("A", body)
        self.assertIn("B", body)

    def test_missing_file_raises(self):
        with self.assertRaises(FleetCodeError):
            load_fleet_code(self.path)

    def test_no_separator_raises(self):
        self._write("# Fleet code\n\nRule one.\n")
        with self.assertRaises(FleetCodeError):
            load_fleet_code(self.path)

    def test_wrong_heading_raises(self):
        self._write("# Header\n\n---\n\n# Something else\n")
        with self.assertRaises(FleetCodeError):
            load_fleet_code(self.path)


if __name__ == "__main__":
    unittest.main()
