"""Tests for the kanban parser Luna uses as an executive assistant.

The date semantics are the whole risk here. The first version treated `@{DATE}`
as a due date and produced twenty false overdues on a twenty-two card board,
including a card due in 2028. Most of these tests exist to keep that from
coming back.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.luna import tasks as T  # noqa: E402

TODAY = date(2026, 8, 15)

BOARD = """---
kanban-plugin: board
---

## Inbox

- [ ] Old untriaged thing [[SomeNote]] @{2026-07-09}
- [ ] Submit CTP Info Request 3 by 2026-09-07 [[Playbook]] @{2026-07-13}
- [ ] Prep for the annual review by 2028-01 [[Letter]] @{2026-07-13}
- [ ] Recent addition @{2026-08-14}

## Now

- [ ] OVERDUE 17d, regulator-escalated: submit Info Request 1 [[Notes]] @{2026-07-27}
- [ ] DD01 evidence to the internal deadline @{2026-08-25}
- [ ] Undated committed thing @{2026-08-14}

## Next

- [ ] Something later by 2026-08-20 @{2026-08-01}

## Done

- [x] Finished thing @{2026-08-02}
"""


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.tasks = T.parse_board(BOARD, today=TODAY)

    def test_counts_and_done_excluded(self):
        self.assertEqual(len(self.tasks), 9)
        self.assertEqual(len(T.open_tasks(self.tasks)), 8)

    def test_lanes_assigned(self):
        lanes = {t.lane for t in self.tasks}
        self.assertEqual(lanes, {"Inbox", "Now", "Next", "Done"})

    def test_links_and_text_cleaned(self):
        t = [x for x in self.tasks if x.text.startswith("Old untriaged")][0]
        self.assertEqual(t.links, ("SomeNote",))
        self.assertNotIn("[[", t.text)
        self.assertNotIn("@{", t.text)


class TestDateSemantics(unittest.TestCase):
    """The regression suite for the false-overdue bug."""

    def setUp(self):
        self.tasks = T.parse_board(BOARD, today=TODAY)

    def _by(self, fragment):
        return [t for t in self.tasks if fragment in t.text][0]

    def test_past_trigger_is_added_not_deadline(self):
        t = self._by("Old untriaged")
        self.assertEqual(t.added, date(2026, 7, 9))
        self.assertIsNone(t.deadline)
        self.assertEqual(t.days_stale(TODAY), 37)

    def test_future_deadline_in_text_is_not_overdue(self):
        # The exact bug: "by 2028-01" was being reported as 33 days overdue.
        t = self._by("annual review")
        self.assertEqual(t.deadline, date(2028, 1, 1))
        self.assertFalse(t.is_overdue(TODAY))

    def test_in_text_deadline_parsed(self):
        t = self._by("CTP Info Request 3")
        self.assertEqual(t.deadline, date(2026, 9, 7))
        self.assertFalse(t.is_overdue(TODAY))
        self.assertEqual(t.days_overdue(TODAY), -23)

    def test_future_trigger_is_read_as_deadline(self):
        # A card cannot have been added tomorrow.
        t = self._by("DD01 evidence")
        self.assertIsNone(t.added)
        self.assertEqual(t.deadline, date(2026, 8, 25))

    def test_in_text_deadline_wins_over_trigger(self):
        t = self._by("Something later")
        self.assertEqual(t.deadline, date(2026, 8, 20))
        self.assertEqual(t.added, date(2026, 8, 1))

    def test_explicit_overdue_flag_trusted(self):
        t = self._by("Info Request 1")
        self.assertTrue(t.flagged_overdue)
        self.assertTrue(t.escalated)
        self.assertTrue(t.is_overdue(TODAY))

    def test_undated_is_never_overdue(self):
        t = self._by("Undated committed")
        self.assertIsNone(t.deadline)
        self.assertFalse(t.is_overdue(TODAY))
        self.assertIsNone(t.days_overdue(TODAY))


class TestBuckets(unittest.TestCase):
    def setUp(self):
        self.tasks = T.parse_board(BOARD, today=TODAY)

    def test_overdue_only_real_ones(self):
        od = T.overdue(self.tasks, TODAY)
        self.assertEqual(len(od), 1)
        self.assertTrue(od[0].escalated)

    def test_escalated_sorts_first(self):
        od = T.overdue(self.tasks, TODAY)
        self.assertTrue(od[0].escalated)

    def test_due_soon_window(self):
        soon = T.due_soon(self.tasks, TODAY)
        deadlines = {t.deadline for t in soon}
        self.assertIn(date(2026, 8, 20), deadlines)
        self.assertIn(date(2026, 8, 25), deadlines)
        self.assertNotIn(date(2026, 9, 7), deadlines)   # outside 14 days
        self.assertNotIn(date(2028, 1, 1), deadlines)

    def test_stale_inbox_only_inbox(self):
        stale = T.stale_inbox(self.tasks, TODAY)
        self.assertTrue(all(t.lane == "Inbox" for t in stale))
        self.assertTrue(any("Old untriaged" in t.text for t in stale))
        self.assertFalse(any("Recent addition" in t.text for t in stale))

    def test_committed_undated_excludes_inbox(self):
        und = T.committed_undated(self.tasks)
        self.assertTrue(all(t.lane in T.COMMITTED_LANES for t in und))
        self.assertTrue(any("Undated committed" in t.text for t in und))


class TestFormatting(unittest.TestCase):
    def setUp(self):
        self.tasks = T.parse_board(BOARD, today=TODAY)

    def test_report_leads_with_past_due(self):
        out = T.format_for_prompt(self.tasks, TODAY)
        self.assertTrue(out.startswith("PAST DUE"))
        self.assertIn("ESCALATED", out)

    def test_report_has_no_false_overdue(self):
        out = T.format_for_prompt(self.tasks, TODAY)
        self.assertNotIn("2028", out.split("DUE WITHIN")[0])

    def test_empty_board(self):
        self.assertEqual(T.format_for_prompt([], TODAY), "")

    def test_malformed_board_does_not_raise(self):
        self.assertEqual(T.parse_board("not a board at all"), [])
        self.assertEqual(T.parse_board(""), [])

    def test_missing_file_returns_empty(self):
        self.assertEqual(T.load_board(Path("/nonexistent/board.md")), [])


if __name__ == "__main__":
    unittest.main()
