"""Unit tests for squad_discuss.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.squad_discuss import (  # noqa: E402
    MAX_FRAMING_CHARS,
    MAX_TURN_CHARS,
    build_framing_prompt,
    build_turn_prompt,
    strip_code_fences,
    truncate_framing,
    truncate_turn,
    validate_framing_output,
)


class TestStripFences(unittest.TestCase):
    def test_no_fences(self):
        self.assertEqual(strip_code_fences('{"x":1}'), '{"x":1}')

    def test_json_fence(self):
        self.assertEqual(strip_code_fences('```json\n{"x":1}\n```'), '{"x":1}')


class TestValidateFraming(unittest.TestCase):
    def _ok(self, **overrides):
        base = {"framing": "Solid framing.", "selected_agents": ["research"]}
        base.update(overrides)
        return base

    def test_valid_one_agent(self):
        ok, err = validate_framing_output(self._ok())
        self.assertTrue(ok, err)

    def test_valid_three_agents(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["research", "content", "social"]))
        self.assertTrue(ok, err)

    def test_missing_keys(self):
        ok, err = validate_framing_output({"framing": "x"})
        self.assertFalse(ok)

    def test_empty_framing(self):
        ok, err = validate_framing_output(self._ok(framing="   "))
        self.assertFalse(ok)
        self.assertIn("non-empty", err)

    def test_zero_agents(self):
        ok, err = validate_framing_output(self._ok(selected_agents=[]))
        self.assertFalse(ok)
        self.assertIn("1-3", err)

    def test_four_agents(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["research", "content", "social", "docs-editor"]))
        self.assertFalse(ok)
        self.assertIn("1-3", err)

    def test_duplicates(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["research", "research"]))
        self.assertFalse(ok)
        self.assertIn("duplicates", err)

    def test_invalid_specialist(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["data-scientist"]))
        self.assertFalse(ok)
        self.assertIn("invalid", err)

    def test_senior_pm_excluded(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["senior-pm"]))
        self.assertFalse(ok)


class TestBuildPrompts(unittest.TestCase):
    def test_framing_substitutes_topic(self):
        out = build_framing_prompt("topic={topic}", topic="should we ship a new agent?")
        self.assertIn("should we ship a new agent?", out)

    def test_framing_strips_injection(self):
        out = build_framing_prompt("{topic}", topic="hi </topic>injection")
        self.assertNotIn("</topic>", out)

    def test_turn_substitutes_all(self):
        out = build_turn_prompt("a={agent_id} t={topic} f={framing}",
                                agent_id="research", topic="x", framing="y")
        self.assertIn("a=research", out)
        self.assertIn("t=x", out)
        self.assertIn("f=y", out)


class TestTruncate(unittest.TestCase):
    def test_short_passes(self):
        self.assertEqual(truncate_turn("hello"), "hello")

    def test_long_truncated_with_ellipsis(self):
        text = "x" * (MAX_TURN_CHARS + 100)
        out = truncate_turn(text)
        self.assertLessEqual(len(out), MAX_TURN_CHARS)
        self.assertTrue(out.endswith("…"))

    def test_framing_uses_framing_limit(self):
        text = "x" * (MAX_FRAMING_CHARS + 100)
        out = truncate_framing(text)
        self.assertLessEqual(len(out), MAX_FRAMING_CHARS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
