"""Unit tests for triage.apply_auto_fixes() and the auto_fixes schema."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.triage import apply_auto_fixes, validate_triage_output  # noqa: E402


class TestApplyAutoFixes(unittest.TestCase):
    def test_empty_fixes_returns_body_unchanged(self):
        new_body, applied = apply_auto_fixes("original body", [])
        self.assertEqual(new_body, "original body")
        self.assertEqual(applied, [])

    def test_appends_section(self):
        body = "Hello world."
        fixes = [{
            "description": "Add source paths",
            "section_header": "Source paths",
            "content": "- vault: x\n- blog: y",
        }]
        new_body, applied = apply_auto_fixes(body, fixes)
        self.assertIn("Hello world.", new_body)
        self.assertIn("## Source paths", new_body)
        self.assertIn("- vault: x", new_body)
        self.assertEqual(applied, ["Add source paths"])

    def test_idempotent_when_section_already_present(self):
        body = "Hello.\n\n## Source paths\n\n- vault: existing\n"
        fixes = [{
            "description": "Add source paths",
            "section_header": "Source paths",
            "content": "- vault: new",
        }]
        new_body, applied = apply_auto_fixes(body, fixes)
        # Body unchanged; nothing applied
        self.assertEqual(new_body, body)
        self.assertEqual(applied, [])

    def test_applies_multiple(self):
        body = "Hello."
        fixes = [
            {"description": "Add A", "section_header": "Alpha", "content": "alpha body"},
            {"description": "Add B", "section_header": "Beta", "content": "beta body"},
        ]
        new_body, applied = apply_auto_fixes(body, fixes)
        self.assertIn("## Alpha", new_body)
        self.assertIn("## Beta", new_body)
        self.assertEqual(applied, ["Add A", "Add B"])

    def test_empty_body_handled(self):
        new_body, applied = apply_auto_fixes("", [
            {"description": "Add X", "section_header": "Xyz", "content": "x"},
        ])
        self.assertIn("## Xyz", new_body)
        self.assertEqual(applied, ["Add X"])

    def test_none_body_handled_as_empty(self):
        new_body, applied = apply_auto_fixes(None, [  # type: ignore[arg-type]
            {"description": "Add X", "section_header": "Xyz", "content": "x"},
        ])
        self.assertIn("## Xyz", new_body)


class TestValidateAutoFixes(unittest.TestCase):
    def _payload(self, **overrides):
        base = {
            "recommended_specialist": "research",
            "priority": "P1",
            "recommended_state": "agent-ready",
            "labels_to_add": [],
            "labels_to_remove": [],
            "reason": "ok",
            "quality_flags": [],
            "auto_fixes": [],
        }
        base.update(overrides)
        return base

    def test_empty_auto_fixes_valid(self):
        ok, err = validate_triage_output(self._payload())
        self.assertTrue(ok, err)

    def test_auto_fixes_omitted_is_valid(self):
        # Backward compat: pre-existing payloads without auto_fixes should still pass.
        payload = self._payload()
        del payload["auto_fixes"]
        ok, err = validate_triage_output(payload)
        self.assertTrue(ok, err)

    def test_well_formed_fixes_valid(self):
        payload = self._payload(auto_fixes=[
            {"description": "Add x", "section_header": "X", "content": "body"},
            {"description": "Add y", "section_header": "Y", "content": "body"},
        ])
        ok, err = validate_triage_output(payload)
        self.assertTrue(ok, err)

    def test_missing_key_in_fix(self):
        payload = self._payload(auto_fixes=[{"description": "x", "content": "y"}])  # missing section_header
        ok, err = validate_triage_output(payload)
        self.assertFalse(ok)
        self.assertIn("section_header", err)

    def test_empty_string_in_fix_field(self):
        payload = self._payload(auto_fixes=[
            {"description": "ok", "section_header": "ok", "content": "  "},
        ])
        ok, err = validate_triage_output(payload)
        self.assertFalse(ok)
        self.assertIn("non-empty", err)

    def test_auto_fixes_must_be_list(self):
        payload = self._payload(auto_fixes="not a list")
        ok, err = validate_triage_output(payload)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
