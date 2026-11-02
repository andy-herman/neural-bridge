"""A flush sends only the new part of a session, as conversation
(hooks/transcript_delta.py, 2026-10-20)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(HOOKS.parent))

import flush  # noqa: E402
import transcript_delta as delta  # noqa: E402


def rec(kind: str, content, **extra) -> str:
    return json.dumps({"type": kind, "message": {"role": kind, "content": content},
                       "timestamp": "2026-10-20T02:10:09.333Z", **extra}) + "\n"


MENTION = ("## Your role definition\n\n" + "charter " * 2000 + "\n\n## Andy's mention\n\n"
           "<message>\nThe venue form is done, all three parts.\n</message>\n\n## Tools you have\n...")


class TestCondense(unittest.TestCase):
    def test_keeps_conversation_and_drops_bookkeeping(self):
        jsonl = "".join([
            json.dumps({"type": "file-history-snapshot", "snapshot": "x" * 5000}) + "\n",
            json.dumps({"type": "queue-operation", "operation": "enqueue"}) + "\n",
            json.dumps({"type": "system", "content": "hook ran"}) + "\n",
            rec("user", "<command-name>/clear</command-name>", isMeta=True),
            rec("user", MENTION),
            rec("assistant", [{"type": "thinking", "thinking": "secret"},
                              {"type": "tool_use", "name": "Bash",
                               "input": {"command": "python -m scripts.luna.tasks close venue form"}}]),
            rec("user", [{"type": "tool_result", "content": "Closed: the venue form (moved from Now to Done)."}]),
            rec("assistant", [{"type": "text", "text": "Closed all three."}]),
            rec("assistant", [{"type": "text", "text": "Sidechain chatter."}], isSidechain=True),
            "not json\n",
        ])
        turns = delta.condense(jsonl)
        text = "\n\n".join(turns)
        self.assertEqual(len(turns), 4)
        self.assertTrue(turns[0].startswith("USER (2026-10-20T02:10:09):\nThe venue form is done"))
        self.assertNotIn("charter", text, "injected context is dropped, the message kept")
        self.assertIn('[tool call: Bash {"command": "python -m scripts.luna.tasks close venue form"}]', text)
        self.assertIn("[tool result: Closed: the venue form", text)
        self.assertIn("Closed all three.", text)
        for gone in ("snapshot", "enqueue", "hook ran", "/clear", "secret", "Sidechain"):
            self.assertNotIn(gone, text)

    def test_consecutive_records_of_one_role_join(self):
        turns = delta.condense(rec("assistant", "one") + rec("assistant", "two") + rec("user", "three"))
        self.assertEqual([t.split(":", 1)[0].split(" ")[0] for t in turns], ["ASSISTANT", "USER"])
        self.assertIn("one\ntwo", turns[0])

    def test_long_tool_results_are_cut(self):
        turns = delta.condense(rec("user", [{"type": "tool_result", "content": [{"type": "text", "text": "y" * 5000}]}]))
        self.assertLess(len(turns[0]), delta.TOOL_RESULT_CHARS + 80)

    def test_lead_in_keeps_the_newest_whole_turns(self):
        turns = [f"USER (t):\n{'a' * 100} {i}" for i in range(200)]
        lead = delta.lead_in(turns, limit=1000)
        self.assertIn(" 199", lead)
        self.assertNotIn(" 150", lead)
        self.assertLessEqual(len(lead), 1000)
        self.assertTrue(delta.lead_in(["x" * 5000], limit=100).startswith("…"))

    def test_render(self):
        self.assertEqual(delta.render("NEW", [], None), "NEW", "a first flush is just the conversation")
        out = delta.render("NEW", ["USER (t):\nold"], ["Which date?"])
        self.assertLess(out.index("ALREADY FLUSHED"), out.index("old"))
        self.assertLess(out.index("old"), out.index("- Which date?"))
        self.assertLess(out.index("- Which date?"), out.index("NEW SINCE THE LAST FLUSH"))
        self.assertTrue(out.endswith("NEW"))


class TestOffsets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "offsets"

    def tearDown(self):
        self.tmp.cleanup()

    def test_claims_follow_each_other(self):
        self.assertEqual(delta.claim("s", 100, self.dir), (0, 100))
        self.assertEqual(delta.claim("s", 250, self.dir), (100, 250))
        self.assertEqual(delta.claim("other", 50, self.dir), (0, 50))

    def test_release_hands_back_unless_claimed_past(self):
        delta.claim("s", 100, self.dir)
        start, end = delta.claim("s", 200, self.dir)
        delta.release("s", start, end, self.dir)
        self.assertEqual(delta.claim("s", 200, self.dir), (100, 200), "a failed flush is retried")
        start, end = delta.claim("s", 300, self.dir)
        delta.claim("s", 400, self.dir)  # a later flush took 300..400
        delta.release("s", start, end, self.dir)
        self.assertEqual(delta.claim("s", 400, self.dir), (400, 400), "a later claim is never undone")

    def test_a_rewritten_shorter_file_starts_over(self):
        delta.claim("s", 1000, self.dir)
        self.assertEqual(delta.claim("s", 300, self.dir), (0, 300))

    def test_unreadable_store_means_a_full_flush(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("x")
        self.assertEqual(delta.claim("s", 100, blocker / "sub"), (0, 100))

    def test_corrupt_offset_file_starts_over(self):
        self.dir.mkdir(parents=True)
        (self.dir / "s.json").write_text("{not json")
        self.assertEqual(delta.claim("s", 100, self.dir), (0, 100))

    def test_session_ids_are_made_safe_for_file_names(self):
        delta.claim("../../etc/passwd", 10, self.dir)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["______etc_passwd.json"])

    def test_prune_drops_old_offsets(self):
        delta.claim("old", 10, self.dir)
        delta.claim("new", 10, self.dir)
        old = self.dir / "old.json"
        stamp = time.time() - delta.OFFSET_TTL_SECONDS - 60
        os.utime(old, (stamp, stamp))
        delta.prune(self.dir)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["new.json"])

    def test_a_partial_last_record_waits(self):
        self.assertEqual(delta.complete_lines(b'{"a":1}\n{"b":'), 8)
        self.assertEqual(delta.complete_lines(b"no newline"), 0)


class TestSeed(unittest.TestCase):
    def test_seeds_recent_transcripts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, offsets = Path(tmp) / "projects", Path(tmp) / "offsets"
            (root / "p").mkdir(parents=True)
            fresh, old, partial = (root / "p" / f"{n}.jsonl" for n in ("fresh", "old", "partial"))
            fresh.write_text(rec("user", "a") + rec("assistant", "b"))
            old.write_text(rec("user", "a"))
            stamp = time.time() - delta.OFFSET_TTL_SECONDS - 60
            os.utime(old, (stamp, stamp))
            partial.write_text(rec("user", "a") + '{"type": "user", "mess')
            self.assertEqual(delta.seed(root, offsets), 2)
            self.assertEqual(delta.claim("fresh", fresh.stat().st_size, offsets)[0], fresh.stat().st_size)
            self.assertEqual(delta.claim("partial", 999, offsets)[0], len(rec("user", "a").encode()))
            self.assertFalse((offsets / "old.json").exists())
            (root / "p" / "new.jsonl").write_text(rec("user", "c"))
            self.assertEqual(delta.seed(root, offsets), 1, "only sessions without an offset")


class TestResumedSessionFlush(unittest.TestCase):
    """Two turns of one resumed session: the second flush sends only its turn."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        for target, value in (("flush.DAILY_LOGS_DIR", tmp / "daily-logs"),
                              ("flush.QUEUE_LOG", tmp / "daily-logs" / "_queue.log"),
                              ("transcript_delta.OFFSETS_DIR", tmp / "offsets"),
                              ("scripts.discord_bot.progress_log.AGENTS_BASE", tmp / "no-vault")):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.transcript = tmp / "s.jsonl"
        self.prompts: list[str] = []
        self.queued: list[dict] = []

    def tearDown(self):
        self.tmp.cleanup()

    def flush_once(self, reply: dict | None, questions=("Which date?",)) -> int:
        def runner(argv, **kw):
            prompt = kw["input"]
            self.prompts.append(prompt[prompt.index("<transcript>\n"):prompt.rindex("</transcript>")])

            class R:
                returncode = 0 if reply is not None else 1
                stdout = json.dumps(reply) if reply is not None else "boom"
                stderr = ""
            return R()

        argv = ["flush.py", "--agent", "luna", "--session-id", "s1", "--transcript", str(self.transcript),
                "--no-discord"]
        with patch.object(sys, "argv", argv), patch("flush.subprocess.run", side_effect=runner), \
                patch("flush._earlier_questions", return_value=list(questions)), \
                patch("flush._queue_questions",
                      side_effect=lambda *a, **kw: self.queued.append(kw) or (0, "queued")):
            return flush.main()

    def test_second_turn_sends_only_itself(self):
        reply = {"decisions": ["Closed it"], "findings": [], "open_questions": [], "proposed_concepts": []}
        self.transcript.write_text(rec("user", "first turn") + rec("assistant", "first reply"))
        self.flush_once(reply)
        self.assertNotIn("ALREADY FLUSHED", self.prompts[0])
        self.assertIn("first turn", self.prompts[0])

        with self.transcript.open("a") as f:
            f.write(json.dumps({"type": "file-history-snapshot", "snapshot": "z" * 9000}) + "\n")
            f.write(rec("user", MENTION) + rec("assistant", "Closed all three."))
        self.flush_once(reply)
        second = self.prompts[1]
        new_part = second[second.index("NEW SINCE THE LAST FLUSH"):]
        self.assertIn("The venue form is done", new_part)
        self.assertNotIn("first turn", new_part)
        self.assertIn("first turn", second[:second.index("NEW SINCE")], "old turns are context only")
        self.assertIn("- Which date?", second)
        self.assertNotIn("zzzz", second)
        self.assertNotIn("charter", second)
        self.assertEqual(self.queued[-1], {"complete": True},
                         "the model saw every open question, so settled ones may clear")

    def test_unknown_earlier_questions_clear_nothing(self):
        reply = {"decisions": ["X"], "findings": [], "open_questions": [], "proposed_concepts": []}
        self.transcript.write_text(rec("user", "one"))
        self.flush_once(reply)
        with self.transcript.open("a") as f:
            f.write(rec("user", "two"))
        with patch("flush._earlier_questions", return_value=None):
            def runner(argv, **kw):
                class R:  # noqa: N801
                    returncode, stdout, stderr = 0, json.dumps(reply), ""
                return R()
            argv = ["flush.py", "--agent", "luna", "--session-id", "s1", "--transcript",
                    str(self.transcript), "--no-discord"]
            with patch.object(sys, "argv", argv), patch("flush.subprocess.run", side_effect=runner), \
                    patch("flush._queue_questions",
                          side_effect=lambda *a, **kw: self.queued.append(kw) or (0, "queued")):
                flush.main()
        self.assertEqual(self.queued[-1], {"complete": False})

    def test_a_failed_flush_is_retried_by_the_next(self):
        ok = {"decisions": ["X"], "findings": [], "open_questions": [], "proposed_concepts": []}
        self.transcript.write_text(rec("user", "one"))
        self.flush_once(ok)
        with self.transcript.open("a") as f:
            f.write(rec("user", "two"))
        self.flush_once(None)  # both attempts fail
        with self.transcript.open("a") as f:
            f.write(rec("user", "three"))
        self.flush_once(ok)
        last = self.prompts[-1]
        new_part = last[last.index("NEW SINCE THE LAST FLUSH"):]
        self.assertIn("two", new_part)
        self.assertIn("three", new_part)

    def test_only_bookkeeping_since_the_last_flush_costs_no_call(self):
        ok = {"decisions": ["X"], "findings": [], "open_questions": [], "proposed_concepts": []}
        self.transcript.write_text(rec("user", "one"))
        self.flush_once(ok)
        with self.transcript.open("a") as f:
            f.write(json.dumps({"type": "queue-operation", "operation": "dequeue"}) + "\n")
        self.flush_once(ok)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("skipped:no_new_turns", flush.QUEUE_LOG.read_text())


if __name__ == "__main__":
    unittest.main()
