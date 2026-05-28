"""Unit tests for honcho_client.py.

The module is a thin wrapper around the honcho-ai SDK with aggressive
no-op fallbacks (missing SDK, disabled env var, client init failure,
runtime exceptions). The hot paths run on every Discord mention so the
no-op behavior is load-bearing for daemon stability.

These tests run on system Python without honcho-ai installed (the module
already handles that case). Where we need to exercise the live-SDK path,
we mock honcho.Honcho via sys.modules injection.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot import honcho_client  # noqa: E402


def _reset_module_state() -> None:
    """Tests run in random order. The module caches a client and warning
    flags at module level. Reset between tests so cross-test pollution
    doesn't leak."""
    honcho_client._client_cache = None
    honcho_client._warned_unavailable = False
    honcho_client._warned_disabled = False


class TestEnabledGate(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def test_enabled_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HONCHO_ENABLED", None)
            self.assertTrue(honcho_client._enabled())

    def test_enabled_when_true(self):
        with mock.patch.dict(os.environ, {"HONCHO_ENABLED": "true"}, clear=False):
            self.assertTrue(honcho_client._enabled())

    def test_disabled_when_false(self):
        with mock.patch.dict(os.environ, {"HONCHO_ENABLED": "false"}, clear=False):
            self.assertFalse(honcho_client._enabled())

    def test_disabled_when_false_uppercase(self):
        with mock.patch.dict(os.environ, {"HONCHO_ENABLED": "FALSE"}, clear=False):
            self.assertFalse(honcho_client._enabled())

    def test_warns_once_when_disabled(self):
        with mock.patch.dict(os.environ, {"HONCHO_ENABLED": "false"}, clear=False):
            with self.assertLogs("nb_discord.honcho", level="INFO") as cm:
                honcho_client._enabled()
                honcho_client._enabled()  # second call should not log again
            self.assertEqual(len(cm.records), 1)


class TestGetClientWithoutSDK(unittest.TestCase):
    """Exercise the path where the honcho-ai SDK is NOT importable."""

    def setUp(self):
        _reset_module_state()

    def test_no_op_when_sdk_missing(self):
        # Force the import inside _get_client to fail by stashing a None entry.
        with mock.patch.dict(sys.modules, {"honcho": None}):
            client = honcho_client._get_client()
        self.assertIsNone(client)

    def test_warns_once_when_sdk_missing(self):
        with mock.patch.dict(sys.modules, {"honcho": None}):
            with self.assertLogs("nb_discord.honcho", level="WARNING") as cm:
                honcho_client._get_client()
                honcho_client._get_client()  # second call should not re-warn
        self.assertEqual(len(cm.records), 1)
        self.assertIn("honcho-ai SDK not installed", cm.records[0].getMessage())


class TestGetClientInitFailure(unittest.TestCase):
    """Exercise the path where SDK is importable but Honcho() raises."""

    def setUp(self):
        _reset_module_state()

    def test_returns_none_on_init_exception(self):
        fake_honcho_module = mock.MagicMock()
        fake_honcho_module.Honcho.side_effect = RuntimeError("connection refused")
        with mock.patch.dict(sys.modules, {"honcho": fake_honcho_module}):
            client = honcho_client._get_client()
        self.assertIsNone(client)

    def test_warns_once_on_init_exception(self):
        fake_honcho_module = mock.MagicMock()
        fake_honcho_module.Honcho.side_effect = RuntimeError("connection refused")
        with mock.patch.dict(sys.modules, {"honcho": fake_honcho_module}):
            with self.assertLogs("nb_discord.honcho", level="WARNING") as cm:
                honcho_client._get_client()
                honcho_client._get_client()  # second call should not re-warn
        self.assertEqual(len(cm.records), 1)


class TestGetClientHappyPath(unittest.TestCase):
    """Exercise the path where Honcho() returns a client."""

    def setUp(self):
        _reset_module_state()

    def test_caches_client(self):
        fake_client = mock.MagicMock()
        fake_module = mock.MagicMock()
        fake_module.Honcho.return_value = fake_client
        with mock.patch.dict(sys.modules, {"honcho": fake_module}):
            c1 = honcho_client._get_client()
            c2 = honcho_client._get_client()
        self.assertIs(c1, fake_client)
        self.assertIs(c2, fake_client)
        # Honcho() should only be called once because of caching.
        self.assertEqual(fake_module.Honcho.call_count, 1)

    def test_uses_env_overrides(self):
        env = {
            "HONCHO_BASE_URL": "http://custom:9000",
            "HONCHO_WORKSPACE": "test-workspace",
            "HONCHO_API_KEY": "secret-key",
            "HONCHO_TIMEOUT": "60",
        }
        fake_module = mock.MagicMock()
        fake_module.Honcho.return_value = mock.MagicMock()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.dict(sys.modules, {"honcho": fake_module}):
                honcho_client._get_client()
        kwargs = fake_module.Honcho.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "http://custom:9000")
        self.assertEqual(kwargs["workspace_id"], "test-workspace")
        self.assertEqual(kwargs["api_key"], "secret-key")
        self.assertEqual(kwargs["timeout"], 60.0)

    def test_defaults_when_env_unset(self):
        # Clear the Honcho env vars; expect baked-in defaults.
        env_to_clear = ["HONCHO_BASE_URL", "HONCHO_WORKSPACE", "HONCHO_API_KEY", "HONCHO_TIMEOUT"]
        clean_env = {k: v for k, v in os.environ.items() if k not in env_to_clear}
        fake_module = mock.MagicMock()
        fake_module.Honcho.return_value = mock.MagicMock()
        with mock.patch.dict(os.environ, clean_env, clear=True):
            with mock.patch.dict(sys.modules, {"honcho": fake_module}):
                honcho_client._get_client()
        kwargs = fake_module.Honcho.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "http://localhost:8001")
        self.assertEqual(kwargs["workspace_id"], "hermes")
        self.assertEqual(kwargs["api_key"], "self-hosted")


