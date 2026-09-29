"""Luna's bot as the queue surface: cards, buttons, /queue, the pusher loop,
and the health check that watches it. The Telegram bot is a fake that
records calls; the store and the handlers are real."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.review_queue import health, render  # noqa: E402
from scripts.review_queue import store as st  # noqa: E402
from scripts.review_queue.surface import QueueSurface  # noqa: E402

ANDY = 111
STRANGER = 999
T0 = 1_800_000_000


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self._next_id = 100
        self.fail_sends = False

    async def send_message(self, **kw):
        if self.fail_sends:
            from telegram.error import NetworkError
            raise NetworkError("offline")
        self._next_id += 1
        self.sent.append(kw)
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, **kw):
        self.edits.append(kw)

    @staticmethod
    def buttons(markup) -> list[str]:
        if markup is None:
            return []
        return [b.callback_data for row in markup.inline_keyboard for b in row]


class FakeQuery:
    def __init__(self, user: int, data: str, chat_id: int = ANDY, message_id: int = 101):
        self.from_user = SimpleNamespace(id=user)
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None):
        self.answers.append(text)


class FakeMessage:
    def __init__(self):
        self.replies: list[dict] = []

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, **kw})


class SurfaceCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.store = st.Store(tmp / "q.db")
        self.status = tmp / "status.json"
        self.applied: list[str] = []
        self.bot = FakeBot()
        self.logs: list[str] = []

        def apply_capture(item):
            self.applied.append(f"{item.id}:{item.verb}")
            return True, "https://github.com/example/nb/pull/1"

        self.surface = QueueSurface(
            store=self.store, repo=tmp, chat_ids=lambda: {ANDY}, log=self.logs.append,
            push_kinds=(st.CAPTURE,),
            appliers={st.CAPTURE: apply_capture, st.ALERT: lambda item: (True, "")},
            capture_sync=lambda: ([], []))
        self.context = SimpleNamespace(bot=self.bot)
        self._status_env = health.STATUS_ENV
        import os
        self._saved_status = os.environ.get(self._status_env)
        os.environ[self._status_env] = str(self.status)

    def tearDown(self):
        import os
        if self._saved_status is None:
            os.environ.pop(self._status_env, None)
        else:
            os.environ[self._status_env] = self._saved_status
        self._tmp.cleanup()

    def capture(self, key="c1", title="idor-still-a01"):
        item, _ = self.store.raise_item(source="compile_quarantine", key=key, kind=st.CAPTURE,
                                        title=title, detail="Held by the filing gate.",
                                        ref="https://github.com/example/nb/blob/x/q.md",
                                        agent="teaching-prep", unique=True)
        return item

    def alert(self, key="a1"):
        item, _ = self.store.raise_item(source="output_watchdog", key=key, kind=st.ALERT,
                                        title="Weekly deep dive overdue")
        return item

    async def press(self, data: str, user: int = ANDY, message_id: int = 101) -> FakeQuery:
        query = FakeQuery(user, data, message_id=message_id)
        await self.surface.on_callback(SimpleNamespace(callback_query=query), self.context)
        # Let the apply the press kicked off run to completion.
        await self.surface.apply_pending()
        return query


class TestPusher(SurfaceCase):
    async def test_tick_sends_pushable_items_once_and_writes_a_heartbeat(self):
        cap = self.capture()
        self.alert()
        await self.surface.tick(self.bot, now=T0)
        await self.surface.tick(self.bot, now=T0 + 15)
        self.assertEqual(len(self.bot.sent), 1, "alerts stay quiet during the shadow week")
        sent = self.bot.sent[0]
        self.assertEqual(sent["chat_id"], ANDY)
        self.assertIn("idor-still-a01", sent["text"])
        self.assertIn(cap.id, sent["text"])
        self.assertEqual(FakeBot.buttons(sent["reply_markup"]),
                         [f"q:{cap.id}:approve", f"q:{cap.id}:reject", f"q:{cap.id}:snooze"])
        status = json.loads(self.status.read_text())
        self.assertEqual(status["heartbeat"], T0 + 15)
        self.assertIsNone(status["oldest_unsent_age"])
        self.assertEqual(self.store.meta_get(health.HEARTBEAT_KEY), str(T0 + 15))

    async def test_a_failed_send_is_retried_and_counted_as_unsent(self):
        self.capture()
        self.bot.fail_sends = True
        await self.surface.tick(self.bot, now=T0)
        self.assertEqual(self.bot.sent, [])
        self.assertIsNotNone(json.loads(self.status.read_text())["oldest_unsent_age"])
        self.bot.fail_sends = False
        await self.surface.tick(self.bot, now=T0 + 15)
        self.assertEqual(len(self.bot.sent), 1)

    async def test_a_cleared_item_has_its_card_redrawn_without_buttons(self):
        cap = self.capture()
        await self.surface.tick(self.bot, now=T0)
        self.store.sync(source="compile_quarantine", kind=st.CAPTURE, entries=[], unique=True,
                        now=T0 + 30)
        await self.surface.tick(self.bot, now=T0 + 45)
        edit = self.bot.edits[-1]
        self.assertIn("Nothing to do", edit["text"])
        self.assertIsNone(edit["reply_markup"])
        self.assertEqual(self.store.open_renders(cap.id), [], "a finished card is closed")

    async def test_capture_sync_failure_is_recorded_for_the_health_check(self):
        def broken():
            raise RuntimeError("git ls-tree failed")
        self.surface._capture_sync = broken
        await self.surface.tick(self.bot, now=T0)
        self.assertTrue(self.store.meta_get("capture_sync").startswith("error"))
        result = health.check(self.store, (st.CAPTURE,), now=T0 + 1)
        self.assertTrue(any("quarantine sync failed" in p for p in result["problems"]))


class TestButtons(SurfaceCase):
    async def asyncSetUp(self):
        self.cap = self.capture()
        await self.surface.tick(self.bot, now=T0)
        self.message_id = 101

    async def test_filing_takes_two_taps_then_applies_and_redraws(self):
        q = await self.press(f"q:{self.cap.id}:approve")
        self.assertEqual(self.store.get(self.cap.id).state, st.OPEN, "one tap decides nothing")
        prompt = self.bot.edits[-1]
        self.assertIn("File this into the wiki?", prompt["text"])
        self.assertEqual(FakeBot.buttons(prompt["reply_markup"]),
                         [f"q:{self.cap.id}:approve!", f"q:{self.cap.id}:show"])

        q = await self.press(f"q:{self.cap.id}:approve!")
        self.assertEqual(q.answers, ["Recorded."])
        self.assertEqual(self.applied, [f"{self.cap.id}:approve"])
        item = self.store.get(self.cap.id)
        self.assertEqual((item.state, item.verb), (st.APPLIED, st.APPROVE))
        self.assertEqual(item.result, "https://github.com/example/nb/pull/1")
        await self.surface.tick(self.bot, now=T0 + 60)
        final = self.bot.edits[-1]
        self.assertIn("Filed", final["text"])
        self.assertIn("pull/1", final["text"])
        self.assertIsNone(final["reply_markup"])

    async def test_cancel_restores_the_card(self):
        await self.press(f"q:{self.cap.id}:reject")
        await self.press(f"q:{self.cap.id}:show")
        self.assertEqual(FakeBot.buttons(self.bot.edits[-1]["reply_markup"])[0],
                         f"q:{self.cap.id}:approve")
        self.assertEqual(self.store.get(self.cap.id).state, st.OPEN)

    async def test_a_second_decision_is_refused(self):
        await self.press(f"q:{self.cap.id}:reject!")
        q = await self.press(f"q:{self.cap.id}:approve!")
        self.assertEqual(q.answers, ["Already handled."])
        self.assertEqual(self.store.get(self.cap.id).verb, st.REJECT)
        self.assertEqual(self.applied, [f"{self.cap.id}:reject"])

    async def test_strangers_cannot_press_buttons(self):
        q = await self.press(f"q:{self.cap.id}:approve!", user=STRANGER)
        self.assertEqual(self.store.get(self.cap.id).state, st.OPEN)
        self.assertEqual(q.answers, [None])
        self.assertTrue(any("unauthorized" in line for line in self.logs))

    async def test_snooze_hides_the_card_until_it_is_due(self):
        q = await self.press(f"q:{self.cap.id}:snooze")
        self.assertEqual(q.answers, ["Snoozed for a day."])
        self.assertEqual(self.store.get(self.cap.id).state, st.SNOOZED)
        self.assertIn("Snoozed until", self.bot.edits[-1]["text"])
        self.assertEqual(self.store.open_renders(self.cap.id), [])

    async def test_failed_apply_offers_retry(self):
        self.surface.appliers[st.CAPTURE] = lambda item: (False, "PR open but not merged")
        await self.press(f"q:{self.cap.id}:approve!")
        await self.surface.tick(self.bot, now=T0 + 60)
        card = self.bot.edits[-1]
        self.assertIn("did not go through", card["text"])
        self.assertEqual(FakeBot.buttons(card["reply_markup"]), [f"q:{self.cap.id}:retry"])
        self.surface.appliers[st.CAPTURE] = lambda item: (True, "https://github.com/example/nb/pull/2")
        await self.press(f"q:{self.cap.id}:retry")
        self.assertEqual(self.store.get(self.cap.id).state, st.APPLIED)

    async def test_alerts_acknowledge_in_one_tap(self):
        alert = self.alert()
        await self.press(f"q:{alert.id}:ack")
        item = self.store.get(alert.id)
        self.assertEqual((item.state, item.verb), (st.APPLIED, st.ACKNOWLEDGE))

    async def test_foreign_callback_data_is_ignored(self):
        q = await self.press("something-else")
        self.assertEqual(q.answers, [None])
        self.assertEqual(self.store.get(self.cap.id).state, st.OPEN)


class TestQueueCommand(SurfaceCase):
    async def test_lists_everything_waiting_with_a_button_each(self):
        cap = self.capture()
        alert = self.alert()
        message = FakeMessage()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=ANDY), message=message)
        await self.surface.cmd_queue(update, self.context)
        reply = message.replies[0]
        self.assertIn("2 waiting", reply["text"])
        self.assertIn("Weekly deep dive overdue", reply["text"], "shadow alerts are listed")
        self.assertEqual(FakeBot.buttons(reply["reply_markup"]),
                         [f"q:{cap.id}:open", f"q:{alert.id}:open"])

        await self.press(f"q:{alert.id}:open")
        self.assertIn("Weekly deep dive overdue", self.bot.sent[-1]["text"])
        self.assertEqual(len(self.store.open_renders(alert.id)), 1, "an opened card is tracked")

    async def test_strangers_get_nothing(self):
        self.capture()
        message = FakeMessage()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=STRANGER), message=message)
        await self.surface.cmd_queue(update, self.context)
        self.assertEqual(message.replies, [])


class TestRender(unittest.TestCase):
    def item(self, **kw) -> st.Item:
        base = dict(id="abc123", kind=st.CAPTURE, source="compile_quarantine", key="k",
                    agent="teaching-prep", title="a <b> & c", detail="d", ref="https://x/y",
                    created_at=T0, updated_at=T0)
        base.update(kw)
        return st.Item(**base)

    def test_callback_round_trip_and_rejects(self):
        self.assertEqual(render.parse_callback("q:abc123:approve!"), ("abc123", "approve!"))
        for bad in ("q:abc123:explode", "q:ABC123:ack", "x:abc123:ack", "", None):
            self.assertIsNone(render.parse_callback(bad))
        self.assertLessEqual(len(render.callback("abc123", "approve!").encode()), 64)

    def test_card_escapes_titles_and_carries_no_body_text(self):
        text = render.card_text(self.item())
        self.assertIn("a &lt;b&gt; &amp; c", text)
        self.assertIn('<a href="https://x/y">Read it</a>', text)
        self.assertIn("<code>abc123</code>", text)

    def test_non_https_refs_are_not_linked(self):
        self.assertNotIn("<a ", render.card_text(self.item(ref="/Users/someone/vault/note.md")))

    def test_finished_items_have_no_buttons(self):
        for state in (st.APPLIED, st.SUPERSEDED, st.SNOOZED, st.DECIDED, st.APPLYING):
            self.assertEqual(render.keyboard(self.item(state=state)), [], state)


class TestHealth(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = st.Store(Path(self._tmp.name) / "q.db")

    def tearDown(self):
        self._tmp.cleanup()

    def test_healthy_when_the_probe_round_trips_and_the_pusher_is_fresh(self):
        self.store.meta_set(health.HEARTBEAT_KEY, str(T0))
        result = health.check(self.store, (st.CAPTURE,), now=T0 + 60)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.store.items(), [], "probes stay out of Andy's list")

    def test_a_missing_or_stale_heartbeat_is_a_problem(self):
        self.assertIn("no heartbeat", " ".join(health.check(self.store, (), now=T0)["problems"]))
        self.store.meta_set(health.HEARTBEAT_KEY, str(T0))
        stale = health.check(self.store, (), now=T0 + health.HEARTBEAT_MAX_AGE + 60)
        self.assertIn("last heartbeat", " ".join(stale["problems"]))

    def test_unsent_backlog_and_failed_applies_are_named(self):
        self.store.meta_set(health.HEARTBEAT_KEY, str(T0 + 3600))
        item, _ = self.store.raise_item(source="s", key="k", kind=st.CAPTURE, title="t", now=T0)
        other, _ = self.store.raise_item(source="s", key="k2", kind=st.ALERT, title="t2", now=T0)
        self.store.decide(other.id, st.ACKNOWLEDGE, actor="t")
        self.store.claim(other.id, actor="t")
        self.store.finish(other.id, False, "gh exploded", actor="t")
        result = health.check(self.store, (st.CAPTURE,), now=T0 + 3600)
        text = " ".join(result["problems"])
        self.assertIn("never sent", text)
        self.assertIn(f"{other.id} (gh exploded)", text)
        self.assertFalse(result["ok"])

    def test_push_kinds_default_to_captures_until_the_shadow_week_ends(self):
        import os
        saved = os.environ.pop(health.PUSH_KINDS_ENV, None)
        try:
            self.assertEqual(health.push_kinds(), (st.CAPTURE,))
            os.environ[health.PUSH_KINDS_ENV] = "capture, alert, bogus"
            self.assertEqual(health.push_kinds(), (st.CAPTURE, st.ALERT))
        finally:
            os.environ.pop(health.PUSH_KINDS_ENV, None)
            if saved is not None:
                os.environ[health.PUSH_KINDS_ENV] = saved


if __name__ == "__main__":
    unittest.main()
