---
type: adr
project: Neural Bridge
status: proposed
created: 2026-05-08
tags: [neural-bridge, decision]
---

# ADR-0002: Ship Architecture A first, graduate to B after two weeks of real use

**Status:** Proposed (agent-drafted, awaiting human review)
**Date:** 2026-05-08
**Tracks:** Issue #3 — andy-herman/neural-bridge

## Context

Three architecture tiers are on the table (see `Architecture/Architecture Options.md` in the planning vault). A is native `.claude/agents/` plus Anthropic Channels and a single hive.db hook, runnable in a weekend. B adds a TypeScript supervisor, Hono dashboard, SQLite FTS5 + sqlite-vec, node-cron, and a 2D graph, in 2-4 weeks. C is the full ClaudeClaw clone. Jumping straight to B produces the more interesting first blog post but locks in a UI surface before there's evidence of what the daily workflow actually wants.

## Decision

Ship A in week 1-2. Use it for two weeks of real work. Whatever is found wanting becomes the written spec for B. Don't pre-commit to C.

## Consequences

**Positive:**
- Working agents in days, not weeks. V1 evidence-gathering starts immediately.
- The B spec is grounded in observed gaps, not speculation about what a dashboard "should" have.
- Honest test of whether kanban, graph, and scheduled cron are load-bearing or cosmetic.

**Negative / accepted trade-off:**
- First public blog post is less visually impressive (no UI screenshots).
- Some A-tier glue (launchd unit, tmux attach flow) will be discarded when B lands.

**Foreclosed:**
- Building the Hono dashboard, supervisor, or graph view before A has run for two weeks.
- Any C-tier work (war-room voice, BrainGraph3D, per-agent bots) until the audience or daily use demands it.
