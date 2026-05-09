---
type: adr
number: "006"
title: Three concrete weekly use cases
status: accepted
created: 2026-05-09
tags: [neural-bridge, adr, scope, agents, v1, v2]
tracks: ["#4"]
---

# ADR-006 — Three concrete weekly use cases

> Drafted 2026-05-09. Status: accepted. Closes scoping decision #4.

## Context

The "personal multi-agent AI substrate" framing in `Vision and Scope` is too broad to design against. Without three concrete use cases the system must serve weekly, scope creep is guaranteed and the V2 pipeline (compile, lint, filing gate) has no anchor for what counts as good output.

Five candidates were on the table:

1. INFO 310 lecture prep automation
2. Substack inbox triage and draft generation
3. Calendar awareness and daily briefing
4. Research agent for current events and regulation watching
5. Content idea pipeline for the build-in-public blog

Each candidate maps cleanly onto one of the three V1 specialist agents (research, teaching-prep, content) — except (2) Substack triage and (3) calendar briefing, which are inbound-stream-shaped work that none of the current agents own.

## Decision

The three weekly use cases for V1 are **(1) INFO 310 lecture prep**, **(4) regulation and current-events research**, and **(5) content idea pipeline**. Each maps to one existing specialist agent. Substack triage and calendar briefing are deferred — they exercise a different work shape (inbound stream processing) and belong to a future agent that isn't on the V1 roster.

### Use case 1: INFO 310 lecture prep automation

**Owner:** `teaching-prep` agent (with `research` agent as a collaborator).

**Cadence:** Weekly during UW iSchool quarters (Sep-Dec, Jan-Mar, Apr-Jun). Saturday: scan the week's AI-security and cybersecurity stories for items worth folding into the next lecture as concrete examples. Sunday: outline the lecture deck and draft speaker notes. Monday: review and revise. Tuesday: lecture day. Wednesday: capture what landed and what didn't into `knowledge/agents/teaching-prep/`. Off-quarter cadence drops to research-only — no deck or lab work.

**Done-when (per session):** lecture deck draft + speaker notes + post-lecture reflection, all committed to per-agent state.

### Use case 2: Regulation and current-events research

**Owner:** `research` agent.

**Cadence:** Weekly Sunday morning. Scan the previous seven days of: AI security incident disclosures (Noma, Lakera, Unit 42, Embrace the Red), regulatory developments (EU AI Act, NIST AI RMF guidance, Colorado AI Act case law, NIS2 enforcement), and academic papers (NeurIPS, USENIX Security, IEEE S&P). Output: `knowledge/agents/research/<YYYY-MM-DD>-week-in-ai-security.md` listing the 3-5 most consequential items, each with source, summary, why-it-matters, and a pointer to which downstream agent (`teaching-prep`, `content`, or wiki promotion) should pick it up.

**Done-when (per session):** week-in-ai-security note in research subdir, with per-item routing pointers.

### Use case 3: Content idea pipeline for the build-in-public blog

**Owner:** `content` agent.

**Cadence:** Weekly Friday. Review the week's research notes, lecture artifacts, and any incidents that caught the user's attention. Output: 2-3 blog post ideas to `knowledge/agents/content/drafts/`, each with a working title, a single-line hook, and a 5-7 bullet outline. The content agent does NOT write full drafts in this use case — that's a separate ad-hoc invocation. Pipeline-curation only. If no idea is "ready" by Friday, capture why nothing landed (too noisy a news week, too much teaching-prep debt, hook-not-yet-found) so the next week's research run can adjust signal.

**Done-when (per session):** 2-3 idea outlines committed, OR a one-paragraph "why nothing landed" note.

## Consequences

**Positive:**
- Each V1 specialist agent now has a concrete, recurring deliverable. No agent is speculative.
- The V2 compile pipeline has a real corpus to compile from — three predictable streams of daily-log content per week.
- Scope creep is bounded: anything that doesn't serve one of these three use cases is V2+ work.

**Negative / accepted trade-off:**
- Substack triage and calendar briefing are deferred. If those become higher-leverage than expected, they'll need a new specialist agent in a future plugin (e.g. `neural-bridge-inbound`).
- INFO 310 cadence drops to ~25% off-quarter, which means `teaching-prep` is dormant for 3-4 months a year. That's fine — the wiki memory persists and the agent picks up where it left off.
- Friday content cadence assumes a meaningful week of upstream signal. Slow weeks will produce empty outputs more often than is comfortable. Captured as a known limitation; revisit after one quarter of use.

**Foreclosed:**
- Adding a fourth or fifth weekly use case to V1. New use cases land in V2+ via new agents, not by overloading existing ones.
- Treating any of these three use cases as ad-hoc instead of weekly. Cadence is the design.

## Implementation note

This ADR doesn't change agent definitions directly — the existing three specialist agent files in `plugins/neural-bridge-core/agents/` already broadly fit these use cases. PR #21 follow-up work on issue #13 (apply V1 audit findings) should now reference these specific cadences when refining each agent's description and operating rules.
