# Memory consolidation: design, held ready

**Status: designed, not executed.** Nothing here has been run. Execution is
gated on the evidence in step 0 below.

Phase 0 of the roadmap calls for collapsing the memory stack into "one durable
progress log plus one bounded, human-readable note store, re-read at session
start" (Anthropic primary sources, high confidence). This document turns that
into a concrete migration with decision gates, so it can be executed quickly
once the telemetry supports each call.

## Why this is gated rather than done

The 8-store sprawl was itself built one confident decision at a time. Merging
or deleting memory systems on inference would repeat exactly that. Every
retire/keep call below names the telemetry query that decides it.

The instrumentation shipped 2026-08-02 and the first 14 days of clean data are
already decisive for one layer. It is not yet decisive for three others.

## Current state, measured 2026-08-15

Eight stores. Sizes are real, not estimates.

| # | Store | Size / state | Injected? | Telemetry (14d) |
|---|---|---|---|---|
| 1 | `session_store` | 955 B | no | not memory; Claude session continuity |
| 2 | `conversation_log` | **4 files total** | no (grep on demand) | not instrumented |
| 3 | `semantic_index` | 13.4 MB | no (query on demand) | not instrumented |
| 4 | `lessons_digest` | **1 file** | yes, 4000 char cap | **1/7 = 14%, DEGRADED** |
| 5 | `honcho` | service up | yes, 2000 char cap | capture 6/6, card 22/25 = 88% |
| 6 | `luna_notes` | 16,089 B | yes, 8000 char cap | 1/1 |
| 7 | `echo_profile` | 11 files | yes (3 agents), 6000 cap | 1/1 |
| 8 | `repo_wiki` | 7 concepts, 8 quarantined | via SessionStart hook | last compile 2026-05-10 |

Four of these are injected into prompts and therefore compete for the same
context budget. That is the actual problem: worst case a Luna turn carried
about 73,000 characters of preamble.

## Target state

Two artifacts per agent, both human-readable, both re-read at session start.

```
Agents/<Name>/
  notes.md      # THE BOUNDED NOTE STORE. Curated, durable, injected.
                # Rules, preferences, decisions, open threads.
                # Already exists for Luna and is section-budgeted.
  progress.md   # THE PROGRESS LOG. Append-only narrative of what happened
                # and what is open. Yor's Journal/ is the working model:
                # 56 files, near-unbroken, the healthiest artifact in the
                # entire system.
```

Everything else becomes either **retrieval-only** (searched on demand, never
injected) or is **retired**.

## Migration steps

### Step 0. Decision gates (BLOCKING)

Do not execute any later step until its gate is answered from telemetry.

| Gate | Query | Decides |
|---|---|---|
| G1 | `lessons_digest` success rate over 30d | retire vs repair store 4 |
| G2 | `echo_voice` retrieve count for content/social vs luna | keep for 3 agents or 1 |
| G3 | `honcho_peer_card` non-empty rate over 30d | keep store 5 as an injected layer |
| G4 | any read of `knowledge/concepts` since 2026-05-10 | store 8 alive or dead |

**G1 is already answered.** 1/7 over 14 days, and the 6 failures are all
"no lessons-learned dir" for agents that have never had one. The layer serves a
single agent and costs a 4000-character budget slot on every other agent's
turn. Its stated job, "compress last week's signal into what to carry forward",
is the note store's job. **Retire it, fold its one live digest into that
agent's `notes.md`.**

G2, G3 and G4 need more traffic. They are cheap to answer once the fleet is in
regular use; all three are single queries against the telemetry log.

### Step 1. Introduce `progress.md` (additive, no deletions)

Create the progress log for each active agent and add it to the injection
chain behind `notes.md`. Model it on Yor's journal: narrative, append-only,
one short entry per working session, written at session close.

Reversible. Nothing is removed. This is the only step safe to run before the
gates are answered.

### Step 2. Retire `lessons_digest` (gate G1, ANSWERED)

1. Copy the single live digest into that agent's `notes.md` under a dated
   heading.
2. Delete `_lessons_block` from the injection chain in `mention.py`.
3. Leave `summarize_weekly.py` in place but retarget it to append to
   `progress.md` instead of writing a separate digest tree, or disable its
   launchd job. Do not delete the script in the same commit as the injection
   change; separate the behavior change from the code removal so a revert is
   one commit.
4. Reclaims 4000 characters of prompt budget on every turn for every agent.

### Step 3. Demote `conversation_log` and `semantic_index` to retrieval-only

They already are: neither is injected. This step is documentation plus a
guard so neither is added back to the injection chain. `conversation_log` has
4 files, which means the archive is nearly empty and the semantic index is
largely indexing the vault rather than agent conversations. Keep both; they
cost nothing per turn.

### Step 4. Decide `honcho` (gate G3)

Honcho overlaps `notes.md` directly: both answer "what do I know about Andy".
The difference is that Honcho is cross-agent and externally derived. Keep it
only if the peer card is non-empty at a materially better rate than today's
88%-reachable-but-sometimes-empty, and if its content is not restating what
`notes.md` already holds. Otherwise demote it to retrieval-only and let
`notes.md` be the injected answer.

### Step 5. Decide `repo_wiki` (gate G4)

Last compile 2026-05-10, 7 concepts, 8 quarantined, and the SessionStart hook's
`KNOWN_AGENTS` list is stale (9 of 14 agents). Either revive the compile
pipeline or delete it. It is currently neither maintained nor removed, which is
the worst of both.

### Step 6. Re-measure

After each removal, run the canary and confirm no store moved to SILENT or
DEGRADED. A consolidation that quietly breaks a surviving layer is the same
failure class this whole phase exists to prevent.

## Rollback

Every step is a separate commit. Steps 2, 4 and 5 remove an injection; the data
on disk is never deleted in the same commit as the code change. To roll back,
revert the commit; the store's files are still there.

## What this does NOT do

- Does not touch `session_store`. It is Claude session continuity, not memory,
  and it was only ever counted among the eight by accident of proximity.
- Does not merge the vault and the repo wiki into one knowledge store. That is
  a larger decision about whether `knowledge/` survives at all, and it belongs
  with Step 5 rather than inside it.
- Does not introduce a new memory technology. Adding a ninth store to fix
  having eight is the obvious trap.
