# Neural Bridge — Project Schema

This file is the schema document for any AI agent (e.g. Claude Code, Cursor, Codex) working in this repository. Read it first.

## What this repo is

Neural Bridge is a personal multi-agent AI substrate. It runs on a Mac Mini 24/7 (target deployment) and exposes specialized agents via a chat transport. The substrate's job is to make agent work *compound* across sessions and across agents.

The runtime is [Claude Code](https://docs.claude.com/en/docs/claude-code); subagents and hooks live in the `.claude/` directory using the conventional schema.

## Architecture

Six layers, built bottom-up:

| Layer | Status (2026-07) | Lives in |
|---|---|---|
| 1. Agents | ✅ 14 defined (13 registered on Discord) | `plugins/neural-bridge-core/agents/*.md` |
| 2. Skills | inherits from user-level skills; plugin-level skills still pending | (user-level) |
| 3. Transport | ✅ Discord daemon + Telegram bridge (Luna, Loid) | `scripts/discord_bot/`, `scripts/telegram_bot/` |
| 4. Shared state | ✅ wiki + daily logs + filing gate + Honcho peer memory | `knowledge/`, `daily-logs/`, `scripts/discord_bot/honcho_client.py` |
| 5. Orchestration | ✅ Discord daemon, senior-pm, cross-agent handoff, squad-discuss | `scripts/discord_bot/` |
| 6. Frontend | dashboard generator + fleet heartbeats into the Obsidian vault | `scripts/dashboard.py`, `scripts/fleet_heartbeat.py` |

## Agent roster

<!-- AGENTS-ROSTER:BEGIN — kept in sync with plugins/neural-bridge-core/agents/*.md; checked by `scripts/lint.py --check agents-roster` -->
| Agent | Role |
|---|---|
| `automation-engineer` | launchd agents, shell scripts, GitHub Actions, daemon and cron work |
| `content` | Long-form build-in-public drafts (blog, video scripts) |
| `docs-editor` | Internal docs: SOPs, ADRs, runbooks, READMEs, repo wiki drift |
| `echo` | Andy's voice-double: voice profile upkeep and AI-tell review of drafts |
| `librarian` | Maintains the Luna Master Obsidian vault: INDEX, audits, structure |
| `loid` | Career strategist; Synapse DB via CLI; Telegram and Discord |
| `luna` | Executive assistant: calendar and Gmail via MCP, proactive scheduling, handoffs |
| `recruiter` | Designs and provisions new specialist agents |
| `research` | Multi-source synthesis, citations, threat-model write-ups |
| `security-reviewer` | Audits prompts, flows, auth gates, and PRs for injection and leak risk |
| `senior-pm` | Issue and PR triage, board hygiene, weekly summaries |
| `social` | X growth: tweets, threads, cadence (drafts only, never publishes) |
| `teaching-prep` | INFO 310 virtual peer: slide stress-tests, lab alignment, examples |
| `ux-designer` | Look and feel for neural-bridge-blog and other web surfaces |
<!-- AGENTS-ROSTER:END -->

## Directory layout

```
.claude-plugin/        Marketplace manifest (this repo declares itself a Claude Code plugin marketplace)
  marketplace.json     Lists the plugins this repo ships
plugins/               One subdirectory per plugin
  neural-bridge-core/  V1 core plugin (3 specialist agents)
    .claude-plugin/
      plugin.json      Plugin manifest
    agents/            Subagent definitions
.claude/               Project-local coding-agent config (NOT plugin-shipped)
  settings.json        Hooks, permissions for sessions running in this repo
knowledge/             The wiki - LLM-maintained, never hand-edited
  AGENTS.md            Wiki-specific schema
  index.md             Starting point for any query
  log.md               Append-only chronological record
  concepts/            Cross-agent concept articles
  connections/         Cross-references between concepts
  agents/              Per-agent memory subdirectories
raw/                   External ingest (Web Clipper, papers) - gitignored
daily-logs/            Per-agent session summaries - gitignored
hooks/                 Lifecycle hooks: session_start, session_end, flush, discord_post (shipped, tested)
scripts/               compile.py, lint.py, dashboard.py, fleet_heartbeat.py, discord_bot/, telegram_bot/, launchd/
decisions/             Architecture decision records (ADRs)
docs/                  Build status, build plans, audits, lint reports, Honcho integration docs
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

V1 shipped and running (Discord daemon, memory pipeline, filing gate, Telegram bridges, Honcho peer memory). Scaffold created 2026-05-08; docs truth pass 2026-07-09. See [docs/STATUS.md](docs/STATUS.md) for the running build status.

## For AI agents reading this

If you're an AI agent working in this repo:

1. Read this file first.
2. Read [knowledge/index.md](knowledge/index.md) before answering any user query — it's the wiki entry point.
3. **Read broadly, write narrow.** Read `knowledge/concepts/`, `knowledge/connections/`, AND every `knowledge/agents/<role>/` subdirectory to maintain cross-agent context. Write only to your own `knowledge/agents/<your-role>/` subdirectory.
4. Don't write to `knowledge/concepts/` directly; that goes through the compile pass.
5. Match the voice in existing files: tight, sourced, opinionated. No marketing-speak.
