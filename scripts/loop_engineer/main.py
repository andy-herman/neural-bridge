"""Loop engineer entry point.

    python -m scripts.loop_engineer.main [--once] [--dry-run] [--owner LABEL] [-v]

Lifecycle:
    1. Resolve config + repo, set up logging and signal traps.
    2. Recover: reset any issue orphaned in `agent-running` back to `agent-ready`.
    3. Loop: claim the oldest `agent-ready` issue, process it end to end, repeat
       until the per-run or per-day ceiling is hit (or, in --once mode, until the
       queue drains).
    4. On SIGINT/SIGTERM: reset the in-flight issue to `agent-ready` and exit
       cleanly so nothing is left stuck.

Per-issue pipeline (process_issue): worktree → implement → integrity gate →
size gate → test gate (with up-to-N-strike fix loop) → draft PR. Each terminal
outcome moves the issue to review / blocked / failed and notifies Discord.

Nothing runs unattended until the launchd plist is installed by hand.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

from scripts.discord_bot.repos import repo_for

from . import notify, pr, queue, verify, worktree
from .config import LoopConfig
from .executor import fix_after_failure, implement
from .queue import Issue
from .state import bump_count, load_count

_log = logging.getLogger("nb_loop")


class _Shutdown(Exception):
    """Raised from the signal handler to unwind the loop cleanly."""


class LoopEngineer:
    def __init__(self, cfg: LoopConfig):
        self.cfg = cfg
        repo = repo_for(cfg.repo_id)
        if repo is None:
            raise SystemExit(f"unknown repo_id {cfg.repo_id!r} (see discord_bot/repos.py)")
        self.repo = repo
        self.clone = repo.local_path
        self.gh_slug = repo.gh_slug
        self._current: Issue | None = None  # in-flight issue, for signal cleanup
        self.stats = {"considered": 0, "opened": 0, "blocked": 0, "escalated": 0}

    # ---- terminal outcomes ----

    def _to_review(self, issue: Issue) -> None:
        queue.transition(self.gh_slug, issue.number,
                         remove=[self.cfg.running_label], add=[self.cfg.review_label])

    def _to_blocked(self, issue: Issue, reason: str, detail: str = "") -> None:
        queue.transition(self.gh_slug, issue.number,
                         remove=[self.cfg.running_label], add=[self.cfg.blocked_label])
        notify.notify(notify.blocked_msg(issue.number, reason, detail))
        self.stats["blocked"] += 1

    def _to_failed(self, issue: Issue, reason: str, detail: str = "") -> None:
        queue.transition(self.gh_slug, issue.number,
                         remove=[self.cfg.running_label], add=[self.cfg.failed_label])
        notify.notify(notify.escalated_msg(issue.number, reason, detail))
        self.stats["escalated"] += 1

    # ---- per-issue pipeline ----

    def process_issue(self, issue: Issue) -> None:
        cfg = self.cfg
        _log.info("processing #%s: %s", issue.number, issue.title)

        ok, wt, err = worktree.create(cfg, self.clone, issue.number)
        if not ok or wt is None:
            self._to_failed(issue, "could not create worktree", err)
            return

        keep_worktree = False
        subject, cbody = pr.commit_message(issue)
        try:
            # Baseline: measure test failures on the CLEAN checkout before the
            # agent touches anything, then wipe the run's side effects (compile
            # tests write into knowledge/). The gate later requires the agent's
            # change to add no NEW failures on top of this baseline — a red
            # baseline (known pre-existing failures) must not block every issue.
            baseline = verify.run_tests(cfg, wt)
            _log.info("#%s baseline: exit=%s, %s pre-existing failure(s)",
                      issue.number, baseline.exit_code, len(baseline.failures))
            worktree.reset_clean(wt)

            result = implement(cfg, wt, issue)

            if result.error_reason == "claude_cli_not_found":
                # Config/environment problem, not the issue's fault. Bounce it
                # back to the queue and abort the whole run upstream.
                queue.transition(self.gh_slug, issue.number,
                                 remove=[cfg.running_label], add=[cfg.ready_label])
                raise SystemExit("claude CLI not found on PATH — aborting run")

            # An infra-class invocation failure (timeout, API/auth error, CLI
            # crash) is distinct from a successful-but-empty agent run. Escalate
            # with the accurate reason instead of misreporting it downstream as
            # "no change". `max_turns` is excluded: the agent made real partial
            # progress and is worth committing + gating.
            if not result.ok and result.error_reason != "max_turns":
                keep_worktree = True
                self._to_failed(issue, f"claude invocation failed ({result.error_reason})",
                                result.summary[:400])
                return

            # Commit the agent's work NOW, before any test run can pollute the
            # tree. Every gate below is then a clean commit-to-commit diff.
            committed, reason = worktree.commit_work(wt, subject, cbody, amend=False)
            if not committed:
                keep_worktree = True
                self._to_failed(issue, "agent produced no change",
                                reason if reason != "empty" else result.summary[:400])
                return

            # Gate 1: test integrity (anti reward-hacking). Hard stop.
            integrity = verify.check_test_integrity(cfg, wt)
            if not integrity.ok:
                keep_worktree = True
                self._to_failed(issue, "tampered with existing tests",
                                "; ".join(integrity.violations))
                return

            # Gate 2: diff size. Over cap -> human review, keep the work.
            ok, files, lines = worktree.diff_line_count(cfg, wt)
            if not ok:
                keep_worktree = True
                self._to_failed(issue, "could not compute diff size")
                return
            size = verify.check_diff_size(cfg, files, lines)
            if not size.ok:
                keep_worktree = True
                self._to_blocked(issue, "diff too large",
                                 f"{size.lines} lines > {size.limit} cap ({size.files} files)")
                return

            # Gate 3: the change must add NO new test failures vs the baseline.
            # Each test run pollutes the tree, so reset_clean after every run.
            # Up to max_strikes fix attempts, each feeding failing output back
            # and amended into the single WIP commit.
            test_result = verify.run_tests(cfg, wt)
            worktree.reset_clean(wt)
            gate = verify.check_no_new_failures(baseline, test_result)
            strikes = 0
            while not gate.ok and strikes < cfg.max_strikes:
                strikes += 1
                _log.info("#%s strike %s/%s: %s",
                          issue.number, strikes, cfg.max_strikes, gate.detail)
                if strikes >= cfg.max_strikes:
                    break
                if not result.session_id:
                    break  # no session to resume; can't ask for a fix
                result = fix_after_failure(cfg, wt, result.session_id, test_result.output_tail)
                committed, reason = worktree.commit_work(wt, subject, cbody, amend=True)
                if not committed:
                    _log.info("#%s fix produced no change (%s)", issue.number, reason)
                # Re-check integrity after each fix — a "fix" could tamper tests.
                integrity = verify.check_test_integrity(cfg, wt)
                if not integrity.ok:
                    keep_worktree = True
                    self._to_failed(issue, "tampered with existing tests during fix",
                                    "; ".join(integrity.violations))
                    return
                test_result = verify.run_tests(cfg, wt)
                worktree.reset_clean(wt)
                gate = verify.check_no_new_failures(baseline, test_result)

            if not gate.ok:
                keep_worktree = True
                self._to_failed(issue, f"tests failing after {cfg.max_strikes} strikes",
                                gate.detail + "\n\n" + test_result.output_tail)
                return

            # Re-measure size after any fix rounds, then open the PR.
            ok, files, lines = worktree.diff_line_count(cfg, wt)

            if cfg.dry_run:
                _log.info("[dry-run] would open PR for #%s (%s files, %s lines)",
                          issue.number, files, lines)
                notify.notify(f"🔧 **loop** [dry-run] #{issue.number} passed all gates "
                              f"({files} files, {lines} lines) — PR suppressed")
                # Leave the label as running in dry-run; do not advance state.
                return

            pr_result = pr.open_pr(cfg, self.gh_slug, wt, issue,
                                   result.summary, files, lines)
            if not pr_result.ok:
                keep_worktree = True
                self._to_failed(issue, "PR creation failed", pr_result.error)
                return

            self._to_review(issue)
            notify.notify(notify.pr_opened_msg(issue.number, pr_result.pr_url or "(url?)",
                                               files, lines))
            self.stats["opened"] += 1
            _log.info("#%s -> PR %s", issue.number, pr_result.pr_url)

        finally:
            status = worktree.remove(cfg, self.clone, wt, keep_for_inspection=keep_worktree)
            _log.info("#%s teardown: %s", issue.number, status)

    # ---- outer loop ----

    def run(self) -> int:
        cfg = self.cfg
        reset = queue.recover_stuck(self.gh_slug, cfg.running_label, cfg.ready_label)
        if reset:
            _log.info("recovered stuck issues -> ready: %s", reset)

        processed = 0
        try:
            while processed < cfg.max_issues_per_run:
                day_count = load_count(cfg.state_dir)
                if day_count >= cfg.max_issues_per_day:
                    _log.info("daily cap reached (%s); stopping", day_count)
                    notify.notify(f"🔧 **loop** daily cap reached ({day_count}); pausing until tomorrow")
                    break

                issues = queue.list_ready(self.gh_slug, cfg.ready_label_filter())
                issue = queue.select_next(issues)
                if issue is None:
                    if cfg.once:
                        break
                    time.sleep(cfg.poll_interval_seconds)
                    continue

                if not queue.claim(self.gh_slug, issue, cfg.ready_label, cfg.running_label):
                    _log.info("#%s claim lost (race or label change); skipping", issue.number)
                    continue

                self._current = issue
                self.stats["considered"] += 1
                notify.notify(notify.claimed_msg(issue.number, issue.title))
                try:
                    self.process_issue(issue)
                finally:
                    self._current = None

                processed += 1
                bump_count(cfg.state_dir)
        except _Shutdown:
            self._graceful_release()
        finally:
            notify.notify(notify.run_summary_msg(
                self.stats["opened"], self.stats["blocked"],
                self.stats["escalated"], self.stats["considered"]))

        _log.info("run complete: %s", self.stats)
        return 0

    def _graceful_release(self) -> None:
        """On shutdown, hand the in-flight issue back to the queue."""
        if self._current is not None:
            _log.info("shutdown: releasing in-flight #%s back to ready", self._current.number)
            queue.transition(self.gh_slug, self._current.number,
                             remove=[self.cfg.running_label], add=[self.cfg.ready_label])
            self._current = None


def _install_signal_traps() -> None:
    def handler(signum, _frame):
        _log.info("signal %s received; shutting down", signum)
        raise _Shutdown()
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="loop_engineer", description="Neural Bridge autonomous loop engineer")
    p.add_argument("--once", action="store_true",
                   help="process available issues then exit (no polling)")
    p.add_argument("--dry-run", action="store_true",
                   help="run all gates but do not push, open PRs, or advance labels past running")
    p.add_argument("--owner", metavar="LABEL", default=None,
                   help="only claim issues also carrying this owner label, e.g. owner:automation-engineer")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.verbose)
    cfg = LoopConfig.from_env()
    if args.once:
        cfg.once = True
    if args.dry_run:
        cfg.dry_run = True
    if args.owner:
        cfg.owner_label = args.owner
    _install_signal_traps()
    _log.info("loop engineer starting: repo=%s once=%s dry_run=%s owner=%s "
              "caps[run=%s day=%s turns=%s strikes=%s diff=%s]",
              cfg.repo_id, cfg.once, cfg.dry_run, cfg.owner_label,
              cfg.max_issues_per_run, cfg.max_issues_per_day, cfg.max_turns,
              cfg.max_strikes, cfg.max_diff_lines)
    return LoopEngineer(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
