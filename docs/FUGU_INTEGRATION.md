# Fugu Integration: A Coordination + Resilience Layer for Neural Bridge

**Status:** Research note and design proposal. Nothing built yet. For review.
**Date:** 2026-06-24
**Scope:** How the Sakana "Fugu" concept maps onto Neural Bridge, what it would buy us, and where it hooks into the existing code.
**Companion:** [IMPROVEMENT_ROADMAP.md](IMPROVEMENT_ROADMAP.md) (the broader "other methods" survey this note is one slice of).

## TL;DR

Do not adopt Fugu the product. Adopt the Fugu pattern: a coordination layer that sits in front of a heterogeneous pool of models and agents, decides who handles what, verifies, and synthesizes, behind one interface, with model-swappability for resilience.

Neural Bridge already has the bones of this (the Discord daemon routes mentions; `squad_discuss` runs multi-agent rounds). What it lacks, and what Fugu makes the case for, is three things:

1. A single front door that auto-selects the right specialist instead of forcing a manual `@mention`.
2. Model-swappable resilience. Today every LLM call in the substrate is hardwired to one vendor.
3. A verify-and-synthesize step (multi-vote), which doubles as a hardening of the filing gate against memory poisoning.

## What Fugu actually is

Sakana AI released Fugu on 2026-06-22 (two days before this note). Treat everything here as early and vendor-sourced.

- Fugu is a multi-agent orchestration system that behaves like a single OpenAI-compatible model: one API endpoint on the outside, a coordinated pool of frontier LLMs on the inside, with dynamic per-step routing, delegation, verification, and synthesis.
- Fugu is itself a small language model trained to call other LLMs in a swappable pool. It is built on Sakana's two ICLR 2026 papers: Trinity (an evolved ~0.6B CMA-ES coordinator, arXiv 2512.04695) and Conductor (a 7B RL-trained orchestrator, arXiv 2512.04388).
- It can call itself recursively, reading its own prior output to decide whether to revise its strategy. Sakana frames this as test-time scaling.
- Lineage: it is the productized successor to Sakana's research line (evolutionary model merging, The AI Scientist, ShinkaEvolve, AB-MCTS). AB-MCTS is the key prior result: multiple frontier models cooperating via tree search can beat any single model.
- Headline pitch: provider-agnostic resilience. Quote from the release page: "If a single provider restricts access, Fugu dynamically routes around the disruption." The motivating example is export controls on frontier models.

### Caveats (carried from the verified research)

- Frontier-parity benchmark claims (matching models like Fable 5 / GPT-5.5 without training one) are vendor benchmarks. No independent reproductions existed at the time of writing.
- The routing mechanism is proprietary. Per-query model selection is hidden, and the resilience guarantee is claimed, not demonstrated.
- Recursion implies recursive cost and latency. Early analysis reports waits up to ~30 minutes, and questions whether the quality edge over a single frontier model is real yet.

## Where Neural Bridge stands today (the gap Fugu names)

The substrate is hardwired to a single vendor at every LLM call. Each call shells out to the `claude` CLI with `--model claude-sonnet-4-6`:

| Call site | File | What breaks if Anthropic is down / rate-limited / restricted |
|---|---|---|
| Discord + Telegram agent turns | `scripts/discord_bot/claude_invoke.py:106` | Every agent goes silent |
| Filing gate (PROMOTE / QUARANTINE / REJECT) | `scripts/compile.py:358` | Nightly memory pipeline stalls; no concepts promoted |
| Concept writer (rich article body) | `scripts/compile.py:487` | Concepts fall back to stubs or fail |
| Weekly lint pass | `scripts/lint.py:251` | Wiki drift goes unchecked |
| Weekly summary | `scripts/summarize_weekly.py:126` | No digest |

This is exactly the exposure Fugu pitches against. For a personal substrate that runs 24/7 on a Mac Mini under launchd, a single-vendor outage is a silent gap in the wiki, not just a chat that fails.

