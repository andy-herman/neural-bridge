"""Tests for the council bridge: who gets addressed, /discuss, Luna's lane, and
the room log.

No network and no model calls: the advisor invokers, the Telegram message and
bot, Honcho, and the router's claude call are all fakes. As a backstop, the
module swaps the bridge's run_agent_turn for one that fails the test, and
points the room log at a temporary file, so a test that forgets a fake can
neither reach a model nor write the real log.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.telegram_bot import council_bridge as cb  # noqa: E402

ANDY = 1001
CHAT = 1001  # a DM: the chat id is the user id

_module_patches: list = []
_module_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule():
    global _module_tmp
    _module_tmp = tempfile.TemporaryDirectory()
    _module_patches.extend([
        patch.dict(os.environ, {cb.ROOM_LOG_ENV: str(Path(_module_tmp.name) / "room.jsonl")}),
        patch.object(cb, "run_agent_turn",
                     AsyncMock(side_effect=AssertionError("real agent turn in a test"))),
    ])
    for p in _module_patches:
        p.start()


def tearDownModule():
    for p in reversed(_module_patches):
        p.stop()
    _module_patches.clear()
    if _module_tmp is not None:
        _module_tmp.cleanup()


def _ok(response: str = "fine"):
    return SimpleNamespace(ok=True, response=response, setup_error="", error_reason="")


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
    in a list shared by all advisors so the speaking order can be read back.
    `gates` maps a call number (1-based, this advisor's own) to a coroutine
    function awaited before that call returns. Once the script runs out, it
    answers `default`, or "<agent> default"."""

    def __init__(self, agent: str, replies: list[tuple[bool, str]], calls: list[dict],
                 gates: dict | None = None, delay: float = 0.0,
                 default: tuple[bool, str] | None = None):
        self.agent = agent
        self.replies = list(replies)
        self.calls = calls
        self.gates = gates or {}
        self.delay = delay
        self.default = default
        self.n = 0

    async def __call__(self, text, history, directive=None):
        self.n += 1
        self.calls.append({"agent": self.agent, "text": text, "history": list(history),
                           "directive": directive})
        if self.n in self.gates:
            await self.gates[self.n]()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.replies:
            return self.replies.pop(0)
        return self.default or (True, f"{self.agent} default")


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


