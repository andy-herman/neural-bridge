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
    def __init__(self, sid="11111111-2222-3333-4444-555555555555", turns=0, static_hash="",
                 context_tokens=0):
        self.session_id = sid
        self.turn_count = turns
        self.static_hash = static_hash
        self.context_tokens = context_tokens


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


class _Harness:
    def setUp(self):
        self.calls: list[dict] = []

    def _fake_call(self, results):
        """results: list of (ok, stdout, err) returned in order."""
        seq = list(results)

        async def fake(prompt, **kwargs):
            self.calls.append({"prompt": prompt, **kwargs})
            return seq.pop(0) if seq else (True, "ok", "")

        return fake

    def _patched(self, *, call_results, is_new=False, exists=True, model=None, static_hash="",
                 context=None, stored_context=0):
        """Patch the runtime's collaborators. Returns a context manager stack."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(agent_runtime, "call_claude",
                                         side_effect=self._fake_call(call_results)))
        stack.enter_context(patch.object(agent_runtime, "MENTION_PROMPT_PATH",
                                         _FakePromptPath(exists=exists)))
        stack.enter_context(patch.object(agent_runtime, "load_agent_definition",
                                         return_value="CHARTER"))
        stack.enter_context(patch.object(
            agent_runtime, "build_mention_parts",
            return_value=agent_runtime.MentionParts(full="PROMPT", continuation="CONTINUE", static_hash="h1")))
        stack.enter_context(patch.object(agent_runtime.claude_usage, "context_tokens", return_value=context))
        stack.enter_context(patch.object(agent_runtime, "allowed_tools_for", return_value="Read"))
        stack.enter_context(patch.object(agent_runtime, "add_dirs_for", return_value=[]))
        stack.enter_context(patch.object(agent_runtime, "timeout_for", return_value=99))
        stack.enter_context(patch.object(agent_runtime, "effort_for", return_value="low"))
        stack.enter_context(patch.object(agent_runtime, "model_for", return_value=model))
        stack.enter_context(patch.object(agent_runtime, "max_response_chars_for", return_value=50))
        stack.enter_context(patch.object(agent_runtime, "truncate_response",
                                         side_effect=lambda t, limit: t[:limit]))
        store = stack.enter_context(patch.object(agent_runtime, "SESSION_STORE"))
        store.get_or_create.return_value = (_FakeSessionRec(static_hash=static_hash,
                                                            context_tokens=stored_context), is_new)
        store.reset.return_value = _FakeSessionRec(sid="fresh-session-id")
        return stack, store


class TestRunAgentTurn(_Harness, unittest.TestCase):
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

    MISSING = "exit_1:No conversation found with session ID: 6b0b5c1e"

    def test_resume_failure_retries_exactly_once(self):
        # Claude Code pruned the session file; a fresh id succeeds.
        stack, store = self._patched(call_results=[(False, "", self.MISSING), (True, "recovered", "")],
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
        stack, _ = self._patched(call_results=[(False, "", self.MISSING), (False, "", "e2")], is_new=False)
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=7,
                                                  message_content="hi")))
        self.assertFalse(res.ok)
        self.assertEqual(res.error_reason, "e2")
        self.assertEqual(len(self.calls), 2)  # never a third

    def test_a_session_too_long_also_starts_fresh(self):
        stack, store = self._patched(
            call_results=[(False, "", "exit_1:API Error: 400 prompt is too long: 1052113 tokens > 1000000"),
                          (True, "ok", "")], is_new=False)
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=7,
                                                  message_content="hi")))
        self.assertTrue(res.ok)
        store.reset.assert_called_once()

    def test_other_failures_of_a_resumed_turn_do_not_bill_twice(self):
        # A timeout, an API error or a spend cap would fail the same way on a
        # fresh session, after re-running the whole tool loop. Until
        # 2026-10-20 each of them did, and lost the session as well.
        for err in ("timeout", "exit_1:API Error: 500 internal", "exit_1:error_max_budget_usd"):
            with self.subTest(err=err):
                self.calls = []
                stack, store = self._patched(call_results=[(False, "", err)], is_new=False)
                with stack:
                    res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=7,
                                                          message_content="hi")))
                self.assertFalse(res.ok)
                self.assertFalse(res.resume_retried)
                self.assertEqual(len(self.calls), 1)
                store.reset.assert_not_called()

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


class TestSessionContext(_Harness, unittest.TestCase):
    """Step 11 (2026-10-20): resumed turns send only what changed, and a
    session past ROTATE_AT_TOKENS is dropped after its turn."""

    def turn(self, **patched):
        results = patched.pop("call_results", [(True, "ok", "")])
        stack, store = self._patched(call_results=results, **patched)
        with stack:
            res = _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=7, message_content="hi")))
        return res, store

    def test_a_resumed_session_with_the_same_fixed_context_gets_the_short_prompt(self):
        res, store = self.turn(is_new=False, static_hash="h1", context=40_000)
        self.assertEqual(self.calls[0]["prompt"], "CONTINUE")
        self.assertTrue(res.continued)
        store.touch.assert_called_once_with(7, "luna", static_hash="h1", context_tokens=40_000)

    def test_changed_fixed_context_or_a_new_session_gets_the_full_prompt(self):
        for name, kwargs in (("notes or charter changed", {"is_new": False, "static_hash": "old"}),
                             ("a session from before", {"is_new": False, "static_hash": ""}),
                             ("a new session", {"is_new": True, "static_hash": ""})):
            with self.subTest(name):
                self.calls = []
                res, store = self.turn(**kwargs)
                self.assertEqual(self.calls[0]["prompt"], "PROMPT")
                self.assertFalse(res.continued)
                self.assertEqual(store.touch.call_args.kwargs["static_hash"], "h1")

    def test_a_fresh_retry_sends_the_full_prompt(self):
        res, store = self.turn(is_new=False, static_hash="h1", call_results=[
            (False, "", "exit_1:No conversation found with session ID: x"), (True, "ok", "")])
        self.assertEqual([c["prompt"] for c in self.calls], ["CONTINUE", "PROMPT"])
        self.assertFalse(res.continued)

    def test_a_large_session_starts_fresh_before_the_turn(self):
        res, store = self.turn(is_new=False, static_hash="h1", stored_context=agent_runtime.ROTATE_AT_TOKENS)
        self.assertTrue(res.ok)
        self.assertEqual(res.rotated_from, agent_runtime.ROTATE_AT_TOKENS)
        store.reset.assert_called_once_with(7, "luna")
        self.assertEqual(self.calls[0]["prompt"], "PROMPT", "a fresh session needs the full prompt")
        self.assertFalse(self.calls[0]["resume"])
        self.assertEqual(self.calls[0]["session_id"], "fresh-session-id")

    def test_a_record_from_before_reads_the_transcript(self):
        # Luna's session was at 352,544 tokens with no size on its record.
        res, store = self.turn(is_new=False, static_hash="", stored_context=0, context=352_544)
        self.assertEqual(res.rotated_from, 352_544)
        store.reset.assert_called_once()

    def test_below_the_limit_the_session_continues(self):
        res, store = self.turn(is_new=False, static_hash="h1", stored_context=60_000, context=64_000)
        self.assertIsNone(res.rotated_from)
        store.reset.assert_not_called()
        self.assertEqual(self.calls[0]["prompt"], "CONTINUE")
        store.touch.assert_called_once_with(7, "luna", static_hash="h1", context_tokens=64_000)

    def test_an_unreadable_context_keeps_the_session(self):
        res, store = self.turn(is_new=False, static_hash="h1", stored_context=0, context=None)
        self.assertIsNone(res.rotated_from)
        store.reset.assert_not_called()
        store.touch.assert_called_once()

    def test_stateless_turns_always_send_the_full_prompt(self):
        stack, store = self._patched(call_results=[(True, "ok", "")], is_new=False, static_hash="h1")
        with stack:
            _run(run_agent_turn(TurnRequest(agent_id="loid", conversation_key=0, message_content="hi",
                                            stateless=True)))
        self.assertEqual(self.calls[0]["prompt"], "PROMPT")
        store.reset.assert_not_called()

    def test_a_prefix_goes_on_either_prompt(self):
        stack, _ = self._patched(call_results=[(True, "ok", "")], is_new=False, static_hash="h1")
        with stack:
            _run(run_agent_turn(TurnRequest(agent_id="luna", conversation_key=7, message_content="hi",
                                            prompt_prefix="INGEST\n")))
        self.assertEqual(self.calls[0]["prompt"], "INGEST\nCONTINUE")


if __name__ == "__main__":
    unittest.main()
