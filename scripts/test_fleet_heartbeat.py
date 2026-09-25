"""fleet_heartbeat must not write the live Fleet heartbeat from a test run.

On the Mac the Fleet dir exists, so before the guard every local run of the
compile tests stamped fake "compile (live): PROMOTE=3" events into
Fleet/heartbeats/neural-bridge.json, the file the fleet dashboard reads.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import fleet_heartbeat  # noqa: E402


class TestFleetHeartbeatUnderTest(unittest.TestCase):
    def test_log_event_is_suppressed_under_a_test_runner(self):
        touch, emit = MagicMock(), MagicMock()
        with patch.object(fleet_heartbeat, "_AVAILABLE", True), \
             patch.object(fleet_heartbeat, "_touch", touch, create=True), \
             patch.object(fleet_heartbeat, "_emit", emit, create=True):
            fleet_heartbeat.log_event("compile (live): PROMOTE=3")
            fleet_heartbeat.set_state(headline="x")
        touch.assert_not_called()
        emit.assert_not_called()

    def test_env_var_also_suppresses(self):
        with patch.dict("os.environ", {fleet_heartbeat.ENV_DISABLE: "1"}), \
             patch.dict(sys.modules):
            sys.modules.pop("unittest", None)
            sys.modules.pop("pytest", None)
            self.assertTrue(fleet_heartbeat._suppressed())

    def test_production_process_is_not_suppressed(self):
        with patch.dict("os.environ", {}, clear=False), patch.dict(sys.modules):
            import os
            os.environ.pop(fleet_heartbeat.ENV_DISABLE, None)
            sys.modules.pop("unittest", None)
            sys.modules.pop("pytest", None)
            self.assertFalse(fleet_heartbeat._suppressed())


if __name__ == "__main__":
    unittest.main()