class TestLunaMentionsAndRouting(unittest.IsolatedAsyncioTestCase):
    POSITIVE = {
        "@Luna_assistant_bot what is tomorrow like?": ["luna"],
        "@luna am I free at 3?": ["luna"],
        "Luna, what's on my plate?": ["luna"],
        "What's Luna's read on Thursday?": ["luna"],
        "LUNA ARE YOU THERE": ["luna"],
        "@Yor_agent_bot then @Luna_assistant_bot": ["yor", "luna"],
        "luna first, then loid": ["luna", "loid"],
    }
    NEGATIVE = ["a lunar eclipse tonight", "that lunatic plan", "pure lunacy",
                "file it in Luna Master", "Lunaria seeds", "the lunas of the world"]

    def test_luna_positive(self):
        for text, expected in self.POSITIVE.items():
            with self.subTest(text=text):
                self.assertEqual(cb._named_advisors(text), expected)

    def test_luna_negative(self):
        for text in self.NEGATIVE:
            with self.subTest(text=text):
                self.assertEqual(cb._named_advisors(text), [])

    def test_heuristic_sends_logistics_to_luna(self):
        self.assertEqual(cb._heuristic_route("what's on my calendar tomorrow?"), ["luna"])
        self.assertEqual(cb._heuristic_route("anything in my inbox I owe a reply on?"), ["luna"])
        self.assertEqual(cb._heuristic_route("can we reschedule the dentist"), ["luna"])
        # Spanning lanes: career advice first, then where it fits.
        self.assertEqual(cb._heuristic_route("remind me to prep for the interview"), ["loid", "luna"])
        self.assertEqual(cb._heuristic_route("help me frame this essay"), ["yor"])
        self.assertEqual(cb._heuristic_route("a lunar calendar question"), ["luna"])  # keyword, not name

    def test_router_instruction_has_a_luna_lane(self):
        text = cb.ROUTER_INSTRUCTION
        self.assertIn("- luna:", text)
        for word in ("calendar", "inbox", "commitments", "reminders", "scheduling",
                     "what's on my plate", "logistics", "follow-ups"):
            with self.subTest(word=word):
                self.assertIn(word, text)
        self.assertIn("yor, loid and/or luna", text)

    async def test_tag_routes_to_luna_without_the_llm(self):
        with patch.object(cb, "call_claude", AsyncMock(side_effect=AssertionError("router called"))):
            self.assertEqual(await cb._route("@Luna_assistant_bot when is it?", []), ["luna"])

    async def test_llm_router_may_pick_luna(self):
        router = AsyncMock(return_value=(True, '{"order": ["luna", "nobody"]}', ""))
        with patch.object(cb, "call_claude", router):
            self.assertEqual(await cb._route("when am I free this week?", []), ["luna"])

    def test_luna_is_an_advisor_with_a_moon(self):
        self.assertEqual(cb.ADVISORS["luna"]["label"], "Luna")
        self.assertTrue(cb.ADVISORS["luna"]["prefix"].startswith("\U0001F319"))
        self.assertIs(cb._INVOKERS["luna"], cb._invoke_luna)
        self.assertNotIn("luna", cb.DISCUSSANTS)


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

    def setUp(self):
        # Keep the one-shot's working folder out of the real Application Support tree.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(cb, "YOR_CWD", Path(self._tmp.name) / "council-yor")
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.last_spawn = spawn
        return spawn.call_args.args[3]

    async def test_yor_runs_from_a_neutral_folder_with_her_dm_toolsets(self):
        await self._yor_query()
        args, kwargs = self.last_spawn.call_args.args, self.last_spawn.call_args.kwargs
        # Not the repo: a project AGENTS.md in the cwd would join her identity prompt.
        self.assertEqual(kwargs["cwd"], str(cb.YOR_CWD))
        self.assertTrue(cb.YOR_CWD.is_dir())
        self.assertNotEqual(Path(kwargs["cwd"]).resolve(), Path(cb.__file__).resolve().parents[2])
        self.assertEqual(args[args.index("-t") + 1], "file,memory,session_search,vision,search,pocket")

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
                self.assertIn("check the record first: session_search", query)
                self.assertIn("then your own notes", query)
                self.assertIn("injected summary can be a day old", query)


class TestRoomFraming(unittest.IsolatedAsyncioTestCase):
    """Yor and Loid know Luna is in the room, and that her lines are hers."""

    HISTORY = [{"author": "Andy", "content": "q"}, {"author": "Luna", "content": "You are free at 3."}]

    def test_yor_knows_luna(self):
        for directive in (None, "D"):
            with self.subTest(directive=directive):
                query = cb._yor_query("q", self.HISTORY, directive)
                self.assertIn("Luna, his executive assistant", query)
                self.assertIn("a line marked Loid or Luna is theirs", query)

    async def test_loid_knows_luna(self):
        turn = AsyncMock(return_value=_ok())
        with patch.object(cb, "run_agent_turn", turn):
            await cb._invoke_loid("q", self.HISTORY)
            normal = turn.call_args.args[0]
            await cb._invoke_loid("q", self.HISTORY, "D")
            discussing = turn.call_args.args[0]
        self.assertIn("Luna his executive assistant", normal.channel_kind)
        self.assertIn("a line marked Yor or Luna is theirs", normal.channel_kind)
        self.assertEqual(normal.prompt_prefix, "")
        self.assertIn("a line marked Yor or Luna is theirs", discussing.prompt_prefix)

    def test_yor_leaves_scheduling_to_luna_when_closing(self):
        self.assertIn("Luna follows with the next steps", cb._CLOSING_DIRECTIVE)

    async def test_help_names_luna(self):
        sent: list[str] = []
        with patch.dict(os.environ, {cb.ALLOWED_USERS_ENV: str(ANDY)}):
            await cb.cmd_start(_update(FakeMessage("/start", sent)), SimpleNamespace(bot=FakeBot()))
        self.assertIn("Luna", sent[0])
        self.assertIn("next steps", sent[0])


