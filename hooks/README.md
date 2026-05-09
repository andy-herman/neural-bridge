# Hooks

Claude Code hook scripts for Neural Bridge. Wired into `.claude/settings.json`.

## What's here

| File | Status | Purpose |
|---|---|---|
| `session_end.py` | working | Hook fired by Claude Code on `SessionEnd` and `PreCompact`. Resolves which agent the session was for, spawns `flush.py` as a detached subprocess, exits 0 immediately so the CLI never blocks on summarization. |
| `flush.py` | **STUB** | V1 plumbing-only. Appends a STUB session block to `daily-logs/<agent>/YYYY-MM-DD.md` to verify the hook -> flush -> daily-log path works end-to-end. The real flush logic (LLM-driven session summarization) ships in [issue #9, Phase A](https://github.com/andy-herman/neural-bridge/issues/9). |

## Schema

Daily-log file format is locked in [ADR-007](../decisions/ADR-007-daily-log-schema.md). The stub flush emits the same section structure (`Decisions / Findings / Open questions / Proposed concepts`) so when the real flush replaces the stub, downstream consumers (`compile.py`, `lint.py`) don't need changes.

## Requirements

- **Python 3.10+** on `PATH` as `python3` (Mac, Linux) or accessible to `sys.executable` from a Claude Code subprocess (Windows/Git Bash typically just works).
- Write access to `daily-logs/` in the repo root.

No external Python packages required. Standard library only.

## Agent identity resolution

The hook resolves `<agent>` for the daily log path in this order:

1. `payload['agent_type']` from the Claude Code hook event
2. `NB_AGENT` environment variable (manual override; future use)
3. `cwd` basename, if it matches a known agent (`research`, `teaching-prep`, `content`, `senior-pm`)
4. `_unattributed` (fallback)

Sessions that resolve to `_unattributed` still get a daily log; the compile pass downstream decides what to do with them (typically: route per-content rather than per-agent).

## Testing the plumbing

After installing the hook (settings.json), open a Claude Code session in the repo, do anything, and exit. You should see:

- A new line in `daily-logs/_queue.log`: `<timestamp> <agent> <session_id> flush_spawned` followed shortly by `<timestamp> <agent> <session_id> stub_flushed`
- A new file at `daily-logs/<agent>/YYYY-MM-DD.md` (or appended block if the file exists for today) with a `## STUB Session` heading

If you see `failed:` lines instead, check the status field for the failure mode (`bad_payload`, `transcript_missing`, `flush_script_missing`, `spawn_*`).

## What does NOT happen yet (V2)

- Actual LLM summarization of the transcript
- Filing-gate / quarantine logic
- Cross-session deduplication (same `session_id` re-fired won't be deduped — V2 work)
- Schema-version validation on read (V2 work)
