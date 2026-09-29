# Review queue

One place where agent output that needs Andy lands, with a button for each decision. Phase 1 of the 2026-08-02 roadmap ("stand up one push queue where draft PRs, agent questions, and capture proposals all land for approve, edit, answer, or rewind").

Code: `scripts/review_queue/`. Surface: Luna's Telegram bot. State: `~/Library/Application Support/neural-bridge/review-queue.db`, local only.

## Why

The fleet went dormant because every path waited for Andy to start a conversation, or pushed a one-way notice he could not act on. Meanwhile work that needed a decision piled up with nothing collecting it:

- Eight captures had sat in `knowledge/quarantine/` since 2026-05-10.
- Alerts arrived as messages with no acknowledge and no next step.
- The one approve gate in the system (`pr_proposals.py`) lived in memory for fifteen minutes.

The queue inverts that: producers write items, and the bot brings each one to Andy with the decision attached.

## What is live (slice A)

| Kind | Producer | Pushed | Buttons |
|---|---|---|---|
| capture | compile quarantine, synced every 10 minutes by the bridge | yes | File it, Reject (both confirm with a second tap), Snooze 1 day |
| alert | output watchdog, failure watcher (vault scripts), memory canary, auto-reload | not yet, see shadow week | Acknowledge, Snooze 1 day |

In Telegram:

- `/queue` lists everything waiting, with a button that opens each item's card.
- A card is redrawn when its item changes: decided, applied, failed (with a Retry button) or cleared.

**Filing a capture.** The applier moves the quarantine file to `knowledge/concepts/` with `verdict: PROMOTE` and a `reviewed:` line, adds the concept to `knowledge/index.md`, and logs it in `knowledge/log.md`. Rejecting deletes the quarantine file; git history keeps the text. Either way:

- the applier opens a pull request and merges it at once;
- the PR title carries `review queue <id>`;
- the work happens in the private worktree `.trees/review-queue`, never in the shared checkout;
- the diff must stay inside `knowledge/` and pass the outbound guard before anything is pushed.

## The producer contract

Every call is idempotent, so a producer can report its whole state on every run. The CLI is the contract; the vault scripts and `auto_reload.sh` shell out to it with the repo's venv Python.

```bash
python -m scripts.review_queue raise --source S --key K --kind alert --title "..." [--detail "..."] [--ref URL] [--agent A]
python -m scripts.review_queue clear --source S --key K
echo '[{"key": "k", "title": "..."}]' | python -m scripts.review_queue sync --source S --kind alert
```

- **Alerts are incidents.** A live `(source, key)` is raised once, and later raises only refresh its detail. `clear` resolves it: an undecided item is superseded and its card says so. The same key raised later is a new item.
- **Captures are unique.** The key is the file's blob hash, so a decided capture never comes back, even while a stale checkout still shows the file.
- **`sync` replaces the source's live set,** so a check removed from a producer's list resolves on its own.

A producer must never fail because the queue is unavailable: log it and carry on. The queue's own health check catches a queue that stops taking items.

## Health

`python -m scripts.review_queue health` checks four things (the memory canary runs the same check daily and fails when it does):

1. **Read-back.** It writes a probe item, reads it back, decides it, and confirms the event log.
2. **Pusher heartbeat.** It must be under 10 minutes old; the bridge writes it on every successful loop.
3. **Unsent backlog.** No pushable item may stay unsent for more than 15 minutes.
4. **Appliers.** Any failed, stuck or interrupted apply is named.

The pusher also writes `review-queue-status.json` beside the database. The failure watcher reads that file and alerts directly on Telegram when the heartbeat goes stale. That path is deliberately out of band: the queue cannot report its own death.

## Shadow week, then alerts go through the queue

Alerts are queued from day one, but the bot pushes only the kinds in `health.DEFAULT_PUSH_KINDS` (captures), overridable with `NB_QUEUE_PUSH_KINDS`. For the first week the producers' existing Telegram messages remain the push, so nothing arrives twice, and `/queue` already lists the alerts.

**Cutover** happens after a clean week, meaning:

- `health` passed every day;
- every direct alert has a matching item (`python -m scripts.review_queue list --all --kind alert`).

The steps:

1. Add `alert` to `DEFAULT_PUSH_KINDS` (a one-line PR).
2. Remove the direct Telegram sends from the output watchdog and the failure watcher's per-agent alerts.
3. Keep the failure watcher's Fleet-API and queue-liveness alerts direct.

## Measuring Phase 1

```bash
python -m scripts.review_queue stats --days 7
```

The command reports:

- decisions per kind;
- median hours to decision;
- items created per source (a wired producer that goes quiet shows up as missing);
- how many items are waiting, and the oldest one's age.

The bar after four weeks:

- at least 5 decisions a week;
- median time to decision under 24 hours;
- under 20 percent expired or bulk-dismissed.

Missing the bar is the signal to rethink the surface or the producers before Phase 2, not to add agents.

## Privacy

Telegram is a third-party channel, so a card carries metadata only: kind, agent, title, one line of detail and a link. Rules:

- Links are only rendered for `https://` refs, so a vault path never becomes a link.
- The database holds titles and pointers, lives under `~/Library/Application Support`, and is created mode 0600.
- Tests never touch it: under a test runner `db_path()` points at a temporary directory unless `NB_REVIEW_QUEUE_DB` is set.

## Next slices

- **B.** Flush `open_questions` become question items, answered with a ForceReply that lands in the agent's progress log. Discord action blocks become action items, retiring `pr_proposals.py`'s in-memory store.
- **C.** Loop-engineer draft PRs become pr_review items. Approve merges when checks pass; rewind resets the issue.
- **D.** Needs Andy is regenerated from the queue on the Mac, Sunday publish prep becomes publish items, and the persona summon surface is demoted.

Scope and evidence: vault note `Neural Bridge/2026-09-28 - Phase 1 review queue scope.md`.
