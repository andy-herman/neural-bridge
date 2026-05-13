---
type: blog-draft
created: 2026-05-11
status: draft
draft: true
target_repo: neural-bridge-blog
target_path: src/content/posts/six-layers-back-of-house.mdx
title: "The 6 layers, and why the back of house matters more than the dashboard"
slug: six-layers-back-of-house
description: "A personal AI substrate has six layers. V1 of Neural Bridge ships three of them, on purpose. Here's the map, and why the front of house comes last."
tags: [neural-bridge, architecture, build-in-public, v1]
issue: "https://github.com/andy-herman/neural-bridge/issues/12"
---

# The 6 layers, and why the back of house matters more than the dashboard

Every "personal AI" demo I have seen starts with the chat box, the avatar, the 3D graph spinning in the corner. The front of house. The thing you screenshot.

Neural Bridge spends almost all of its V1 budget on the back of house, because that is where the work compounds.

Here is the map I am building against.

## The six layers

```
1. Agents          specialist .md plugin files
2. Skills          reusable skill files the agents can pick up
3. Transport       how I reach the agents from any device
4. Shared state    the markdown wiki, daily logs, filing gate
5. Orchestration   who routes a request, who hands off to whom
6. Frontend        the dashboard, the graph, the visible surface
```

Read it bottom up if you like. The layers stack. An agent without state is a chat tab. State without orchestration is a folder of notes nobody reads. Orchestration without a frontend is fine, actually. Most of the value lives between layers 1 and 5.

Layer 6 is last because it is the easiest piece to fake. A pretty graph over a thin substrate is a screenshot, not a system.

## What V1 actually ships

V1 is the scaffold. I am calling out three of those deliverables as the layers that matter for this map:

- Three agent definitions in `plugins/neural-bridge-core/agents/` (research, teaching-prep, content). That is layer 1.
- A user-level skills inheritance, not a plugin-level skills story yet. That is layer 2, in the smallest form that counts as shipping.
- An empty wiki skeleton in `knowledge/`, with the schema, the index file, and the per-agent subdirectories. That is layer 4, also in the smallest form that counts.

Three layers, all of them at "the structure exists and you can write into it" maturity. No hooks. No daily logs being written. No nightly compile pass. No Hono dashboard. No 3D graph.

