"""Tests for memory telemetry and the read-back canary.

The cases that matter are the three real failure shapes from 2026-08-01/02:
a store that logs nothing at all, a store that logs only failures, and a quiet
fleet that must NOT be mistaken for either.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.discord_bot import memory_telemetry as mem  # noqa: E402
from scripts.memory_canary import (  # noqa: E402
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

    def tearDown(self):
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
