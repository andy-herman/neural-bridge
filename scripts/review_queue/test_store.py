"""The review queue store: idempotent producers, race-safe decisions, and an
event log complete enough to measure Phase 1 by."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.review_queue import store as st  # noqa: E402

T0 = 1_800_000_000


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = st.Store(Path(self._tmp.name) / "q.db")

    def tearDown(self):
        self._tmp.cleanup()

    def alert(self, key="k1", now=T0, **kw):
        return self.store.raise_item(source="watchdog", key=key, kind=st.ALERT,
                                     title=kw.pop("title", f"{key} overdue"), now=now, **kw)


class TestProducers(StoreCase):
    def test_raise_creates_once_and_logs_the_creation(self):
        item, created = self.alert()
        again, created_again = self.alert(now=T0 + 3600)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(item.id, again.id)
        self.assertEqual(item.state, st.OPEN)
        self.assertEqual([e["event"] for e in self.store.events(item.id)], ["created"])

    def test_repeat_raise_refreshes_detail_without_an_event(self):
        item, _ = self.alert(detail="9 days")
        self.alert(detail="10 days", now=T0 + 86400)
        self.assertEqual(self.store.get(item.id).detail, "10 days")
        self.assertEqual(len(self.store.events(item.id)), 1)

    def test_clear_supersedes_an_undecided_incident_and_frees_the_key(self):
        first, _ = self.alert()
        cleared = self.store.clear(source="watchdog", key="k1", now=T0 + 60)
        self.assertEqual(cleared.state, st.SUPERSEDED)
        self.assertIsNotNone(cleared.resolved_at)
        second, created = self.alert(now=T0 + 120)
        self.assertTrue(created, "the same condition coming back is a new incident")
        self.assertNotEqual(first.id, second.id)

    def test_clear_keeps_a_decision_andy_already_made(self):
        item, _ = self.alert()
        self.store.decide(item.id, st.ACKNOWLEDGE, actor="t")
        self.store.claim(item.id, actor="t")
        self.store.finish(item.id, True, "", actor="t")
        cleared = self.store.clear(source="watchdog", key="k1")
        self.assertEqual(cleared.state, st.APPLIED)
        self.assertIsNotNone(cleared.resolved_at)

    def test_acknowledged_alert_is_not_raised_again_while_it_persists(self):
        item, _ = self.alert()
        self.store.decide(item.id, st.ACKNOWLEDGE, actor="t")
        _, created = self.alert(now=T0 + 3600)
        self.assertFalse(created, "an acknowledged, still-live alert must not re-page Andy")

    def test_unique_items_never_come_back_once_resolved(self):
        kw = dict(source="compile_quarantine", key="slug@abc", kind=st.CAPTURE, title="slug", unique=True)
        item, _ = self.store.raise_item(**kw)
        self.store.decide(item.id, st.APPROVE, actor="t")
        self.store.claim(item.id, actor="t")
        self.store.finish(item.id, True, "https://x/pr/1", actor="t", resolve=True)
        again, created = self.store.raise_item(**kw)
        self.assertFalse(created, "a stale checkout still showing the file must not re-queue it")
        self.assertEqual(again.id, item.id)

    def test_sync_raises_listed_and_clears_the_rest(self):
        self.store.sync(source="watchdog", kind=st.ALERT, entries=[
            {"key": "a", "title": "A"}, {"key": "b", "title": "B"}], now=T0)
        created, cleared = self.store.sync(source="watchdog", kind=st.ALERT, entries=[
            {"key": "b", "title": "B"}, {"key": "c", "title": "C"}], now=T0 + 60)
        self.assertEqual([i.key for i in created], ["c"])
        self.assertEqual([i.key for i in cleared], ["a"])
        live = {i.key for i in self.store.items(states=st.WAITING)}
        self.assertEqual(live, {"b", "c"})

    def test_sync_leaves_other_sources_alone(self):
        self.store.raise_item(source="canary", key="x", kind=st.ALERT, title="X")
        self.store.sync(source="watchdog", kind=st.ALERT, entries=[])
        self.assertEqual(len(self.store.items(states=st.WAITING)), 1)

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.raise_item(source="s", key="k", kind="nonsense", title="t")


class TestDecisions(StoreCase):
    def test_only_the_first_decision_counts(self):
        item, _ = self.alert()
        self.assertTrue(self.store.decide(item.id, st.ACKNOWLEDGE, actor="telegram:1"))
        self.assertFalse(self.store.decide(item.id, st.REJECT, actor="cli"))
        got = self.store.get(item.id)
        self.assertEqual((got.state, got.verb), (st.DECIDED, st.ACKNOWLEDGE))

    def test_superseded_items_cannot_be_decided(self):
        item, _ = self.alert()
        self.store.clear(source="watchdog", key="k1")
        self.assertFalse(self.store.decide(item.id, st.ACKNOWLEDGE, actor="t"))

    def test_only_one_applier_wins_the_claim(self):
        item, _ = self.alert()
        self.store.decide(item.id, st.ACKNOWLEDGE, actor="t")
        self.assertTrue(self.store.claim(item.id, actor="bridge"))
        self.assertFalse(self.store.claim(item.id, actor="cli"))

    def test_failed_apply_can_be_retried(self):
        item, _ = self.alert()
        self.store.decide(item.id, st.ACKNOWLEDGE, actor="t")
        self.store.claim(item.id, actor="t")
        self.store.finish(item.id, False, "boom", actor="t")
        self.assertEqual(self.store.get(item.id).state, st.APPLY_FAILED)
        self.assertIn(self.store.get(item.id), self.store.items(states=st.WAITING))
        self.assertTrue(self.store.retry(item.id, actor="t"))
        self.assertEqual(self.store.get(item.id).state, st.DECIDED)

    def test_interrupted_apply_becomes_a_visible_failure(self):
        item, _ = self.alert()
        self.store.decide(item.id, st.ACKNOWLEDGE, actor="t", now=T0)
        self.store.claim(item.id, actor="t", now=T0)
        failed = self.store.fail_stale_applying(T0 + 600, now=T0 + 601)
        self.assertEqual(failed, [item.id])
        self.assertIn("interrupted", self.store.get(item.id).result)

    def test_snooze_hides_until_due_then_reopens(self):
        item, _ = self.alert()
        self.assertTrue(self.store.snooze(item.id, T0 + 100, actor="t", now=T0))
        self.assertEqual(self.store.wake_due(T0 + 50), [])
        self.assertEqual(self.store.wake_due(T0 + 100), [item.id])
        got = self.store.get(item.id)
        self.assertEqual(got.state, st.OPEN)
        self.assertIsNone(got.snooze_until)

    def test_the_event_log_tells_the_whole_story(self):
        item, _ = self.alert(now=T0)
        self.store.decide(item.id, st.ACKNOWLEDGE, actor="telegram:1", now=T0 + 10)
        self.store.claim(item.id, actor="bridge", now=T0 + 11)
        self.store.finish(item.id, True, "", actor="bridge", now=T0 + 12)
        self.assertEqual([(e["event"], e["actor"]) for e in self.store.events(item.id)], [
            ("created", "watchdog"), ("decided", "telegram:1"),
            ("applying", "bridge"), ("applied", "bridge")])


class TestRenders(StoreCase):
    def test_only_pushable_open_items_without_a_live_message_need_sending(self):
        cap, _ = self.store.raise_item(source="q", key="c", kind=st.CAPTURE, title="c")
        self.alert()
        self.assertEqual([i.id for i in self.store.needing_render((st.CAPTURE,))], [cap.id])
        self.store.add_render(cap, 42, 7)
        self.assertEqual(self.store.needing_render((st.CAPTURE,)), [])
        self.assertEqual(self.store.needing_render(()), [])

    def test_a_changed_item_marks_its_message_stale_until_redrawn(self):
        cap, _ = self.store.raise_item(source="q", key="c", kind=st.CAPTURE, title="c", now=T0)
        self.store.add_render(cap, 42, 7, now=T0)
        self.assertEqual(self.store.stale_renders(), [])
        self.store.decide(cap.id, st.APPROVE, actor="t", now=T0 + 5)
        stale = self.store.stale_renders()
        self.assertEqual([(r.chat_id, r.message_id, i.state) for r, i in stale], [(42, 7, st.DECIDED)])
        self.store.mark_shown(stale[0][0], stale[0][1].rev, close=False, now=T0 + 6)
        self.assertEqual(self.store.stale_renders(), [])

    def test_changes_in_the_same_second_still_mark_the_card_stale(self):
        cap, _ = self.store.raise_item(source="q", key="c", kind=st.CAPTURE, title="c", now=T0)
        self.store.add_render(cap, 42, 7, now=T0)
        self.store.decide(cap.id, st.APPROVE, actor="t", now=T0)
        render, item = self.store.stale_renders()[0]
        self.store.mark_shown(render, item.rev, close=False, now=T0)
        self.store.claim(cap.id, actor="t", now=T0)
        self.store.finish(cap.id, True, "done", actor="t", now=T0)
        self.assertEqual([i.state for _, i in self.store.stale_renders()], [st.APPLIED])

    def test_a_snoozed_item_is_sent_again_when_it_wakes(self):
        cap, _ = self.store.raise_item(source="q", key="c", kind=st.CAPTURE, title="c", now=T0)
        self.store.add_render(cap, 42, 7, now=T0)
        self.store.snooze(cap.id, T0 + 100, actor="t", now=T0 + 1)
        render, item = self.store.stale_renders()[0]
        self.store.mark_shown(render, item.rev, close=True, now=T0 + 2)
        self.store.wake_due(T0 + 100)
        self.assertEqual([i.id for i in self.store.needing_render((st.CAPTURE,))], [cap.id])


class TestStats(StoreCase):
    def test_decisions_time_to_decision_and_producer_volume(self):
        a, _ = self.alert(key="a", now=T0)
        b, _ = self.alert(key="b", now=T0)
        self.store.raise_item(source="q", key="c", kind=st.CAPTURE, title="c", now=T0)
        self.store.decide(a.id, st.ACKNOWLEDGE, actor="t", now=T0 + 3600)
        self.store.decide(b.id, st.ACKNOWLEDGE, actor="t", now=T0 + 3 * 3600)
        stats = self.store.stats(since=T0 - 1, now=T0 + 86400)
        self.assertEqual(stats["decisions"], {st.ALERT: 2})
        self.assertEqual(stats["median_hours_to_decision"], {st.ALERT: 2.0})
        self.assertEqual(stats["created"], {"watchdog": 2, "q": 1})
        self.assertEqual(stats["waiting"], 1)
        self.assertEqual(stats["oldest_waiting_days"], 1.0)

    def test_probes_are_invisible_to_lists_and_stats(self):
        self.store.raise_item(source="health", key="p", kind=st.PROBE, title="probe", now=T0)
        self.assertEqual(self.store.items(), [])
        self.assertEqual(self.store.stats(since=T0 - 1, now=T0)["created"], {})


class TestPath(unittest.TestCase):
    def test_tests_never_touch_the_real_queue(self):
        saved = os.environ.pop(st.DB_ENV, None)
        try:
            self.assertNotEqual(st.db_path(), st.DEFAULT_DB)
            self.assertTrue(str(st.db_path()).startswith(tempfile.gettempdir()))
        finally:
            if saved is not None:
                os.environ[st.DB_ENV] = saved

    def test_env_override_wins(self):
        saved = os.environ.get(st.DB_ENV)
        os.environ[st.DB_ENV] = "/tmp/elsewhere/q.db"
        try:
            self.assertEqual(st.db_path(), Path("/tmp/elsewhere/q.db"))
        finally:
            if saved is None:
                os.environ.pop(st.DB_ENV, None)
            else:
                os.environ[st.DB_ENV] = saved

    def test_database_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = st.Store(Path(tmp) / "q.db")
            store.items()
            self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
