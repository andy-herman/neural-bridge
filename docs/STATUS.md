# Build Status

## V1 — Scaffold

**Date:** 2026-05-08
**Status:** Scaffold created. No agents yet active.

### What ships in V1

- 3 agent definitions in `plugins/neural-bridge-core/agents/` (research, teaching-prep, content)
- This repo is a Claude Code plugin marketplace (`.claude-plugin/marketplace.json`); the core plugin is installable via `/plugin install neural-bridge-core@neural-bridge`
- Empty wiki skeleton in `knowledge/`
- Empty `hooks/` and `scripts/` directories (placeholders for V2)
- Project schema in [AGENTS.md](../AGENTS.md)
- Wiki schema in [knowledge/AGENTS.md](../knowledge/AGENTS.md)

### What V1 does NOT include

- Working hook scripts (V2)
- `flush.py` / `compile.py` / `lint.py` / `query.py` (V2)
- TypeScript supervisor (V2)
- Hono dashboard (V2)
- Telegram bridge (configured separately via Anthropic Channels)
- 3D BrainGraph (V3)

### Next steps (V1 → V2)

1. Wire `SessionEnd` hook → write transcript summary to `daily-logs/<agent>/`
2. Implement `flush.py` using the agent SDK
3. Implement `compile.py` for nightly daily-log → concept-article promotion
4. First public blog post on the spine ("The 6 layers — and why the back of house matters more than the dashboard")

### Open scoping decisions

- Three concrete weekly use cases the system must serve at v1 (currently inferred from agent roster)
- Wiki ownership scope (shared / per-agent / hybrid — leaning hybrid)
- Whether wiki contents are public-by-default or per-agent personal
- Auto-memory interaction with `~/.claude/memory/`

See the (private) Obsidian vault `Neural Bridge/Decisions/Decisions To Be Made.md` for the full list.
