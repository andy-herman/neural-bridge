"""Unit tests for agent_builder.py — schema validation and pure helpers.

The full execute_create_agent flow involves git/gh subprocess calls and
file writes against the live repo; we don't test that path here. We
test the validation, file-rendering, and KNOWN_AGENTS-update helpers
which are pure-ish (some touch tempfiles).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.agent_builder import (  # noqa: E402
    bump_minor_version,
    render_plugin_file,
    update_known_agents,
    validate_create_agent_payload,
)


class TestValidate(unittest.TestCase):
    def _ok(self, **overrides):
        base = {
            "agent_id": "data-analyst",
            "display_name": "Data Analyst",
            "description": "Processes CSV/Excel inputs into summary stats and short narrative.",
            "color": "yellow",
            "tools": ["Read", "Glob", "Grep", "Write"],
            "model": "claude-sonnet-4-6",
            "body": "x" * 200,
        }
        base.update(overrides)
        return base

    def test_valid(self):
        ok, err = validate_create_agent_payload(self._ok())
        self.assertTrue(ok, err)

    def test_missing_key(self):
        bad = self._ok()
        del bad["model"]
        ok, err = validate_create_agent_payload(bad)
        self.assertFalse(ok)
        self.assertIn("model", err)

    def test_bad_agent_id(self):
        for bad_id in ["DataAnalyst", "data_analyst", "data analyst", "1agent", "-leading", "trailing-"]:
            ok, err = validate_create_agent_payload(self._ok(agent_id=bad_id))
            self.assertFalse(ok, f"should reject {bad_id!r}")

    def test_unknown_color(self):
        ok, err = validate_create_agent_payload(self._ok(color="taupe"))
        self.assertFalse(ok)

    def test_unknown_tool(self):
        ok, err = validate_create_agent_payload(self._ok(tools=["Read", "InvalidTool"]))
        self.assertFalse(ok)
        self.assertIn("InvalidTool", err)

    def test_short_body_rejected(self):
        ok, err = validate_create_agent_payload(self._ok(body="too short"))
        self.assertFalse(ok)


class TestRenderPluginFile(unittest.TestCase):
    def test_renders_frontmatter_and_body(self):
        action = {
            "agent_id": "data-analyst",
            "display_name": "Data Analyst",
            "description": "Processes CSV.",
            "color": "yellow",
            "tools": ["Read", "Write"],
            "model": "claude-sonnet-4-6",
            "body": "You are the Data Analyst agent.\n\nDo data things.",
        }
        out = render_plugin_file(action)
        self.assertTrue(out.startswith("---\n"))
        self.assertIn("description: Processes CSV.", out)
        self.assertIn("tools: [Read, Write]", out)
        self.assertIn("color: yellow", out)
        self.assertIn("model: claude-sonnet-4-6", out)
        self.assertIn("You are the Data Analyst agent.", out)


class TestUpdateKnownAgents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
        self.tmp.write(
            'KNOWN_AGENTS = {\n'
            '    "research", "teaching-prep", "content", "senior-pm",\n'
            '    "social", "recruiter",\n'
            '}\n'
        )
        self.tmp.close()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.path.unlink()

    def test_adds_new_agent(self):
        changed = update_known_agents(self.path, "data-analyst")
        self.assertTrue(changed)
        contents = self.path.read_text(encoding="utf-8")
        self.assertIn('"data-analyst"', contents)

    def test_idempotent_when_already_present(self):
        changed = update_known_agents(self.path, "research")
        self.assertFalse(changed)
        contents = self.path.read_text(encoding="utf-8")
        # Still has just one occurrence
        self.assertEqual(contents.count('"research"'), 1)


class TestBumpMinorVersion(unittest.TestCase):
    def test_bumps_minor(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write('{"version": "0.5.0", "name": "test"}')
            path = Path(f.name)
        try:
            new_version = bump_minor_version(path, ["version"])
            self.assertEqual(new_version, "0.6.0")
            import json as _json
            self.assertEqual(_json.loads(path.read_text())["version"], "0.6.0")
        finally:
            path.unlink()

    def test_nested_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write('{"plugins": [{"version": "0.5.0"}]}')
            path = Path(f.name)
        try:
            new_version = bump_minor_version(path, ["plugins", 0, "version"])
            self.assertEqual(new_version, "0.6.0")
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
