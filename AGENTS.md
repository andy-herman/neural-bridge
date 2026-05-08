# Neural Bridge — Project Schema

This file is the schema document for any AI agent (e.g. Claude Code, Cursor, Codex) working in this repository. Read it first.

## What this repo is

Neural Bridge is a personal multi-agent AI substrate. It runs on a Mac Mini 24/7 (target deployment) and exposes specialized agents via a chat transport. The substrate's job is to make agent work *compound* across sessions and across agents.

The runtime is [Claude Code](https://docs.claude.com/en/docs/claude-code); subagents and hooks live in the `.claude/` directory using the conventional schema.

## Architecture

Six layers, built bottom-up:

| Layer | V1 status | Lives in |
|---|---|---|
| 1. Agents | ✅ 3 active | `.claude/agents/*.md` |
| 2. Skills | inherits from user-level skills | (user-level) |
| 3. Transport | configured externally | (out of repo) |
| 4. Shared state | ✅ wiki skeleton, ⏳ daily logs, ⏳ hive.db | `knowledge/`, `daily-logs/` |
| 5. Orchestration | native subagent dispatch | (the agent CLI) |
| 6. Frontend | none in V1 (CLI only) | — |

## Directory layout

```
.claude/             Project-level coding-agent config
  agents/            Subagent definitions
  settings.json      Hooks, permissions
knowledge/           The wiki — LLM-maintained, never hand-edited
  AGENTS.md          Wiki-specific schema
  index.md           Starting point for any query
  log.md             Append-only chronological record
  concepts/          Cross-agent concept articles
  connections/       Cross-references between concepts
  agents/            Per-agent memory subdirectories
raw/                 External ingest (Web Clipper, papers) — gitignored
daily-logs/          Per-agent session summaries — gitignored
hooks/               Hook scripts (V2)
scripts/             Utility scripts: compile, flush, lint, query (V2)
docs/                Build-in-public blog drafts, design docs
```

## Conventions

- All wiki articles use YAML frontmatter with `type`, `created`, `tags`
- Wiki-links use `[[Page Name]]` format (Obsidian compatible)
- Daily logs are markdown, named `YYYY-MM-DD.md`, scoped per agent
- Concept articles live in `knowledge/concepts/` and are owned by the wiki, not any one agent

## Agent design rules

- Every agent has a clear `description` field — the parent uses it for routing
- Agents use lightweight models for routing/classification, larger models for actual work
- Sensitive tools (e.g. `send_email`) are scoped to one agent only via `disallowedTools` on the others
- Per-agent skills go in the agent's frontmatter; shared skills inherit from user-level

## Build status

V1 scaffold — created 2026-05-08. See [docs/STATUS.md](docs/STATUS.md) for the running build status.

## For AI agents reading this

If you're an AI agent working in this repo:

1. Read this file first.
2. Read [knowledge/index.md](knowledge/index.md) before answering any user query — it's the wiki entry point.
3. **Read broadly, write narrow.** Read `knowledge/concepts/`, `knowledge/connections/`, AND every `knowledge/agents/<role>/` subdirectory to maintain cross-agent context. Write only to your own `knowledge/agents/<your-role>/` subdirectory.
4. Don't write to `knowledge/concepts/` directly; that goes through the compile pass.
5. Match the voice in existing files: tight, sourced, opinionated. No marketing-speak.
