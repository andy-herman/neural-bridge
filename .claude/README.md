# `.claude/` — Neural Bridge project-level Claude Code config

## What's here

- **`settings.json`** — project-level Claude Code settings. Wires the `SessionEnd` and `PreCompact` hooks that fire `hooks/session_end.py` for the V2 daily-log pipeline.

## V2 enforcement model (planned, not yet active)

`AGENTS.md` line 55 says:

> "Sensitive tools (e.g. `send_email`) are scoped to one agent only via `disallowedTools` on the others."

V1 has nothing sensitive to scope (no MCP tools that mutate external state are wired yet), so `permissions.allow` is currently `[]` and no `disallowedTools` are set on any agent. This is a deliberate scaffold-only state.

When V2 wires:
- A Discord push tool (per #28) — restricted to a future `dispatch` agent or to `senior-pm` only
- An email/Slack outbound tool — restricted to `content` agent only after explicit user authorization in the request
- A GitHub-write tool — restricted to `senior-pm` only (it already has `Bash` + `gh`)

…each addition gets a corresponding `disallowedTools` block on every agent that should NOT have it, listed in the agent's frontmatter. The agent definition is the canonical place; `settings.json` becomes the project-wide allow-list.

## Why this README exists

JSON does not allow comments. This file is the comment-block for `settings.json`. If you change the hook config or add `permissions.allow` entries, update this file too.

## Hook plumbing

The `SessionEnd` and `PreCompact` hooks both fire `python3 hooks/session_end.py`, which spawns `hooks/flush.py` as a detached subprocess. Flush calls `claude -p` with the prompt at `hooks/prompts/flush_v1.md` and writes ADR-007-shaped session blocks to `daily-logs/<agent>/YYYY-MM-DD.md`. See `hooks/README.md` for full status reference.
