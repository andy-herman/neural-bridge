"""Tests for the council bridge: who gets addressed, and /discuss.

No network and no model calls: the advisor invokers, the Telegram message and
bot, Honcho, and the router's claude call are all fakes.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.telegram_bot import council_bridge as cb  # noqa: E402

ANDY = 1001
CHAT = 1001  # a DM: the chat id is the user id


# ---------------------------------------------------------------- fakes

class FakeMessage:
    def __init__(self, text: str, sent: list[str], chat_id: int = CHAT):
        self.text = text
        self.chat_id = chat_id
        self._sent = sent

    async def reply_text(self, text: str, **_kw) -> None:
        self._sent.append(text)


class FakeBot:
    def __init__(self):
        self.actions: list[tuple[int, str]] = []

    async def send_chat_action(self, chat_id: int, action) -> None:
        self.actions.append((chat_id, action))


class Script:
    """A fake invoker: scripted (ok, text) replies in order, every call recorded
    in a list shared by both advisors so the speaking order can be read back.
    `gates` maps a call number (1-based, this advisor's own) to a coroutine
    function awaited before that call returns."""

    def __init__(self, agent: str, replies: list[tuple[bool, str]], calls: list[dict],
                 gates: dict | None = None, delay: float = 0.0):
        self.agent = agent
        self.replies = list(replies)
        self.calls = calls
        self.gates = gates or {}
        self.delay = delay
        self.n = 0

    async def __call__(self, text, history, directive=None):
        self.n += 1
        self.calls.append({"agent": self.agent, "text": text, "history": list(history),
                           "directive": directive})
        if self.n in self.gates:
            await self.gates[self.n]()
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.replies.pop(0) if self.replies else (True, f"{self.agent} default")


def _update(message, user_id: int = ANDY):
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=user_id))


class Gate:
    """Hold an advisor mid-turn until the test releases it."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self):
        self.started.set()
        await self.release.wait()


# ---------------------------------------------------------------- mentions

class TestMentions(unittest.IsolatedAsyncioTestCase):
    POSITIVE = {
        "@Yor_agent_bot what do you think?": ["yor"],
        "@yor thoughts?": ["yor"],
        "Yor, help me frame this": ["yor"],
        "What's Yor's take?": ["yor"],
        "YOR ARE YOU THERE": ["yor"],
        "@Loid_agent_bot prep me": ["loid"],
        "loid's read on this?": ["loid"],
        "@Yor_agent_bot and @Loid_agent_bot both": ["yor", "loid"],
        "Loid first, then Yor, then loid again": ["loid", "yor"],
    }
    NEGATIVE = ["yorick was here", "a Loidian argument", "Yorkshire tea", "the mayor said",
                "celluloid film", "nothing addressed here"]

    def test_named_advisors_positive(self):
        for text, expected in self.POSITIVE.items():
            with self.subTest(text=text):
                self.assertEqual(cb._named_advisors(text), expected)

    def test_named_advisors_negative(self):
        for text in self.NEGATIVE:
            with self.subTest(text=text):
                self.assertEqual(cb._named_advisors(text), [])

    def test_heuristic_route_uses_the_tag(self):
        # The tag wins over a career keyword that would otherwise lean Loid.
        self.assertEqual(cb._heuristic_route("@Yor_agent_bot interview prep thoughts"), ["yor"])
        self.assertEqual(cb._heuristic_route("yorick on interview prep"), ["loid"])
        self.assertEqual(cb._heuristic_route("Loidian musings"), ["yor"])

    async def test_route_honors_the_tag_without_the_llm(self):
        with patch.object(cb, "call_claude", AsyncMock(side_effect=AssertionError("router called"))):
            self.assertEqual(await cb._route("@Yor_agent_bot what now?", []), ["yor"])
            self.assertEqual(await cb._route("@Loid_agent_bot what now?", []), ["loid"])

    async def test_route_falls_to_the_llm_without_a_name(self):
        router = AsyncMock(return_value=(True, '{"order": ["loid"]}', ""))
        with patch.object(cb, "call_claude", router):
            self.assertEqual(await cb._route("yorick and the mayor", []), ["loid"])
        router.assert_awaited_once()


# ---------------------------------------------------------------- pure helpers

class TestDiscussHelpers(unittest.TestCase):
    def test_parse_default_rounds(self):
        self.assertEqual(cb._parse_discuss_args("/discuss Should I take it?"), (3, "Should I take it?"))

    def test_parse_explicit_rounds(self):
        self.assertEqual(cb._parse_discuss_args("/discuss 4 Should I take it?"), (4, "Should I take it?"))

    def test_parse_caps_and_floors(self):
        self.assertEqual(cb._parse_discuss_args("/discuss 9 q"), (6, "q"))
        self.assertEqual(cb._parse_discuss_args("/discuss 0 q"), (1, "q"))

    def test_parse_long_number_is_question(self):
        self.assertEqual(cb._parse_discuss_args("/discuss 2027 plans"), (3, "2027 plans"))

    def test_parse_command_with_bot_suffix_and_newlines(self):
        self.assertEqual(cb._parse_discuss_args("/discuss@council_bot 2 line one\nline two"),
                         (2, "line one\nline two"))

    def test_parse_empty(self):
        self.assertEqual(cb._parse_discuss_args("/discuss"), (3, ""))
        self.assertEqual(cb._parse_discuss_args("/discuss 4"), (4, ""))

    def test_split_done(self):
        self.assertEqual(cb._split_done("[DONE]"), ("", True))
        self.assertEqual(cb._split_done("  `[done]`. "), ("", True))
        self.assertEqual(cb._split_done("[PASS]"), ("", True))
        self.assertEqual(cb._split_done(""), ("", True))
        self.assertEqual(cb._split_done("Agreed, ship it. [DONE]"), ("Agreed, ship it.", True))
        self.assertEqual(cb._split_done("That is how it gets done."), ("That is how it gets done.", False))

    def test_directives(self):
        opening = cb._discussion_directive("loid", opening=True)
        self.assertIn("opening a back-and-forth with Loid", opening)
        later = cb._discussion_directive("yor", opening=False)
        self.assertIn("Answer Yor's last point directly", later)
        self.assertIn("reply exactly [DONE]", later)


# ---------------------------------------------------------------- the real invokers

class TestInvokerDirective(unittest.IsolatedAsyncioTestCase):
    HISTORY = [{"author": "Andy", "content": "q"}, {"author": "Yor", "content": "Y" * 900}]

    async def test_loid_gets_directive_and_full_room(self):
        result = SimpleNamespace(ok=True, response="fine", setup_error="", error_reason="")
        turn = AsyncMock(return_value=result)
        with patch.object(cb, "run_agent_turn", turn):
            self.assertEqual(await cb._invoke_loid("q", self.HISTORY, "DIRECTIVE-X"), (True, "fine"))
        req = turn.call_args.args[0]
        self.assertTrue(req.prompt_prefix.startswith("[COUNCIL DISCUSSION] DIRECTIVE-X"))
        self.assertIn("<council-room>", req.prompt_prefix)
        # The 900-character turn arrives whole, past the history block's cap.
        self.assertIn("Yor: " + "Y" * 900, req.prompt_prefix)

    async def test_loid_without_directive_is_unchanged(self):
        result = SimpleNamespace(ok=True, response="fine", setup_error="", error_reason="")
        turn = AsyncMock(return_value=result)
        with patch.object(cb, "run_agent_turn", turn):
            await cb._invoke_loid("q", self.HISTORY)
        self.assertEqual(turn.call_args.args[0].prompt_prefix, "")

    async def _yor_query(self, directive=None) -> str:
        proc = SimpleNamespace(returncode=0,
                               communicate=AsyncMock(return_value=(b"hello\nSession: abc", b"")))
        spawn = AsyncMock(return_value=proc)
        with patch.object(cb.asyncio, "create_subprocess_exec", spawn):
            ok, resp = await cb._invoke_yor("q", self.HISTORY, directive)
        self.assertEqual((ok, resp), (True, "hello"))
        return spawn.call_args.args[3]

    async def test_yor_gets_directive_instead_of_pass(self):
        query = await self._yor_query("DIRECTIVE-Y")
        self.assertIn("DIRECTIVE-Y", query)
        self.assertNotIn("[PASS]", query)
        self.assertTrue(query.endswith("Andy's question: q"))

    async def test_yor_normal_message_keeps_pass(self):
        query = await self._yor_query()
        self.assertIn("reply with exactly [PASS]", query)
        self.assertTrue(query.endswith("Andy's latest message: q"))

    async def test_yor_checks_her_notes_before_stating_a_thread(self):
        for directive in (None, "D"):
            with self.subTest(directive=directive):
                query = await self._yor_query(directive)
                self.assertIn("check the record in your own notes first", query)
                self.assertIn("injected summary can be a day old", query)


# ---------------------------------------------------------------- /discuss end to end

class DiscussBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cb._chat_history.clear()
        cb._discussions.clear()
        self.sent: list[str] = []
        self.calls: list[dict] = []
        self.bot = FakeBot()
        self.ctx = SimpleNamespace(bot=self.bot)
        self.honcho = patch.object(cb.honcho_client, "submit_turn")
        self.honcho_mock = self.honcho.start()
        self.env = patch.dict(os.environ, {cb.ALLOWED_USERS_ENV: str(ANDY)})
        self.env.start()
        self.typing = patch.object(cb, "TYPING_REFRESH_SECONDS", 0.01)
        self.typing.start()

    async def asyncTearDown(self):
        for p in (self.honcho, self.env, self.typing):
            p.stop()
        cb._chat_history.clear()
        cb._discussions.clear()

    def script(self, yor=(), loid=(), yor_gates=None, loid_delay=0.0):
        self.yor = Script("yor", list(yor), self.calls, yor_gates)
        self.loid = Script("loid", list(loid), self.calls, delay=loid_delay)
        p = patch.dict(cb._INVOKERS, {"yor": self.yor, "loid": self.loid})
        p.start()
        self.addCleanup(p.stop)

    async def discuss(self, text: str):
        await cb.cmd_discuss(_update(FakeMessage(text, self.sent)), self.ctx)
        return cb._discussions.get(CHAT)

    def agents(self) -> list[str]:
        return [c["agent"] for c in self.calls]


class TestDiscussFlow(DiscussBase):
    async def test_alternates_to_the_round_cap_then_yor_closes(self):
        self.script(yor=[(True, "Y1"), (True, "Y2"), (True, "SUM")],
                    loid=[(True, "L1"), (True, "L2")])
        disc = await self.discuss("/discuss 2 Should I take the offer?")
        await disc.task

        self.assertEqual(self.agents(), ["yor", "loid", "yor", "loid", "yor"])
        # Each speaker sees the room grow by one turn: question, then every reply.
        lengths = [len(c["history"]) for c in self.calls]
        self.assertEqual(lengths, [1, 2, 3, 4, 5])
        self.assertEqual(self.calls[0]["history"][-1], {"author": "Andy", "content": "Should I take the offer?"})
        self.assertEqual(self.calls[1]["history"][-1], {"author": "Yor", "content": "Y1"})
        self.assertEqual(self.calls[2]["history"][-1], {"author": "Loid", "content": "L1"})
        self.assertTrue(all(c["text"] == "Should I take the offer?" for c in self.calls))
        # The directive reaches both advisors; the opener's differs.
        self.assertIn("opening a back-and-forth with Loid", self.calls[0]["directive"])
        self.assertIn("Answer Yor's last point", self.calls[1]["directive"])
        self.assertIn("Answer Loid's last point", self.calls[2]["directive"])
        self.assertEqual(self.calls[4]["directive"], cb._CLOSING_DIRECTIVE)

        yor, loid = cb.ADVISORS["yor"]["prefix"], cb.ADVISORS["loid"]["prefix"]
        self.assertEqual(self.sent, [f"{yor}\n\nY1", f"{loid}\n\nL1", f"{yor}\n\nY2", f"{loid}\n\nL2",
                                     f"{yor}\n\nSUM", "Discussion ended: round cap reached (4 turns)."])
        # Every turn is in the room for later normal messages.
        self.assertEqual([h["content"] for h in cb._chat_history[CHAT]],
                         ["Should I take the offer?", "Y1", "L1", "Y2", "L2", "SUM"])
        self.assertNotIn(CHAT, cb._discussions)
        self.honcho_mock.assert_called_once()

    async def test_default_is_three_rounds(self):
        self.script()
        disc = await self.discuss("/discuss what next?")
        await disc.task
        self.assertEqual(len(self.calls), 7)  # 6 turns plus the summary
        self.assertEqual(self.sent[-1], "Discussion ended: round cap reached (6 turns).")

    async def test_cap_is_six_rounds(self):
        self.script()
        disc = await self.discuss("/discuss 40 what next?")
        await disc.task
        self.assertEqual(self.sent[-1], "Discussion ended: round cap reached (12 turns).")

    async def test_named_advisor_opens(self):
        self.script()
        disc = await self.discuss("/discuss 1 @Loid_agent_bot is this a leveling question, or for Yor?")
        await disc.task
        self.assertEqual(self.agents(), ["loid", "yor", "yor"])
        self.assertIn("opening a back-and-forth with Yor", self.calls[0]["directive"])

    async def test_both_done_ends_it(self):
        self.script(yor=[(True, "Y1"), (True, "[DONE]"), (True, "SUM")],
                    loid=[(True, "L1"), (True, "[DONE]")])
        disc = await self.discuss("/discuss 6 q")
        await disc.task
        self.assertEqual(self.agents(), ["yor", "loid", "yor", "loid", "yor"])
        self.assertEqual(self.sent[-1], "Discussion ended: both done (4 turns).")
        # A bare [DONE] is not posted, but the room records it.
        self.assertFalse(any("[DONE]" in s for s in self.sent))
        self.assertIn({"author": "Yor", "content": "(nothing new to add)"}, cb._chat_history[CHAT])

    async def test_done_streak_needs_both_in_a_row(self):
        self.script(yor=[(True, "[DONE]"), (True, "[DONE]"), (True, "SUM")],
                    loid=[(True, "L1"), (True, "[DONE]")])
        disc = await self.discuss("/discuss 6 q")
        await disc.task
        self.assertEqual(self.sent[-1], "Discussion ended: both done (4 turns).")

    async def test_reply_ending_in_done_is_posted_and_counts(self):
        self.script(yor=[(True, "Y1"), (True, "[DONE]"), (True, "SUM")],
                    loid=[(True, "Agreed, take it. [DONE]")])
        disc = await self.discuss("/discuss 6 q")
        await disc.task
        self.assertIn(f"{cb.ADVISORS['loid']['prefix']}\n\nAgreed, take it.", self.sent)
        self.assertEqual(self.sent[-1], "Discussion ended: both done (3 turns).")

    async def test_two_failures_in_a_row_end_it(self):
        self.script(yor=[(False, "boom")], loid=[(False, "boom")])
        disc = await self.discuss("/discuss 3 q")
        await disc.task
        self.assertEqual(self.agents(), ["yor", "loid"])  # no summary: nothing was said
        self.assertEqual(self.sent, ["_(Yor hit an error and stayed quiet.)_",
                                     "_(Loid hit an error and stayed quiet.)_",
                                     "Discussion ended: two failed turns in a row (2 turns)."])
        self.honcho_mock.assert_not_called()

    async def test_one_failure_does_not_end_it(self):
        self.script(yor=[(False, "boom"), (True, "Y1"), (True, "SUM")], loid=[(True, "L1"), (True, "L2")])
        disc = await self.discuss("/discuss 2 q")
        await disc.task
        self.assertEqual(self.sent[-1], "Discussion ended: round cap reached (4 turns).")

    async def test_logs_each_turn(self):
        self.script(yor=[(True, "Y1"), (True, "SUM")], loid=[(True, "L1")])
        with self.assertLogs("nb_telegram_council", level="INFO") as logs:
            disc = await self.discuss("/discuss 1 q")
            await disc.task
        turns = [line for line in logs.output if "DISCUSS turn" in line]
        self.assertEqual(len(turns), 3)
        self.assertRegex(turns[0], r"n=1 agent=yor reply chars=2 dur=\d+\.\ds")
        self.assertRegex(turns[2], r"n=close agent=yor reply chars=3 dur=")


class TestDiscussControl(DiscussBase):
    async def test_stop_finishes_the_in_flight_turn(self):
        gate = Gate()
        self.script(yor=[(True, "Y1"), (True, "SUM")], yor_gates={1: gate})
        disc = await self.discuss("/discuss 3 q")
        await gate.started.wait()
        await cb.cmd_stop(_update(FakeMessage("/stop", self.sent)), self.ctx)
        gate.release.set()
        await disc.task

        self.assertEqual(self.agents(), ["yor", "yor"])  # Loid never spoke; Yor closed
        self.assertEqual(self.calls[1]["directive"], cb._CLOSING_DIRECTIVE)
        self.assertEqual(self.sent[0], "Stopping after the current turn.")
        self.assertEqual(self.sent[-1], "Discussion ended: stopped by Andy (1 turn).")

    async def test_stop_with_nothing_running(self):
        self.script()
        await cb.cmd_stop(_update(FakeMessage("/stop", self.sent)), self.ctx)
        self.assertEqual(self.sent, ["No discussion is running."])

    async def test_new_message_stops_then_is_handled(self):
        gate = Gate()
        self.script(yor=[(True, "Y1"), (True, "SUM")], loid=[(True, "L-normal")], yor_gates={1: gate})
        disc = await self.discuss("/discuss 3 q")
        await gate.started.wait()

        with patch.object(cb, "_route", AsyncMock(return_value=["loid"])):
            handler = asyncio.create_task(
                cb.handle_text(_update(FakeMessage("actually, new topic", self.sent)), self.ctx))
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertEqual(disc.stop_reason, "stopped by Andy")
            self.assertFalse(handler.done())  # waits for the discussion to wrap up
            gate.release.set()
            await handler

        self.assertTrue(disc.task.done())
        loid = cb.ADVISORS["loid"]["prefix"]
        self.assertEqual(self.sent[-2:], ["Discussion ended: stopped by Andy (1 turn).",
                                          f"{loid}\n\nL-normal"])
        # Handled as a normal message: no directive, and it lands after the discussion.
        self.assertIsNone(self.calls[-1]["directive"])
        self.assertEqual(cb._chat_history[CHAT][-2:], [{"author": "Andy", "content": "actually, new topic"},
                                                       {"author": "Loid", "content": "L-normal"}])

    async def test_one_discussion_per_chat(self):
        gate = Gate()
        self.script(yor_gates={1: gate})
        disc = await self.discuss("/discuss 1 first")
        await gate.started.wait()
        await self.discuss("/discuss 1 second")
        self.assertIs(cb._discussions[CHAT], disc)
        self.assertIn("A discussion is already running. Send /stop first.", self.sent)
        disc.request_stop("stopped by Andy")
        gate.release.set()
        await disc.task
        self.assertTrue(all(c["text"] == "first" for c in self.calls))
        self.assertNotIn(CHAT, cb._discussions)

    async def test_usage_without_a_question(self):
        self.script()
        for text in ("/discuss", "/discuss 4"):
            with self.subTest(text=text):
                self.assertIsNone(await self.discuss(text))
        self.assertTrue(all(s.startswith("Usage: /discuss") for s in self.sent))
        self.assertEqual(self.calls, [])

    async def test_unauthorized_user_is_ignored(self):
        self.script()
        await cb.cmd_discuss(_update(FakeMessage("/discuss q", self.sent), user_id=999), self.ctx)
        self.assertEqual((self.sent, cb._discussions), ([], {}))

    async def test_help_mentions_discuss_and_stop(self):
        await cb.cmd_start(_update(FakeMessage("/start", self.sent)), self.ctx)
        self.assertIn("/discuss", self.sent[0])
        self.assertIn("/stop", self.sent[0])


class TestTyping(DiscussBase):
    async def test_typing_refreshes_while_an_advisor_generates(self):
        self.script(loid=[(True, "L1")], loid_delay=0.06)
        with patch.object(cb, "_route", AsyncMock(return_value=["loid"])):
            await cb.handle_text(_update(FakeMessage("@loid hi", self.sent)), self.ctx)
        during = len(self.bot.actions)
        self.assertGreaterEqual(during, 3)
        self.assertEqual({a for _, a in self.bot.actions}, {cb.ChatAction.TYPING})
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.bot.actions), during)  # stopped once the reply was posted

    async def test_typing_runs_during_discussion_turns(self):
        self.script(loid=[(True, "L1")], loid_delay=0.06)
        disc = await self.discuss("/discuss 1 q")
        await disc.task
        self.assertGreaterEqual(len(self.bot.actions), 3)
        settled = len(self.bot.actions)
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.bot.actions), settled)


if __name__ == "__main__":
    unittest.main()
