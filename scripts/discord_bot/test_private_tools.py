"""Tests for private MCP tools: the grant is owner-only and fails closed."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot import agent_runtime, claude_invoke  # noqa: E402
from scripts.discord_bot.agent_runtime import TurnRequest, run_agent_turn  # noqa: E402
from scripts.discord_bot.private_tools import grant_for, merge_tools  # noqa: E402

TOOL = "mcp__private-server__lookup"


class TestGrantFor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.mcp = d / "private-mcp.json"
        self.mcp.write_text(json.dumps({"mcpServers": {"private-server": {"command": "true"}}}))
        self.grant_file = d / "private-tools.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        self.grant_file.write_text(data if isinstance(data, str) else json.dumps(data))

    def valid(self, **over):
        data = {"mcp_config": str(self.mcp), "agents": {"research": [TOOL]}}
        data.update(over)
        return data

    def test_valid_grant_for_owner(self):
        self.write(self.valid())
        g = grant_for("research", True, self.grant_file)
        self.assertEqual((g.mcp_config, g.tools), (str(self.mcp), TOOL))

    def test_not_owner_invoked_gets_nothing(self):
        self.write(self.valid())
        self.assertIsNone(grant_for("research", False, self.grant_file))

    def test_unlisted_agent_gets_nothing(self):
        self.write(self.valid())
        self.assertIsNone(grant_for("luna", True, self.grant_file))

    def test_missing_or_malformed_file_fails_closed(self):
        self.assertIsNone(grant_for("research", True, self.grant_file))   # no file
        self.write("{not json")
        self.assertIsNone(grant_for("research", True, self.grant_file))
        self.write([1, 2])
        self.assertIsNone(grant_for("research", True, self.grant_file))

    def test_missing_mcp_config_fails_closed(self):
        self.write(self.valid(mcp_config=str(self.mcp.parent / "absent.json")))
        self.assertIsNone(grant_for("research", True, self.grant_file))
        self.write(self.valid(mcp_config=""))
        self.assertIsNone(grant_for("research", True, self.grant_file))

    def test_non_mcp_tool_names_fail_closed(self):
        for bad in (["Bash"], ["mcp__x"], [TOOL, "Write"], [], "mcp__a__b"):
            self.write(self.valid(agents={"research": bad}))
            self.assertIsNone(grant_for("research", True, self.grant_file), bad)

    def test_merge_tools(self):
        self.assertEqual(merge_tools("Read,Glob", TOOL), f"Read,Glob,{TOOL}")
        self.assertEqual(merge_tools(None, TOOL), TOOL)


class TestClaudeArgs(unittest.TestCase):
    def test_mcp_config_reaches_the_command_line(self):
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args

            class R:
                returncode, stdout, stderr = 0, "ok", ""
            return R()

        with patch.object(claude_invoke.subprocess, "run", side_effect=fake_run):
            claude_invoke.call_claude_sync("hi", allowed_tools=f"Read,{TOOL}", mcp_config="/p/mcp.json")
        a = seen["args"]
        self.assertEqual(a[a.index("--mcp-config") + 1], "/p/mcp.json")
        self.assertIn(TOOL, a[a.index("--allowedTools") + 1])

    def test_no_mcp_config_by_default(self):
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args

            class R:
                returncode, stdout, stderr = 0, "ok", ""
            return R()

        with patch.object(claude_invoke.subprocess, "run", side_effect=fake_run):
            claude_invoke.call_claude_sync("hi", allowed_tools="Read")
        self.assertNotIn("--mcp-config", seen["args"])


class TestRuntimeGrant(unittest.TestCase):
    """run_agent_turn asks for the grant with the request's owner flag."""

    def _run(self, owner_invoked, grant):
        calls = []

        async def fake_call(prompt, **kwargs):
            calls.append(kwargs)
            return True, "ok", ""

        class Rec:
            session_id, turn_count = "11111111-2222-3333-4444-555555555555", 0

        class Prompt:
            def exists(self):
                return True

            def read_text(self, encoding="utf-8"):
                return "T"

        from contextlib import ExitStack
        with ExitStack() as s:
            s.enter_context(patch.object(agent_runtime, "call_claude", side_effect=fake_call))
            s.enter_context(patch.object(agent_runtime, "MENTION_PROMPT_PATH", Prompt()))
            s.enter_context(patch.object(agent_runtime, "load_agent_definition", return_value="C"))
            s.enter_context(patch.object(agent_runtime, "build_mention_prompt", return_value="P"))
            s.enter_context(patch.object(agent_runtime, "allowed_tools_for", return_value="Read"))
            s.enter_context(patch.object(agent_runtime, "add_dirs_for", return_value=[]))
            s.enter_context(patch.object(agent_runtime, "timeout_for", return_value=9))
            s.enter_context(patch.object(agent_runtime, "effort_for", return_value="low"))
            s.enter_context(patch.object(agent_runtime, "max_response_chars_for", return_value=50))
            s.enter_context(patch.object(agent_runtime, "truncate_response", side_effect=lambda t, limit: t))
            gf = s.enter_context(patch.object(agent_runtime, "grant_for", return_value=grant))
            store = s.enter_context(patch.object(agent_runtime, "SESSION_STORE"))
            store.get_or_create.return_value = (Rec(), True)
            asyncio.run(run_agent_turn(TurnRequest(agent_id="research", conversation_key=1,
                                                   message_content="q", owner_invoked=owner_invoked)))
        return calls, gf

    def test_owner_turn_gets_private_tools(self):
        from scripts.discord_bot.private_tools import PrivateGrant
        calls, gf = self._run(True, PrivateGrant("/p/mcp.json", TOOL))
        gf.assert_called_once_with("research", True)
        self.assertEqual(calls[0]["mcp_config"], "/p/mcp.json")
        self.assertEqual(calls[0]["allowed_tools"], f"Read,{TOOL}")

    def test_default_request_is_not_owner_invoked(self):
        self.assertFalse(TurnRequest(agent_id="research", conversation_key=1, message_content="q").owner_invoked)

    def test_no_grant_means_no_mcp_config(self):
        calls, gf = self._run(False, None)
        gf.assert_called_once_with("research", False)
        self.assertIsNone(calls[0]["mcp_config"])
        self.assertEqual(calls[0]["allowed_tools"], "Read")


if __name__ == "__main__":
    unittest.main()