class TestLunaInvoker(unittest.IsolatedAsyncioTestCase):
    HISTORY = [{"author": "Andy", "content": "q"}, {"author": "Loid", "content": "L" * 900},
               {"author": "Yor", "content": "SUM"}]

    async def _request(self, directive=None):
        turn = AsyncMock(return_value=_ok("Next steps\n- Andy: prep, Thursday"))
        with patch.object(cb, "run_agent_turn", turn):
            ok, resp = await cb._invoke_luna("q", self.HISTORY, directive)
        self.assertEqual((ok, resp), (True, "Next steps\n- Andy: prep, Thursday"))
        return turn.call_args.args[0]

    async def test_request_shape(self):
        req = await self._request()
        self.assertEqual(req.agent_id, "luna")
        self.assertEqual(req.channel_kind, "COUNCIL (shared Telegram room with Andy, Yor and Loid)")
        self.assertTrue(req.stateless)
        self.assertIsNone(req.model)            # her normal model, chosen by agent_runtime
        self.assertFalse(req.owner_invoked)     # as in her DM bridge
        self.assertEqual(req.message_content, "q")
        self.assertEqual(req.history, self.HISTORY)

    async def test_normal_turn_gets_framing_and_pass(self):
        req = await self._request()
        self.assertTrue(req.prompt_prefix.startswith("[COUNCIL] You are in a shared Telegram room"))
        self.assertIn("executive-assistant lane", req.prompt_prefix)
        self.assertIn("reply with exactly [PASS]", req.prompt_prefix)
        self.assertNotIn("<council-room>", req.prompt_prefix)

    async def test_next_steps_turn_gets_directive_and_full_room(self):
        req = await self._request(cb._NEXT_STEPS_DIRECTIVE)
        self.assertIn("[COUNCIL NEXT STEPS] " + cb._NEXT_STEPS_DIRECTIVE, req.prompt_prefix)
        self.assertIn("<council-room>", req.prompt_prefix)
        self.assertIn("Loid: " + "L" * 900, req.prompt_prefix)  # whole, past the 500 cap
        self.assertIn("a line marked Yor or Loid is theirs", req.prompt_prefix)

    def test_next_steps_directive_asks_for_owner_when_calendar_and_pass(self):
        d = cb._NEXT_STEPS_DIRECTIVE
        for phrase in ("Next steps", "an owner and a when", "check his calendar",
                       "conflict", "reply exactly [PASS]"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, d)

    async def test_failure_is_reported(self):
        turn = AsyncMock(return_value=SimpleNamespace(ok=False, response="", setup_error="",
                                                      error_reason="timeout"))
        with patch.object(cb, "run_agent_turn", turn):
            self.assertEqual(await cb._invoke_luna("q", []), (False, "timeout"))


