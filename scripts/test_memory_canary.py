"""Tests for memory telemetry and the read-back canary.

The cases that matter are the three real failure shapes from 2026-08-01/02:
a store that logs nothing at all, a store that logs only failures, and a quiet
fleet that must NOT be mistaken for either.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.discord_bot import memory_telemetry as mem  # noqa: E402
from scripts.memory_canary import (  # noqa: E402
    DEGRADED,
    FAILING,
    HEALTHY,
    IDLE,
    SILENT,
    degraded,
    evaluate,
    format_report,
    had_agent_traffic,
)

WATCH = {
    "luna_notes": {"stage": mem.RETRIEVE, "traffic_gated": True},
    "honcho_capture": {"stage": mem.WRITE, "traffic_gated": True},
}


class TestTelemetryRecording(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.jsonl"
        self._saved = mem.LOG_PATH
        mem.LOG_PATH = self.path
        # record() no-ops under a test runner so mocked failures elsewhere in
        # the suite cannot pollute the production log. This module is the
        # exception: it has to exercise the recorder itself.
        import os
        os.environ[mem.ENV_FORCE] = "1"

    def tearDown(self):
        import os
        os.environ.pop(mem.ENV_FORCE, None)
        mem.LOG_PATH = self._saved
        self.tmp.cleanup()

    def test_roundtrip(self):
        mem.record(mem.RETRIEVE, "luna_notes", agent_id="luna", ok=True, chars=100)
        events = mem.read_events(self.path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["store"], "luna_notes")
        self.assertEqual(events[0]["stage"], mem.RETRIEVE)
        self.assertTrue(events[0]["ok"])

    def test_records_failures_with_detail(self):
        mem.record(mem.WRITE, "honcho_capture", ok=False, detail="connection refused")
        summary = mem.summarize(mem.read_events(self.path))
        self.assertEqual(summary["honcho_capture"]["failed"], 1)
        self.assertIn("connection refused", summary["honcho_capture"]["last_detail"])

    def test_never_raises_on_bad_path(self):
        # Telemetry must not be able to take down a turn.
        mem.LOG_PATH = Path("/nonexistent-root-dir/nope/t.jsonl")
        mem.record(mem.WRITE, "x", ok=True)  # must not raise

    def test_malformed_lines_are_skipped(self):
        self.path.write_text('{"store":"a","epoch":1,"ok":true}\nNOT JSON\n[]\n', encoding="utf-8")
        self.assertEqual(len(mem.read_events(self.path)), 1)

    def test_since_epoch_filters(self):
        self.path.write_text(
            '{"store":"a","epoch":100,"ok":true}\n{"store":"a","epoch":900,"ok":true}\n',
            encoding="utf-8",
        )
        self.assertEqual(len(mem.read_events(self.path, since_epoch=500)), 1)

    def test_disable_flag_suppresses(self):
        import os
        os.environ["NB_NO_MEMORY_TELEMETRY"] = "1"
        try:
            mem.record(mem.WRITE, "x", ok=True)
            self.assertEqual(mem.read_events(self.path), [])
        finally:
            del os.environ["NB_NO_MEMORY_TELEMETRY"]


class TestSummarize(unittest.TestCase):
    def test_counts_and_last_ok(self):
        events = [
            {"store": "a", "epoch": 10, "ok": True},
            {"store": "a", "epoch": 20, "ok": False, "detail": "boom"},
            {"store": "b", "epoch": 5, "ok": True},
        ]
        s = mem.summarize(events)
        self.assertEqual(s["a"]["total"], 2)
        self.assertEqual(s["a"]["ok"], 1)
        self.assertEqual(s["a"]["failed"], 1)
        self.assertEqual(s["a"]["last_epoch"], 20)
        self.assertEqual(s["a"]["last_ok_epoch"], 10)
        self.assertEqual(s["a"]["last_detail"], "boom")


class TestCanaryClassification(unittest.TestCase):
    """The three real shapes."""

    def test_healthy_store(self):
        summary = {"luna_notes": {"total": 5, "ok": 5, "failed": 0, "last_detail": ""},
                   "honcho_capture": {"total": 5, "ok": 5, "failed": 0, "last_detail": ""}}
        res = evaluate(summary, had_traffic=True, watched=WATCH)
        self.assertEqual(res["luna_notes"]["status"], HEALTHY)
        self.assertEqual(degraded(res), [])

    def test_failing_store_is_caught(self):
        # Honcho reachable-but-broken: attempts logged, none succeeded.
        summary = {"luna_notes": {"total": 3, "ok": 3, "failed": 0, "last_detail": ""},
                   "honcho_capture": {"total": 3, "ok": 0, "failed": 3,
                                      "last_detail": "connection refused"}}
        res = evaluate(summary, had_traffic=True, watched=WATCH)
        self.assertEqual(res["honcho_capture"]["status"], FAILING)
        self.assertIn("connection refused", res["honcho_capture"]["reason"])
        self.assertEqual(degraded(res), ["honcho_capture"])

    def test_silent_store_while_others_active_is_caught(self):
        # THE ten-week bug: one layer logs nothing while the fleet is clearly
        # running. Silence next to traffic is the signal.
        summary = {"luna_notes": {"total": 9, "ok": 9, "failed": 0, "last_detail": ""}}
        res = evaluate(summary, had_traffic=True, watched=WATCH)
        self.assertEqual(res["honcho_capture"]["status"], SILENT)
        self.assertEqual(degraded(res), ["honcho_capture"])

    def test_partial_degradation_is_caught(self):
        # The gap the first version missed: one success made a store failing
        # most of its writes report healthy.
        summary = {"luna_notes": {"total": 10, "ok": 10, "failed": 0, "last_detail": ""},
                   "honcho_capture": {"total": 15, "ok": 6, "failed": 9,
                                      "last_detail": "connection refused"}}
        res = evaluate(summary, had_traffic=True, watched=WATCH)
        self.assertEqual(res["honcho_capture"]["status"], DEGRADED)
        self.assertIn("40%", res["honcho_capture"]["reason"])
        self.assertEqual(degraded(res), ["honcho_capture"])

    def test_small_samples_do_not_trigger_rate_alarm(self):
        # 1 ok / 1 failed is 50% but only two events; too noisy to alert on.
        summary = {"luna_notes": {"total": 2, "ok": 1, "failed": 1, "last_detail": "x"},
                   "honcho_capture": {"total": 2, "ok": 1, "failed": 1, "last_detail": "x"}}
        res = evaluate(summary, had_traffic=True, watched=WATCH)
        self.assertEqual(res["luna_notes"]["status"], HEALTHY)

    def test_telemetry_suppressed_under_test_runner(self):
        # The defect this guards: honcho's mocked "network blip" failures were
        # landing in the production log, and the canary read them as a real 60%
        # failure rate. Restores whatever the flag was so the suppression stays
        # active for the rest of the suite.
        import os
        prior = os.environ.get(mem.ENV_FORCE)
        try:
            os.environ.pop(mem.ENV_FORCE, None)
            self.assertTrue(mem._under_test(), "suppression must be on under a test runner")
            os.environ[mem.ENV_FORCE] = "1"
            self.assertFalse(mem._under_test(), "force flag must override suppression")
        finally:
            if prior is None:
                os.environ.pop(mem.ENV_FORCE, None)
            else:
                os.environ[mem.ENV_FORCE] = prior

    def test_quiet_fleet_is_idle_not_degraded(self):
        # This fleet genuinely sits dormant for weeks. A canary that fires on
        # every quiet day gets muted, and a muted canary misses the real one.
        res = evaluate({}, had_traffic=False, watched=WATCH)
        self.assertEqual(res["luna_notes"]["status"], IDLE)
        self.assertEqual(res["honcho_capture"]["status"], IDLE)
        self.assertEqual(degraded(res), [])

    def test_had_agent_traffic(self):
        self.assertFalse(had_agent_traffic({}))
        self.assertFalse(had_agent_traffic({"a": {"total": 0}}))
        self.assertTrue(had_agent_traffic({"a": {"total": 1}}))

    def test_report_renders_all_statuses(self):
        summary = {"luna_notes": {"total": 1, "ok": 0, "failed": 1, "last_detail": "x"}}
        text = format_report(evaluate(summary, had_traffic=True, watched=WATCH), 7)
        self.assertIn("luna_notes", text)
        self.assertIn("honcho_capture", text)


if __name__ == "__main__":
    unittest.main()



class TestLayerFourStores(unittest.TestCase):
    """The wiki stores added 2026-09-22 after the read loop was found open."""

    def test_compile_is_not_traffic_gated(self):
        from scripts.memory_canary import WATCHED
        self.assertFalse(WATCHED["compile_concepts"]["traffic_gated"])
        # A quiet fleet is still SILENT for compile: launchd runs it regardless.
        res = evaluate({}, had_traffic=False, watched={"compile_concepts": WATCHED["compile_concepts"]})
        self.assertEqual(res["compile_concepts"]["status"], SILENT)

    def test_wiki_and_flush_are_traffic_gated(self):
        from scripts.memory_canary import WATCHED
        sub = {k: WATCHED[k] for k in ("wiki_recall", "flush_daily_log")}
        res = evaluate({}, had_traffic=False, watched=sub)
        self.assertEqual({r["status"] for r in res.values()}, {IDLE})
        res = evaluate({}, had_traffic=True, watched=sub)
        self.assertEqual({r["status"] for r in res.values()}, {SILENT})

    def test_scheduled_compile_alone_is_not_agent_traffic(self):
        # A nightly compile in an otherwise idle week must not flip every
        # traffic-gated store from IDLE to SILENT.
        from scripts.memory_canary import WATCHED
        summary = {"compile_concepts": {"total": 7, "ok": 7, "failed": 0, "last_detail": ""}}
        self.assertFalse(had_agent_traffic(summary, WATCHED))
        summary["wiki_recall"] = {"total": 1, "ok": 1, "failed": 0, "last_detail": ""}
        self.assertTrue(had_agent_traffic(summary, WATCHED))
        # Unknown stores still count as traffic, as before.
        self.assertTrue(had_agent_traffic({"new_store": {"total": 1, "ok": 1, "failed": 0}}, WATCHED))

    def test_report_appends_grounding_line_when_events_given(self):
        summary = {"wiki_recall": {"total": 2, "ok": 2, "failed": 0, "last_detail": ""}}
        events = [
            {"store": "wiki_recall", "stage": "utilize", "agent_id": "research", "ok": True, "chars": 300},
            {"store": "wiki_recall", "stage": "utilize", "agent_id": "luna", "ok": True, "chars": 0},
        ]
        watched = {"wiki_recall": {"stage": mem.UTILIZE, "traffic_gated": True}}
        text = format_report(evaluate(summary, had_traffic=True, watched=watched), 7, events)
        self.assertIn("1 of 2 agent turns grounded", text)
        # Without events the line is absent, so --json callers are unaffected.
        text2 = format_report(evaluate(summary, had_traffic=True, watched=watched), 7)
        self.assertNotIn("grounded", text2)


class TestConsolidationGates(unittest.TestCase):
    """docs/MEMORY_CONSOLIDATION.md Step 0, as one command."""

    def _ev(self, store, stage, agent="a", ok=True, chars=1, epoch=100):
        return {"store": store, "stage": stage, "agent_id": agent, "ok": ok, "chars": chars, "epoch": epoch}

    def test_no_data_says_no_data(self):
        from scripts.memory_canary import gates
        g = gates([])
        self.assertEqual(g["G2"]["answer"], "no data")
        self.assertEqual(g["G3"]["answer"], "no data")
        self.assertEqual(g["G4"]["answer"], "undecided: no agent turns and no compile runs in the window")
        self.assertEqual(g["progress_log"]["answer"], "none yet")

    def test_g2_luna_only_versus_all_three(self):
        from scripts.memory_canary import gates
        only = [self._ev("echo_voice", mem.RETRIEVE, agent="luna")] * 3
        self.assertEqual(gates(only)["G2"]["answer"], "luna only")
        mixed = only + [self._ev("echo_voice", mem.RETRIEVE, agent="content")]
        self.assertEqual(gates(mixed)["G2"]["answer"], "keep for all three")
        self.assertEqual(gates(mixed)["G2"]["by_agent"], {"luna": 3, "content": 1})

    def test_g2_ignores_unattributed_reads(self):
        # Pre-2026-09-25 reads carry no agent; they must not vote.
        from scripts.memory_canary import gates
        legacy = [self._ev("echo_voice", mem.RETRIEVE, agent=None)] * 37
        self.assertEqual(gates(legacy)["G2"]["answer"], "undecided: no read carries an agent yet")
        mixed = legacy + [self._ev("echo_voice", mem.RETRIEVE, agent="luna")]
        self.assertEqual(gates(mixed)["G2"]["answer"], "luna only")

    def test_g4_dead_only_when_agents_ran_without_reading(self):
        from scripts.memory_canary import gates
        # Traffic from before the read path existed is not evidence.
        old = [self._ev("honcho_peer_card", mem.RETRIEVE, chars=500)] * 3
        self.assertEqual(gates(old)["G4"]["answer"],
                         "undecided: no agent turns and no compile runs in the window")
        turns = [self._ev("progress_log", mem.RETRIEVE, chars=0)] * 3
        self.assertEqual(gates(turns)["G4"]["answer"],
                         "dead: agents ran but never read the wiki, and compile never ran")

    def test_g3_threshold(self):
        from scripts.memory_canary import G3_KEEP_RATE, gates
        good = [self._ev("honcho_peer_card", mem.RETRIEVE, chars=500)] * 9 + \
               [self._ev("honcho_peer_card", mem.RETRIEVE, chars=0)]
        self.assertEqual(gates(good)["G3"]["answer"], "keep injected")
        bad = [self._ev("honcho_peer_card", mem.RETRIEVE, chars=500)] * 5 + \
              [self._ev("honcho_peer_card", mem.RETRIEVE, ok=True, chars=0)] * 5
        g = gates(bad)["G3"]
        self.assertEqual(g["answer"], "demote to retrieval-only")
        self.assertLess(g["rate"], G3_KEEP_RATE)

    def test_g4_shapes(self):
        from scripts.memory_canary import gates
        reads = [self._ev("wiki_recall", mem.UTILIZE, chars=300), self._ev("wiki_recall", mem.UTILIZE, chars=0)]
        comp = [self._ev("compile_concepts", mem.WRITE, agent="compile", epoch=555)]
        self.assertEqual(gates(reads)["G4"]["answer"], "alive: read but compile not running")
        self.assertEqual(gates(comp)["G4"]["answer"], "alive: compiling but never read")
        g = gates(reads + comp)["G4"]
        self.assertEqual(g["answer"], "alive")
        self.assertEqual((g["reads"], g["grounded"], g["last_compile_epoch"]), (2, 1, 555))

    def test_progress_log_adoption_lists_agents_with_injected_logs(self):
        from scripts.memory_canary import gates
        ev = [self._ev("progress_log", mem.RETRIEVE, agent="luna", chars=200),
              self._ev("progress_log", mem.RETRIEVE, agent="research", chars=0)]
        self.assertEqual(gates(ev)["progress_log"]["agents"], ["luna"])

    def test_gates_flag_prints_and_exits_zero(self):
        from scripts import memory_canary as mc
        import io
        from contextlib import redirect_stdout
        with patch.object(mem, "read_events", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mc.main(["--gates", "--no-notify"])
        self.assertEqual(rc, 0)
        self.assertIn("Consolidation gates", buf.getvalue())
