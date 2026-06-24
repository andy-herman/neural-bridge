# Neural Bridge Improvement Roadmap

**Status:** Research note. Survey of methods, prioritized for this architecture. For review.
**Date:** 2026-06-24
**Scope:** State-of-the-art methods (2024 to 2026) for improving a Claude-Code-plugin plus markdown-wiki multi-agent substrate, mapped to Neural Bridge with honest adoptability calls.
**Companion:** [FUGU_INTEGRATION.md](FUGU_INTEGRATION.md) (the coordination/resilience slice).

## How to read this

Five themes, each tagged with an adoptability call for this specific architecture (single Mac Mini, Claude Code, markdown wiki, launchd). The single highest-leverage area is Theme A: it strengthens the threat model Neural Bridge was built to address, and most of it is cheap and self-contained.

Important honesty note up front: the multi-agent memory-poisoning defenses below are at the research edge. Every defense surfaced is either unvalidated (single preprints) or empirically fragile. Adopt them as defense-in-depth and calibrate against a real eval set (Theme D). Do not assume any of them works out of the box.

## Theme A: Harden the filing gate against memory poisoning  [adoptability: HIGH]

This is Neural Bridge's stated reason to exist, and it is the best-evidenced threat in the field.

**The threat.** MINJA (NeurIPS 2025) is query-only memory poisoning: any ordinary user can inject malicious records into a shared agent memory using only normal queries, no write access. The records lie dormant and steer a later, different query. Reported ~95-98% injection and ~70-77% attack success under idealized conditions (lower with realistic pre-existing memory volume). OWASP codified this as ASI06 "Memory and Context Poisoning" in the 2026 Agentic AI Top 10. One survey notes long-term-memory contamination is "more problematic than transient prompt injection" and spreads via shared memory stores, which is exactly the filing-gate model.

**Concrete upgrades to `scripts/compile.py`:**

1. **Multi-vote gate.** Replace the single `call_filing_gate` verdict (`compile.py:77-80`) with a 3-vote majority. The defense literature shows single fixed-threshold judges fail in both directions; ensembles plus adaptive thresholds are the recommendation. (This is also the Fugu verify pattern, see companion doc.)
2. **NLI write-validation firewall (SSGM, arXiv 2603.11768).** Admit a candidate concept only if it does not logically contradict a protected core-fact set, via a natural-language-inference contradiction check. Adds a semantic gate on top of the current pattern-match injection checks.
3. **Per-agent Beta-Binomial trust model (arXiv 2603.02240).** Cheap (no LLM inference, ~10ms), asymmetric (bad evidence outweighs good). Track which agents and sessions produce QUARANTINE / REJECT verdicts over time and down-weight low-trust sources. Hooks into a filing-gate feedback loop.
4. **Identity-scoped reads (ABAC).** Agents already "write narrow," but reads are broad. Denser memory connectivity increases leakage (Topology Matters, arXiv 2512.04668), so scope reads by agent identity and provenance for sensitive concepts rather than giving every agent a fully-connected view.
5. **Adaptive thresholds plus temporal decay** on trust-aware retrieval, not fixed cutoffs.

**Caveat:** SSGM is a conceptual framework with no empirical validation; the trust-model metrics are self-reported simulations against hand-scripted adversaries. The mechanisms are sound and cheap; the numbers are illustrative. Calibrate (Theme D).

## Theme B: Orchestration and routing  [adoptability: MEDIUM]

1. **The Conductor auto-router.** See [FUGU_INTEGRATION.md](FUGU_INTEGRATION.md) Layer 1. Reuses the existing framing prompt.
2. **MAST failure taxonomy (arXiv 2503.13657).** The first failure taxonomy for multi-agent LLM systems: 14 failure modes in 3 categories (specification/design, inter-agent misalignment, task verification). Use as an audit checklist against `squad_discuss` and the handoff flows. The security-reviewer agent is the natural owner.
3. **Fan-out, verify, synthesize.** Anthropic's multi-agent research system and the deep-research harness both use this shape. It is a clean upgrade path for `squad_discuss` on high-stakes questions. Note Anthropic's caveat that multi-agent fan-out can burn ~15x the tokens of a single chat, so gate it on stakes.
4. **Claude Code native primitives.** Subagents in `.claude/agents/`, CLAUDE.md / auto-memory, and the auto-mode classifier all map onto this architecture. Caveat: basic subagents cannot message each other peer-to-peer (they only report to a parent; "Agent Teams" is the inter-agent feature). The Discord daemon is already Neural Bridge's coordinator, so this is "could simplify," not "must adopt."

## Theme C: Memory architecture  [adoptability: MEDIUM]

