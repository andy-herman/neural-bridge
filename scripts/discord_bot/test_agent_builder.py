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
    update_agents_json,
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


class TestClientIdValidation(unittest.TestCase):
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

    def test_client_id_optional(self):
        ok, _ = validate_create_agent_payload(self._ok())  # no client_id
        self.assertTrue(ok)

    def test_valid_client_id_accepted(self):
        ok, err = validate_create_agent_payload(self._ok(client_id="1502882229599342642"))
        self.assertTrue(ok, err)

    def test_short_client_id_rejected(self):
        ok, err = validate_create_agent_payload(self._ok(client_id="123"))
        self.assertFalse(ok)
        self.assertIn("snowflake", err)

    def test_non_numeric_client_id_rejected(self):
        ok, err = validate_create_agent_payload(self._ok(client_id="abc12345678901234"))
        self.assertFalse(ok)

    def test_non_string_client_id_rejected(self):
        ok, err = validate_create_agent_payload(self._ok(client_id=1502882229599342642))
        self.assertFalse(ok)


class TestUpdateAgentsJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        import json as _json
        _json.dump({
            "authorized_user_ids": ["1288934410992750592"],
            "guild_id": "1502587535384379533",
            "default_repo": "andy-herman/neural-bridge",
            "agents": [
                {
                    "id": "research",
                    "client_id": "1502047591393919169",
                    "token_keychain_service": "neural-bridge-discord-bot-research",
                    "is_orchestrator": False,
                    "display_name": "Research",
                },
            ],
        }, self.tmp)
        self.tmp.close()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.path.unlink()

    def _read(self):
        import json as _json
        return _json.loads(self.path.read_text(encoding="utf-8"))

    def test_adds_new_entry(self):
        changed = update_agents_json(
            self.path,
            agent_id="luna",
            display_name="Luna",
            client_id="1502882229599342642",
        )
        self.assertTrue(changed)
        agents = self._read()["agents"]
        self.assertEqual(len(agents), 2)
        luna = next(a for a in agents if a["id"] == "luna")
        self.assertEqual(luna["client_id"], "1502882229599342642")
        self.assertEqual(luna["token_keychain_service"], "neural-bridge-discord-bot-luna")
        self.assertEqual(luna["display_name"], "Luna")
        self.assertFalse(luna["is_orchestrator"])

    def test_idempotent_when_id_present(self):
        changed = update_agents_json(
            self.path,
            agent_id="research",  # already there
            display_name="Research",
            client_id="9999999999999999999",
        )
        self.assertFalse(changed)
        agents = self._read()["agents"]
        # Still one research entry, original client_id preserved
        research_entries = [a for a in agents if a["id"] == "research"]
        self.assertEqual(len(research_entries), 1)
        self.assertEqual(research_entries[0]["client_id"], "1502047591393919169")

    def test_orchestrator_flag_passes_through(self):
        update_agents_json(
            self.path,
            agent_id="commander",
            display_name="Commander",
            client_id="1502882229599342643",
            is_orchestrator=True,
        )
        agents = self._read()["agents"]
        commander = next(a for a in agents if a["id"] == "commander")
        self.assertTrue(commander["is_orchestrator"])

    def test_malformed_agents_json_raises(self):
        # Overwrite with an agents.json that's missing the `agents` array.
        self.path.write_text('{"foo": "bar"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            update_agents_json(
                self.path, agent_id="luna", display_name="Luna",
                client_id="1502882229599342642",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
