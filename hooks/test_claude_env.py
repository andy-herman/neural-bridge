"""claude_env.direct_env must remove every Anthropic routing variable.

Each of these silently overrides the Claude Code login. On 2026-09-25 flush
was found failing on every agent turn because it inherited the copilot-api
proxy URL, and direct calls under ~/.hermes/.env failed on its credit-less
API key.
"""

from __future__ import annotations

import os
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_env  # noqa: E402

INHERITED = {
    "ANTHROPIC_BASE_URL": "http://localhost:4141",
    "ANTHROPIC_API_KEY": "sk-ant-no-credit",
    "ANTHROPIC_AUTH_TOKEN": "tok",
    "ANTHROPIC_TOKEN": "tok",
    "PATH": "/usr/bin",
    "NB_AGENT_ID": "research",
}


class TestDirectEnv(unittest.TestCase):
    def test_strips_every_routing_variable(self):
        env = claude_env.direct_env(INHERITED)
        for key in claude_env.ROUTING_VARS:
            self.assertNotIn(key, env)

    def test_keeps_everything_else(self):
        env = claude_env.direct_env(INHERITED)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["NB_AGENT_ID"], "research")

    def test_does_not_mutate_the_input(self):
        source = dict(INHERITED)
        claude_env.direct_env(source)
        self.assertEqual(source, INHERITED)


if __name__ == "__main__":
    unittest.main()


class TestProxyEnv(unittest.TestCase):
    def setUp(self):
        # Hermetic: the real gateway may be running on this machine.
        patcher = mock.patch.object(claude_env, "gateway_up", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pins_the_proxy_whatever_was_inherited(self):
        env = claude_env.proxy_env({**INHERITED, "ANTHROPIC_BASE_URL": "https://desktop.invalid"})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], claude_env.DEFAULT_PROXY_BASE)
        self.assertEqual(env["ANTHROPIC_API_KEY"], claude_env.PROXY_PLACEHOLDER_KEY)
        self.assertNotIn("ANTHROPIC_TOKEN", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env["NB_AGENT_ID"], "research")

    def test_nb_copilot_api_base_moves_the_proxy(self):
        env = claude_env.proxy_env({"NB_COPILOT_API_BASE": "http://127.0.0.1:9999"})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9999")


class TestGatewayRoute(unittest.TestCase):
    """Proxy-route calls go through the model gateway when it is up, and
    straight to copilot-api when it is not, so a dead gateway never takes the
    fleet down with it."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("NB_COPILOT_API_BASE", None)
        self.addCleanup(self._env.stop)

    def listening(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)  # each proxy_base() check is one connect nobody accepts
        self.addCleanup(sock.close)
        return f"http://127.0.0.1:{sock.getsockname()[1]}"

    def closed(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return f"http://127.0.0.1:{port}"

    def test_gateway_up_goes_through_it(self):
        gw = self.listening()
        self.assertEqual(claude_env.proxy_base({"NB_MODEL_GATEWAY_BASE": gw}), gw)
        self.assertEqual(claude_env.proxy_env({"NB_MODEL_GATEWAY_BASE": gw})["ANTHROPIC_BASE_URL"], gw)

    def test_gateway_down_falls_back_to_the_proxy(self):
        self.assertEqual(claude_env.proxy_base({"NB_MODEL_GATEWAY_BASE": self.closed()}),
                         claude_env.DEFAULT_PROXY_BASE)

    def test_an_explicit_base_wins_over_both(self):
        env = {"NB_COPILOT_API_BASE": "http://127.0.0.1:9", "NB_MODEL_GATEWAY_BASE": self.listening()}
        self.assertEqual(claude_env.proxy_base(env), "http://127.0.0.1:9")

    def test_claude_5_is_flagged_as_gateway_only(self):
        self.assertFalse(claude_env.proxy_supports("claude-sonnet-5"))
        self.assertTrue(claude_env.proxy_supports("claude-opus-4.8"))


class TestMcpArgs(unittest.TestCase):
    def test_no_config_loads_no_server(self):
        self.assertEqual(claude_env.mcp_args(), ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])

    def test_a_granted_config_is_the_only_one_loaded(self):
        self.assertEqual(claude_env.mcp_args("/tmp/private.json"),
                         ["--strict-mcp-config", "--mcp-config", "/tmp/private.json"])


class TestErrorSnippet(unittest.TestCase):
    CONNECTORS = ("claude.ai connectors are disabled because ANTHROPIC_API_KEY or another "
                  "auth source is set and takes precedence over your claude.ai login · Unset "
                  "it to load your organization's connectors")

    def test_api_error_on_stdout_wins(self):
        out = 'thinking...\nAPI Error: 400 {"error":{"message":"prefill"}}\n'
        self.assertEqual(claude_env.error_snippet(out, self.CONNECTORS),
                         'API Error: 400 {"error":{"message":"prefill"}}')

    RETIRED = "⚠ Claude Opus 4 was retired on June 15, 2026. Consider switching to a newer model."

    def test_luna_2026_09_30_names_the_403(self):
        # Replayed with Claude Code 2.1.207 against a proxy answering 403
        # "forbidden", as copilot-api did once its upstream token went stale.
        # The daemon showed the connectors line and "⚠ Claude Op" (the
        # retirement warning); the cause was on stdout, after "Failed to
        # authenticate.", where a startswith("API Error") check never looked.
        out = "Failed to authenticate. API Error: 403 forbidden\n"
        err = f"⚠ {self.CONNECTORS}\n{self.RETIRED}\n"
        self.assertEqual(claude_env.error_snippet(out, err),
                         "Failed to authenticate. API Error: 403 forbidden")

    def test_benign_warnings_are_dropped_so_a_real_warning_shows(self):
        err = f"⚠ {self.CONNECTORS}\n{self.RETIRED}\n⚠ Something that actually matters\n"
        snip = claude_env.error_snippet("", err)
        self.assertEqual(snip, "⚠ Something that actually matters")

    def test_only_benign_lines_fall_back_to_them(self):
        self.assertEqual(claude_env.error_snippet("", self.CONNECTORS), self.CONNECTORS)

    def test_nothing_useful_falls_back_to_stdout_tail_then_marker(self):
        self.assertEqual(claude_env.error_snippet("partial answer\nlast line", ""), "last line")
        self.assertEqual(claude_env.error_snippet("", ""), "(no output)")
        self.assertEqual(claude_env.error_snippet(None, None), "(no output)")

    def test_truncates_and_flattens(self):
        snip = claude_env.error_snippet("", "x " * 400, limit=50)
        self.assertEqual(len(snip), 50)
        self.assertTrue(snip.endswith("..."))
        self.assertNotIn("\n", claude_env.error_snippet("", "a\nb\nc"))


class TestProxySupports(unittest.TestCase):
    def test_claude_5_ids_are_rejected_on_the_proxy(self):
        for model in ("claude-sonnet-5", "claude-opus-5", "claude-opus-5.5", "claude-haiku-5"):
            self.assertFalse(claude_env.proxy_supports(model), model)

    def test_the_pipeline_model_is_supported(self):
        self.assertTrue(claude_env.proxy_supports(claude_env.PIPELINE_MODEL))