class TestLunaToolParity(unittest.IsolatedAsyncioTestCase):
    """The council hands Luna the same tools as her DM bridge: both requests go
    through the real agent_runtime, and what reaches `claude -p` must match."""

    async def _claude_kwargs(self, req) -> dict:
        from scripts.discord_bot import agent_runtime as ar
        from scripts.discord_bot.mention import MentionParts

        claude = AsyncMock(return_value=(True, "fine", ""))
        store = MagicMock()
        store.get_or_create.return_value = (SimpleNamespace(session_id="s", static_hash="",
                                                            context_tokens=0), True)
        with patch.object(ar, "call_claude", claude), \
                patch.object(ar, "build_mention_parts",
                             return_value=MentionParts(full="P", continuation="", static_hash="h")), \
                patch.object(ar, "SESSION_STORE", store), \
                patch.object(ar.claude_usage, "context_tokens", return_value=None):
            result = await ar.run_agent_turn(req)
        self.assertTrue(result.ok)
        return claude.call_args.kwargs

    async def _council_request(self):
        turn = AsyncMock(return_value=_ok())
        with patch.object(cb, "run_agent_turn", turn):
            await cb._invoke_luna("what is on today?", [])
        return turn.call_args.args[0]

    async def _dm_request(self):
        from scripts.telegram_bot import luna_bridge as lb

        turn = AsyncMock(return_value=SimpleNamespace(ok=True, response="fine", setup_error="",
                                                      error_reason="", session_id="s"))
        message = FakeMessage("what is on today?", [])
        with patch.object(lb, "run_agent_turn", turn), \
                patch.object(lb.checkin_log, "since", return_value=[]), \
                patch.object(lb.honcho_client, "submit_turn"), \
                patch.dict(os.environ, {"LUNA_TELEGRAM_ALLOWED_USERS": str(ANDY)}):
            await lb.handle_text(_update(message), SimpleNamespace(bot=FakeBot()))
        lb._chat_history.clear()
        return turn.call_args.args[0]

    async def test_same_tools_as_her_dm(self):
        from scripts.discord_bot.mention import MODEL_PER_AGENT, add_dirs_for, allowed_tools_for

        council = await self._claude_kwargs(await self._council_request())
        dm = await self._claude_kwargs(await self._dm_request())
        for key in ("agent_id", "allowed_tools", "add_dirs", "mcp_config", "model", "effort",
                    "timeout"):
            with self.subTest(key=key):
                self.assertEqual(council[key], dm[key])
        self.assertEqual(council["agent_id"], "luna")  # becomes NB_AGENT_ID for guard_bash
        self.assertEqual(council["allowed_tools"], allowed_tools_for("luna"))
        self.assertIn("Bash", council["allowed_tools"].split(","))
        self.assertEqual(council["add_dirs"], add_dirs_for("luna"))
        self.assertEqual(council["model"], MODEL_PER_AGENT["luna"])
        self.assertIsNone(council["mcp_config"])

    def test_her_bash_reaches_only_her_clis(self):
        sys.path.insert(0, str(REPO_ROOT / "hooks"))
        import guard_bash
        from scripts.discord_bot.claude_invoke import _subprocess_env

        self.assertEqual(_subprocess_env(agent_id="luna")["NB_AGENT_ID"], "luna")
        self.assertTrue(guard_bash.decide("python -m scripts.luna.calendar today", "luna")[0])
        self.assertTrue(guard_bash.decide("python -m scripts.luna.inbox unread", "luna")[0])
        self.assertFalse(guard_bash.decide("ls /tmp", "luna")[0])


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
        # Each test reads back its own room log.
        self._tmp = tempfile.TemporaryDirectory()
        self.room_log = Path(self._tmp.name) / "room.jsonl"
        self.room_env = patch.dict(os.environ, {cb.ROOM_LOG_ENV: str(self.room_log)})
        self.room_env.start()

    async def asyncTearDown(self):
        for p in (self.honcho, self.env, self.typing, self.room_env):
            p.stop()
        self._tmp.cleanup()
        cb._chat_history.clear()
        cb._discussions.clear()

    def script(self, yor=(), loid=(), luna=(), yor_gates=None, loid_delay=0.0):
        """Luna passes unless told otherwise, so a test about Yor and Loid
        sees her next-steps turn in the call order but nothing posted."""
        self.yor = Script("yor", list(yor), self.calls, yor_gates)
        self.loid = Script("loid", list(loid), self.calls, delay=loid_delay)
        self.luna = Script("luna", list(luna), self.calls, default=(True, "[PASS]"))
        p = patch.dict(cb._INVOKERS, {"yor": self.yor, "loid": self.loid, "luna": self.luna})
        p.start()
        self.addCleanup(p.stop)

    def room(self) -> list[dict]:
        if not self.room_log.exists():
            return []
        return [json.loads(line) for line in self.room_log.read_text(encoding="utf-8").splitlines()]

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

        self.assertEqual(self.agents(), ["yor", "loid", "yor", "loid", "yor", "luna"])
        # Each speaker sees the room grow by one turn: question, then every reply.
        lengths = [len(c["history"]) for c in self.calls]
        self.assertEqual(lengths, [1, 2, 3, 4, 5, 6])
        self.assertEqual(self.calls[0]["history"][-1], {"author": "Andy", "content": "Should I take the offer?"})
        self.assertEqual(self.calls[1]["history"][-1], {"author": "Yor", "content": "Y1"})
        self.assertEqual(self.calls[2]["history"][-1], {"author": "Loid", "content": "L1"})
        self.assertTrue(all(c["text"] == "Should I take the offer?" for c in self.calls))
        # The directive reaches both advisors; the opener's differs.
        self.assertIn("opening a back-and-forth with Loid", self.calls[0]["directive"])
        self.assertIn("Answer Yor's last point", self.calls[1]["directive"])
        self.assertIn("Answer Loid's last point", self.calls[2]["directive"])
        self.assertEqual(self.calls[4]["directive"], cb._CLOSING_DIRECTIVE)
        self.assertEqual(self.calls[5]["directive"], cb._NEXT_STEPS_DIRECTIVE)

        yor, loid = cb.ADVISORS["yor"]["prefix"], cb.ADVISORS["loid"]["prefix"]
        self.assertEqual(self.sent, [f"{yor}\n\nY1", f"{loid}\n\nL1", f"{yor}\n\nY2", f"{loid}\n\nL2",
                                     f"{yor}\n\nSUM", "Discussion ended: round cap reached (4 turns)."])
        # Every turn is in the room for later normal messages; Luna's pass
        # posted nothing.
        self.assertEqual([h["content"] for h in cb._chat_history[CHAT]],
                         ["Should I take the offer?", "Y1", "L1", "Y2", "L2", "SUM",
                          "(nothing new to add)"])
        self.assertNotIn(CHAT, cb._discussions)
        self.honcho_mock.assert_called_once()

    async def test_default_is_three_rounds(self):
        self.script()
        disc = await self.discuss("/discuss what next?")
        await disc.task
        self.assertEqual(len(self.calls), 8)  # 6 turns, the summary, Luna's next steps
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
        self.assertEqual(self.agents(), ["loid", "yor", "yor", "luna"])
        self.assertIn("opening a back-and-forth with Yor", self.calls[0]["directive"])

    async def test_both_done_ends_it(self):
        self.script(yor=[(True, "Y1"), (True, "[DONE]"), (True, "SUM")],
                    loid=[(True, "L1"), (True, "[DONE]")])
        disc = await self.discuss("/discuss 6 q")
        await disc.task
        self.assertEqual(self.agents(), ["yor", "loid", "yor", "loid", "yor", "luna"])
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
        self.assertEqual(len(turns), 4)
        self.assertRegex(turns[0], r"n=1 agent=yor reply chars=2 dur=\d+\.\ds")
        self.assertRegex(turns[2], r"n=close agent=yor reply chars=3 dur=")
        self.assertRegex(turns[3], r"n=next-steps agent=luna done chars=0 dur=")


