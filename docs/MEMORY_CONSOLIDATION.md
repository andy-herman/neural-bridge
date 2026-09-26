# Memory consolidation: design, held ready

**Status: executed except G2 (2026-09-25).** Steps 1 to 5 are done or
decided. G2 (voice profile for three agents or Luna only) is instrumented and
waits on attributed reads; Step 6 is the re-measure. `python -m
scripts.memory_canary --gates --days 30` answers each gate in one command. See
"Execution record" at the end.

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
| 8 | `repo_wiki` | 7 concepts, 8 quarantined | via SessionStart hook | last compile 2026-05-10 (see execution record: read loop and telemetry added 2026-09-22) |

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

## Execution record

### 2026-08-16: Step 2 done

`lessons_digest` retired (commit `4b56023`). Its one live digest was folded
into Luna's `notes.md`; the 4000 reclaimed characters paid for raising her
notes budget to 12,000.

### 2026-09-22: Steps 1, 3, 5 done; gates made answerable

The September audit found the wiki's read loop had never closed: articles were
compiled once (2026-05-10) and no code path ever opened one. It also found the
reason 13 of 14 agents had never written a concept: the Discord daemon stamps
`NB_AGENT_ID` on every turn, but the SessionEnd hook only read `NB_AGENT`, so
every Discord turn was flushed as `_unattributed`, which `compile.py` skips.

- **Step 1 done.** `scripts/discord_bot/progress_log.py` is the progress log.
  `hooks/flush.py` appends one dated entry per non-empty session at session
  close (decided / found / open); `mention.py` injects the most recent
  entries, 3000 characters, behind `notes.md` on every mention. The store is
  `progress_log` in the canary. A missing file is recorded as ok with zero
  chars, deliberately: lessons_digest died of counting "no file yet" as
  failure. Adoption is reported by `--gates`.
- **Step 3 done.** `test_mention.py` carries a guard that fails if
  `semantic_search` or a `conversation_log` reader is ever imported into the
  prompt builder. Both stores stay retrieval-only.
- **Step 5 decided: revive, not delete.** PR #162 fixed the compile state
  bug, unified the agent roster, honoured `NB_AGENT_ID` at SessionEnd so agent
  work actually reaches `daily-logs/<agent>/`, added query-time retrieval
  (`hooks/wiki_recall.py`, the UserPromptSubmit hook, and the Discord prompt
  builder), and instrumented flush, compile, and recall so the canary covers
  the wiki. G4 is therefore answered structurally: the wiki is read on every
  turn and the reads are counted. Whether it is *useful* is the grounding
  metric in the canary report; if that stays near zero for a month, the
  delete branch of Step 5 reopens.
- **Gates G2 and G3 (Step 4) still open.** They need Mac-side telemetry.
  Run `python -m scripts.memory_canary --gates --days 30`. G3 demotes Honcho
  to retrieval-only if its peer card is non-empty less than 90% of the time.
- **Step 6.** After the Mac pulls this, run the canary daily for a week and
  confirm `progress_log`, `flush_daily_log`, and `wiki_recall` are healthy
  and `compile_concepts` stops being SILENT once the nightly job has run.

Stores after this pass: `notes.md` (curated, injected), `progress.md`
(narrative, injected), `repo_wiki` (compiled concepts, injected on relevance),
`honcho` (injected, kept per G3), `echo_profile` (injected, pending G2),
`conversation_log` and `semantic_index` (retrieval-only). `session_store` is
not memory.

### 2026-09-25: deployed, the write path repaired, Step 4 decided

**The 2026-09-22 work had never run on the Mac.** The auto-reload watcher had
exited 1 on every tick since 2026-05-13 (fixed in #164), so #162 and #163 sat
undeployed until today. Once deployed, the canary showed `flush_daily_log`
FAILING, which exposed two defects that had kept the write side of the whole
pipeline dead:

- **flush ran on the wrong route.** It is the SessionEnd hook of every agent
  turn and inherits that turn's environment, which points at the copilot-api
  proxy. Its model is `claude-sonnet-5`, and Claude 5 models fail through
  Claude Code on the proxy: 400, "does not support assistant message
  prefill" (copilot-api's log held 456 of these). So no agent turn has ever
  produced a daily log or a progress entry, and compile had nothing to read.
- **The direct route was poisoned by `~/.hermes/.env`.** Since 2026-08-16 the
  Telegram bridges load it, and its `ANTHROPIC_API_KEY` has no credit. Any key
  or base URL overrides the Claude Code login, so a direct call spawned under
  it failed with "Credit balance is too low".

`hooks/claude_env.py` now owns the route policy, and every call pins its route
whatever it inherited. Andy chose to keep the memory pipeline off Max limits,
so flush, compile and lint now run on the copilot-api proxy on
`claude-opus-4.8` (`PIPELINE_MODEL`), the fleet's own route and model; tests
fail if a pipeline model is one the proxy rejects. The fleet's proxy branch
forces its placeholder key instead of letting an inherited one win, and the
loop engineer's direct branch strips inherited keys. Verified end to end:
flush, run inside a simulated agent turn with a foreign base URL and the
`.env` key both inherited, made a real call on the proxy and wrote a correct
progress entry (decision, three findings, open question) from a synthetic
research session.

The filing gate was calibrated on `claude-sonnet-5` (#158), so the model change
was checked with `scripts/eval_filing_gate.py` (votes=3, 15 cases) on
`claude-opus-4.8`: 14/15 passed, `attack_admitted` 0 and `false_admit` 0, so
no injection, poison, thin or self-promoting case got in. `benign_blocked` 1:
`borderline-attack-catalog` was quarantined instead of promoted, the safe
direction and a tuning signal only. No Sonnet 5 run was ever recorded, so
there is no baseline to diff against; this run is now the baseline.

- **Step 4 decided: keep Honcho injected (G3).** Peer card non-empty in 194 of
  197 retrieves (98.5%, 2026-08-15 to 2026-09-25), above the 90% bar. On
  content, the card is mostly facts `notes.md` does not hold (employer, next
  role, expertise, sponsor, the career strategist), and it is the only
  about-Andy context for the twelve agents that are not Luna, so it does not
  restate the note store.
- **G2 still open, now answerable.** All 37 `echo_voice` reads on record carry
  no agent, so the gate could not be decided; it used to print "keep for all
  three" anyway. Reads are now attributed, unattributed ones no longer vote,
  and the gate reports "undecided" until attributed reads exist. Decide after
  30 days of them.
- **G4 no longer reports "dead" for a window with no evidence.** It now calls
  the wiki dead only when agent turns ran under the code that reads it
  (`progress_log` retrieves, logged beside every wiki read) and none read it.
  Otherwise it says "undecided".
- **Step 6, what to watch.** After the next real agent turns, the daily canary
  should show `flush_daily_log` and `progress_log` healthy and `--gates` should
  list agents under progress_log adoption. The first nightly compile after a
  flushed turn should turn `compile_concepts` healthy. If flush fails again,
  the canary's FAIL line now carries the head of the API error (it used to
  show only `exit_1`), and the full output is in
  `daily-logs/<agent>/_failed/<session>.txt`.

