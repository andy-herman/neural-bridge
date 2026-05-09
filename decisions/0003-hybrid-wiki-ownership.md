---
type: adr
project: Neural Bridge
status: proposed
created: 2026-05-08
tags: [neural-bridge, decision]
---

# ADR-0003: Hybrid wiki ownership — shared concepts/, per-agent raw subdirs

**Status:** Proposed (agent-drafted, awaiting human review)
**Date:** 2026-05-08
**Tracks:** Issue #5 — andy-herman/neural-bridge

## Context

When the wiki layer ships, ownership has to be decided up front because it shapes the compile pass and the agent prompts. A single shared wiki risks cross-talk and duplicate concept articles as agents stomp on each other. Per-agent wikis with optional cross-links keep each agent clean but kill the compounding loop that's the whole point of the substrate.

## Decision

Hybrid. Each agent writes raw notes only into its own `knowledge/agents/<role>/` subdirectory. The shared `knowledge/concepts/` and `knowledge/connections/` directories are owned by the wiki itself and only written to by a nightly compile pass that promotes and cross-links material from the per-agent subdirs.

## Consequences

**Positive:**
- Agents can be messy in their own subdir without polluting shared space.
- Compounding loop stays alive: cross-agent reads happen against a clean concepts/ layer.
- Compile pass becomes the one place where merging logic lives, which is testable.

**Negative / accepted trade-off:**
- Concepts lag raw notes by one compile cycle (typically a day).
- Compile pass is now a load-bearing component; if it breaks, concepts/ goes stale.

**Foreclosed:**
- Direct agent writes to `knowledge/concepts/` or `knowledge/connections/`.
- A pure-shared wiki where any agent can edit any file.
- Per-agent silos with no shared concept space.