class TestUserPeerId(unittest.TestCase):
    def test_default(self):
        env_clean = {k: v for k, v in os.environ.items() if k != "HONCHO_USER_PEER"}
        with mock.patch.dict(os.environ, env_clean, clear=True):
            self.assertEqual(honcho_client._user_peer_id(), "andyherman")

    def test_override(self):
        with mock.patch.dict(os.environ, {"HONCHO_USER_PEER": "someone-else"}, clear=False):
            self.assertEqual(honcho_client._user_peer_id(), "someone-else")


class TestGetPeerCardContext(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def test_returns_empty_when_disabled(self):
        with mock.patch.dict(os.environ, {"HONCHO_ENABLED": "false"}, clear=False):
            self.assertEqual(honcho_client.get_peer_card_context("luna"), "")

    def test_returns_empty_when_sdk_missing(self):
        with mock.patch.dict(sys.modules, {"honcho": None}):
            self.assertEqual(honcho_client.get_peer_card_context("luna"), "")

    def test_returns_empty_when_card_is_none(self):
        fake_client = mock.MagicMock()
        agent_peer = mock.MagicMock()
        agent_peer.get_card.return_value = None
        user_peer = mock.MagicMock()
        user_peer.get_card.return_value = None
        # client.peer(agent_id) returns agent_peer; client.peer(user_peer_id) returns user_peer
        fake_client.peer.side_effect = lambda pid: agent_peer if pid == "luna" else user_peer
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            self.assertEqual(honcho_client.get_peer_card_context("luna"), "")

    def test_falls_back_to_user_peer_card_when_agent_card_empty(self):
        fake_client = mock.MagicMock()
        agent_peer = mock.MagicMock()
        agent_peer.get_card.return_value = None  # agent doesn't know Andy yet
        user_peer = mock.MagicMock()
        user_peer.get_card.return_value = ["Andy loves Seoul E-Land", "Builds in public"]
        fake_client.peer.side_effect = lambda pid: agent_peer if pid == "luna" else user_peer
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            out = honcho_client.get_peer_card_context("luna")
        self.assertIn("Andy loves Seoul E-Land", out)
        self.assertIn("Builds in public", out)
        self.assertIn("Honcho peer card", out)

    def test_formats_list_card_as_bullets(self):
        fake_client = mock.MagicMock()
        agent_peer = mock.MagicMock()
        agent_peer.get_card.return_value = ["fact A", "fact B"]
        fake_client.peer.return_value = agent_peer
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            out = honcho_client.get_peer_card_context("luna")
        self.assertIn("- fact A", out)
        self.assertIn("- fact B", out)

    def test_formats_string_card_as_body(self):
        fake_client = mock.MagicMock()
        agent_peer = mock.MagicMock()
        agent_peer.get_card.return_value = "Andy is an iSchool faculty"
        fake_client.peer.return_value = agent_peer
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            out = honcho_client.get_peer_card_context("luna")
        self.assertIn("Andy is an iSchool faculty", out)

    def test_truncates_long_body(self):
        fake_client = mock.MagicMock()
        agent_peer = mock.MagicMock()
        agent_peer.get_card.return_value = "x" * 5000
        fake_client.peer.return_value = agent_peer
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            out = honcho_client.get_peer_card_context("luna", max_chars=200)
        # The wrapper text adds ~300 chars; the body inside should be capped at 200.
        # Just check the truncation marker landed.
        self.assertIn("...", out)

    def test_swallows_exceptions(self):
        fake_client = mock.MagicMock()
        fake_client.peer.side_effect = RuntimeError("network blip")
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            # Should not raise, just return empty.
            result = honcho_client.get_peer_card_context("luna")
        self.assertEqual(result, "")

    def test_skips_none_entries_in_list_card(self):
        fake_client = mock.MagicMock()
        agent_peer = mock.MagicMock()
        agent_peer.get_card.return_value = ["fact A", None, "", "fact B"]
        fake_client.peer.return_value = agent_peer
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            out = honcho_client.get_peer_card_context("luna")
        self.assertIn("- fact A", out)
        self.assertIn("- fact B", out)
        # Empty/None entries should not produce empty bullets.
        self.assertNotIn("- \n", out)


class TestSubmitTurn(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def test_no_op_when_disabled(self):
        # Should not raise. With no client mocked, this implicitly verifies
        # that the no-op path returns cleanly before reaching the SDK.
        with mock.patch.dict(os.environ, {"HONCHO_ENABLED": "false"}, clear=False):
            honcho_client.submit_turn("luna", "hi", "hello")

    def test_no_op_when_sdk_missing(self):
        with mock.patch.dict(sys.modules, {"honcho": None}):
            honcho_client.submit_turn("luna", "hi", "hello")
        # No exception raised; that's the test.

    def test_calls_session_add_messages(self):
        fake_client = mock.MagicMock()
        fake_session = mock.MagicMock()
        fake_client.session.return_value = fake_session
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            honcho_client.submit_turn("luna", "hi", "hello", session_id="sess-123")
        fake_client.session.assert_called_once_with("sess-123")
        fake_session.add_messages.assert_called_once()
        msgs = fake_session.add_messages.call_args[0][0]
        self.assertEqual(len(msgs), 2)
        # First message is the user turn; second is the agent turn.
        self.assertEqual(msgs[0]["content"], "hi")
        self.assertEqual(msgs[1]["content"], "hello")
        self.assertEqual(msgs[1]["peer_id"], "luna")

    def test_uses_default_session_id_when_none_provided(self):
        fake_client = mock.MagicMock()
        fake_session = mock.MagicMock()
        fake_client.session.return_value = fake_session
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            honcho_client.submit_turn("luna", "hi", "hello")
        # Default session_id pattern: <agent_id>-default
        fake_client.session.assert_called_once_with("luna-default")

    def test_swallows_exceptions(self):
        fake_client = mock.MagicMock()
        fake_client.session.side_effect = RuntimeError("network blip")
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            # Must not raise; daemon stability is load-bearing.
            honcho_client.submit_turn("luna", "hi", "hello")


class TestEnsurePeer(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def test_no_op_when_disabled(self):
        with mock.patch.dict(os.environ, {"HONCHO_ENABLED": "false"}, clear=False):
            honcho_client.ensure_peer("luna")

    def test_no_op_when_sdk_missing(self):
        with mock.patch.dict(sys.modules, {"honcho": None}):
            honcho_client.ensure_peer("luna")

    def test_touches_both_peers(self):
        fake_client = mock.MagicMock()
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            honcho_client.ensure_peer("luna")
        # Should have called .peer() twice: once for the user, once for the agent.
        self.assertEqual(fake_client.peer.call_count, 2)
        called_ids = {c.args[0] for c in fake_client.peer.call_args_list}
        self.assertEqual(called_ids, {"luna", "andyherman"})

    def test_idempotent_on_exception(self):
        fake_client = mock.MagicMock()
        fake_client.peer.side_effect = RuntimeError("network blip")
        with mock.patch.object(honcho_client, "_get_client", return_value=fake_client):
            # Must not raise.
            honcho_client.ensure_peer("luna")


if __name__ == "__main__":
    unittest.main(verbosity=2)
