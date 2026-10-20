"""One-shot calls drop Claude Code's tool and skill scaffolding (2026-10-20).

Every `claude -p` loads the built-in tool definitions and the skill listing,
whether the call uses them or not: 25,096 input tokens for a one-line probe,
8,777 with `--tools "" --disable-slash-commands`. These tests keep the flags on
the calls whose prompts carry all their input, and off everything else.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "hooks"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import claude_env  # noqa: E402


def has_no_tools(args: list[str]) -> bool:
    i = args.index("--tools") if "--tools" in args else -1
    return i >= 0 and args[i + 1] == "" and "--disable-slash-commands" in args


def capture_run(module, call):
    """Run `call` with `module.subprocess.run` faked; return the argv."""
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
        call()
    return seen["args"]


class TestCallClaudeSync(unittest.TestCase):
    def test_no_tools_reaches_the_cli(self):
        from scripts.discord_bot import claude_invoke
        args = capture_run(claude_invoke, lambda: claude_invoke.call_claude_sync("hi", no_tools=True))
        self.assertTrue(has_no_tools(args), args)

    def test_default_keeps_the_tools(self):
        from scripts.discord_bot import claude_invoke
        args = capture_run(claude_invoke, lambda: claude_invoke.call_claude_sync("hi"))
        self.assertNotIn("--tools", args)
        self.assertNotIn("--disable-slash-commands", args)

    def test_no_tools_with_allowed_tools_is_a_mistake(self):
        from scripts.discord_bot import claude_invoke
        with self.assertRaises(ValueError):
            claude_invoke.call_claude_sync("hi", allowed_tools="Read", no_tools=True)

    def test_async_wrapper_passes_it_through(self):
        import asyncio
        from scripts.discord_bot import claude_invoke
        with mock.patch.object(claude_invoke, "call_claude_sync", return_value=(True, "x", "")) as call:
            asyncio.run(claude_invoke.call_claude("hi", no_tools=True))
        self.assertIs(call.call_args.kwargs["no_tools"], True)


class TestOneShotCallers(unittest.TestCase):
    def test_check_in(self):
        from scripts.telegram_bot import luna_checkin
        with mock.patch.object(luna_checkin, "call_claude_sync_with_fallback",
                               return_value=(True, "x", "", "m")) as call:
            luna_checkin.generate("prompt")
        self.assertIs(call.call_args.kwargs["no_tools"], True)

    def test_echo_synthesis(self):
        from scripts.echo import synthesize_profile
        with mock.patch.object(synthesize_profile, "call_claude_sync", return_value=(True, "x", "")) as call:
            synthesize_profile.invoke_synthesis("p")
        self.assertIs(call.call_args.kwargs["no_tools"], True)

    def test_publish_prep(self):
        text = (REPO_ROOT / "scripts/publish/prepare_week.py").read_text(encoding="utf-8")
        self.assertIn("flush=False, no_tools=True)", text)

    def test_council_router(self):
        text = (REPO_ROOT / "scripts/telegram_bot/council_bridge.py").read_text(encoding="utf-8")
        router = text[text.index("model=ROUTER_MODEL"):]
        self.assertIn("no_tools=True", router[:router.index("\n    )")])

    def test_flush(self):
        import flush
        args = capture_run(flush, lambda: flush.call_claude("p", "claude-opus-4-8", 5))
        self.assertTrue(has_no_tools(args), args)

    def test_compile_gate_and_writer(self):
        import compile as compile_mod
        args = capture_run(compile_mod, lambda: compile_mod._claude_or_fallback("p", "claude-opus-4-8", 5))
        self.assertTrue(has_no_tools(args), args)

    def test_lint_imperative_check(self):
        import lint
        args = capture_run(lint, lambda: lint.call_imperative_check("p", "claude-opus-4-8", 5))
        self.assertTrue(has_no_tools(args), args)

    def test_the_flags_are_what_the_cli_documents(self):
        self.assertEqual(claude_env.NO_TOOLS_ARGS, ("--tools", "", "--disable-slash-commands"))


if __name__ == "__main__":
    unittest.main()
