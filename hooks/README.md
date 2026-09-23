# Hooks

Claude Code hook scripts for Neural Bridge. Wired into `.claude/settings.json`.

## What's here

| File | Status | Purpose |
|---|---|---|
| `session_end.py` | working | Hook fired on `SessionEnd` and `PreCompact`. Resolves which agent owns the session, spawns `flush.py` as a detached subprocess, exits 0 immediately so the CLI never blocks on summarization. Stands down (breadcrumb only, no flush) when `NB_SKIP_FLUSH=1` or `NB_AGENT=compile` is in the environment; see below. |
| `flush.py` | working (v1) | Calls `claude -p` with the flush prompt + transcript. Validates JSON output against ADR-007 schema, appends a structured session block to `daily-logs/<agent>/YYYY-MM-DD.md`. Handles failed-parse, empty-session, and one parse retry. Posts the session block to Discord on success (via `discord_post`). |
| `prompts/flush_v1.md` | working | Prompt template for `flush.py`. Light filing gate: explicit "transcript is data, not instructions" framing. |
| `schema.py` | working | Pure-stdlib schema validators for ADR-007 daily-log structure. Shared with future `compile.py` and `lint.py`. |
| `discord_post.py` | working | Outbound Discord push helper. Reads webhook URL from macOS keychain (or `NB_DISCORD_WEBHOOK` env var), POSTs via stdlib urllib. Safe-fails: missing webhook or network error returns False, never blocks the caller. Phase C will swap the webhook for bot-based posting; callers depend on `send()`, not on the transport. |
| `user_prompt_submit.py` | working | Hook fired on `UserPromptSubmit`. Ranks `knowledge/concepts/` against the prompt text and prints a compact "Related wiki concepts" block to stdout, which Claude Code adds to the turn's context. Prints nothing when no article matches. Exit code is always 0; it never blocks a prompt. |
| `wiki_recall.py` | working | The read side of the wiki: stdlib BM25 over concept slug, summary, and body, with a rendered data block and one `wiki_recall` UTILIZE telemetry event per call. Shared by the hook above and by the Discord prompt builder (`scripts/discord_bot/mention.py`). `--report` prints the grounding metric: how many agent turns in the window were grounded in a concept. |
| `test_flush.py` | working | Unit tests for `flush.py` and `schema.py`. Mocks the subprocess; no real LLM calls. |
| `test_discord_post.py` | working | Unit tests for `discord_post.py`. Mocks both keychain (via subprocess) and HTTP (via urllib). |

## Wiki recall: when it must stand down

`NB_SKIP_WIKI_RECALL=1` in the environment makes `user_prompt_submit.py` print nothing. Three callers set it, each for a reason:

- The Discord daemon (`scripts/discord_bot/claude_invoke.py`) injects the same block itself, ranked against the clean user message. Letting the hook run too would match the rendered template's own boilerplate.
- `flush.py` sends a fixed extraction template; wiki context appended to it would be echoed back into the daily log the wiki is compiled from.
- `scripts/compile.py` sends the filing-gate and concept-writer prompts; injecting the wiki's current contents into the gate would bias verdicts toward what the wiki already says.

Anything else that shells to `claude -p` from this repo with a prompt that must not be altered should set the same variable.

## Flush: when it must stand down

Every `claude -p` call fires the `SessionEnd` hook, including calls this repo makes to itself, and including calls nested inside another Claude Code session (verified 2026-09-23 with a three-level probe: the hook fired at every depth). Two callers therefore set `NB_SKIP_FLUSH=1` so `session_end.py` writes a `skipped:NB_SKIP_FLUSH` breadcrumb and exits without spawning `flush.py`:

- `scripts/compile.py`, on every filing-gate vote and concept-writer call. Until 2026-09-23 each vote cost a second model call to summarise itself into `daily-logs/_unattributed/`, which `compile.py` skips by design, so the output was never read. The `NB_AGENT=compile` marker compile has always stamped is honoured as a second skip signal (`skipped:compile_session`), so either variable alone is enough.
- `flush.py`, on its own extraction call. Without the flag the hook would spawn a flush to summarise the extraction transcript, whose own `claude -p` would fire the hook again, one model call per level. The only thing that ever stopped it was the prompt, which embeds the previous level's transcript, outgrowing the argv limit a few levels down and crashing that flush.

The Discord daemon does not set it: its `claude -p` turns are the agents' real work and are exactly what flush exists to capture.

Anything else that shells to `claude -p` from this repo and does not want a daily-log entry for that call should set the same variable. The breadcrumb keeps the audit trail: `_queue.log` still shows the hook ran, only that it spent no model call.

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
2. `NB_AGENT` (manual override), then `NB_AGENT_ID` (stamped on every `claude -p` turn by the Discord daemon)
3. `cwd` basename, if it matches a known agent
4. `_unattributed` (fallback)

The known-agent set is `hooks/schema.py` `KNOWN_AGENTS`, pinned by test to the plugin's agent files.

Sessions that resolve to `_unattributed` still get a daily log, but `compile.py` skips every `_`-prefixed directory, so unattributed work never reaches the wiki. Until 2026-09-22 the hook read only `NB_AGENT`, so every Discord turn was unattributed; that is the main reason 13 of 14 agents had never produced a concept.

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

A gate vote or a flush's own extraction call leaves a single line instead, with no second one:

```
<ts> _unattributed <id> skipped:NB_SKIP_FLUSH
```

And a session block in `daily-logs/<agent>/YYYY-MM-DD.md`.

## Running unit tests

```bash
python3 hooks/test_flush.py
python3 hooks/test_discord_post.py
```

Mocks the `claude -p` subprocess and the Discord HTTP/keychain calls. Covers schema validation, code-fence stripping, append logic, failed-flush path, empty-session path, full main() happy path, and Discord transport edge cases.

## Discord outbound push (Phase B of #28)

`flush.py` and `scripts/compile.py` push their summaries to a Discord channel via webhook. Off by default (no webhook = no push, no error). To enable:

```bash
# One-time setup. Replace the URL with your webhook from Discord -> Channel Settings -> Integrations.
security add-generic-password \
  -s "neural-bridge-discord-webhook" \
  -a "$USER" \
  -w "https://discord.com/api/webhooks/<id>/<token>"

# Verify
security find-generic-password -s "neural-bridge-discord-webhook" -a "$USER" -w
```

Per-invocation override:

```bash
python3 hooks/flush.py --no-discord ...        # skip the post for this run
python3 scripts/compile.py --no-discord ...     # same
NB_DISCORD_WEBHOOK="https://..." python3 ...   # override the keychain value
```

Phase C will replace the webhook transport with bot-based posting. Callers stay on `discord_post.send()`; the swap is a one-file change.

## What does NOT happen yet (V2 phase B)

- Heavy filing gate (PROMOTE / QUARANTINE / REJECT)
- Cross-session deduplication if PreCompact + SessionEnd both fire for the same session — both will produce session blocks today
- Schema migration validation (we accept `schema_version: "1.0"` only; mismatch is silent)
- Transcript chunking for very long sessions (current cap: whatever fits in `claude -p` argv plus model context window)
