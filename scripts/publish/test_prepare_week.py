"""Unit + integration tests for prepare_week.py.

Focus: the subprocess-orphan fix from #110. The bug pattern was that
generate_korean_translation spawned `python -> node -> claude -p`, and on
timeout the default subprocess.run() behavior killed node but left claude
orphaned to PID 1. The fix is start_new_session=True + killpg on cleanup.

Run: `python3 scripts/publish/test_prepare_week.py`
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.publish import prepare_week  # noqa: E402


def _pid_alive(pid: int) -> bool:
    """Check if a PID is still alive (POSIX). Signal 0 doesn't kill, just
    checks. ProcessLookupError means gone; PermissionError means alive but
    not ours (treat as alive since the process exists)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------- _kill_process_group: the orphan-cleanup primitive ----------

class TestKillProcessGroup(unittest.TestCase):
    """The primitive that fixes #110. Verify it kills the whole subprocess
    tree, not just the direct child."""

    def test_grandchild_dies_with_parent(self):
        """The actual bug repro. `bash -c "sleep 60 & wait"` produces a tree
        where bash is the direct child of our Popen and sleep is bash's child
        (the grandchild). Under the OLD code (Python's subprocess.run timeout),
        Python kills bash but sleep gets reparented to PID 1 and keeps
        running. Under the NEW code (killpg on the session group), both die.
        """
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 60 & wait"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        sleep_pid = None
        try:
            # Wait for the grandchild to spawn.
            time.sleep(0.3)
            pgid = os.getpgid(proc.pid)
            # Find all processes in the group via pgrep -g.
            grep = subprocess.run(
                ["pgrep", "-g", str(pgid)], capture_output=True, text=True,
            )
            pids_in_group = [int(p) for p in grep.stdout.split() if p]
            self.assertGreaterEqual(
                len(pids_in_group), 2,
                f"expected bash + sleep in group {pgid}, got {pids_in_group}",
            )
            other_pids = [p for p in pids_in_group if p != proc.pid]
            sleep_pid = other_pids[0]
            self.assertTrue(_pid_alive(sleep_pid), "grandchild should be alive pre-kill")

            # The fix under test.
            prepare_week._kill_process_group(proc, label="test")

            # Both should be dead. Give SIGTERM up to 2s to land.
            for _ in range(20):
                time.sleep(0.1)
                if not _pid_alive(proc.pid) and not _pid_alive(sleep_pid):
                    break
            self.assertFalse(
                _pid_alive(proc.pid),
                f"bash PID {proc.pid} survived _kill_process_group",
            )
            self.assertFalse(
                _pid_alive(sleep_pid),
                f"sleep PID {sleep_pid} survived _kill_process_group "
                f"(this is the orphan bug from #110)",
            )
        finally:
            # Defensive: nuke anything still alive from the test process group.
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            if sleep_pid is not None and _pid_alive(sleep_pid):
                try:
                    os.kill(sleep_pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

    def test_already_dead_process_is_idempotent(self):
        """If the proc has already exited before the helper runs, the
        ProcessLookupError on getpgid should be swallowed cleanly."""
        proc = subprocess.Popen(
            ["true"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        proc.communicate()  # already reaped
        # Should not raise.
        prepare_week._kill_process_group(proc, label="already-dead")


# ---------- generate_korean_translation: integration test ----------

class TestGenerateKoreanTranslation(unittest.TestCase):
    """Integration test: timeout path through generate_korean_translation
    must clean up the orphan tree."""

    def _make_candidate(self, file_path: Path) -> prepare_week.Candidate:
        from datetime import date
        return prepare_week.Candidate(
            file_path=file_path,
            slug="dummy",
            title="dummy",
            description="dummy",
            pub_date=date(2026, 5, 11),
            content_type="posts",
            body="",
        )

    def test_dry_run_short_circuits(self):
        """Smoke: dry_run=True returns without invoking node."""
        c = self._make_candidate(Path("/tmp/fake.md"))
        ok, msg = prepare_week.generate_korean_translation(c, dry_run=True, force=False)
        self.assertTrue(ok)
        self.assertIn("dry-run", msg)

    def test_missing_translate_script_errors_cleanly(self):
        """If TRANSLATE_SCRIPT path doesn't exist, return False with a
        descriptive error, no subprocess spawn."""
        c = self._make_candidate(Path("/tmp/fake.md"))
        with mock.patch.object(prepare_week, "TRANSLATE_SCRIPT", Path("/tmp/does-not-exist.mjs")):
            ok, msg = prepare_week.generate_korean_translation(c, dry_run=False, force=False)
        self.assertFalse(ok)
        self.assertIn("translate script missing", msg)

    def test_timeout_path_kills_grandchild(self):
        """End-to-end repro of the orphan bug. Replace TRANSLATE_SCRIPT with
        a mock node script that itself spawns a 60-second sleep, then sleeps.
        With timeout=1s, the OLD code would orphan the sleep. The NEW code
        must reap it."""
        if not shutil.which("node"):
            self.skipTest("node not in PATH")

        tmpdir = Path(f"/tmp/nb-test-pw-{os.getpid()}-{int(time.time())}")
        tmpdir.mkdir(parents=True, exist_ok=True)
        try:
            mock_script = tmpdir / "mock_translate.mjs"
            # Node spawns sleep as its child, then itself sleeps. Both are in
            # our session group via start_new_session=True. When we time out
            # and killpg, the OS sends SIGTERM to both.
            mock_script.write_text(
                "import { spawn } from 'node:child_process';\n"
                "spawn('sleep', ['60'], { stdio: 'ignore' });\n"
                "await new Promise(r => setTimeout(r, 60000));\n",
                encoding="utf-8",
            )
            source_file = tmpdir / "fake.md"
            source_file.write_text("---\ntitle: fake\n---\n", encoding="utf-8")

            c = self._make_candidate(source_file)

            # Capture the Popen so we can inspect the process group after.
            captured = {}
            real_popen = subprocess.Popen

            def capturing_popen(*args, **kwargs):
                p = real_popen(*args, **kwargs)
                captured["proc"] = p
                captured["pgid"] = os.getpgid(p.pid)
                return p

            with mock.patch.object(prepare_week, "TRANSLATE_SCRIPT", mock_script), \
                 mock.patch.object(prepare_week, "TRANSLATE_TIMEOUT", 1), \
                 mock.patch.object(prepare_week, "BLOG_REPO", tmpdir), \
                 mock.patch.object(prepare_week.subprocess, "Popen", side_effect=capturing_popen):
                ok, msg = prepare_week.generate_korean_translation(
                    c, dry_run=False, force=False,
                )

            self.assertFalse(ok)
            self.assertEqual(msg, "translate timeout")

            # Verify start_new_session was used.
            self.assertIn("proc", captured, "Popen should have been called")
            proc = captured["proc"]
            pgid = captured["pgid"]
            self.assertEqual(pgid, proc.pid, "proc should be group leader (start_new_session=True)")

            # Verify the entire process group is gone.
            for _ in range(30):
                time.sleep(0.1)
                grep = subprocess.run(
                    ["pgrep", "-g", str(pgid)], capture_output=True, text=True,
                )
                pids_left = [int(p) for p in grep.stdout.split() if p]
                if not pids_left:
                    break
            self.assertEqual(
                pids_left, [],
                f"process group {pgid} still has members after timeout cleanup: {pids_left} "
                f"(this is the orphan bug from #110)",
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
