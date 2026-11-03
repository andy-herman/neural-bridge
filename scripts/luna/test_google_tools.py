"""Tests for Luna's calendar and inbox CLIs.

No network. Everything here exercises parsing, formatting, conflict detection
and the not-configured paths, which is where the logic actually lives.

Two behaviors are asserted deliberately as policy, not just as code:
  - no send command exists anywhere in the inbox CLI
  - the default OAuth scopes are read-only
Both are things a future edit could quietly regress.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

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


class TestDuplicateDetection(unittest.TestCase):
    """The real case: the same flight entered in English and Korean."""

    def _pair(self, t1, t2, s=9, e=11):
        return _ev(t1, s, e), _ev(t2, s, e)

    def test_cross_language_flight_is_duplicate(self):
        a, b = self._pair("Flight to Seattle (KE 41)", "Flight to 시애틀 (KE 41)")
        self.assertTrue(cal.looks_duplicate(a, b))

    def test_duplicate_excluded_from_conflicts(self):
        a, b = self._pair("Flight to Seattle (KE 41)", "Flight to 시애틀 (KE 41)")
        self.assertEqual(cal.find_conflicts([a, b]), [])
        self.assertEqual(len(cal.find_duplicates([a, b])), 1)

    def test_identical_titles_are_duplicate(self):
        a, b = self._pair("Standup", "Standup")
        self.assertTrue(cal.looks_duplicate(a, b))

    def test_different_meetings_same_slot_stay_a_conflict(self):
        # The thing that must NOT be swallowed: two real meetings booked over
        # each other is exactly what Andy needs told.
        a, b = self._pair("Budget review", "Dentist")
        self.assertFalse(cal.looks_duplicate(a, b))
        self.assertEqual(len(cal.find_conflicts([a, b])), 1)

    def test_partial_overlap_is_never_duplicate(self):
        a, b = _ev("Flight to Seattle (KE 41)", 9, 11), _ev("Flight to 시애틀 (KE 41)", 10, 12)
        self.assertFalse(cal.looks_duplicate(a, b))
        self.assertEqual(len(cal.find_conflicts([a, b])), 1)

    def test_shared_digit_token_drives_it(self):
        # Course codes and room numbers survive translation the same way.
        a, b = self._pair("INFO 310 lecture", "INFO 310 강의")
        self.assertTrue(cal.looks_duplicate(a, b))

    def test_same_number_different_event_is_not_duplicate(self):
        # A shared digit token alone must not merge unrelated events... but
        # here the titles genuinely share "310", so this documents the limit:
        # identical start AND end is also required, and these differ.
        a, b = _ev("INFO 310 lecture", 9, 11), _ev("Room 310 booking", 13, 14)
        self.assertFalse(cal.looks_duplicate(a, b))

    def test_untitled_events_same_slot_treated_as_duplicate(self):
        a, b = self._pair("", "")
        self.assertTrue(cal.looks_duplicate(a, b))

    def test_report_shows_duplicates_separately(self):
        a, b = self._pair("Flight to Seattle (KE 41)", "Flight to 시애틀 (KE 41)")
        out = cal.format_conflicts(cal.find_conflicts([a, b]), cal.find_duplicates([a, b]))
        self.assertIn("No overlapping meetings", out)
        self.assertIn("DUPLICATES", out)

    def test_report_omits_duplicate_section_when_none(self):
        out = cal.format_conflicts([], [])
        self.assertNotIn("DUPLICATES", out)


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


class _FakeServer:
    """Stands in for the setup command's loopback listener: no port is opened."""

    def __init__(self, *_args, **_kwargs):
        pass

    def handle_request(self):
        pass

    def server_close(self):
        pass


