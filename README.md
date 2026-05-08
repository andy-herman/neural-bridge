# Neural Bridge

A personal AI substrate: multiple specialized agents sharing a markdown wiki memory and a chat-based mobile interface, so your agent work *compounds* instead of fragmenting across point tools.

> 🚧 **V1 scaffold — not yet functional.** This repo contains the directory structure, agent definitions, and wiki skeleton for V1. Hook scripts and the compile pipeline land in subsequent iterations.

## The problem this solves

Most personal AI workflows fragment into point tools — a chat tab here, a coding session there, a separate research workflow somewhere else. You repeat the same setup, lose context between tools, and start fresh every time.

Spinning up *"yet another AI helper"* project makes it worse — every project is a silo. None of them share memory. None of them compound.

## What it is

Neural Bridge is a *substrate*: one place where specialized agents live together, share a markdown wiki memory, and (eventually) reach you on your phone.

- **Specialized agents** — one for research, one for teaching prep, one for content drafting (or whatever roles fit your life)
- **A shared markdown wiki** — agents read across each other's memory, write into their own subdirectories, and the wiki compounds with every session
- **An external chat transport** — reach the system from your phone via the messaging app of your choice
- **Built on a coding-agent CLI** — uses [Claude Code](https://docs.claude.com/en/docs/claude-code) as the runtime, riding your existing subscription

The wiki is **maintained by LLMs, not by hand**. That's the [Karpathy pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f), adapted for cross-agent personal use following Cole Medin's [claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler).

## Who this is for

- Knowledge workers and educators running multi-domain lives (research + teach + ship)
- Power users with [Claude Max](https://www.anthropic.com/pricing) who want their agents to compound across projects
- Anyone tired of starting fresh every time they open a new AI tool

## Requirements

- [Claude Code](https://docs.claude.com/en/docs/claude-code) — the coding-agent CLI this scaffold is configured for
- (Optional but recommended) [Obsidian](https://obsidian.md/) for viewing the wiki with graph view + backlinks
- Mac, Linux, or Windows

## Architecture (six layers)

```
┌──────────────────────────────────────────────────────┐
│  6. Frontend       — V3 (web dashboard, graph)       │
│  5. Orchestration  — native subagent dispatch        │
│  4. Shared state   — wiki + daily logs + hive (V2)   │
│  3. Transport      — mobile chat (external)          │
│  2. Skills         — inherited from user-level       │
│  1. Agents         — .claude/agents/*.md             │
└──────────────────────────────────────────────────────┘
```

V1 ships layers 1, 2, and the wiki side of 4. The rest lands in V2/V3.

## Status

| Layer | V1 (this repo) | V2 (next) | V3 (later) |
|---|---|---|---|
| Agents | ✅ 3 definitions in `.claude/agents/` | + per-domain specialists | |
| Skills | inherits from user-level | | |
| Transport | external (configured separately) | + custom mobile bridge | + voice war room |
| Shared state | ✅ wiki skeleton in `knowledge/` | + SQLite hive | |
| Orchestration | native subagent dispatch | + supervisor process | |
| Frontend | none — CLI only | + web dashboard | + activity graph |

## Repo map

```
.claude/agents/        subagent definitions
.claude/settings.json  hooks + permissions
knowledge/             markdown wiki (LLM-maintained)
  AGENTS.md            wiki schema doc
  index.md             always-loaded starting point
  log.md               chronological append-only record
  concepts/            cross-agent concept articles
  connections/         explicit cross-references
  agents/              per-agent memory subdirectories
daily-logs/            per-agent session summaries (gitignored)
raw/                   external ingest landing (gitignored)
hooks/                 hook scripts (V2)
scripts/               compile / flush / lint / query (V2)
docs/                  build journal, design docs
AGENTS.md              project schema for any AI agent in the repo
ATTRIBUTION.md         credits and prior art
```

## View it in Obsidian

The repo is designed to be opened directly as an [Obsidian](https://obsidian.md/) vault. Open the Neural-Bridge folder in Obsidian to get:

- Graph view across the wiki
- Wiki-link navigation between concept articles
- Backlinks panel
- Search across all markdown content

Open it as a *separate vault* from any personal vault you already keep — Neural Bridge's wiki is intentionally project-scoped.

## Setup

> Full setup docs land after V1 review. V1 is a scaffold — agents don't yet do useful work; V2 wires up the hooks and compile pipeline. Watch [docs/STATUS.md](docs/STATUS.md) for build progress.

The short version once V2 ships:

1. Clone this repo
2. Install [Claude Code](https://docs.claude.com/en/docs/claude-code)
3. (Optional) Open the repo folder in Obsidian as a vault
4. Run `claude` from the repo root — the three agents auto-load via `.claude/agents/`

## License

MIT — see [LICENSE](LICENSE)

## Build journal

Public progress: see [docs/STATUS.md](docs/STATUS.md).

## Attribution

See [ATTRIBUTION.md](ATTRIBUTION.md) for credits and prior art.
