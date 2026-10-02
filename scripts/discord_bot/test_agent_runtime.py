"""Tests for the shared agent turn runtime.

This module replaced four hand-copied pipelines, so the behaviors that used to
live in each copy are asserted here once: the single resume-retry, the stateless
council variant, prompt prefixing, truncation, and the model override.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot import agent_runtime  # noqa: E402
from scripts.discord_bot.agent_runtime import TurnRequest, run_agent_turn  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _FakeSessionRec:
    def __init__(self, sid="11111111-2222-3333-4444-555555555555", turns=0):
        self.session_id = sid
        self.turn_count = turns


class _FakePromptPath:
    """Stand-in for MENTION_PROMPT_PATH. A real PosixPath will not accept
    patched methods, so the module attribute is swapped wholesale instead."""

    def __init__(self, exists: bool = True, text: str = "TEMPLATE"):
        self._exists = exists
        self._text = text

    def exists(self) -> bool:
        return self._exists

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text

    def __str__(self) -> str:
        return "/fake/mention_v1.md"


class TestRunAgentTurn(unittest.TestCase):
    def setUp(self):
        self.calls: list[dict] = []

    def _fake_call(self, results):
        """results: list of (ok, stdout, err) returned in order."""
        seq = list(results)

        async def fake(prompt, **kwargs):
            self.calls.append({"prompt": prompt, **kwargs})
            return seq.pop(0) if seq else (True, "ok", "")

        return fake

    def _patched(self, *, call_results, is_new=False, exists=True, model=None):
        """Patch the runtime's collaborators. Returns a context manager stack."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(agent_runtime, "call_claude",
                                         side_effect=self._fake_call(call_results)))
        stack.enter_context(patch.object(agent_runtime, "MENTION_PROMPT_PATH",
                                         _FakePromptPath(exists=exists)))
        stack.enter_context(patch.object(agent_runtime, "load_agent_definition",
                                         return_value="CHARTER"))
        stack.enter_context(patch.object(agent_runtime, "build_mention_prompt",
                                         return_value="PROMPT"))
        stack.enter_context(patch.object(agent_runtime, "allowed_tools_for", return_value="Read"))
        stack.enter_context(patch.object(agent_runtime, "add_dirs_for", return_value=[]))
        stack.enter_context(patch.object(agent_runtime, "timeout_for", return_value=99))
        stack.enter_context(patch.object(agent_runtime, "effort_for", return_value="low"))
        stack.enter_context(patch.object(agent_runtime, "model_for", return_value=model))
        stack.enter_context(patch.object(agent_runtime, "max_response_chars_for", return_value=50))
        stack.enter_context(patch.object(agent_runtime, "truncate_response",
                                         side_effect=lambda t, limit: t[:limit]))
        store = stack.enter_context(patch.object(agent_runtime, "SESSION_STORE"))
        store.get_or_create.return_value = (_FakeSessionRec(), is_new)
        store.reset.return_value = _FakeSessionRec(sid="fresh-session-id")
        return stack, store

    def test_missing_template_is_setup_error(self):
        stack, _ = self._patched(call_results=[], exists=False)
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1,
                                                  message_content="hi")))
        self.assertFalse(res.ok)
        self.assertIn("missing", res.setup_error)
        self.assertEqual(self.calls, [])  # claude was never called

    def test_happy_path_touches_session_and_truncates(self):
        stack, store = self._patched(call_results=[(True, "x" * 200, "")])
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=7,
                                                  message_content="hi")))
        self.assertTrue(res.ok)
        self.assertEqual(len(res.response), 50)      # truncated to the cap
        self.assertEqual(len(res.raw_stdout), 200)   # raw preserved for action parsing
        store.touch.assert_called_once()

    def test_resume_failure_retries_exactly_once(self):
        # Existing session fails, retry with a fresh id succeeds.
        stack, store = self._patched(call_results=[(False, "", "exit_1:boom"), (True, "recovered", "")],
                                     is_new=False)
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=7,
                                                  message_content="hi")))
        self.assertTrue(res.ok)
        self.assertTrue(res.resume_retried)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[0]["resume"])      # first attempt resumed
        self.assertFalse(self.calls[1]["resume"])     # retry did not
        store.reset.assert_called_once()

    def test_new_session_failure_does_not_retry(self):
        # Nothing to resume, so a failure is real; do not burn a second call.
        stack, store = self._patched(call_results=[(False, "", "exit_1:boom")], is_new=True)
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=7,
                                                  message_content="hi")))
        self.assertFalse(res.ok)
        self.assertFalse(res.resume_retried)
        self.assertEqual(len(self.calls), 1)
        store.reset.assert_not_called()
        store.touch.assert_not_called()

    def test_second_failure_gives_up(self):
        stack, _ = self._patched(call_results=[(False, "", "e1"), (False, "", "e2")], is_new=False)
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=7,
                                                  message_content="hi")))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_reason, "e2")
        self.assertEqual(len(self.calls), 2)  # never a third

    def test_stateless_skips_session_store_and_retry(self):
        # The council room rebuilds context from the shared transcript, so there
        # is no session to resume and a failure must not trigger a retry.
        stack, store = self._patched(call_results=[(False, "", "boom")], is_new=False)
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="loid", conversation_key=0,
                                                  message_content="hi", stateless=True)))
        self.assertFalse(res.ok)
        self.assertFalse(res.resume_retried)
        self.assertEqual(len(self.calls), 1)
        store.get_or_create.assert_not_called()
        store.reset.assert_not_called()
        store.touch.assert_not_called()

    def test_stateless_success_does_not_touch_session(self):
        stack, store = self._patched(call_results=[(True, "hi", "")])
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="loid", conversation_key=0,
                                                  message_content="hi", stateless=True)))
        self.assertTrue(res.ok)
        store.touch.assert_not_called()

    def test_prompt_prefix_is_prepended(self):
        stack, _ = self._patched(call_results=[(True, "ok", "")])
        with stack:
            _run(run_agent_turn(TurnRequest(agent_id="echo", conversation_key=1,
                                            message_content="hi",
                                            prompt_prefix="INGEST-BLOCK\n")))
        self.assertTrue(self.calls[0]["prompt"].startswith("INGEST-BLOCK\n"))

    def test_model_override_is_passed_through(self):
        stack, _ = self._patched(call_results=[(True, "ok", "")])
        with stack:
            _run(run_agent_turn(TurnRequest(agent_id="loid", conversation_key=1,
                                            message_content="hi", model="claude-opus-4.8")))
        self.assertEqual(self.calls[0].get("model"), "claude-opus-4.8")

    def test_no_model_override_leaves_default(self):
        stack, _ = self._patched(call_results=[(True, "ok", "")])
        with stack:
            _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1,
                                            message_content="hi")))
        self.assertNotIn("model", self.calls[0])

    def test_per_agent_model_is_used(self):
        stack, _ = self._patched(call_results=[(True, "ok", "")], model="claude-opus-5")
        with stack:
            _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1, message_content="hi")))
        self.assertEqual(self.calls[0]["model"], "claude-opus-5")

    def test_request_override_beats_the_per_agent_model(self):
        stack, _ = self._patched(call_results=[(True, "ok", "")], model="claude-opus-5")
        with stack:
            _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1, message_content="hi",
                                            model="claude-opus-4.8")))
        self.assertEqual(self.calls[0]["model"], "claude-opus-4.8")

    def test_claude_5_failure_falls_back_on_the_same_session(self):
        # A resumed session carries across models (verified live 2026-09-30),
        # so the fallback keeps the conversation instead of resetting it.
        stack, store = self._patched(call_results=[(False, "", "exit_1:API Error: 400 prefill"),
                                                   (True, "answer", "")], model="claude-opus-5")
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1, message_content="hi")))
        self.assertTrue(res.ok)
        self.assertTrue(res.model_fallback)
        self.assertEqual([c["model"] for c in self.calls], ["claude-opus-5", agent_runtime.DEFAULT_MODEL])
        self.assertEqual([c["resume"] for c in self.calls], [True, True])
        self.assertEqual(self.calls[0]["session_id"], self.calls[1]["session_id"])
        store.reset.assert_not_called()

    def test_new_session_fallback_uses_a_fresh_id(self):
        stack, store = self._patched(call_results=[(False, "", "exit_1:boom"), (True, "answer", "")],
                                     is_new=True, model="claude-opus-5")
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1, message_content="hi")))
        self.assertTrue(res.ok)
        self.assertEqual(self.calls[1]["session_id"], "fresh-session-id")
        self.assertFalse(self.calls[1]["resume"])

    def test_timeout_does_not_fall_back(self):
        stack, _ = self._patched(call_results=[(False, "", "timeout")], is_new=True, model="claude-opus-5")
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1, message_content="hi")))
        self.assertFalse(res.ok)
        self.assertEqual(len(self.calls), 1)

    def test_a_4x_failure_does_not_fall_back(self):
        stack, _ = self._patched(call_results=[(False, "", "exit_1:boom")], is_new=True,
                                 model="claude-opus-4.8")
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1, message_content="hi")))
        self.assertFalse(res.model_fallback)
        self.assertEqual(len(self.calls), 1)

    def test_effort_and_timeout_come_from_policy(self):
        stack, _ = self._patched(call_results=[(True, "ok", "")])
        with stack:
            _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1,
                                            message_content="hi")))
        self.assertEqual(self.calls[0]["effort"], "low")
        self.assertEqual(self.calls[0]["timeout"], 99)

    def test_empty_property(self):
        stack, _ = self._patched(call_results=[(True, "   ", "")])
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=1,
                                                  message_content="hi")))
        self.assertTrue(res.empty)


if __name__ == "__main__":
    unittest.main()
