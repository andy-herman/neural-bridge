# Loop Engineer — autonomous issue → draft PR daemon

Closes the squad-discuss loop. Squad discussions already *file* GitHub issues
with `owner:*` labels (see `handlers.py` step 6); this daemon *consumes* them:
it claims one `agent-ready` issue at a time, implements it in an isolated git
worktree with a fresh `claude -p` session, gates the result behind checks the
agent cannot self-report around, and opens a **draft** PR for you to review.

It is a **separate process from the Discord bot** and shares nothing with it at
runtime. The Discord daemon is unaffected whether this is running or not.

## The label state machine

```
agent-ready ──claim──▶ agent-running ──PR opened──▶ agent-review
                                     ──diff too big─▶ agent-blocked
                                     ──strikes/error▶ agent-failed
```

- **agent-ready** — queued. Put this label on an issue (plus optionally an
  `owner:*` label) to enqueue it. The squad pipeline can file issues straight
  into this state; you can also add it by hand.
- **agent-running** — claimed, in progress. Claiming is an atomic GitHub label
  swap, so two processes never grab the same issue.
- **agent-review** — a draft PR is open and green (no new test failures). Yours
  to review and merge. Nothing auto-merges, ever.
- **agent-blocked** — needs a human decision (today: the diff exceeded the size
  cap). The branch is kept for inspection.
- **agent-failed** — escalated: tests still failing after the strike budget, an
  attempt to tamper with existing tests, or an infra error. Branch kept.

Crash recovery: on startup and on SIGINT/SIGTERM the daemon resets any issue
stuck in `agent-running` back to `agent-ready`, so nothing is orphaned.

## The gates (why you can trust a draft PR)

Every gate is computed by the daemon, never from the agent's claim of success:

1. **Test integrity** — the change must not delete or edit an *existing* test.
   Adding new tests is fine; removing lines from a committed test, or deleting a
   test file, is a hard escalation. This is the anti-reward-hacking gate.
2. **Diff size** — over `max_diff_lines` (default 500) → `agent-blocked` for
   human review, regardless of test result. Catches scope creep.
3. **No new test failures** — the daemon runs the suite on the *clean* checkout
   first (baseline), then after the agent's change, and requires **no new
   failures vs baseline**. This is deliberate: the repo's baseline is currently
   red (a couple of known pre-existing failures), and the loop is responsible
   only for not *adding* failures, not for fixing what it didn't touch. A change
   that fixes a baseline failure also passes.

On a failing gate the agent gets up to `max_strikes` (default 3) fix attempts,
each fed the failing test output on the same session. The strike counter is held
by the daemon — the agent cannot reset it.

## Isolation

Each issue runs in `~/Development/neural-bridge/.trees/eng-<n>` — a private git
worktree with its own HEAD, index, and branch (`eng/<n>`). Creating a worktree
does not touch the shared clone's `main` checkout, so the loop never fights the
auto-reload watcher. `.trees/` is gitignored. On success the worktree is removed
(branch stays for the PR); on failure it is kept so you can inspect what the
agent did.

## Running it

**Always do a supervised foreground run before installing the launchd job.**

Dry run (all gates, but no push / no PR / no label change past running):

```
cd ~/Development/neural-bridge
.venv/bin/python -m scripts.loop_engineer.main --once --dry-run -v
```

Real single batch (opens draft PRs):

```
.venv/bin/python -m scripts.loop_engineer.main --once -v
```

Scope to one owner's queue:

```
.venv/bin/python -m scripts.loop_engineer.main --once --owner owner:automation-engineer -v
```

## Enqueuing work

Add `agent-ready` to any open issue you want implemented:

```
gh issue edit <n> --repo andy-herman/neural-bridge --add-label agent-ready
```

The squad-discuss pipeline can also file issues directly with `agent-ready` so a
discussion's action items flow straight into the queue.

## Configuration (env vars, all optional)

| Var | Default | Meaning |
|-----|---------|---------|
| `NB_LOOP_OWNER_LABEL` | (unset) | only claim issues with this `owner:*` label |
| `NB_LOOP_MAX_ISSUES_PER_RUN` | 3 | ceiling per process invocation |
| `NB_LOOP_MAX_ISSUES_PER_DAY` | 5 | ceiling per day, persisted to disk |
| `NB_LOOP_MAX_TURNS` | 30 | claude -p tool-use round trips per attempt |
| `NB_LOOP_MAX_STRIKES` | 3 | consecutive failed gates before escalation |
| `NB_LOOP_MAX_DIFF_LINES` | 500 | over this → agent-blocked |
| `NB_LOOP_PER_ISSUE_TIMEOUT` | 1800 | seconds per claude -p attempt |
| `NB_LOOP_MODEL` | claude-sonnet-5 | model for the coding agent |
| `NB_LOOP_TEST_COMMAND` | unittest discovery over `scripts/` | the gate command |
| `NB_LOOP_ONCE` | false | process a batch then exit (set by the plist) |
| `NB_LOOP_DRY_RUN` | false | gates only, no PR |

These are the budget kill-switch Risks.md (R1) calls for — start small, raise
only after a clean observed cycle.

## Discord

Posts to a `#engineering-loop` webhook if configured (keychain service
`neural-bridge-loop-webhook` or env `NB_LOOP_DISCORD_WEBHOOK`), else falls back
to the main NB webhook. Messages: claimed, PR opened, blocked, escalated, and a
per-run summary. Set the loop webhook with:

```
security add-generic-password -a "$USER" -s "neural-bridge-loop-webhook" -w "https://discord.com/api/webhooks/<id>/<token>"
```

## Installing the launchd job (only after you trust it)

The plist is **not** wired into `scripts/launchd/install.sh` on purpose. Install
it by hand:

```
cp scripts/launchd/com.andyherman.neural-bridge.loop-engineer.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.andyherman.neural-bridge.loop-engineer.plist
```

It runs `--once` on an hourly `StartInterval` (not `KeepAlive`, so the per-run
ceiling actually holds) and stops for the day once the per-day cap is hit. Kick a
run immediately with:

```
launchctl kickstart gui/$(id -u)/com.andyherman.neural-bridge.loop-engineer
```

Tear down:

```
launchctl bootout gui/$(id -u)/com.andyherman.neural-bridge.loop-engineer
rm ~/Library/LaunchAgents/com.andyherman.neural-bridge.loop-engineer.plist
```

## Design notes / deferred hardening

- **No repo-root CLAUDE.md.** The research recommends CLAUDE.md invariants that
  survive context compaction. We deliberately do *not* add one, because the
  Discord daemon also runs `claude -p` from the repo root and would inherit
  loop-agent framing. Instead the invariants live in the prompt
  (`prompts/implement_issue_v1.md`, re-sent every fresh session) and are
  re-asserted on each fix attempt, with the daemon-side test-integrity gate as
  the hard backstop. Per-attempt `max_turns` is low enough that a single attempt
  rarely triggers compaction anyway.
- **PreToolUse hook (deferred).** A hook that blocks Write/Edit to test files
  before the edit happens would save wasted turns. The current post-hoc git
  integrity gate already *prevents the PR*, so this is a cost optimization, not a
  safety gap. Worth adding when the squad's write-path-allowlist hook (2026-06-15
  report) lands, since they share machinery.
- **Provider fallback.** A 24/7 loop is exactly the workload that wants the Fugu
  provider-fallback shim (PRs #155/#156). Land those and the loop inherits
  resilience to a single-vendor outage.