class TestDiscussControl(DiscussBase):
    async def test_stop_finishes_the_in_flight_turn(self):
        gate = Gate()
        self.script(yor=[(True, "Y1"), (True, "SUM")], yor_gates={1: gate})
        disc = await self.discuss("/discuss 3 q")
        await gate.started.wait()
        await cb.cmd_stop(_update(FakeMessage("/stop", self.sent)), self.ctx)
        gate.release.set()
        await disc.task

        self.assertEqual(self.agents(), ["yor", "yor", "luna"])  # Loid never spoke; Yor closed
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


class TestLunaInTheRoom(DiscussBase):
    async def test_discuss_ends_with_yor_summary_then_luna_next_steps(self):
        steps = "Next steps\n- Andy: draft the reply, Thursday 2-3pm (free)\n- Andy: decide by Friday"
        self.script(yor=[(True, "Y1"), (True, "SUM")], loid=[(True, "L1")], luna=[(True, steps)])
        disc = await self.discuss("/discuss 1 Should I take it?")
        await disc.task

        self.assertEqual(self.agents(), ["yor", "loid", "yor", "luna"])
        luna_call = self.calls[-1]
        self.assertEqual(luna_call["directive"], cb._NEXT_STEPS_DIRECTIVE)
        self.assertEqual(luna_call["text"], "Should I take it?")
        self.assertEqual(luna_call["history"][-1], {"author": "Yor", "content": "SUM"})
        yor, luna = cb.ADVISORS["yor"]["prefix"], cb.ADVISORS["luna"]["prefix"]
        self.assertEqual(self.sent[-3:], [f"{yor}\n\nSUM", f"{luna}\n\n{steps}",
                                          "Discussion ended: round cap reached (2 turns)."])
        self.assertEqual(cb._chat_history[CHAT][-1], {"author": "Luna", "content": steps})

    async def test_luna_pass_posts_nothing(self):
        self.script(yor=[(True, "Y1"), (True, "SUM")], loid=[(True, "L1")], luna=[(True, "[PASS]")])
        disc = await self.discuss("/discuss 1 q")
        await disc.task
        self.assertEqual(self.agents(), ["yor", "loid", "yor", "luna"])
        self.assertEqual(self.sent[-2:], [f"{cb.ADVISORS['yor']['prefix']}\n\nSUM",
                                          "Discussion ended: round cap reached (2 turns)."])
        self.assertFalse(any("[PASS]" in s or s.startswith(cb.ADVISORS["luna"]["prefix"])
                             for s in self.sent))

    async def test_no_next_steps_when_nothing_was_said(self):
        self.script(yor=[(True, "[DONE]")], loid=[(True, "[DONE]")])
        disc = await self.discuss("/discuss 3 q")
        await disc.task
        self.assertEqual(self.agents(), ["yor", "loid"])

    async def test_naming_luna_does_not_put_her_in_the_rotation(self):
        self.script()
        disc = await self.discuss("/discuss 1 @Luna_assistant_bot can I fit this in?")
        await disc.task
        self.assertEqual(self.agents(), ["yor", "loid", "yor", "luna"])

    async def test_tagged_message_goes_to_luna(self):
        self.script(luna=[(True, "You are free from 2 to 4.")])
        with patch.object(cb, "call_claude", AsyncMock(side_effect=AssertionError("router called"))):
            await cb.handle_text(_update(FakeMessage("@luna when am I free?", self.sent)), self.ctx)
        self.assertEqual(self.agents(), ["luna"])
        self.assertIsNone(self.calls[0]["directive"])
        self.assertEqual(self.sent, [f"{cb.ADVISORS['luna']['prefix']}\n\nYou are free from 2 to 4."])

    async def test_routed_luna_may_pass(self):
        self.script(luna=[(True, "`[PASS]`")])
        with patch.object(cb, "_route", AsyncMock(return_value=["luna"])):
            await cb.handle_text(_update(FakeMessage("hm", self.sent)), self.ctx)
        self.assertEqual(self.sent, [])
        self.assertEqual(cb._chat_history[CHAT][-1], {"author": "Luna", "content": "(passed)"})


