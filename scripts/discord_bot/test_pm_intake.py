"""Unit tests for pm_intake.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.pm_intake import (  # noqa: E402
    APPROVAL_VERBS_RE,
    CANCEL_VERBS_RE,
    PMIntake,
    SessionState,
    _shorten_title,
)


class TestRegexes(unittest.TestCase):
    def test_approval_matches(self):
        for text in ["go", "GO", "yes", "Yes!", "ship it", "approve.", "ok", "okay", "looks good"]:
            self.assertIsNotNone(APPROVAL_VERBS_RE.match(text), f"failed for {text!r}")

    def test_approval_does_not_match_long_text(self):
        for text in ["go ahead and do this and that", "yes but actually no", "shippingit"]:
            self.assertIsNone(APPROVAL_VERBS_RE.match(text), f"unexpected match: {text!r}")

    def test_cancel_matches(self):
        for text in ["cancel", "Nevermind", "drop it", "stop"]:
            self.assertIsNotNone(CANCEL_VERBS_RE.match(text))


class TestPMIntakeFlow(unittest.TestCase):
    def setUp(self):
        self.intake = PMIntake()

    def test_start_session_returns_closure_question(self):
        session, question = self.intake.start_session(
            thread_id="t1", user_id="u1", request="Build a memory dashboard."
        )
        self.assertEqual(session.state, SessionState.AWAITING_USER)
        self.assertIn("done", question.lower())
        self.assertEqual(len(session.clarifications), 1)
        self.assertIsNone(session.clarifications[0].answer)

    def test_duplicate_session_rejected(self):
        self.intake.start_session(thread_id="t1", user_id="u1", request="x")
        with self.assertRaises(ValueError):
            self.intake.start_session(thread_id="t1", user_id="u1", request="y")

    def test_first_answer_advances_to_ready(self):
        self.intake.start_session(thread_id="t1", user_id="u1", request="Build dashboard.")
        session, response = self.intake.continue_session(
            thread_id="t1",
            user_message="When the dashboard renders all my concepts grouped by topic with timestamps.",
        )
        self.assertEqual(session.state, SessionState.READY_TO_FILE)
        self.assertEqual(session.clarifications[0].answer,
                         "When the dashboard renders all my concepts grouped by topic with timestamps.")
        self.assertIn("Closure", response)
        self.assertIn("`go`", response)

    def test_go_after_answer_keeps_ready(self):
        self.intake.start_session(thread_id="t1", user_id="u1", request="x")
        self.intake.continue_session(thread_id="t1", user_message="closure criteria one")
        # Now session is READY_TO_FILE with no unanswered questions
        session, response = self.intake.continue_session(thread_id="t1", user_message="go")
        self.assertEqual(session.state, SessionState.READY_TO_FILE)
        self.assertIn("Filing", response)

    def test_go_before_answer_reprompts(self):
        self.intake.start_session(thread_id="t1", user_id="u1", request="x")
        # latest is still unanswered
        session, response = self.intake.continue_session(thread_id="t1", user_message="go")
        self.assertEqual(session.state, SessionState.AWAITING_USER)
        self.assertIn("still need an answer", response)

    def test_cancel(self):
        self.intake.start_session(thread_id="t1", user_id="u1", request="x")
        session, response = self.intake.continue_session(thread_id="t1", user_message="cancel")
        self.assertEqual(session.state, SessionState.CANCELLED)
        self.assertIn("Cancelled", response)

    def test_cancel_after_filed_is_idempotent(self):
        self.intake.start_session(thread_id="t1", user_id="u1", request="x")
        self.intake.continue_session(thread_id="t1", user_message="closure")
        self.intake.mark_filed(thread_id="t1", issue_number=99)
        session, response = self.intake.continue_session(thread_id="t1", user_message="cancel")
        self.assertEqual(session.state, SessionState.FILED)
        self.assertIn("already closed", response.lower())

    def test_extra_context_after_answer_keeps_ready(self):
        self.intake.start_session(thread_id="t1", user_id="u1", request="x")
        self.intake.continue_session(thread_id="t1", user_message="closure")
        session, response = self.intake.continue_session(thread_id="t1", user_message="oh and also one more thing")
        # Extra context doesn't append clarification turns; session stays READY_TO_FILE
        self.assertEqual(session.state, SessionState.READY_TO_FILE)
        self.assertIn("`go`", response)

    def test_continue_without_session_raises(self):
        with self.assertRaises(KeyError):
            self.intake.continue_session(thread_id="nonexistent", user_message="x")

    def test_mark_filed_records_issue_number(self):
        self.intake.start_session(thread_id="t1", user_id="u1", request="x")
        self.intake.continue_session(thread_id="t1", user_message="closure")
        session = self.intake.mark_filed(thread_id="t1", issue_number=42)
        self.assertEqual(session.state, SessionState.FILED)
        self.assertEqual(session.issue_number, 42)


class TestRenderIssueBody(unittest.TestCase):
    def test_title_truncates_long_request(self):
        intake = PMIntake()
        long_request = "Build a dashboard that shows " + "everything " * 20
        intake.start_session(thread_id="t1", user_id="u1", request=long_request)
        intake.continue_session(thread_id="t1", user_message="closure: x")
        title, _ = intake.render_issue_body("t1", thread_url="https://discord.com/channels/1/2/3")
        self.assertLessEqual(len(title), 70)
        self.assertTrue(title.endswith("…") or len(title) <= 70)

    def test_body_includes_source_clarification_closure(self):
        intake = PMIntake()
        intake.start_session(thread_id="t1", user_id="u1", request="Build dashboard.")
        intake.continue_session(thread_id="t1", user_message="When users can see X.")
        title, body = intake.render_issue_body("t1", thread_url="https://discord.com/threads/abc")
        self.assertIn("Build dashboard.", body)
        self.assertIn("When users can see X.", body)
        self.assertIn("## Source request", body)
        self.assertIn("## PM clarification", body)
        self.assertIn("## Closure criteria", body)
        self.assertIn("https://discord.com/threads/abc", body)
        self.assertEqual(title, "Build dashboard.")

    def test_body_handles_empty_closure(self):
        intake = PMIntake()
        intake.start_session(thread_id="t1", user_id="u1", request="x")
        # Don't answer
        _, body = intake.render_issue_body("t1", thread_url="u")
        self.assertIn("not captured", body)


class TestShortenTitle(unittest.TestCase):
    def test_short_passes(self):
        self.assertEqual(_shorten_title("hello world", max_len=70), "hello world")

    def test_long_truncates_at_word_boundary(self):
        out = _shorten_title("a " * 50, max_len=10)
        self.assertLessEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
