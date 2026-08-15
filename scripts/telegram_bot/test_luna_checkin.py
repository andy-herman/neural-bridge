"""Tests for Luna's proactive check-in.

The behavior that matters most is that silence works. A check-in that cannot
stay quiet becomes noise, gets muted, and then the ones that matter are missed
too.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.telegram_bot import luna_checkin as ci  # noqa: E402


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

    def test_stale_briefing_is_labeled_in_context(self):
        # A stale briefing presented as current state would have her reporting
        # yesterday's fleet as today's.
        old = (date.today() - timedelta(days=2)).isoformat()
        (self.dir / f"{old}.md").write_text("stale content", encoding="utf-8")
        ctx = ci.gather_context(self.dir)
        self.assertIn("NOT today's", ctx)

    def test_todays_briefing_not_labeled_stale(self):
        today = date.today().isoformat()
        (self.dir / f"{today}.md").write_text("fresh content", encoding="utf-8")
        ctx = ci.gather_context(self.dir)
        self.assertNotIn("NOT today's", ctx)

    def test_no_briefing_yields_empty_context(self):
        self.assertEqual(ci.gather_context(self.dir), "")


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