1. **Memory taxonomy as a design vocabulary (arXiv 2512.13564, 47-author survey).** Three axes: forms (token / parametric / latent), functions (factual / experiential / working), dynamics (formation / evolution / retrieval). Neural Bridge's wiki is token-level and factual only. The insight: it has no experiential layer (what worked or failed) and no working memory. An experiential layer (which routing decisions and which concepts proved useful) would feed the trust model and the router. Low-effort lens, useful for ADRs.
2. **Temporal knowledge-graph memory (Zep / Graphiti, arXiv 2501.13956).** A temporally-aware KG that preserves how relationships change over time. Reports up to +18.5% relative accuracy and ~90% lower latency on LongMemEval (vendor-authored; the latency win is partly because the baseline stuffs the full conversation into the prompt; "18.5%" is relative, not absolute). For Neural Bridge this is the natural backbone for the planned V3 query engine. Caveat: it adds a service to a single-host setup. The `connections/` writer in `compile.py` already does a primitive version (links concepts sharing a source session). Recommendation: defer until the wiki outgrows markdown-plus-grep, or adopt specifically for V3 query.

## Theme D: Reliability and evaluation  [adoptability: MEDIUM-HIGH]

1. **A filing-gate eval set.** This is the linchpin for Theme A. The defense research shows thresholds must be calibrated, not assumed. Build a small labeled set: known-bad injections (MINJA-style) that the gate must catch, and known-good concepts it must pass. Measure false-positive rate on benign edits. Without this, a multi-vote gate is faith-based.
2. **Cover the untested critical paths.** End-to-end compile (mock gate, real daily logs) and the future query engine are currently untested. The Discord bot, hooks, and echo synthesis have good `test_*.py` coverage already.
3. **MAST as a standing audit.** See Theme B.

## Theme E: Cost and latency  [adoptability: MEDIUM]

1. **Difficulty-aware model routing.** Easy queries to a cheap, fast model; hard ones to frontier. Pairs with the Conductor router and the model-abstraction layer.
2. **Provider fallback as cost arbitrage.** The same Layer 2 shim (companion doc) that buys resilience also lets the substrate pick the cheapest healthy provider for a given call.
3. **Be deliberate about fan-out.** Multi-agent and multi-vote multiply token cost. Gate them on stakes, not by default.

## Prioritized shortlist

1. **Multi-vote filing gate + per-agent trust signal** (Theme A). Highest ROI: hardens the core threat model, cheap, self-contained.
2. **Filing-gate eval set** (Theme D). Do this alongside or just before 1, so the gate is calibrated, not guessed.
3. **Provider fallback for the text-only cron calls** (companion doc, Layer 2). Resilience for the 24/7 path.
4. **The Conductor auto-router** (Theme B / companion Layer 1). Removes routing friction; reuses an existing prompt.
5. **NLI write-validation firewall** (Theme A.2). Adds semantic depth to the gate once the eval set exists to measure it.
6. Later: experiential memory layer (Theme C.1); temporal KG for V3 query (Theme C.2); MAST audit (Theme B.2); verify-and-synthesize squad (companion Layer 3).

## Build status

2026-06-24: shortlist items 1 (multi-vote filing gate) and 2 (filing-gate eval set) are implemented. See `scripts/compile.py` (`call_filing_gate_voted`, `aggregate_verdicts`, `--votes` default 3) and `scripts/eval_filing_gate.py` over `scripts/eval/filing_gate_cases.jsonl`. Still open: a live calibration run of the eval, and the per-agent Beta-Binomial trust model (A.3).

## Sources

- Memory survey (forms/functions/dynamics): arXiv 2512.13564
- MINJA memory poisoning (NeurIPS 2025): arXiv 2503.03704, https://openreview.net/forum?id=QINnsnppv8
- MINJA defense (trust scoring, adaptive thresholds): arXiv 2601.05504
- SSGM governance middleware (NLI write gate, ABAC reads): arXiv 2603.11768
- Topology Matters (connectivity increases leakage): arXiv 2512.04668
- SuperLocalMemory (Beta-Binomial trust model): arXiv 2603.02240
- Zep / Graphiti temporal KG memory: arXiv 2501.13956
- MAST multi-agent failure taxonomy: arXiv 2503.13657
- Survey on Trustworthy LLM Agents: arXiv 2503.09648
- Anthropic multi-agent research system: https://www.anthropic.com/engineering/multi-agent-research-system
- Claude Code docs: https://code.claude.com/docs/en/best-practices , /sub-agents , /memory
- Claude Code auto mode (injection-defense classifier): https://www.anthropic.com/engineering/claude-code-auto-mode

## Caveats

- Fugu is two days old (released 2026-06-22); its benchmark and resilience claims are vendor-stated and not independently reproduced.
- SSGM and SuperLocalMemory are single, non-peer-reviewed preprints; treat their numbers as illustrative of the mechanism, not as benchmarks.
- Zep's figures are vendor-authored and partly relative.
- Multi-agent memory and trustworthiness are named open research frontiers as of early 2026. Neural Bridge's filing gate sits at that edge, not on mature off-the-shelf solutions.
