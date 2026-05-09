---
type: adr
project: Neural Bridge
status: proposed
created: 2026-05-08
tags: [neural-bridge, decision]
---

# ADR-0005: Ingest ~/.claude/memory/ into the wiki via compile.py

**Status:** Proposed (agent-drafted, awaiting human review)
**Date:** 2026-05-08
**Tracks:** Issue #7 — andy-herman/neural-bridge

## Context

Claude Code already writes auto-memory to `~/.claude/memory/`. Neural Bridge's wiki layer also writes memory. Three ways to reconcile: disable auto-memory and let the wiki own everything; keep both with no integration and accept duplication; or ingest auto-memory into the wiki on each compile pass.

## Decision

Keep auto-memory on. Have `compile.py` read `~/.claude/memory/` files and ingest them into the wiki alongside the per-agent daily-log promotions. Don't fight Anthropic's primitive; absorb it.

## Consequences

**Positive:**
- Anything Claude Code learns at the user level (cross-project facts, preferences) flows into the substrate's compounding loop automatically.
- No fight against a moving target if Anthropic improves auto-memory; we benefit from upstream changes for free.
- A user running `claude` outside this repo still feeds the wiki on the next compile cycle.

**Negative / accepted trade-off:**
- `compile.py` has to handle a second source format and de-duplicate against per-agent notes.
- Memory file path is a Claude Code implementation detail; if the format changes, the ingest path breaks.

**Foreclosed:**
- Disabling Claude Code auto-memory.
- Treating `~/.claude/memory/` and `knowledge/` as fully independent stores with no integration.
