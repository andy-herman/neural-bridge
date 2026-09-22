"""Every launchd plist in scripts/launchd/ must parse.

Three plists (memory-canary, echo-transcripts, loop-engineer) shipped with a
double hyphen inside an XML comment, which XML forbids. plutil and launchd
reject such a file, so the memory canary, whose whole purpose is to detect
silent memory failures, could never load. install.sh never touched those
three, so nothing surfaced it. This test makes the class of error impossible
to commit again.
"""

from __future__ import annotations

import plistlib
import re
import unittest
from pathlib import Path

LAUNCHD_DIR = Path(__file__).resolve().parent / "launchd"
COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)


class TestLaunchdPlists(unittest.TestCase):
    def _plists(self) -> list[Path]:
        found = sorted(LAUNCHD_DIR.glob("*.plist"))
        self.assertTrue(found, f"no plists found under {LAUNCHD_DIR}")
        return found

    def test_every_plist_parses_and_has_label(self):
        for path in self._plists():
            with self.subTest(plist=path.name):
                with path.open("rb") as fh:
                    data = plistlib.load(fh)
                self.assertIn("Label", data, f"{path.name} has no Label key")
                self.assertEqual(
                    data["Label"], path.stem,
                    f"{path.name}: Label should match the filename stem",
                )
                self.assertIn("ProgramArguments", data, f"{path.name} has no ProgramArguments")

    def test_no_double_hyphen_inside_xml_comments(self):
        # plistlib already rejects these, but the explicit check names the cause.
        for path in self._plists():
            text = path.read_text(encoding="utf-8")
            for m in COMMENT_RE.finditer(text):
                with self.subTest(plist=path.name):
                    self.assertNotIn(
                        "--", m.group(1),
                        f"{path.name}: XML comments may not contain '--' "
                        f"(spell flags out in prose instead)",
                    )


if __name__ == "__main__":
    unittest.main()
