"""The CLI is the producers' contract (the vault's watchdogs and auto_reload.sh
call it), so its behavior is tested through main() exactly as they call it."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.review_queue import __main__ as cli  # noqa: E402
from scripts.review_queue import store as st  # noqa: E402


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "q.db"
        self._env = mock.patch.dict(os.environ, {st.DB_ENV: str(self.db)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def run_cli(self, *argv: str, stdin: str = "") -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            rc = cli.main(list(argv))
        return rc, out.getvalue()

    def test_raise_is_idempotent_and_clear_resolves(self):
        args = ("raise", "--source", "auto_reload", "--key", "blocked", "--kind", "alert",
                "--title", "Auto-reload blocked for ~30 min")
        rc, out = self.run_cli(*args)
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip().endswith("created"))
        _, out = self.run_cli(*args)
        self.assertTrue(out.strip().endswith("exists"))
        rc, out = self.run_cli("clear", "--source", "auto_reload", "--key", "blocked")
        self.assertIn("resolved", out)
        rc, out = self.run_cli("clear", "--source", "auto_reload", "--key", "blocked")
        self.assertEqual((rc, out.strip()), (0, "nothing live under that key"))

    def test_sync_reads_the_full_state_from_stdin(self):
        entries = [{"key": "deep-dive", "title": "Weekly deep dive overdue"},
                   {"key": "inbox", "title": "Weekly inbox report overdue"}]
        rc, out = self.run_cli("sync", "--source", "output_watchdog", "--kind", "alert",
                               stdin=json.dumps(entries))
        self.assertEqual((rc, out.strip()), (0, "created 2, cleared 0, live 2"))
        rc, out = self.run_cli("sync", "--source", "output_watchdog", "--kind", "alert",
                               stdin=json.dumps(entries[:1]))
        self.assertEqual(out.strip(), "created 0, cleared 1, live 1")

    def test_sync_refuses_malformed_input(self):
        rc, _ = self.run_cli("sync", "--source", "s", "--kind", "alert", stdin='{"key": "x"}')
        self.assertEqual(rc, 2)
        rc, _ = self.run_cli("sync", "--source", "s", "--kind", "alert", stdin='[{"key": "x"}]')
        self.assertEqual(rc, 2)

    def test_decide_list_show_and_refusals(self):
        _, out = self.run_cli("raise", "--source", "s", "--key", "k", "--kind", "alert",
                              "--title", "Something overdue")
        item_id = out.split()[0]
        _, listing = self.run_cli("list")
        self.assertIn(item_id, listing)
        self.assertEqual(self.run_cli("decide", item_id, "acknowledge")[0], 0)
        self.assertEqual(self.run_cli("decide", item_id, "reject")[0], 1, "second decision refused")
        rc, shown = self.run_cli("show", item_id)
        self.assertIn("decided", shown)
        self.assertIn("created", shown)
        self.assertEqual(self.run_cli("apply", item_id)[0], 0)
        self.assertEqual(st.Store(self.db).get(item_id).state, st.APPLIED)
        _, listing = self.run_cli("list")
        self.assertIn("Nothing is waiting", listing)

    def test_health_fails_without_a_pusher(self):
        rc, out = self.run_cli("health")
        self.assertEqual(rc, 1)
        self.assertIn("no heartbeat", out)

    def test_probe_kind_cannot_be_raised_by_producers(self):
        with self.assertRaises(SystemExit):
            self.run_cli("raise", "--source", "s", "--key", "k", "--kind", "probe", "--title", "t")


if __name__ == "__main__":
    unittest.main()