## Integration design

Three layers, crawl / walk / run. Each is independently shippable.

### Layer 1: the Conductor (auto-routing front door)

**What:** A single entry point (an `@conductor`, or a plain DM with no `@`) that classifies the request and dispatches to the right specialist(s). This is Fugu's "one endpoint that internally routes."

**Why it is cheap:** the routing prompt already exists. `scripts/discord_bot/prompts/squad_discuss_framing_v1.md` already has senior-pm "pick 1 to 3 specialists" and emit `{"framing", "selected_agents"}`. The Conductor is that prompt with the framing dropped and the selection auto-dispatched, extended to the full 14-agent roster (the framing prompt currently lists only 8).

**Where it hooks:** new prompt `prompts/route_v1.md`; wiring in `scripts/discord_bot/mention.py` (routing) and `handlers.py` (dispatch). Keep manual `@mention` as an override.

**Effort:** small. **Risk:** misroutes; mitigated by the manual override and by showing which agent was picked.

### Layer 2: model-swappable resilience (the real Fugu value)

**What:** A model-abstraction shim so a failed primary call falls back to an alternate provider or model instead of failing. `call_claude_sync` in `claude_invoke.py:77` already returns a clean `(ok, stdout, error_reason)` with reasons (`timeout`, `exit_N`, `claude_cli_not_found`). That is the seam: wrap it in a `model_invoke(prompt, chain=[...])` that walks a fallback chain on failure.

**Scope it correctly.** Two classes of call:

- Text-only generation (no tools): the filing gate, concept writer, lint, weekly summary, flush. These pass `allowed_tools=None` and use no Claude-Code-specific flags, so they can fall back to any OpenAI-compatible provider (direct API, or a gateway like LiteLLM / OpenRouter). **Start here.** This is the 24/7 cron-critical path and the cleanest win.
- Tool-using agent turns: these depend on `--allowedTools`, `--add-dir`, and `--session-id`, which are Claude-Code-specific. Cross-provider fallback here needs a tool bridge. **Defer.**

**Where it hooks:** new `scripts/model_invoke.py` (or extend `claude_invoke.py`); swap the four text-only call sites above to route through it. A per-call `chain` plus a per-agent default preference.

**Effort:** medium. **Risk:** provider parity for tool use and the `actions` block format; avoided by scoping to text-only first.

**Note:** Andy has shipped a fast-model fallback before (Touchstone). Same shape.

### Layer 3: verify-and-synthesize (and a hardened filing gate)

**What:** For high-stakes work, run N specialists or N models, then a synthesizer pass picks or merges the best answer. This is Fugu's delegation-plus-verification-plus-synthesis, and it is the same shape as the deep-research harness (fan-out, adversarially verify, synthesize).

**The high-value slice is the filing gate.** Today the gate is a single `claude -p` verdict (`compile.py:77-80`, `call_filing_gate`). Replace it with a 3-vote majority. This is not gold-plating: the memory-poisoning defense research is explicit that single fixed-threshold judges swing between blocking everything and admitting confident malice, and recommends ensemble plus adaptive thresholds. So Fugu's "verify with multiple models" directly hardens Neural Bridge's stated threat model. See [IMPROVEMENT_ROADMAP.md](IMPROVEMENT_ROADMAP.md) Theme A.

**Where it hooks:** `compile.py` `call_filing_gate` -> `call_filing_gate_voted`. Optionally a `squad` upgrade that fans out then synthesizes.

**Effort:** higher overall; the filing-gate multi-vote is a contained, high-ROI subset. **Risk:** multiplies cost and latency; scope to the gate and to genuinely high-stakes squad runs.

## Benefits, concretely

