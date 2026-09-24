# Build Status

## 2026-09-22 — Memory layer repair (PR #162)

A four-agent audit found that the memory layer, the substrate's stated reason to exist, had not compounded since 2026-05-10: one compile pass ever produced content, nothing read the wiki at query time, and the failures that caused it were silent. PR #162 repairs it in four steps.

1. **Silent failures fixed.** The Discord Honcho write had been dead behind a bare `except: pass`; three launchd plists (including the memory canary) were invalid XML and could never load; a compile dry run advanced state; the agent roster was copied into three files that disagreed. Each fix carries a test that catches its class of bug, and a GitHub Actions workflow now runs `pytest hooks/ scripts/` on every push and PR.
2. **The read loop closed.** `hooks/wiki_recall.py` ranks `knowledge/concepts/` against every prompt (a `UserPromptSubmit` hook for Claude Code sessions, `mention.py` for Discord) and injects the relevant articles as a data block. Flush, compile, and recall emit telemetry; the canary now covers Layer 4 and reports how many agent turns were grounded in a concept.
3. **Stores consolidated.** The root cause of the empty wiki: the daemon stamps `NB_AGENT_ID`, the session hooks only read `NB_AGENT`, so every Discord turn was unattributed and skipped by compile. Fixed. A per-agent `progress.md` (written by flush at session close, injected behind `notes.md`) implements Step 1 of `docs/MEMORY_CONSOLIDATION.md`; Step 3 has a guard; Step 5 is decided (revive). `python -m scripts.memory_canary --gates` answers the remaining gates from telemetry.
4. **Docs truth, enforced.** `scripts/lint.py` gained a `docs-truth` check: prose agent counts must match the plugin directory, plugin versions must agree across manifests, ADRs may not sit "proposed" past 60 days, and hook commands must be anchored on `$CLAUDE_PROJECT_DIR` (a stray `cd` had locked a whole session out of every hook). ADR-0001 to 0004 are marked accepted with dated notes; 0005 is marked superseded by ADR-001 (see known gaps).

**Known gaps as of 2026-09-22:**
- Fleet heartbeats stopped 2026-05-29 and `scripts/fleet_heartbeat.py` has had no commit since July; it imports from outside the repo and no-ops silently if that import fails. Still undiagnosed; it is not covered by the canary.
- Gates G2 (echo voice by agent) and G3 (Honcho peer card) need Mac-side telemetry; `--gates` prints the answers.
- Five memory stores still overlap for facts about Andy (notes.md, progress.md, Honcho, daily logs via SessionStart, conversation archive). The consolidation doc's Step 4 decides Honcho; retiring the SessionStart daily-log injection in favour of progress.md is the next candidate.
- `feat/proactive-surface-on-relevance` on origin is orphan history from May whose idea (inject related prior turns) is now covered by `wiki_recall`; the branch can be deleted.
- ADR-001's auto-memory ingestion (`compile.py` reading `~/.claude/memory/`) was decided in May and never implemented.
- ~~Each gate vote spawned by `compile.py` still triggers a flush model call into `_unattributed/`, which nothing reads.~~ Resolved 2026-09-23: `session_end.py` honours `NB_SKIP_FLUSH=1` (and the `NB_AGENT=compile` marker); `compile.py` sets it on every gate call and `flush.py` on its own extraction call. The latter closed a second, worse case found while fixing the first: hooks fire for nested `claude -p` sessions, so a flush was summarising its own extraction call, recursively.

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

### Open scoping decisions (all resolved; kept for the record)

- ~~Three concrete weekly use cases the system must serve at v1~~ — resolved: [ADR-006](../decisions/ADR-006-three-weekly-use-cases.md)
- ~~Wiki ownership scope (shared / per-agent / hybrid)~~ — resolved: hybrid, [ADR-0003](../decisions/0003-hybrid-wiki-ownership.md), enforced by `hooks/guard_concepts.py`
- ~~Whether wiki contents are public-by-default or per-agent personal~~ — resolved: public, in this repo, [ADR-0004](../decisions/0004-wiki-lives-in-this-repo.md)
- ~~Auto-memory interaction with `~/.claude/memory/`~~ — decided: [ADR-001](../decisions/ADR-001-auto-memory-interaction.md) (keep both; `compile.py` ingests the primitive). Truth-pass note 2026-09-22: the ingestion was never implemented.

See the (private) Obsidian vault `Neural Bridge/Decisions/Decisions To Be Made.md` for the full list.
