"""Tests for Luna's proactive check-in.

The behavior that matters most is that silence works. A check-in that cannot
stay quiet becomes noise, gets muted, and then the ones that matter are missed
too.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.telegram_bot import luna_checkin as ci  # noqa: E402
from scripts.discord_bot import companion  # noqa: E402


class TestPassDetection(unittest.TestCase):
    """[PASS] is a first-class outcome, so parsing it must be forgiving."""

    def test_bare_token(self):
        self.assertTrue(ci.is_pass("[PASS]"))

    def test_token_with_whitespace_and_newlines(self):
        self.assertTrue(ci.is_pass("  \n[PASS]\n  "))

    def test_token_wrapped_in_markdown(self):
        self.assertTrue(ci.is_pass("`[PASS]`"))
        self.assertTrue(ci.is_pass("*[PASS]*"))

    def test_bare_word_without_brackets(self):
        self.assertTrue(ci.is_pass("PASS"))

    def test_trailing_period(self):
        self.assertTrue(ci.is_pass("[PASS]."))

    def test_empty_response_is_silence(self):
        self.assertTrue(ci.is_pass(""))
        self.assertTrue(ci.is_pass("   \n "))

    def test_real_message_is_not_pass(self):
        self.assertFalse(ci.is_pass("Synapse has been failing for 69 days."))

    def test_message_mentioning_pass_is_not_pass(self):
        # Must not swallow a real message that happens to use the word.
        self.assertFalse(ci.is_pass("You should pass on the Thursday meeting."))


class TestBriefingSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_dir(self):
        self.assertEqual(ci.latest_briefing(self.dir / "nope"), ("", ""))

    def test_empty_dir(self):
        self.assertEqual(ci.latest_briefing(self.dir), ("", ""))

    def test_prefers_today(self):
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=3)).isoformat()
        (self.dir / f"{old}.md").write_text("old", encoding="utf-8")
        (self.dir / f"{today}.md").write_text("fresh", encoding="utf-8")
        name, text = ci.latest_briefing(self.dir)
        self.assertEqual(name, f"{today}.md")
        self.assertEqual(text, "fresh")

    def test_falls_back_to_newest_when_no_today(self):
        old = (date.today() - timedelta(days=3)).isoformat()
        older = (date.today() - timedelta(days=9)).isoformat()
        (self.dir / f"{older}.md").write_text("older", encoding="utf-8")
        (self.dir / f"{old}.md").write_text("old", encoding="utf-8")
        name, text = ci.latest_briefing(self.dir)
        self.assertEqual(name, f"{old}.md")

    def test_oversized_briefing_is_capped(self):
        today = date.today().isoformat()
        (self.dir / f"{today}.md").write_text("x" * 99_000, encoding="utf-8")
        _name, text = ci.latest_briefing(self.dir)
        self.assertLessEqual(len(text), ci.MAX_BRIEFING_CHARS + 40)

    def test_stale_briefing_is_labeled(self):
        # A stale briefing presented as current state would have her reporting
        # last week's fleet as today's. Needs an actionable marker to appear
        # at all, hence "failing".
        old = (date.today() - timedelta(days=2)).isoformat()
        (self.dir / f"{old}.md").write_text("Synapse is failing", encoding="utf-8")
        self.assertIn("NOT today's", ci.gather_fleet(self.dir))

    def test_todays_briefing_not_labeled_stale(self):
        today = date.today().isoformat()
        (self.dir / f"{today}.md").write_text("Synapse is failing", encoding="utf-8")
        self.assertNotIn("NOT today's", ci.gather_fleet(self.dir))

    def test_no_briefing_yields_no_fleet_section(self):
        self.assertEqual(ci.gather_fleet(self.dir), "")

    def test_healthy_fleet_is_omitted_entirely(self):
        # Agent uptime is devops. A green fleet must not crowd out the
        # commitments that actually belong in an EA check-in.
        today = date.today().isoformat()
        (self.dir / f"{today}.md").write_text(
            "# Fleet Briefing\n\n- Status: ALL GOOD\n- Health: 100/100\n", encoding="utf-8")
        self.assertEqual(ci.gather_fleet(self.dir), "")

    def test_fleet_included_when_actionable(self):
        today = date.today().isoformat()
        (self.dir / f"{today}.md").write_text(
            "# Fleet Briefing\n\n- Status: 1 ANOMALY DETECTED\n", encoding="utf-8")
        out = ci.gather_fleet(self.dir)
        self.assertIn("ANOMALY", out)
        self.assertIn("secondary", out)


class TestPromptBuilding(unittest.TestCase):
    TPL = ("kind={kind}\nguidance={kind_guidance}\nctx={context}\nnotes={notes}\n")

    def test_all_placeholders_filled(self):
        out = ci.build_checkin_prompt("morning", "CTX", "NOTES", template=self.TPL)
        self.assertIn("kind=morning", out)
        self.assertIn("ctx=CTX", out)
        self.assertIn("notes=NOTES", out)
        self.assertIn(ci.KIND_GUIDANCE["morning"], out)
        self.assertNotIn("{", out)

    def test_empty_context_gets_placeholder(self):
        out = ci.build_checkin_prompt("evening", "", "N", template=self.TPL)
        self.assertIn("nothing gathered", out)

    def test_real_template_has_every_placeholder(self):
        # Guards against a template edit that silently drops a substitution.
        tpl = ci.PROMPT_PATH.read_text(encoding="utf-8")
        for token in ("{kind}", "{kind_guidance}", "{context}", "{notes}"):
            self.assertIn(token, tpl, f"template lost {token}")

    def test_real_template_renders_clean(self):
        out = ci.build_checkin_prompt("morning", "C", "N")
        self.assertNotIn("{kind}", out)
        self.assertNotIn("{context}", out)
        self.assertIn("[PASS]", out)  # silence instruction survives

    def test_full_companion_contract_in_real_and_custom_templates(self):
        standard = companion.load_companion_standard()
        for template in (None, self.TPL):
            with self.subTest(custom=template is not None):
                prompt = ci.build_checkin_prompt("morning", "C", "N", template=template)
                self.assertTrue(prompt.startswith(f"<companion-standard>\n{standard}\n</companion-standard>"))
                self.assertEqual(prompt.count(standard), 1)


class TestCheckinCompanionSetup(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(ci, "load_default_env"))
        self.stack.enter_context(patch.object(ci, "gather_context", return_value="Synthetic context"))
        self.stack.enter_context(patch.object(ci, "gather_notes", return_value="Synthetic notes"))
        self.model = self.stack.enter_context(patch.object(ci, "call_claude_sync", return_value=(True, "[PASS]", "")))
        self.send = self.stack.enter_context(patch.object(ci, "send_telegram"))
        self.token = self.stack.enter_context(patch.object(ci, "get_token"))
        self.telemetry = self.stack.enter_context(patch.object(ci.mem, "record"))

    def test_missing_contract_is_visible_failure_without_model_or_delivery(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(companion, "COMPANION_STANDARD_PATH", Path(tmp) / "missing.md"), \
             redirect_stderr(StringIO()) as stderr:
            self.assertEqual(ci.main(["--kind", "morning"]), 1)
        self.assertIn("companion_standard_missing; no model was called", stderr.getvalue())
        self.model.assert_not_called()
        self.send.assert_not_called()
        self.token.assert_not_called()
        self.telemetry.assert_called_once_with(
            ci.mem.WRITE, "luna_checkin", agent_id="luna", ok=False,
            detail="morning: companion_standard_missing",
        )

    def test_unreadable_template_is_visible_failure_without_generation(self):
        path = Mock(spec=Path)
        path.read_text.side_effect = PermissionError("synthetic diagnostic")
        with patch.object(ci, "PROMPT_PATH", path), redirect_stderr(StringIO()) as stderr:
            self.assertEqual(ci.main(["--kind", "evening"]), 1)
        self.assertIn("checkin_template_unreadable; no model was called", stderr.getvalue())
        self.assertNotIn("synthetic diagnostic", stderr.getvalue())
        self.model.assert_not_called()
        self.send.assert_not_called()
        self.token.assert_not_called()

    def test_pass_keeps_silence_settings_and_actual_full_prompt(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(ci.main(["--kind", "morning"]), 0)
        prompt = self.model.call_args.args[0]
        standard = companion.load_companion_standard()
        self.assertEqual(prompt.count(standard), 1)
        self.assertIn("[PASS]", prompt)
        self.assertIn("Telegram path is text-only", prompt)
        self.assertEqual(self.model.call_args.kwargs, {"timeout": 240, "effort": "low"})
        self.assertEqual(ci.MAX_TELEGRAM_CHARS, 3900)
        self.send.assert_not_called()
        self.token.assert_not_called()


class TestTelegramFormatting(unittest.TestCase):
    def test_long_message_truncated(self):
        out = ci.clean_for_telegram("y" * 9000)
        self.assertLessEqual(len(out), ci.MAX_TELEGRAM_CHARS)

    def test_short_message_untouched(self):
        self.assertEqual(ci.clean_for_telegram("  hello  "), "hello")


class TestAllowedChatIds(unittest.TestCase):
    def setUp(self):
        import os
        self._prior = os.environ.get(ci.ALLOWED_USERS_ENV)

    def tearDown(self):
        import os
        if self._prior is None:
            os.environ.pop(ci.ALLOWED_USERS_ENV, None)
        else:
            os.environ[ci.ALLOWED_USERS_ENV] = self._prior

    def test_parses_list(self):
        import os
        os.environ[ci.ALLOWED_USERS_ENV] = "123, 456"
        self.assertEqual(ci.allowed_chat_ids(), [123, 456])

    def test_unset_is_empty(self):
        import os
        os.environ.pop(ci.ALLOWED_USERS_ENV, None)
        self.assertEqual(ci.allowed_chat_ids(), [])

    def test_ignores_non_numeric(self):
        import os
        os.environ[ci.ALLOWED_USERS_ENV] = "abc,789"
        self.assertEqual(ci.allowed_chat_ids(), [789])


if __name__ == "__main__":
    unittest.main()
