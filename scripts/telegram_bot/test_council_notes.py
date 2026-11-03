"""Tests for the council meeting notes: rendering a day from the room log, the
summary block, and the nightly summarizer.

Everything runs in temporary folders with a fixed time zone. No network and
no model calls: call_claude_sync is a fake, and as a backstop the module swaps
it for one that fails the test.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import re
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.telegram_bot import council_notes as cn  # noqa: E402

TZ = timezone(timedelta(hours=-7), "TEST")
DAY = date(2026, 3, 10)
EM = "\u2014"

_module_patches: list = []


def setUpModule():
    _module_patches.extend([
        patch.object(cn, "LOCAL_TZ", TZ),
        patch.object(cn, "call_claude_sync", MagicMock(side_effect=AssertionError("model call in a test"))),
        patch.object(cn, "load_default_env", MagicMock()),
    ])
    for p in _module_patches:
        p.start()


def tearDownModule():
    for p in reversed(_module_patches):
        p.stop()
    _module_patches.clear()


def ts(hhmm: str, day: date = DAY) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ).timestamp()


def line(at: float, speaker: str, text: str, *, discussion=None, part="message", mode=None) -> dict:
    return {"ts": at, "chat_id": -100, "speaker": speaker, "text": text,
            "mode": mode or ("discuss" if discussion else "message"),
            "discussion": discussion, "part": part}


def discussion(start: str, question: str, turns, summary=None, steps=None, day: date = DAY):
    """Room-log lines for one /discuss starting at `start`, a minute apart."""
    t0 = ts(start, day)
    did = f"{t0:.6f}"
    rows = [line(t0, "Andy", question, discussion=did, part="question")]
    for i, (speaker, text) in enumerate(turns, start=1):
        rows.append(line(t0 + 60 * i, speaker, text, discussion=did, part="turn"))
    n = len(turns)
    if summary is not None:
        rows.append(line(t0 + 60 * (n + 1), "Yor", summary, discussion=did, part="summary"))
    if steps is not None:
        rows.append(line(t0 + 60 * (n + 2), "Luna", steps, discussion=did, part="next_steps"))
    return rows


class NotesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.room_log = root / "state" / "room.jsonl"
        self.notes = root / "vault" / "Council"
        env = patch.dict(os.environ, {cn.ROOM_LOG_ENV: str(self.room_log),
                                      cn.NOTES_DIR_ENV: str(self.notes)})
        env.start()
        self.addCleanup(env.stop)

    def write_log(self, rows, *, append=False):
        self.room_log.parent.mkdir(parents=True, exist_ok=True)
        with self.room_log.open("a" if append else "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def note(self, day: date = DAY) -> str:
        return (self.notes / f"{day.isoformat()}.md").read_text(encoding="utf-8")


def sample_day():
    return (
        [line(ts("09:00"), "Andy", "morning, what is on today?"),
         line(ts("09:01"), "Luna", "Two meetings, the first at 10."),
         line(ts("09:30"), "Andy", "thanks")]
        + discussion("14:05", "Should I take the offer now or wait for the second round?",
                     [("Yor", "Take the time.\nTwo reasons below.\n\n- one\n- two"),
                      ("Loid", "Agree, with a deadline.")],
                     summary="- Wait, with a deadline\n- Open: the number",
                     steps="Next steps\n- Andy: draft the reply, Thursday")
        + [line(ts("18:00"), "Andy", "one more thought"),
           line(ts("18:02"), "Yor", "Noted.")]
    )


class TestRender(NotesBase):
    def test_full_layout(self):
        self.write_log(sample_day())
        path = cn.render_day(DAY)
        self.assertEqual(path, self.notes / "2026-03-10.md")
        text = self.note()

        self.assertTrue(text.startswith(
            "---\ntype: council\ndate: 2026-03-10\nparticipants:\n  - Andy\n  - Yor\n  - Loid\n"
            "  - Luna\ndiscussions: 1\ntags:\n  - council\n---\n"))
        self.assertIn("\n# Council, 2026-03-10\n", text)
        self.assertIn("## Summary\n\n<!-- council-summary:start -->\n"
                      f"{cn.SUMMARY_PLACEHOLDER}\n<!-- council-summary:end -->", text)

        section = text[text.index("## 14:05 Discussion:"):text.index("## Other exchanges")]
        self.assertTrue(section.startswith(
            "## 14:05 Discussion: Should I take the offer now or wait for the second round?\n\n"
            "### Where they landed\n\n- Wait, with a deadline\n- Open: the number\n\n"
            "### Next steps\n\n- Andy: draft the reply, Thursday\n\n"))
        self.assertIn(
            "> [!note]- Transcript\n"
            "> **Andy** (14:05): Should I take the offer now or wait for the second round?\n"
            ">\n"
            "> **Yor** (14:06): Take the time.\n> Two reasons below.\n>\n> - one\n> - two\n"
            ">\n"
            "> **Loid** (14:07): Agree, with a deadline.\n",
            section)
        # The summary and next steps are shown above, not repeated in the transcript.
        self.assertNotIn("Wait, with a deadline", section[section.index("> [!note]-"):])

        self.assertTrue(text.endswith(
            "## Other exchanges\n\n"
            "> [!note]- 09:00 to 09:30\n"
            "> **Andy** (09:00): morning, what is on today?\n>\n"
            "> **Luna** (09:01): Two meetings, the first at 10.\n>\n"
            "> **Andy** (09:30): thanks\n\n"
            "> [!note]- 18:00 to 18:02\n"
            "> **Andy** (18:00): one more thought\n>\n"
            "> **Yor** (18:02): Noted.\n"))

    def test_sections_in_order(self):
        rows = sample_day() + discussion("11:00", "Earlier question", [("Loid", "L")], summary="S2")
        self.write_log(sorted(rows, key=lambda r: r["ts"]))
        cn.render_day(DAY)
        text = self.note()
        marks = [text.index(m) for m in ("# Council", "## Summary", "## 11:00 Discussion: Earlier",
                                         "## 14:05 Discussion:", "## Other exchanges")]
        self.assertEqual(marks, sorted(marks))
        self.assertIn("discussions: 2", text)

    def test_gap_splitting(self):
        self.write_log([
            line(ts("08:00"), "Andy", "a"),
            line(ts("11:00"), "Yor", "exactly three hours later stays"),
            line(ts("14:01"), "Andy", "over three hours starts a new one"),
            line(ts("14:02"), "Loid", "b"),
        ])
        cn.render_day(DAY)
        titles = re.findall(r"^> \[!note\]- (.*)$", self.note(), re.MULTILINE)
        self.assertEqual(titles, ["08:00 to 11:00", "14:01 to 14:02"])

    def test_no_other_exchanges_section_when_empty(self):
        self.write_log(discussion("10:00", "q", [("Yor", "y")], summary="s"))
        cn.render_day(DAY)
        text = self.note()
        self.assertNotIn("## Other exchanges", text)
        self.assertNotIn("### Next steps", text)  # Luna posted none

    def test_discussion_without_a_summary(self):
        self.write_log(discussion("10:00", "q", []))
        cn.render_day(DAY)
        self.assertIn("### Where they landed\n\n_No closing summary was posted._", self.note())

    def test_participants_in_fixed_order_and_only_present(self):
        self.write_log([line(ts("09:00"), "Luna", "x"), line(ts("09:01"), "Andy", "y")])
        cn.render_day(DAY)
        self.assertIn("participants:\n  - Andy\n  - Luna\ndiscussions: 0", self.note())

    def test_long_question_is_cut_for_the_heading(self):
        question = "word " * 40 + "\nsecond line"
        self.write_log(discussion("10:00", question, [("Yor", "y")]))
        cn.render_day(DAY)
        heading = re.search(r"^## 10:00 Discussion: (.*)$", self.note(), re.MULTILINE).group(1)
        self.assertTrue(heading.endswith("..."))
        self.assertLessEqual(len(heading), cn.HEADING_QUESTION_CHARS + 3)
        self.assertIn("second line", self.note())  # whole in the transcript

    def test_reply_cannot_add_a_heading_outside_a_callout(self):
        self.write_log(discussion("10:00", "q", [("Yor", "y")], summary="## Not a heading\n- ok"))
        cn.render_day(DAY)
        self.assertIn("\\## Not a heading", self.note())
        self.assertNotIn("\n## Not a heading", self.note())

    def test_no_activity_means_no_note(self):
        self.write_log([line(ts("09:00", DAY + timedelta(days=1)), "Andy", "tomorrow")])
        self.assertIsNone(cn.render_day(DAY))
        self.assertFalse(self.notes.exists())
        self.room_log.unlink()
        self.assertIsNone(cn.render_day(DAY))

    def test_malformed_lines_are_skipped(self):
        self.room_log.parent.mkdir(parents=True)
        self.room_log.write_text(
            "not json\n" + json.dumps(line(ts("09:00"), "Andy", "kept")) + "\n"
            + json.dumps({"ts": "x", "speaker": "Andy", "text": "bad ts"}) + "\n[1, 2]\n"
            + '{"ts": NaN, "speaker": "Andy", "text": "nan ts"}\n'
            + json.dumps({"ts": 1e300, "speaker": "Andy", "text": "huge ts"}) + "\n",
            encoding="utf-8")
        cn.render_day(DAY)
        self.assertIn("> **Andy** (09:00): kept", self.note())
        self.assertNotIn("bad ts", self.note())
        self.assertNotIn("nan ts", self.note())

    def test_lines_from_before_the_new_fields_are_other_exchanges(self):
        rows = [{"ts": ts("09:00"), "chat_id": 1, "speaker": "Andy", "text": "old q", "mode": "discuss"},
                {"ts": ts("09:01"), "chat_id": 1, "speaker": "Yor", "text": "old a", "mode": "discuss"}]
        self.write_log(rows)
        cn.render_day(DAY)
        self.assertIn("discussions: 0", self.note())
        self.assertIn("> **Yor** (09:01): old a", self.note())


class TestWrite(NotesBase):
    def test_private_atomic_and_idempotent(self):
        self.write_log(sample_day())
        path = cn.render_day(DAY)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.notes.iterdir()), ["2026-03-10.md"])
        first = path.read_bytes()
        with patch.object(cn, "_write_atomic", MagicMock(side_effect=AssertionError("rewrote"))):
            self.assertEqual(cn.render_day(DAY), path)  # nothing changed, nothing written
        self.assertEqual(path.read_bytes(), first)

    def test_a_failed_write_leaves_the_old_note_and_no_temp_file(self):
        self.write_log(sample_day()[:1])
        path = cn.render_day(DAY)
        before = path.read_text(encoding="utf-8")
        self.write_log(sample_day()[1:2], append=True)
        with patch.object(cn.os, "replace", MagicMock(side_effect=OSError("disk full"))):
            with self.assertRaises(OSError):
                cn.render_day(DAY)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual([p.name for p in self.notes.iterdir()], ["2026-03-10.md"])

    def test_default_folder_is_the_vault_council_folder(self):
        with patch.dict(os.environ, {cn.NOTES_DIR_ENV: ""}):
            self.assertEqual(cn.notes_dir(), Path.home() / "Documents" / "Luna Master" / "Council")


class TestSummaryBlock(NotesBase):
    def test_block_is_kept_byte_for_byte_across_renders(self):
        self.write_log(sample_day()[:3])
        path = cn.render_day(DAY)
        block = ("<!-- council-summary:start last_ts=1773158400.5 -->\n"
                 "- Decided: hand-tuned   spacing stays\n\n  - and odd indents \n"
                 "<!-- council-summary:end -->")
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(cn.existing_summary_block(text), block), encoding="utf-8")

        self.write_log([line(ts("20:00"), "Andy", "a late line")], append=True)
        cn.render_day(DAY)
        after = self.note()
        self.assertIn("\n## Summary\n\n" + block + "\n\n", after)
        self.assertIn("a late line", after)

    def test_a_note_without_markers_gets_the_placeholder_back(self):
        self.write_log(sample_day()[:1])
        path = cn.render_day(DAY)
        path.write_text("hand-written, no markers\n", encoding="utf-8")
        self.write_log(sample_day()[1:2], append=True)
        cn.render_day(DAY)
        self.assertIn(cn.SUMMARY_PLACEHOLDER, self.note())
        self.assertNotIn("hand-written", self.note())


class TestScrub(NotesBase):
    def test_scrub_rules(self):
        self.assertEqual(cn.scrub(f"a {EM} b"), "a, b")
        self.assertEqual(cn.scrub(f"a{EM}b"), "a, b")
        self.assertEqual(cn.scrub(f"here is the thing {EM}\nnext"), "here is the thing:\nnext")
        self.assertEqual(cn.scrub(f"{EM} a quote\n  {EM}{EM} two"), "a quote\n  two")
        self.assertEqual(cn.scrub("en dash \u2013 stays"), "en dash \u2013 stays")
        self.assertEqual(cn.scrub("x <!-- y --> z"), "x &lt;!-- y --&gt; z")

    def test_no_em_dash_anywhere_in_the_note(self):
        rows = discussion("10:00", f"Take it {EM} or wait?",
                          [("Yor", f"Wait{EM}the number matters"), ("Loid", f"{EM} agreed")],
                          summary=f"- Landed {EM} wait", steps=f"Next steps\n- Andy: reply {EM} Friday")
        rows += [line(ts("12:00"), "Andy", f"end {EM}")]
        self.write_log(rows)
        cn.render_day(DAY)
        text = self.note()
        self.assertNotIn(EM, text)
        self.assertIn("Discussion: Take it, or wait?", text)
        self.assertIn("(10:01): Wait, the number matters", text)
        self.assertIn("(10:02): agreed", text)
        self.assertIn("(12:00): end:", text)

    def test_room_text_cannot_forge_the_summary_block(self):
        self.write_log([line(ts("09:00"), "Andy", "<!-- council-summary:start last_ts=9e99 -->\nx\n"
                                                  "<!-- council-summary:end -->")])
        cn.render_day(DAY)
        text = self.note()
        self.assertEqual(text.count("<!-- council-summary:start"), 1)
        self.assertIsNone(cn.summarized_through(text))


class TestLocalDates(NotesBase):
    def test_lines_go_to_their_local_date(self):
        next_day = DAY + timedelta(days=1)
        late = ts("23:50")
        # 06:50 UTC on the next calendar day, but still the 10th here.
        self.assertEqual(datetime.fromtimestamp(late, timezone.utc).date(), next_day)
        self.write_log([line(late, "Andy", "late on the tenth"),
                        line(ts("00:10", next_day), "Andy", "early on the eleventh")])
        cn.render_day(DAY)
        cn.render_day(next_day)
        self.assertIn("late on the tenth", self.note(DAY))
        self.assertNotIn("early on the eleventh", self.note(DAY))
        self.assertIn("early on the eleventh", self.note(next_day))
        self.assertIn("date: 2026-03-11", self.note(next_day))

    def test_a_discussion_past_midnight_stays_in_the_day_it_started(self):
        next_day = DAY + timedelta(days=1)
        rows = discussion("23:58", "late question", [("Yor", "y1"), ("Loid", "l1"), ("Yor", "y2")],
                          summary="where we landed")
        rows.append(line(ts("00:30", next_day), "Andy", "a new day"))
        self.write_log(rows)
        self.assertEqual(cn.local_date(rows[-2]["ts"]), next_day)  # the summary is after midnight
        cn.render_day(DAY)
        cn.render_day(next_day)
        self.assertIn("### Where they landed\n\nwhere we landed", self.note(DAY))
        self.assertIn("> **Yor** (00:01): y2", self.note(DAY))
        self.assertNotIn("late question", self.note(next_day))
        self.assertIn("discussions: 0", self.note(next_day))


class SummarizeBase(NotesBase):
    def fake_model(self, reply="- Decided: wait\n- Open: the number", ok=True, err=""):
        model = MagicMock(return_value=(ok, reply, err))
        p = patch.object(cn, "call_claude_sync", model)
        p.start()
        self.addCleanup(p.stop)
        return model

    def run_quiet(self, fn, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = fn(*args, **kwargs)
        self.stdout, self.stderr = out.getvalue(), err.getvalue()
        return result


class TestSummarize(SummarizeBase):
    def test_quiet_day_makes_no_model_call(self):
        self.write_log([line(ts("09:00", DAY - timedelta(days=1)), "Andy", "the day before")])
        self.assertTrue(self.run_quiet(cn.summarize_day, DAY))  # the module backstop would raise
        self.assertFalse(self.notes.exists())
        self.assertIn("no council activity", self.stdout)

    def test_writes_bullets_and_rerenders(self):
        self.write_log(sample_day())
        model = self.fake_model(
            f"Here is the summary:\n- Decided: wait {EM} with a deadline\n* Open: the number\n"
            "1. Next (Andy): draft the reply\nThat is all.")
        self.assertTrue(self.run_quiet(cn.summarize_day, DAY))

        text = self.note()
        newest = max(r["ts"] for r in sample_day())
        self.assertIn(f"<!-- council-summary:start last_ts={newest!r} -->\n"
                      "- Decided: wait, with a deadline\n- Open: the number\n"
                      "- Next (Andy): draft the reply\n<!-- council-summary:end -->", text)
        self.assertNotIn(cn.SUMMARY_PLACEHOLDER, text)
        self.assertIn("## 14:05 Discussion:", text)  # the rest of the note is rendered too
        self.assertEqual(stat.S_IMODE((self.notes / "2026-03-10.md").stat().st_mode), 0o600)

        prompt = model.call_args.args[0]
        kwargs = model.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-haiku-5-5")
        self.assertEqual(kwargs["effort"], "low")
        self.assertIs(kwargs["no_tools"], True)
        self.assertIs(kwargs["flush"], False)
        self.assertEqual(kwargs["job"], "council_summary")
        self.assertIn("<council-day>", prompt)
        self.assertIn("14:05 Andy (opens a /discuss): Should I take the offer", prompt)
        self.assertIn("09:01 Luna: Two meetings", prompt)
        for phrase in ("3 to 6 bullets", "decisions made", "open questions", "with an owner",
                       "no new facts", "Never use em-dashes"):
            self.assertIn(phrase, prompt)

    def test_current_summary_is_not_redone_until_new_lines_arrive(self):
        self.write_log(sample_day())
        model = self.fake_model()
        self.run_quiet(cn.summarize_day, DAY)
        self.run_quiet(cn.summarize_day, DAY)
        self.assertEqual(model.call_count, 1)
        self.assertIn("summary is current", self.stdout)

        self.write_log([line(ts("21:00"), "Andy", "a late decision")], append=True)
        model.return_value = (True, "- Decided: the late one", "")
        self.run_quiet(cn.summarize_day, DAY)
        self.assertEqual(model.call_count, 2)
        self.assertIn("- Decided: the late one", self.note())
        self.assertIn(f"last_ts={ts('21:00')!r}", self.note())

    def test_dry_run_writes_nothing(self):
        self.write_log(sample_day())
        self.fake_model()
        self.assertTrue(self.run_quiet(cn.summarize_day, DAY, dry_run=True))
        self.assertFalse(self.notes.exists())
        self.assertIn("- Decided: wait", self.stdout)
        self.assertIn("not written", self.stdout)

        cn.render_day(DAY)
        before = self.note()
        self.run_quiet(cn.summarize_day, DAY, dry_run=True)
        self.assertEqual(self.note(), before)

    def test_failed_call_or_no_bullets_leaves_the_note(self):
        self.write_log(sample_day())
        cn.render_day(DAY)
        before = self.note()
        self.fake_model(ok=False, reply="", err="timeout")
        self.assertFalse(self.run_quiet(cn.summarize_day, DAY))
        self.assertIn("summary call failed: timeout", self.stderr)
        self.fake_model(reply="I could not find anything to summarize.")
        self.assertFalse(self.run_quiet(cn.summarize_day, DAY))
        self.assertEqual(self.note(), before)

    def test_bullets_are_capped_at_six(self):
        self.assertEqual(len(cn.parse_bullets("\n".join(f"- b{i}" for i in range(9)))), 6)
        self.assertEqual(cn.parse_bullets("Intro:\n• a\n2) b\n  *  c  \nprose"), ["- a", "- b", "- c"])


class TestCli(SummarizeBase):
    def test_default_dates(self):
        self.assertEqual(cn.default_summary_dates(datetime(2026, 3, 10, 23, 30, tzinfo=TZ)),
                         [date(2026, 3, 9), date(2026, 3, 10)])
        self.assertEqual(cn.default_summary_dates(datetime(2026, 3, 10, 7, 0, tzinfo=TZ)),
                         [date(2026, 3, 9)])

    def test_summarize_with_a_date(self):
        self.write_log(sample_day())
        model = self.fake_model()
        self.assertEqual(self.run_quiet(cn.main, ["summarize", "--date", "2026-03-10"]), 0)
        self.assertEqual(model.call_count, 1)
        self.assertIn("- Decided: wait", self.note())

    def test_summarize_dry_run_from_the_cli(self):
        self.write_log(sample_day())
        self.fake_model()
        self.assertEqual(self.run_quiet(cn.main, ["summarize", "--date", "2026-03-10", "--dry-run"]), 0)
        self.assertFalse(self.notes.exists())

    def test_summarize_failure_exits_nonzero(self):
        self.write_log(sample_day())
        self.fake_model(ok=False, reply="", err="boom")
        self.assertEqual(self.run_quiet(cn.main, ["summarize", "--date", "2026-03-10"]), 1)

    def test_default_run_covers_yesterday(self):
        self.write_log(sample_day())
        self.fake_model()
        with patch.object(cn, "datetime", wraps=datetime) as dt:
            dt.now.return_value = datetime(2026, 3, 11, 9, 0, tzinfo=TZ)
            self.assertEqual(self.run_quiet(cn.main, ["summarize"]), 0)
        self.assertIn("- Decided: wait", self.note(DAY))

    def test_render_command(self):
        self.write_log(sample_day())
        self.assertEqual(self.run_quiet(cn.main, ["render", "--date", "2026-03-10"]), 0)
        self.assertIn("# Council, 2026-03-10", self.note())


class TestNightlyJob(unittest.TestCase):
    LABEL = "com.andyherman.neural-bridge.council-notes"
    LAUNCHD = REPO_ROOT / "scripts" / "launchd"

    def test_plist_runs_the_summarizer_at_2330(self):
        with (self.LAUNCHD / f"{self.LABEL}.plist").open("rb") as fh:
            data = plistlib.load(fh)
        self.assertEqual(data["Label"], self.LABEL)
        self.assertEqual(data["ProgramArguments"][-3:],
                         ["-m", "scripts.telegram_bot.council_notes", "summarize"])
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 23, "Minute": 30})
        self.assertFalse(data["RunAtLoad"])
        self.assertIn(".local/bin", data["EnvironmentVariables"]["PATH"])  # claude resolves

    def test_install_and_uninstall_list_it(self):
        for script in ("install.sh", "uninstall.sh"):
            with self.subTest(script=script):
                text = (self.LAUNCHD / script).read_text(encoding="utf-8")
                agents = text[text.index("AGENTS=("):text.index(")", text.index("AGENTS=("))]
                self.assertIn(f'"{self.LABEL}"', agents)


if __name__ == "__main__":
    unittest.main()
