"""Offline transport wiring and setup-error delivery, with no service calls."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from scripts.discord_bot.agent_runtime import TurnResult
from scripts.telegram_bot import council_bridge, loid_bridge, luna_bridge


class TestCompanionTelegramCallers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.message = SimpleNamespace(chat_id=24680, text="Synthetic request", reply_text=AsyncMock())
        self.update = SimpleNamespace(message=self.message)
        self.context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))
        self.history = [{"author": "Andy", "content": "Synthetic earlier turn"}]
        self.capture = self.stack.enter_context(
            patch.object(luna_bridge.honcho_client, "submit_turn")
        )
        self.stack.enter_context(patch.object(luna_bridge, "_is_authorized", return_value=True))
        self.pushes = {
            bridge: self.stack.enter_context(patch.object(bridge, "_push_history"))
            for bridge in (luna_bridge, loid_bridge)
        }
        for bridge in (luna_bridge, loid_bridge):
            self.stack.enter_context(patch.object(bridge, "_chat_history_for", return_value=self.history))
            self.stack.enter_context(patch.object(bridge, "log"))
        self.stack.enter_context(patch.object(council_bridge, "log"))

    async def invoke(self, bridge):
        if bridge is luna_bridge:
            await bridge.handle_text(self.update, self.context)
        else:
            await bridge._process_message(
                self.message, self.context, text=self.message.text, kind="text"
            )

    async def test_direct_callers_preserve_keys_history_and_owner_gate(self):
        for bridge in (luna_bridge, loid_bridge):
            with self.subTest(agent_id=bridge.AGENT_ID):
                self.message.reply_text.reset_mock()
                self.capture.reset_mock()
                result = TurnResult(
                    ok=True, response="Reply\n```actions\n{}\n```", session_id="existing-session",
                )
                with patch.object(bridge, "run_agent_turn", new_callable=AsyncMock, return_value=result) as run:
                    await self.invoke(bridge)
                req = run.await_args.args[0]
                self.assertEqual(req.transport, "telegram")
                self.assertEqual(req.agent_id, bridge.AGENT_ID)
                self.assertEqual(req.conversation_key, 24680)
                self.assertEqual(req.history, self.history)
                self.assertEqual(req.conversation_log_path, "")
                self.assertFalse(req.owner_invoked)
                self.assertFalse(req.stateless)
                self.assertIsNone(req.model)
                self.message.reply_text.assert_awaited_once_with("Reply")
                self.capture.assert_called_once_with(
                    agent_id=bridge.AGENT_ID, user_message=self.message.text,
                    agent_response="Reply", session_id="existing-session",
                )

    async def test_setup_error_reaches_direct_user_without_capture_or_history_write(self):
        result = TurnResult(
            ok=False, error_reason="companion_standard_missing",
            setup_error="Companion standard missing. No model was called.",
        )
        for bridge in (luna_bridge, loid_bridge):
            with self.subTest(agent_id=bridge.AGENT_ID):
                self.message.reply_text.reset_mock()
                with patch.object(bridge, "run_agent_turn", new_callable=AsyncMock, return_value=result):
                    await self.invoke(bridge)
                self.message.reply_text.assert_awaited_once_with(
                    f"_(Conversation setup failed: {result.setup_error})_"
                )
                self.capture.assert_not_called()
                self.pushes[bridge].assert_not_called()
                bridge.log.assert_any_call(result.setup_error)

    async def test_council_loid_keeps_stateless_pin_and_safe_setup_reason(self):
        result = TurnResult(
            ok=False, error_reason="companion_standard_empty",
            setup_error="Companion standard is empty. No model was called.",
        )
        with patch.object(council_bridge, "run_agent_turn", new_callable=AsyncMock, return_value=result) as run:
            ok, response = await council_bridge._invoke_loid("Synthetic request", self.history)
        self.assertFalse(ok)
        self.assertEqual(response, result.setup_error)
        req = run.await_args.args[0]
        self.assertEqual(req.transport, "telegram")
        self.assertEqual(req.agent_id, "loid")
        self.assertEqual(req.conversation_key, 0)
        self.assertEqual(req.history, self.history)
        self.assertTrue(req.stateless)
        self.assertEqual(req.model, council_bridge.LOID_MODEL)
        self.assertFalse(req.owner_invoked)
        self.assertEqual(req.conversation_log_path, "")
        self.assertEqual(req.channel_kind, "COUNCIL (shared Telegram room with Andy and Yor)")

    async def test_yor_hermes_invocation_receives_no_nb_companion_contract(self):
        process = SimpleNamespace(
            returncode=0, communicate=AsyncMock(return_value=(b"Synthetic reply\n\nSession: test", b""))
        )
        with patch.object(
            council_bridge.asyncio, "create_subprocess_exec", new_callable=AsyncMock, return_value=process
        ) as spawn, patch.object(
            council_bridge.asyncio, "wait_for", wraps=council_bridge.asyncio.wait_for
        ) as wait, patch.object(council_bridge, "run_agent_turn", new_callable=AsyncMock) as nb_turn:
            ok, response = await council_bridge._invoke_yor("Synthetic request", self.history)
        self.assertTrue(ok)
        self.assertEqual(response, "Synthetic reply")
        args = spawn.await_args.args
        self.assertEqual(args[:3], ("hermes", "chat", "-q"))
        self.assertEqual(args[4:], ("-Q", "--accept-hooks"))
        self.assertIn("Follow your 'In the shared room' rules", args[3])
        self.assertNotIn("companion-standard", args[3])
        self.assertEqual(wait.call_args.kwargs["timeout"], council_bridge.YOR_TIMEOUT)
        nb_turn.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
