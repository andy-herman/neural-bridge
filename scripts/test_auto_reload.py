"""auto_reload.sh must deploy origin/main, and must never fail silently.

From 2026-05-13 to 2026-09-25 every tick on main exited 1 before the fetch:
`reset_skip_state` ended in `[ -f "$ALERTED_FLAG" ] && rm ...`, which returns
1 when the flag is absent, and `set -e` killed the script with no log line and
no stderr. Two merged PRs sat undeployed while launchd showed only "last exit
code = 1". These tests run the real script against a throwaway HOME holding a
scratch repo and a bare origin, with launchctl, security and curl stubbed on
PATH, so nothing on the host is touched.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "auto_reload.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _write_exec(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class TestAutoReload(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.calls = self.home / "calls.log"
        self.log = self.home / "Library" / "Logs" / "neural-bridge" / "auto-reload.log"
        self.origin = self.home / "origin.git"
        self.repo = self.home / "Development" / "neural-bridge"
        self.seed = self.home / "seed"

        # Stubs. launchctl reports only luna-telegram as loaded, so the
        # restart loop has one service to restart and two to skip.
        self.bin = self.home / "bin"
        self.bin.mkdir()
        _write_exec(self.bin / "launchctl", (
            f'echo "launchctl $*" >> "{self.calls}"\n'
            'if [ "$1" = print ]; then case "$2" in *luna-telegram) exit 0;; *) exit 1;; esac; fi\n'
            "exit 0\n"
        ))
        _write_exec(self.bin / "security", 'echo "https://discord.invalid/webhook"\n')
        _write_exec(self.bin / "curl", f'echo "curl $*" >> "{self.calls}"\nexit 0\n')

        self._git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.home)
        self._git("clone", "-q", str(self.origin), str(self.seed), cwd=self.home)
        install = self.seed / "scripts" / "launchd" / "install.sh"
        install.parent.mkdir(parents=True)
        _write_exec(install, f'echo "install.sh skip=$NB_INSTALL_SKIP" >> "{self.calls}"\n')
        self._git("add", "scripts/launchd/install.sh", cwd=self.seed)
        self._commit(self.seed, "README.md", "seed\n")
        self._git("clone", "-q", str(self.origin), str(self.repo), cwd=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def _git(self, *args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV},
                       check=True, capture_output=True)

    def _commit(self, clone: Path, rel: str, text: str) -> None:
        path = clone / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self._git("add", rel, cwd=clone)
        self._git("commit", "-q", "-m", f"change {rel}", cwd=clone)
        self._git("push", "-q", "origin", "main", cwd=clone)

    def _run(self, **extra_env: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ, **GIT_ENV,
            "HOME": str(self.home), "USER": "test",
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "NB_WATCHER_SILENT_TICKS_BEFORE_ALERT": "2",
            **extra_env,
        }
        return subprocess.run(["bash", str(SCRIPT)], env=env,
                              capture_output=True, text=True, timeout=60)

    def _log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def _calls(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    # -- tests -------------------------------------------------------------

    def test_up_to_date_main_with_no_state_files_exits_zero(self):
        """The regression: this exact state exited 1 on every tick for four months."""
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FATAL", self._log_text())

    def test_behind_and_clean_pulls_reloads_and_restarts_bridges(self):
        self._commit(self.seed, "scripts/telegram_bot/luna_bridge.py", "x = 1\n")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pulled 1 files", self._log_text())
        self.assertTrue((self.repo / "scripts" / "telegram_bot" / "luna_bridge.py").exists())
        calls = self._calls()
        # install.sh must never re-bootstrap the job running this script.
        self.assertIn("install.sh skip=com.andyherman.neural-bridge.auto-reload", calls)
        kicks = [line for line in calls.splitlines() if "kickstart -k" in line]
        self.assertEqual(len(kicks), 1, "only the loaded bridge is restarted")
        self.assertTrue(kicks[0].endswith("com.andyherman.neural-bridge.luna-telegram"))

    def test_non_daemon_change_pulls_without_reload(self):
        self._commit(self.seed, "docs/NOTES.md", "notes\n")
        self._run()
        self.assertIn("no daemon-relevant changes", self._log_text())
        self.assertNotIn("install.sh", self._calls())

    def test_dirty_tree_behind_origin_counts_and_alerts_once(self):
        self._commit(self.seed, "scripts/discord_bot/mention.py", "y = 2\n")
        (self.repo / "README.md").write_text("local edit\n")

        for _ in range(3):
            result = self._run()
            self.assertEqual(result.returncode, 0, result.stderr)

        log = self._log_text()
        self.assertIn("uncommitted changes", log)
        self.assertIn("alert: blocked 2 ticks", log)
        self.assertIn("already alerted", log)
        self.assertEqual(self._calls().count("curl "), 1, "should ping Discord exactly once")
        self.assertNotIn("install.sh", self._calls())

    def test_blocked_state_clears_once_back_in_step(self):
        self._commit(self.seed, "scripts/discord_bot/mention.py", "y = 2\n")
        readme = self.repo / "README.md"
        readme.write_text("local edit\n")
        self._run()
        self._run()
        state_dir = self.log.parent
        self.assertTrue((state_dir / "auto-reload.alerted").exists())

        readme.write_text("seed\n")  # edit reverted by hand
        self._run()
        self.assertFalse((state_dir / "auto-reload.skip-count").exists())
        self.assertFalse((state_dir / "auto-reload.alerted").exists())

    def test_feature_branch_still_counts_as_blocked(self):
        self._git("checkout", "-q", "-b", "feat/x", cwd=self.repo)
        self._run()
        self.assertIn("on branch 'feat/x'", self._log_text())

    def test_early_failure_is_logged_not_silent(self):
        shutil.rmtree(self.repo)  # `cd "$REPO"` now fails under set -e
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FATAL: auto_reload.sh exited at line", self._log_text())


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestInstallSkip(unittest.TestCase):
    """install.sh honours NB_INSTALL_SKIP, so auto-reload can reinstall the
    fleet without booting itself out halfway through (which is what the one
    reload before 2026-09-25 did)."""

    def test_skipped_label_is_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            calls = home / "calls.log"
            loaded = home / "loaded"
            loaded.mkdir()
            venv_python = home / "Development" / "neural-bridge" / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            _write_exec(venv_python, "exit 0\n")
            bin_dir = home / "bin"
            bin_dir.mkdir()
            _write_exec(bin_dir / "sleep", "exit 0\n")
            # A service counts as loaded once bootstrapped; print reports that.
            _write_exec(bin_dir / "launchctl", (
                f'echo "launchctl $*" >> "{calls}"\n'
                'case "$1" in\n'
                f'  print) [ -e "{loaded}/${{2##*/}}" ] ;;\n'
                f'  bootstrap) touch "{loaded}/$(basename "$3" .plist)" ;;\n'
                '  *) exit 0 ;;\n'
                'esac\n'
            ))
            skip = "com.andyherman.neural-bridge.auto-reload"
            result = subprocess.run(
                ["bash", str(SCRIPT.parent / "launchd" / "install.sh")],
                env={**os.environ, "HOME": str(home),
                     "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                     "NB_INSTALL_SKIP": skip},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"Skipping {skip}", result.stdout)
            log = calls.read_text()
            self.assertNotIn(skip, log)
            self.assertIn("com.andyherman.neural-bridge.discord-bot.plist", log)
            self.assertIn("com.andyherman.neural-bridge.echo-mindframe.plist", log,
                          "agents listed after auto-reload must still be installed")


if __name__ == "__main__":
    unittest.main()