class TestDriveScope(unittest.TestCase):
    """drive.readonly is a second opt-in, for the council room's Drive links."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.token = base / "google_token.json"
        secret = base / "google_client_secret.json"
        secret.write_text(json.dumps({"installed": {"client_id": "cid.apps.example",
                                                    "client_secret": "not-a-real-secret"}}),
                          encoding="utf-8")
        for p in (patch.object(ga, "TOKEN_PATH", self.token),
                  patch.object(ga, "CLIENT_SECRET_PATH", secret)):
            p.start()
            self.addCleanup(p.stop)

    def _setup_scopes_requested(self, argv) -> tuple[list[str], str]:
        """Run `setup` for real, minus the browser and the port, and read the
        scopes off the consent URL it would have opened. --wait 0 gives up at
        once, before any token request."""
        opened = []
        out = io.StringIO()
        with patch("socketserver.TCPServer", _FakeServer), \
                patch("webbrowser.open", side_effect=opened.append), \
                redirect_stdout(out):
            self.assertEqual(ga.main(["setup", "--wait", "0", *argv]), 1)
        query = parse_qs(urlparse(opened[0]).query)
        return query["scope"][0].split(" "), out.getvalue()

    def test_scope_lists(self):
        read = list(ga.READ_SCOPES)
        self.assertEqual(ga.setup_scopes(), read)
        self.assertEqual(ga.setup_scopes(with_drive=True), read + [ga.DRIVE_SCOPE])
        self.assertEqual(ga.setup_scopes(with_compose=True, with_drive=True),
                         read + [ga.COMPOSE_SCOPE, ga.DRIVE_SCOPE])
        self.assertTrue(ga.DRIVE_SCOPE.endswith("/drive.readonly"))
        self.assertNotIn(ga.DRIVE_SCOPE, ga.READ_SCOPES)

    def test_setup_without_drive(self):
        scopes, out = self._setup_scopes_requested([])
        self.assertEqual(scopes, list(ga.READ_SCOPES))
        self.assertNotIn("drive.readonly lets", out)

    def test_setup_with_drive(self):
        scopes, out = self._setup_scopes_requested(["--with-drive"])
        self.assertEqual(scopes, list(ga.READ_SCOPES) + [ga.DRIVE_SCOPE])
        self.assertIn("drive.readonly lets the council read", out)

    def test_setup_with_drive_and_compose_keeps_both(self):
        scopes, _ = self._setup_scopes_requested(["--with-drive", "--with-compose"])
        self.assertEqual(scopes, list(ga.READ_SCOPES) + [ga.COMPOSE_SCOPE, ga.DRIVE_SCOPE])

    def test_rerun_warns_before_dropping_an_opt_in(self):
        self.token.write_text(json.dumps({"scope": f"{ga.COMPOSE_SCOPE} {ga.READ_SCOPES[0]}"}),
                              encoding="utf-8")
        _, out = self._setup_scopes_requested(["--with-drive"])
        self.assertIn("includes gmail.compose", out)
        self.assertIn("add --with-compose to keep it", out)

    def test_has_scope_reads_the_token_file(self):
        self.assertFalse(ga.has_scope(ga.DRIVE_SCOPE))  # no token file yet
        self.token.write_text("{not json", encoding="utf-8")
        self.assertFalse(ga.has_scope(ga.DRIVE_SCOPE))
        self.token.write_text(json.dumps({"scopes": [ga.DRIVE_SCOPE]}), encoding="utf-8")
        self.assertTrue(ga.has_scope(ga.DRIVE_SCOPE))
        # Google's granted list wins over what setup asked for: the consent
        # screen lets a scope be unticked.
        self.token.write_text(json.dumps({"scopes": [ga.DRIVE_SCOPE],
                                          "scope": " ".join(ga.READ_SCOPES)}), encoding="utf-8")
        self.assertFalse(ga.has_scope(ga.DRIVE_SCOPE))
        self.assertTrue(ga.has_scope(ga.READ_SCOPES[0]))

    def test_has_scope_never_prints_the_token(self):
        self.token.write_text(json.dumps({"access_token": "ya29.SECRETACCESSVALUE",
                                          "refresh_token": "1//SECRETREFRESHVALUE",
                                          "scope": ga.DRIVE_SCOPE}), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                self.assertNoLogs(level="DEBUG"):
            self.assertTrue(ga.has_scope(ga.DRIVE_SCOPE))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = io.BytesIO(body)

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class TestApiGetBytes(unittest.TestCase):
    def test_bearer_token_and_size_cap(self):
        seen = []

        def urlopen(req, timeout):
            seen.append(req)
            return _FakeResponse(b"x" * 10)

        with patch.object(ga, "access_token", return_value="ya29.SECRETACCESSVALUE"), \
                patch.object(ga.request, "urlopen", urlopen):
            self.assertEqual(ga.api_get_bytes("https://example.test/f", {"alt": "media"},
                                              max_bytes=10), b"x" * 10)
            self.assertEqual(seen[0].get_header("Authorization"), "Bearer ya29.SECRETACCESSVALUE")
            self.assertEqual(seen[0].full_url, "https://example.test/f?alt=media")
            with self.assertRaises(ga.GoogleAuthError) as ctx:
                ga.api_get_bytes("https://example.test/f", max_bytes=9)
        self.assertNotIn("SECRET", str(ctx.exception))


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
