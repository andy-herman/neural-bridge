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

Every demo of a "personal AI" I have seen starts with the wrong thing. It starts with the chat box, the avatar, the 3D graph spinning in the corner. The front of house. The thing you screenshot.

I want to do the opposite. I want to show the back of house first, because that is where the work actually compounds, and that is where Neural Bridge spends almost all of its V1 budget.

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

The frontend is layer 6 for a reason. It is the easiest piece to fake and the least load-bearing. A pretty graph over a thin substrate is a screenshot, not a system.

## What V1 actually ships

V1 is the scaffold. According to `docs/STATUS.md` it ships three things:

- Three agent definitions in `plugins/neural-bridge-core/agents/` (research, teaching-prep, content). That is layer 1.
- A user-level skills inheritance, not a plugin-level skills story yet. That is layer 2, in the smallest form that counts as shipping.
- An empty wiki skeleton in `knowledge/`, with the schema, the index file, and the per-agent subdirectories. That is layer 4, also in the smallest form that counts.

That is it. Three layers, all of them at "the structure exists and you can write into it" maturity. No hooks. No daily logs being written. No nightly compile pass. No Hono dashboard. No 3D graph.

I want to be precise about what V1 does not include, because the gap between "scaffold" and "working substrate" is exactly where most personal AI projects quietly die:

- No `flush.py`. Sessions end and the transcript is not summarized into a per-agent daily log. Memory does not accrue yet.
- No `compile.py`. Daily logs do not get promoted into shared concept articles. The wiki does not grow on its own.
- No `lint.py`. Nothing is checking the wiki for drift, broken links, or contradictions on a schedule.
- No supervisor. Routing between agents is whatever the native subagent dispatch in Claude Code gives me, which is enough for a scaffold and not enough for anything ambitious.
- No dashboard, no Telegram bridge in the repo, no BrainGraph.

If you are wondering why the front of house gets so little attention, the answer is in that list. Layers 3, 5, and 6 all assume that layer 4 actually contains something worth routing to and rendering. V1's job is to make layer 4 real.

## Why ship layers 1, 2, and 4 first

I did not ship layers 1, 2, and 4 because they were the easy ones. I shipped them because they are the bottleneck.

A specialist agent is cheap to define. A wiki schema is cheap to write down. The thing that is expensive, and the thing that almost always breaks first in personal AI builds, is the discipline that connects them. The convention that says "this is where research notes go, this is where blog drafts go, this is what a concept article looks like, this is who is allowed to write where." Without that, you have nine agents writing into the same folder, stomping on each other, and you discover six weeks later that none of it compounds.

So V1 is mostly a contract. The agents agree on where to write. The wiki agrees on what shape to be. The per-agent subdirectories make it impossible for two specialists to silently overwrite each other's notes. None of this is glamorous. All of it is load-bearing for everything in V2.

Layers 3 and 5 come later because they are easier to retrofit on top of a working layer 4 than the other way around. You can swap transports. You can replace an orchestrator. You cannot retroactively give a substrate a memory it never had.

Layer 6 comes last because if I cannot answer "what is this thing for" using only the bottom four layers, no graph view is going to save me.

## Where the inspiration came from

Two starting points are worth naming, and only naming.

Andre Karpathy posted a [gist on the LLM knowledge base pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) that frames the wiki as a compiler: raw input on one end, structured shared knowledge on the other, with explicit operations (ingest, flush, compile, lint, query) in between. That framing is the reason `knowledge/` is not just a folder of notes. It is a target for a pipeline that does not exist yet.

Cole Medin's [claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) is the hooks-driven, internal-data variant of the same pattern. When V2 ships `flush.py` and `compile.py`, that is the lineage I am building on.

Neither of these is a thesis I am defending. They are documents I read early, took the useful structural ideas from, and moved on. The interesting work is everything those starter docs do not say: how the filing gate decides what gets promoted, how the per-agent subdirectory convention prevents drift, what the contract between specialists actually looks like in practice.

## The point

If you build the dashboard first, you spend the next year propping it up.

If you build the substrate first, you spend the next year letting it compound.

V1 is the substrate. V2 is when it starts compounding. The dashboard is a V3 problem, and a small one, because by then the thing it is rendering will actually be worth looking at.

That is the whole pitch. The back of house first. The screenshot last.