The gap between "scaffold" and "working substrate" is where most personal AI projects quietly die. AgentGPT ([archived January 2026](https://github.com/reworkd/AgentGPT)) is the clearest example: it shipped a polished browser-based UI for configuring and deploying AI agents in early 2023, reached 35,000 GitHub stars, and was archived read-only without ever solving persistent memory. Sessions had no durable substrate. The context window was the only state. A [persistence improvement proposal](https://github.com/reworkd/AgentGPT/pull/1671) sat unmerged until archival; the company pivoted to a different product entirely. So: what V1 does not include.

- No `flush.py`. Sessions end and the transcript is not summarized into a per-agent daily log. Memory does not accrue yet.
- No `compile.py`. Daily logs do not get promoted into shared concept articles. The wiki does not grow on its own.
- No `lint.py`. Nothing is checking the wiki for drift, broken links, or contradictions on a schedule.
- No supervisor. Routing between agents is whatever the native subagent dispatch in Claude Code gives me, which is enough for a scaffold and not enough for anything ambitious.
- No dashboard, no Telegram bridge in the repo, no BrainGraph.

Layers 3, 5, and 6 all assume that layer 4 actually contains something worth routing to and rendering. V1's job is to make layer 4 real.

## Why ship layers 1, 2, and 4 first

I did not ship layers 1, 2, and 4 because they were the easy ones. I shipped them because they are the bottleneck.

A specialist agent is cheap to define. A wiki schema is cheap to write down. The expensive part, and the part that breaks first, is the discipline that connects them. The convention that says "this is where research notes go, this is where blog drafts go, this is what a concept article looks like, this is who is allowed to write where." Without that, you have nine agents writing into the same folder, stomping on each other, and you discover six weeks later that none of it compounds.

In practice: the content agent writes blog drafts to `knowledge/agents/content/drafts/` and nothing else; the research agent writes findings to `knowledge/agents/research/` and nothing else; neither can touch `knowledge/concepts/`, which is owned by the compile pass, not by any specialist. The boundary is not enforced by a permissions system. It is enforced by the convention in `knowledge/AGENTS.md`, which every specialist reads on session start, and by the fact that `compile.py` only reads from `daily-logs/`, never from `knowledge/agents/`, so an agent writing into the wrong directory simply produces output that the pipeline never sees.

So V1 is mostly a contract. The agents agree on where to write. The wiki agrees on what shape to be. The per-agent subdirectories make it impossible for two specialists to silently overwrite each other's notes. None of this is glamorous. All of it is load-bearing for everything in V2. The V2 features that depend most directly on that contract are `flush.py` (which needs the per-agent `daily-logs/` subdirectory structure to exist and be scoped correctly before it can write session transcripts) and `compile.py` (which reads those daily logs, runs them through the filing gate (the compile-time check that asks PROMOTE, QUARANTINE, or REJECT before anything reaches shared memory), and promotes survivors into `knowledge/concepts/`). A promotion that is meaningless if the wiki schema and per-agent subdirectory conventions are not already locked.

Layers 3 and 5 come later because they are easier to retrofit on top of a working layer 4 than the other way around. You can swap transports. You can replace an orchestrator. You cannot retroactively give a substrate a memory it never had.

Layer 6 comes last because if I cannot answer "what is this thing for" using only the bottom four layers, no graph view is going to save me.

## Where the inspiration came from

Two starting points, named and not defended.

Andrej Karpathy posted a [gist on the LLM knowledge base pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) that frames the wiki as a compiler: raw input on one end, structured shared knowledge on the other. The gist defines three core operations: Ingest (drop a source into the raw collection, the LLM reads and integrates it across wiki pages), Query (ask a question, the LLM searches the wiki and synthesizes an answer with citations), and Lint (a periodic health check for contradictions, orphans, and stale claims). Two structural artifacts support them: `index.md` as a content-oriented catalog and `log.md` as an append-only chronological record. That framing is the reason `knowledge/` is not just a folder of notes. It is a target for a pipeline. The parenthetical in an earlier draft of this post listed five operations (ingest, flush, compile, lint, query) and attributed them all to the gist. That was wrong. "Flush" and "compile" are Medin's additions and Neural Bridge's own extensions. Of the five, Neural Bridge implements exactly one today: Query, meaning index-guided navigation by any agent reading the wiki. Flush and Compile are V2. Ingest and Lint are V3.

Cole Medin's [claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) is the hooks-driven, internal-data variant of the same pattern. Concretely, Medin's repo wires a SessionEnd hook (plus a PreCompact safety net) to a daily-log writer that calls the Claude Agent SDK, appends extracted insights to a dated markdown file under `daily/YYYY-MM-DD.md`, and runs a nightly compile pass that promotes daily logs into structured concept articles with cross-references. I am adopting the SessionEnd hook and the daily-log-to-concept promotion pattern directly into V2's `flush.py` and `compile.py`. The main divergence is that Neural Bridge routes compiled concepts through the filing gate before they reach the shared wiki, where Medin's repo promotes them more directly. When V2 ships `flush.py` and `compile.py`, that is the lineage I am building on.

They are documents I read early, took the useful structural ideas from, and moved on. The interesting work is everything those starter docs do not say: how the filing gate decides what gets promoted, how the per-agent subdirectory convention prevents drift, what the contract between specialists actually looks like in practice.

## The point

If you build the dashboard first, you spend the next year propping it up.

If you build the substrate first, you spend the next year letting it compound.

V1 is the substrate. V2 is the question: does it actually compound? The answer lives in whether `flush.py` reliably captures session state, whether `compile.py` produces concept articles you would want to read six months later, and whether the filing gate is catching drift before it reaches shared memory. Those are the three things to watch as V2 ships. If they work, the dashboard is a V3 problem, and a small one, because by then the thing it renders will be worth looking at.
