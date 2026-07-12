"""Autonomous loop engineering agent for Neural Bridge.

A separate launchd-managed daemon (NOT the Discord bot) that closes the
squad-discuss loop: squad discussions already *file* GitHub issues with
`owner:*` labels; this package *consumes* them. It polls the issue queue,
claims one issue at a time via an atomic GitHub label swap, implements it
inside a per-issue git worktree with a fresh `claude -p` session, gates the
result behind checks the agent cannot self-report around (test exit code,
diff size, test-file integrity), and opens a DRAFT PR for Andy to review.

Design provenance — the convergent practices behind this harness:
  - Fresh session per issue (context rot avoidance) — Anthropic long-running
    agents guidance + Ralph loop.
  - Worktree-per-task isolation so the loop never fights the auto-reload
    watcher's `main` checkout (Andy's #1 documented risk is a runaway loop,
    and the daemon shares one clone).
  - Hard, daemon-computed gates (never agent self-report): pytest/unittest
    exit code, ~500-line diff cap, no editing existing tests.
  - "3 strikes then escalate to a human", counter held by the daemon.
  - Per-run and per-day issue ceilings + turn caps — the budget kill-switch
    Andy's Risks.md mandates.
  - Draft PRs only. No auto-merge. Matches the vault's "prefer proposing
    over doing" rule.

Nothing here runs unattended until Andy installs the launchd plist. The
package is import-safe and the local Discord daemon does not depend on it.

Entry point: scripts/loop_engineer/main.py  (python -m scripts.loop_engineer.main)
Runbook:     docs/LOOP_ENGINEER.md
"""