1. **Resilience for the 24/7 path.** The nightly compile, echo synthesis, lint, and weekly summary keep running through an Anthropic outage, rate-limit, or access restriction. Today any one of those is a silent gap in the wiki.
2. **Lower friction.** Auto-routing removes the "which of 14 agents do I mention?" burden.
3. **Stronger memory defense.** A multi-vote gate is a measurable hardening against query-only memory poisoning (MINJA, NeurIPS 2025), which OWASP codified as ASI06 in the 2026 Agentic AI Top 10.
4. **Cost and latency control.** A router can send easy queries to a cheap, fast model and reserve frontier models for hard ones (Fugu's per-step selection is also a cost lever).
5. **Future-proofing.** A model-abstraction layer lets Neural Bridge adopt new models (Fable 5, local models, even Fugu itself as one pool entry) without rewiring every agent.

## What not to do

Do not put Fugu the product at the center of the substrate. It is a proprietary hosted endpoint that would replace Claude Code (the entire runtime), it is two days old and unverified, it adds latency, and it trades Anthropic-dependence for Sakana-dependence. The value is the pattern. If desired later, Fugu can be one entry in the Layer 2 fallback pool, gated behind an eval.

## Risks and open questions

- Routing can misfire. Keep a manual override and surface the routing decision.
- Provider fallback breaks tool-use, `actions`, `--add-dir`, and session parity. Scope to text-only calls first.
- Multi-vote and recursion multiply cost and latency. Scope to the filing gate and high-stakes squad runs.
- Memory-defense thresholds need adaptive, not fixed, calibration. Build a small filing-gate eval set before trusting a multi-vote gate in production (Roadmap Theme D).
- Does Fugu-style routing actually beat a single frontier model on real tasks, at acceptable cost? Unknown until independent benchmarks exist.

## Recommended sequence

1. Multi-vote filing gate plus a per-agent trust signal (Layer 3 slice; hardens the threat model; cheap and self-contained).
2. Provider fallback for the four text-only cron calls (Layer 2; resilience for the 24/7 path).
3. The Conductor auto-router (Layer 1; reuse the framing prompt).
4. A filing-gate eval set to calibrate the gate (Roadmap Theme D).
5. Later: verify-and-synthesize squad upgrade; Fugu as an optional pool entry.

## Build status

2026-06-24: Recommended step 1 (the multi-vote gate) is implemented. `scripts/compile.py` now runs the filing gate as N independent passes (`--votes`, default 3) under a conservative majority (`call_filing_gate_voted`, `aggregate_verdicts`), records the per-pass tally in concept frontmatter (`gate_votes:`) and the quarantine body, and `scripts/eval_filing_gate.py` is a calibration harness over a labeled case set (`scripts/eval/filing_gate_cases.jsonl`).

2026-06-24 (later): Layer 2 (provider fallback) first cut is implemented. `scripts/model_invoke.py` adds an OpenAI-compatible fallback provider (stdlib `urllib`, env-gated by `NB_FALLBACK_BASE_URL` / `NB_FALLBACK_API_KEY` / `NB_FALLBACK_MODEL`), and `compile.py`'s filing gate and concept writer fall back to it when `claude -p` fails at the provider level (timeout, non-zero exit, missing CLI). Off by default (Claude-only) until configured. A returned-but-malformed response does not trigger fallback, only an unavailable primary does. Still open: wiring `lint.py` and `summarize_weekly.py`, a live calibration run, the per-agent trust signal, the NLI write firewall, and the Conductor router (Layer 1). Tool-using agent turns (`scripts/discord_bot/claude_invoke.py`) stay Claude-only by design.

## Sources

- Sakana Fugu release: https://sakana.ai/fugu-release/ and https://sakana.ai/fugu-beta/
- Trinity (arXiv 2512.04695), Conductor (arXiv 2512.04388), AB-MCTS: https://sakana.ai/ab-mcts/
- MINJA memory poisoning, NeurIPS 2025: https://openreview.net/forum?id=QINnsnppv8 (arXiv 2503.03704)
- MINJA defense (trust scoring, adaptive thresholds): arXiv 2601.05504
- OWASP Agentic AI Top 10 (2026), ASI06 Memory and Context Poisoning
