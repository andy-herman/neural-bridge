---
type: adr
number: "001"
title: Auto-memory interaction with ~/.claude/memory/
status: accepted
created: 2026-05-09
tags: [neural-bridge, adr, memory, compile, v2]
tracks: ["#10"]
---

# ADR-001 — Auto-memory interaction with `~/.claude/memory/`

> Drafted 2026-05-09. Status: accepted. Supersedes the open scoping item in `docs/STATUS.md`.

## Context

Claude Code maintains its own primitive memory layer at `~/.claude/memory/`. Neural Bridge maintains a separate, richer wiki in `knowledge/`. These two layers can interact in three ways:

1. **Disable auto-memory** — wiki is the only memory layer; `~/.claude/memory/` is turned off or ignored.
2. **Keep both; wiki ingests `~/.claude/memory/`** — Anthropic's primitive is left on; `compile.py` absorbs its contents into the wiki on each nightly run. *(lean)*
3. **Keep both, no integration** — both layers run independently; duplication is accepted.

Option 1 creates a maintenance burden (remembering to suppress the primitive) and loses any signal Anthropic bakes in automatically. Option 3 allows the two layers to diverge, eroding the wiki's authority as the single source of truth. Option 2 absorbs the primitive rather than fighting it: the wiki stays authoritative, zero suppression overhead, and any auto-captured memory is promoted through the same filing gate as daily-log output.

## Decision

**Option 2: keep both; `compile.py` ingests `~/.claude/memory/`.**

`compile.py` will read `~/.claude/memory/*.md` (or the applicable glob for Anthropic's format at the time of implementation) as an additional input alongside per-agent daily logs. Each file is treated as a candidate source — promoted, quarantined, or rejected via the same two-pass filing gate applied to daily-log output. Provenance frontmatter on every promoted article must include the source path and the sha256 of the source file at ingest time, so the origin of any auto-memory-derived concept is traceable.

`~/.claude/memory/` is **never written to** by Neural Bridge scripts. It remains Anthropic's output only.

## Consequences

**Positive:**
- Wiki stays the single authoritative memory layer; nothing lives only in `~/.claude/memory/`.
- Filing-gate security applies equally to auto-memory content — no unreviewed primitives bypass the adversarial lint.
- Zero ongoing maintenance: no need to suppress or quarantine the primitive proactively.

**Negative / watch items:**
- `compile.py` must handle Anthropic's file format, which may change across Claude Code versions. Pin a format-version check and fail loudly rather than silently ingest garbage.
- `~/.claude/memory/` content is user-scoped, not agent-scoped — it may mix concerns from all three specialist agents. The per-agent pass-1 in `compile.py` should mark auto-memory candidates as `source_agent: unattributed` and let pass-2 reconcile.
- If Anthropic adds a cloud-sync dimension to `~/.claude/memory/`, re-evaluate — the privacy exposure changes.

## Implementation note

This decision takes effect when `compile.py` ships (issue #10). No code change is required before then. The `compile.py` spec in `docs/v2-build-plan.md` is updated by this ADR to list `~/.claude/memory/` as an explicit input.
