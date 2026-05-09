---
type: adr
project: Neural Bridge
status: proposed
created: 2026-05-08
tags: [neural-bridge, decision]
---

# ADR-0001: Stay fully OSS, no Kashef PDF

**Status:** Proposed (agent-drafted, awaiting human review)
**Date:** 2026-05-08
**Tracks:** Issue #1 — andy-herman/neural-bridge

## Context

A paid PDF guide (Kashef, ~$30) covers a chunk of the same scaffolding ground Neural Bridge needs. Buying it would compress weeks of figuring-things-out into an afternoon of reading. The catch: the blog *is* the product here. Build-in-public posts only land if the narrative is *I figured this out from scratch.* Importing someone else's playbook hollows that out, even if no line of their work ever ships in the repo.

## Decision

Skip the PDF. Build the substrate fully OSS, sourced from public docs (Claude Code, Karpathy's wiki gist, Cole Medin's claude-memory-compiler) and primary experimentation.

## Consequences

**Positive:**
- The blog narrative stays clean: every post is an honest "here's what I learned this week."
- Forces deeper engagement with primary sources, which is also better blog content.
- No license ambiguity if anything in this repo is later remixed or relicensed.

**Negative / accepted trade-off:**
- Some weeks of the build will rediscover patterns the PDF likely already documents.
- Higher total time-to-V2.

**Foreclosed:**
- Quoting, paraphrasing, or directly porting any structure from the Kashef PDF in this repo or in the build-in-public posts.
