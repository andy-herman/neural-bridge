# Review queue

One place where agent output that needs Andy lands, with a button for each decision. Phase 1 of the 2026-08-02 roadmap ("stand up one push queue where draft PRs, agent questions, and capture proposals all land for approve, edit, answer, or rewind").

Code: `scripts/review_queue/`. Surface: Luna's Telegram bot. State: `~/Library/Application Support/neural-bridge/review-queue.db`, local only.

## Why

The fleet went dormant because every path waited for Andy to start a conversation, or pushed a one-way notice he could not act on. Meanwhile work that needed a decision piled up with nothing collecting it:

- Eight captures had sat in `knowledge/quarantine/` since 2026-05-10.
- Alerts arrived as messages with no acknowledge and no next step.
- The one approve gate in the system (`pr_proposals.py`) lived in memory for fifteen minutes.

The queue inverts that: producers write items, and the bot brings each one to Andy with the decision attached.

## What is live (slices A and B)

| Kind | Producer | Pushed | Buttons |
|---|---|---|---|
| capture | compile quarantine, synced every 10 minutes by the bridge | yes | File it, Reject (both confirm with a second tap), Snooze 1 day |
| alert | output watchdog, failure watcher (vault scripts), memory canary, auto-reload | yes, since 2026-10-11 | Acknowledge, Snooze 1 day |
| question | `hooks/flush.py`: each open question a session left, for attributed agents | yes | Answer, Dismiss, Snooze 1 day; or reply to the card |
| action | the Discord daemon: an agent's `open_pr_with_changes`, after validation | yes | Open the PR (confirms with a second tap), Reject |

In Telegram:

- `/queue` lists everything waiting, with a button that opens each item's card.
- A card is redrawn when its item changes: decided, applied, failed (with a Retry button) or cleared.

**Filing a capture.** The applier moves the quarantine file to `knowledge/concepts/` with `verdict: PROMOTE` and a `reviewed:` line, adds the concept to `knowledge/index.md`, and logs it in `knowledge/log.md`. Rejecting deletes the quarantine file; git history keeps the text. Either way:

- the applier opens a pull request and merges it at once;
- the PR title carries `review queue <id>`;
- the work happens in the private worktree `.trees/review-queue`, never in the shared checkout;
- the diff must stay inside `knowledge/` and pass the outbound guard before anything is pushed.

**Answering a question.** Reply to the card, or tap Answer and reply to the prompt it sends. The reply is taken before Luna's chat handler sees it, so it never becomes a conversation turn. The applier appends a dated entry to the agent's `Agents/<id>/progress.md` with the question and the answer, so the agent's next session opens with both. Dismiss records that Andy saw it.

- The card shows a one-line summary of the question. Email addresses, links, and phone and account numbers are removed, and the full text stays in the local database. Questions from Loid and Echo, whose material is private, show no text at all. Read and answer those at the desk with `show` and `answer`.
- Unattributed sessions (generic Claude Code) raise no questions. Nothing would read the answer: flush writes them no progress log, and session_start injects none.
- **Conversations are not duplicated.** Every turn of a Discord or Telegram conversation flushes the same session, so a question the agent has just asked in the chat counts as open as soon as the turn ends.
  - A new question is held for 30 minutes and pushed only if the conversation stays quiet that long.
  - Each later flush of the session replaces its undecided questions. One it no longer lists was answered in the chat, and is cleared.
  - A reworded question counts as the same question.
  - One Andy already answered or dismissed on a card is not raised again.
- If the record of a reply's prompt was lost (Telegram delivered the prompt, but the send timed out), the item id shown in the bot's message still finds the question. Only the bot's own messages count.

**Approving an action.** The proposal is validated and screened by the outbound guard when it is staged, and screened again before the push.

- Open the PR pushes the branch and opens the pull request. Nothing merges.
- `approve <id>` and `cancel <id>` in the Discord channel still work and decide the same item, so whichever lands second is told it was already handled.
- An action expires after 24 hours, because its files were written against main as it stood then. Until 2026-10 these proposals lived in memory for 15 minutes and died with any daemon restart.

**The question backlog.** Before the queue existed, flush had written 339 open questions into `daily-logs/`, May to October 2026, all from unattributed sessions. Most are stale. Triage them once, at the desk:

```bash
python -m scripts.review_queue backlog --month 2026-08
python -m scripts.review_queue backlog keep ID --agent AGENT
python -m scripts.review_queue backlog close
```

`keep` turns a question into a live item routed to an agent, so the answer lands in that agent's progress log. `close` records the triage; the rest stay in `daily-logs/`, unanswered.

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

1. **Read-back.** It takes a probe item through its whole lifecycle (create, read back, decide, claim, apply) and confirms the event log.
2. **Pusher heartbeat.** It must be under 10 minutes old; the bridge writes it on every successful loop.
3. **Unsent backlog.** No pushable item may stay unsent for more than 15 minutes.
4. **Appliers.** Any failed, stuck or interrupted apply is named.

The pusher also writes `review-queue-status.json` beside the database. The failure watcher reads that file and alerts directly on Telegram when the heartbeat goes stale. That path is deliberately out of band: the queue cannot report its own death.

## Shadow week, then alerts go through the queue

Alerts were queued from day one, but for the first week the bot pushed only captures and the producers' existing Telegram messages remained the push, so nothing arrived twice. The bot pushes the kinds in `health.DEFAULT_PUSH_KINDS`, overridable with `NB_QUEUE_PUSH_KINDS`.

**Cutover** happened on 2026-10-11, after a clean week, meaning:

- `health` passed every day;
- every direct alert had a matching item (`python -m scripts.review_queue list --all --kind alert`).

The week turned up one false alarm: the memory canary called Luna's and Echo's stores SILENT because other agents had run, though none of their readers had. Stores now name their readers (`readers` in `scripts/memory_canary.py`) and stay idle until one of them takes a turn.

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
