# Hooks

Claude Code hook scripts for Neural Bridge. Wired into `.claude/settings.json`.

## What's here

| File | Status | Purpose |
|---|---|---|
| `session_end.py` | working | Hook fired on `SessionEnd` and `PreCompact`. Resolves which agent owns the session, spawns `flush.py` as a detached subprocess, exits 0 immediately so the CLI never blocks on summarization. |
| `flush.py` | working (v1) | Calls `claude -p` with the flush prompt + transcript. Validates JSON output against ADR-007 schema, appends a structured session block to `daily-logs/<agent>/YYYY-MM-DD.md`. Handles failed-parse, empty-session, and one parse retry. |
| `prompts/flush_v1.md` | working | Prompt template for `flush.py`. Light filing gate: explicit "transcript is data, not instructions" framing. |
| `schema.py` | working | Pure-stdlib schema validators for ADR-007 daily-log structure. Shared with future `compile.py` and `lint.py`. |
| `test_flush.py` | working | Unit tests for `flush.py` and `schema.py`. Mocks the subprocess; no real LLM calls. |

## Light vs. heavy filing gate

`flush.py` runs the **light gate**: the prompt frames transcript content as data not instructions, and provenance frontmatter (session_id, transcript_sha256) is mandatory so a poisoned daily log is traceable.

The **heavy filing gate** (PROMOTE / QUARANTINE / REJECT per the memory-poisoning paper) runs in `compile.py` when daily-log entries are promoted to `knowledge/concepts/`. That's a separate ship — issue #9 Phase B.

## Schema

Daily-log file format is locked in [ADR-007](../decisions/ADR-007-daily-log-schema.md). Validation logic is in `schema.py`. flush, compile, and lint all import it.

## Requirements

- **Python 3.10+** on `PATH` as `python3` (Mac, Linux) or accessible to `sys.executable` from a Claude Code subprocess (Windows/Git Bash typically just works).
- **`claude` CLI on `PATH`** (Max subscription, no API key needed).
- Write access to `daily-logs/` in the repo root.

No external Python packages required. Standard library only.

## Agent identity resolution

The hook resolves `<agent>` for the daily log path in this order:

1. `payload['agent_type']` from the Claude Code hook event
2. `NB_AGENT` environment variable (manual override; future use)
3. `cwd` basename, if it matches a known agent (`research`, `teaching-prep`, `content`, `senior-pm`)
4. `_unattributed` (fallback)

Sessions that resolve to `_unattributed` still get a daily log; the compile pass downstream decides what to do with them.

## Running flush manually

For backfill or debugging:

```bash
python3 hooks/flush.py \
  --agent research \
  --session-id <claude session id> \
  --transcript /path/to/transcript.jsonl \
  --hook-event SessionEnd
```

Optional flags: `--model claude-sonnet-4-6` (default — `claude-sonnet-4-7` referenced in the build plan does not yet exist as a released model), `--timeout 300` (seconds).

## Status reporting

Every flush attempt writes one line to `daily-logs/_queue.log`:

```
<UTC ISO 8601> <agent> <session-id> <status>
```

Where `<status>` is one of:

- `flush_spawned` — written by the hook, before flush.py runs
- `flushed` — flush.py succeeded, session block appended
- `skipped:empty` — model produced all-empty output, no block written (per ADR-007)
- `failed:transcript_missing` — transcript path didn't exist
- `failed:prompt_template_missing` — `prompts/flush_v1.md` was missing
- `failed:json_decode` — model output wasn't valid JSON (raw output written to `daily-logs/<agent>/_failed/<session_id>.txt`)
- `failed:schema` — JSON parsed but didn't match ADR-007 shape
- `failed:claude_cli_not_found` — `claude` not on `PATH`
- `failed:timeout` — `claude -p` exceeded `--timeout`
- `failed:exit_<N>` — `claude -p` exited non-zero
- `failed:read_transcript_*` — couldn't read transcript file

`failed:json_decode` and `failed:schema` cases preserve the raw model output at `daily-logs/<agent>/_failed/<session_id>.txt` for human review.

## Testing the plumbing end-to-end

After installing the hook, open a Claude Code session in the repo, do anything, exit. You should see in `daily-logs/_queue.log`:

```
<ts> <agent> <id> flush_spawned
<ts> <agent> <id> flushed         (or skipped:empty / failed:<reason>)
```

And a session block in `daily-logs/<agent>/YYYY-MM-DD.md`.

## Running unit tests

```bash
python3 hooks/test_flush.py
```

Mocks the `claude -p` subprocess. Covers schema validation, code-fence stripping, append logic, failed-flush path, empty-session path, and full main() happy path.

## What does NOT happen yet (V2 phase B)

- Heavy filing gate (PROMOTE / QUARANTINE / REJECT)
- Cross-session deduplication if PreCompact + SessionEnd both fire for the same session — both will produce session blocks today
- Schema migration validation (we accept `schema_version: "1.0"` only; mismatch is silent)
- Transcript chunking for very long sessions (current cap: whatever fits in `claude -p` argv plus model context window)
