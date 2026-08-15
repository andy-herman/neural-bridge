"""Tests for Luna's calendar and inbox CLIs.

No network. Everything here exercises parsing, formatting, conflict detection
and the not-configured paths, which is where the logic actually lives.

Two behaviors are asserted deliberately as policy, not just as code:
  - no send command exists anywhere in the inbox CLI
  - the default OAuth scopes are read-only
Both are things a future edit could quietly regress.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.luna import calendar as cal  # noqa: E402
from scripts.luna import google_auth as ga  # noqa: E402
from scripts.luna import inbox as ib  # noqa: E402

UTC = timezone.utc


def _ev(summary, start_h, end_h, *, all_day=False, attendees=1, desc=""):
    base = datetime(2026, 8, 15, tzinfo=UTC)
    item = {
        "summary": summary,
        "start": ({"date": "2026-08-15"} if all_day
                  else {"dateTime": (base + timedelta(hours=start_h)).isoformat()}),
        "end": ({"date": "2026-08-16"} if all_day
                else {"dateTime": (base + timedelta(hours=end_h)).isoformat()}),
        "attendees": [{"email": f"a{i}@x.com"} for i in range(attendees)],
        "description": desc,
    }
    return cal.parse_event(item)


class TestCalendarParsing(unittest.TestCase):
    def test_timed_event(self):
        e = _ev("Standup", 9, 10)
        self.assertEqual(e.summary, "Standup")
        self.assertFalse(e.all_day)
        self.assertEqual(e.duration_minutes(), 60)

    def test_all_day_event(self):
        e = _ev("Conference", 0, 0, all_day=True)
        self.assertTrue(e.all_day)

    def test_missing_title(self):
        self.assertEqual(cal.parse_event({}).summary, "(no title)")

    def test_agenda_detection(self):
        self.assertFalse(_ev("A", 9, 10).has_agenda)
        self.assertTrue(_ev("A", 9, 10, desc="1. budget").has_agenda)

    def test_malformed_datetime_does_not_raise(self):
        e = cal.parse_event({"start": {"dateTime": "not-a-date"}})
        self.assertIsNone(e.start)


class TestConflicts(unittest.TestCase):
    def test_overlapping_pair_found(self):
        events = [_ev("A", 9, 11), _ev("B", 10, 12)]
        self.assertEqual(len(cal.find_conflicts(events)), 1)

    def test_adjacent_meetings_do_not_conflict(self):
        # 9-10 then 10-11 is back to back, not a clash.
        events = [_ev("A", 9, 10), _ev("B", 10, 11)]
        self.assertEqual(cal.find_conflicts(events), [])

    def test_all_day_never_conflicts(self):
        events = [_ev("Conf", 0, 0, all_day=True), _ev("Call", 10, 11)]
        self.assertEqual(cal.find_conflicts(events), [])

    def test_three_way_overlap_reports_each_pair(self):
        events = [_ev("A", 9, 12), _ev("B", 10, 11), _ev("C", 10, 13)]
        self.assertEqual(len(cal.find_conflicts(events)), 3)

    def test_no_events(self):
        self.assertEqual(cal.find_conflicts([]), [])


class TestCalendarFormatting(unittest.TestCase):
    def test_empty_day(self):
        self.assertIn("nothing scheduled", cal.format_events([], "Today"))

    def test_flags_meeting_without_agenda(self):
        # The single most useful thing an assistant says about a meeting.
        out = cal.format_events([_ev("Review", 14, 15, attendees=4)], "Today")
        self.assertIn("NO AGENDA", out)

    def test_solo_block_not_flagged(self):
        out = cal.format_events([_ev("Focus", 14, 15, attendees=1)], "Today")
        self.assertNotIn("NO AGENDA", out)

    def test_conflicts_message_when_clear(self):
        self.assertIn("No overlapping", cal.format_conflicts([]))


class TestInboxParsing(unittest.TestCase):
    def _raw(self, **kw):
        base = {
            "id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"],
            "snippet": "hello there",
            "internalDate": str(int(datetime(2026, 8, 10, tzinfo=UTC).timestamp() * 1000)),
            "payload": {"headers": [
                {"name": "From", "value": "Jane Doe <jane@x.com>"},
                {"name": "Subject", "value": "Quarterly filing"},
            ]},
        }
        base.update(kw)
        return base

    def test_parses_headers(self):
        m = ib.parse_message(self._raw())
        self.assertEqual(m.subject, "Quarterly filing")
        self.assertIn("Jane", m.sender)
        self.assertTrue(m.unread)

    def test_missing_subject(self):
        m = ib.parse_message(self._raw(payload={"headers": []}))
        self.assertEqual(m.subject, "(no subject)")

    def test_age_computed(self):
        m = ib.parse_message(self._raw())
        self.assertEqual(m.age_days(datetime(2026, 8, 15, tzinfo=UTC)), 5)

    def test_sent_marks_from_me(self):
        m = ib.parse_message(self._raw(labelIds=["SENT"]))
        self.assertTrue(m.from_me)

    def test_bad_internal_date(self):
        m = ib.parse_message(self._raw(internalDate="nonsense"))
        self.assertIsNone(m.received)
        self.assertIsNone(m.age_days())

    def test_format_empty(self):
        self.assertIn("nothing", ib.format_messages([], "Unread"))


class TestSafetyPolicy(unittest.TestCase):
    """These encode charter rules. A future edit should have to break a test."""

    def _subcommands(self, module, sample_args):
        """The argparse choices a CLI actually accepts."""
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.suppress(SystemExit):
            module.main(sample_args)
        return buf.getvalue()

    def test_inbox_has_no_send_subcommand(self):
        # Behavior, not prose: an unknown subcommand must be rejected.
        err = self._subcommands(ib, ["send"])
        self.assertIn("invalid choice", err.lower())

    def test_inbox_makes_no_write_calls(self):
        # No POST/send endpoint anywhere in the module's actual calls.
        source = Path(ib.__file__).read_text(encoding="utf-8")
        self.assertNotIn("messages/send", source)
        self.assertNotIn("api_post", source)

    def test_calendar_has_no_delete_or_create_subcommand(self):
        for cmd in ("delete", "create", "cancel"):
            err = self._subcommands(cal, [cmd])
            self.assertIn("invalid choice", err.lower(),
                          f"calendar must not accept '{cmd}'")

    def test_calendar_makes_no_write_calls(self):
        source = Path(cal.__file__).read_text(encoding="utf-8")
        self.assertNotIn("api_post", source)
        self.assertNotIn('method="DELETE"', source)

    def test_default_scopes_are_read_only(self):
        for scope in ga.READ_SCOPES:
            self.assertTrue(scope.endswith(".readonly"), f"{scope} is not read-only")

    def test_compose_scope_is_not_in_defaults(self):
        # gmail.compose grants send as well as draft, so it is opt-in only.
        self.assertNotIn(ga.COMPOSE_SCOPE, ga.READ_SCOPES)


class TestNotConfigured(unittest.TestCase):
    """With no credentials the CLIs must say why, not crash or invent."""

    def setUp(self):
        self._saved = (ga.CLIENT_SECRET_PATH, ga.TOKEN_PATH)
        ga.CLIENT_SECRET_PATH = Path("/nonexistent/client.json")
        ga.TOKEN_PATH = Path("/nonexistent/token.json")

    def tearDown(self):
        ga.CLIENT_SECRET_PATH, ga.TOKEN_PATH = self._saved

    def test_status_names_the_missing_piece(self):
        self.assertIn("no client secret", ga.config_status())

    def test_is_configured_false(self):
        self.assertFalse(ga.is_configured())

    def test_calendar_exits_2(self):
        self.assertEqual(cal.main(["today"]), 2)

    def test_inbox_exits_2(self):
        self.assertEqual(ib.main(["unread"]), 2)


class TestRedaction(unittest.TestCase):
    """A partial access token was leaked into a transcript on 2026-08-15."""

    def test_redact_hides_the_secret(self):
        out = ga.redact("ya29.averylongsecrettokenvalue")
        self.assertNotIn("averylongsecret", out)
        self.assertIn("redacted", out)

    def test_redact_handles_empty(self):
        self.assertEqual(ga.redact(""), "<empty>")

    def test_redact_short_value(self):
        self.assertEqual(ga.redact("ab"), "<redacted>")


if __name__ == "__main__":
    unittest.main()
