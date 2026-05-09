---
type: adr
project: Neural Bridge
status: proposed
created: 2026-05-08
tags: [neural-bridge, decision]
---

# ADR-0004: Wiki lives in this repo (re-evaluate if private content emerges)

**Status:** Proposed (agent-drafted, awaiting human review)
**Date:** 2026-05-08
**Tracks:** Issue #6 — andy-herman/neural-bridge

## Context

Neural Bridge's knowledge wiki could live inside the existing Luna Master Obsidian vault (one Obsidian to rule them all, but mixes a personal vault with public-blog-adjacent content) or as its own git repo (clean public/private boundary, but two Obsidians to manage). Build-in-public means the substrate's memory needs to be visible by default, and Luna Master holds personal material that should never be pushed to GitHub.

## Decision

The wiki lives inside this repo, under `knowledge/`. Open the repo folder as a *second* Obsidian vault when graph view and backlinks are wanted. Re-evaluate this decision if a meaningful amount of content turns out to be inherently private.

## Consequences

**Positive:**
- Public-by-default: every commit advances the build-in-public narrative without an extra publishing step.
- Clean separation from Luna Master, which stays fully private.
- One repo to clone for anyone reproducing the substrate.

**Negative / accepted trade-off:**
- Two Obsidian vaults open during work sessions.
- Anything sensitive that an agent learns must be filtered out at the compile/flush step rather than relying on an already-private vault.

**Foreclosed:**
- Storing wiki content under `Luna Master/Neural Bridge/` in the personal vault.
- Any architecture that assumes the wiki and decision docs live in the same Obsidian vault.
