# Build Status

## 2026-07-09 — Docs truth pass

STATUS.md, AGENTS.md, and README had drifted badly behind the code (still describing the 2026-05-08 scaffold). Reconciled all three with reality and added a `agents-roster` drift check to `scripts/lint.py` so the AGENTS.md roster can never silently rot again. Added a root CLAUDE.md that imports AGENTS.md, since Claude Code auto-loads CLAUDE.md only.

## V1.x — What actually shipped (2026-05-08 through 2026-05-27)

Reconstructed from the git log during the 2026-07-09 truth pass; 131 commits on main.

- **Agents:** roster grew from 3 to 14 definitions (added luna, librarian, echo, loid, ux-designer, plus the six specialists from the V1 audit). 13 registered with the Discord daemon.
- **Discord orchestrator:** full daemon in `scripts/discord_bot/` with mention routing, actions blocks executed via `gh`, per-channel handoff budgets, session resumption, attachment ingest (pptx/xlsx), `/pm-*` and `/triage` slash commands, and multi-round `/squad-discuss` with reports and auto-issues.
- **Memory pipeline:** `hooks/` (session_start, session_end, flush) plus `scripts/compile.py` filing gate and `scripts/lint.py` weekly checks, all with pytest coverage.
- **Telegram:** Luna DM bridge and Loid (career strategist, voice and text) via `scripts/telegram_bot/`.
- **Honcho peer memory:** shared `andyherman` peer card across all agents and Hermes-side Yor (`honcho_client.py`; see docs/HONCHO_INTEGRATION.md and docs/HONCHO_COPILOT_PLAYBOOK.md).
- **Echo ingester:** Synapse DB and MindFrame Discord logs into the vault voice corpus.
- **Ops:** fleet heartbeats into the Obsidian vault Fleet dashboard, launchd persistence, auto-reload, Cloudflare tunnel scripts.

Known gaps as of 2026-07-09: fleet heartbeats stopped 2026-05-29 (daemon status unknown); ~15 feature branches unmerged on origin; plugin-level skills still not shipped; marketplace plugin at v0.8.0.

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
- ~~Auto-memory interaction with `~/.claude/memory/`~~ — resolved: [ADR-001](../decisions/ADR-001-auto-memory-interaction.md) (keep both; `compile.py` ingests the primitive)

See the (private) Obsidian vault `Neural Bridge/Decisions/Decisions To Be Made.md` for the full list.