class TestRoomLog(DiscussBase):
    KEYS = {"ts", "chat_id", "speaker", "text", "mode"}

    async def test_normal_message_lines(self):
        self.script(loid=[(True, "L1")], luna=[(True, "Thursday is clear.")])
        with patch.object(cb, "_route", AsyncMock(return_value=["loid", "luna"])):
            await cb.handle_text(_update(FakeMessage("prep and timing?", self.sent)), self.ctx)
        rows = self.room()
        self.assertEqual([(r["speaker"], r["text"], r["mode"]) for r in rows],
                         [("Andy", "prep and timing?", "message"), ("Loid", "L1", "message"),
                          ("Luna", "Thursday is clear.", "message")])
        for r in rows:
            self.assertEqual(set(r), self.KEYS)
            self.assertIsInstance(r["ts"], float)
            self.assertEqual(r["chat_id"], CHAT)

    async def test_voice_is_logged_as_its_transcript(self):
        self.script(yor=[(True, "Y1")])
        with patch.object(cb, "_route", AsyncMock(return_value=["yor"])):
            await cb._process_message(FakeMessage("", self.sent), self.ctx, text="heard words",
                                      kind="voice")
        self.assertEqual([(r["speaker"], r["text"]) for r in self.room()],
                         [("Andy", "heard words"), ("Yor", "Y1")])

    async def test_markers_and_failures_are_not_logged(self):
        self.script(yor=[(True, "[PASS]")], loid=[(False, "boom")])
        with patch.object(cb, "_route", AsyncMock(return_value=["yor", "loid"])):
            await cb.handle_text(_update(FakeMessage("anyone?", self.sent)), self.ctx)
        self.assertEqual([r["speaker"] for r in self.room()], ["Andy"])
        self.assertEqual(self.sent, ["_(Loid hit an error and stayed quiet.)_"])

    async def test_discussion_lines(self):
        steps = "Next steps\n- Andy: decide by Friday"
        self.script(yor=[(True, "Y1"), (True, "[DONE]"), (True, "SUM")],
                    loid=[(True, "Agreed. [DONE]")], luna=[(True, steps)])
        disc = await self.discuss("/discuss 3 take it?")
        await disc.task
        self.assertEqual([(r["speaker"], r["text"], r["mode"]) for r in self.room()],
                         [("Andy", "take it?", "discuss"), ("Yor", "Y1", "discuss"),
                          ("Loid", "Agreed.", "discuss"), ("Yor", "SUM", "discuss"),
                          ("Luna", steps, "discuss")])
        # Status lines are not room speech.
        self.assertFalse(any("Discussion ended" in r["text"] for r in self.room()))

    async def test_file_is_private_and_parent_is_created(self):
        self.room_log = Path(self._tmp.name) / "nested" / "dir" / "room.jsonl"
        with patch.dict(os.environ, {cb.ROOM_LOG_ENV: str(self.room_log)}):
            cb._log_room(CHAT, "Andy", "hello", "message")
        self.assertEqual(stat.S_IMODE(self.room_log.stat().st_mode), 0o600)
        self.assertEqual(self.room()[0]["text"], "hello")

    async def test_an_existing_loose_file_is_tightened(self):
        self.room_log.write_text("", encoding="utf-8")
        os.chmod(self.room_log, 0o644)
        cb._log_room(CHAT, "Andy", "hello", "message")
        self.assertEqual(stat.S_IMODE(self.room_log.stat().st_mode), 0o600)

    async def test_default_path(self):
        with patch.dict(os.environ, {cb.ROOM_LOG_ENV: ""}):
            self.assertEqual(cb._room_log_path(), Path.home() / "Library" / "Application Support"
                             / "neural-bridge" / "council-room.jsonl")

    async def test_a_failed_write_does_not_break_the_turn(self):
        blocker = Path(self._tmp.name) / "a-file"
        blocker.write_text("x", encoding="utf-8")
        self.script(loid=[(True, "L1")])
        with patch.dict(os.environ, {cb.ROOM_LOG_ENV: str(blocker / "room.jsonl")}), \
                patch.object(cb, "_route", AsyncMock(return_value=["loid"])), \
                self.assertLogs("nb_telegram_council", level="INFO") as logs:
            await cb.handle_text(_update(FakeMessage("hi", self.sent)), self.ctx)
        self.assertEqual(self.sent, [f"{cb.ADVISORS['loid']['prefix']}\n\nL1"])
        self.assertTrue(any("ROOMLOG write failed" in line for line in logs.output))


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
