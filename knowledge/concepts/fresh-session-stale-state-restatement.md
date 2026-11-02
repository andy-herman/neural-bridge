---
slug: fresh-session-stale-state-restatement
verdict: PROMOTE
reason: 3-vote gate (3P/0Q/0R) -> PROMOTE; majority promote, no reject
checks_triggered: []
gate_votes: [PROMOTE, PROMOTE, PROMOTE]
compiled_at: 2026-10-14T10:00:36Z
compiler_version: "1.3"
reviewed: "approved by Andy, 2026-10-16; personal and work specifics generalized before publishing"
sources:
  - agent: luna
    session_id: 1d4288b3-5e33-40e0-87bd-5cf819bd6a5c
    transcript_sha256: ca40f61c286a0d4805279a8c21ab1e3e4b447a1b01ef0111fece11426e7d3caf
    source_log: daily-logs/luna/2026-10-13.md
    session_n: 4
---

# fresh-session-stale-state-restatement

> A fresh per-mention session sees only recent context, so an item that was already settled in an earlier exchange reads as new again unless memory explicitly marks it closed.

## Why this matters

Luna runs a new session on each @-mention, seeing only recent context rather than the full history of what has been said and settled. Without a durable signal that an item is closed, the agent re-surfaces it as if encountering it for the first time. In one case this produced three consecutive congratulations on the same work milestone, which Andy had already acknowledged and called out directly. The same blind spot drives misreads: a calendar entry with no owner gets re-interpreted from scratch each time.

This earns a slot in shared memory because it is a structural property of the per-mention session model, not a one-off slip. Any agent that spins up fresh per interaction inherits it.

## Key points

- Each @-mention starts a fresh session scoped to recent context, with no memory of what prior sessions already resolved.
- Settled items read as new unless memory carries an explicit "closed" flag; the absence of such a flag is the failure mode, not the presence of wrong data.
- The repeated congratulation happened across three mentions because nothing recorded that the news had already been acknowledged.
- A travel entry on a shared calendar carried no attendee or owner, so the fresh session had to guess whose trip it was. It guessed Andy; the traveler was a family member.
- The two symptoms (the repeated congratulation and the misread trip) share one root cause: stale state restated as fresh.

## How we use it

Let closed items stay closed. Once Andy has acknowledged something, it is recorded as closed in memory and not raised again unless he brings it up. An entry whose owner is unknown is recorded as such once it has been clarified. These are memory-level closures: the kind of explicit flag whose absence caused the original restatement.

## Open questions

- How does a settled item get durably flagged closed, so a later fresh session reads it as resolved rather than new?
- Should calendar entries without an attendee or owner be treated as unowned by default, rather than assumed to be Andy's?
