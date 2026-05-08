# Attribution

Neural Bridge stands on the shoulders of others. This file credits the prior art and external work that made this project possible.

## Conceptual influences

- **Andre Karpathy** — [LLM knowledge base pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f). The compiler analogy (raw → wiki → lint → query) is the foundation of the `knowledge/` layer.
- **Cole Medin** — [claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler). The hooks-driven internal-data variant of Karpathy's pattern. Neural Bridge's V2 compile pipeline is modeled on it.
- **Mark Kashef** ("ClaudeClaw") — multi-agent dashboard demo. The orchestrator pattern + transport layer + 3D activity graph are inspired by his work, though Neural Bridge takes a different (open, build-in-public) posture.

## Tooling

- [Claude Code](https://docs.claude.com/en/docs/claude-code) — the coding-agent CLI Neural Bridge runs on
- [Obsidian](https://obsidian.md/) — the recommended viewer for the markdown wiki
- The [Model Context Protocol](https://modelcontextprotocol.io/) — open protocol for tool/context integration

## Reference repositories

If you're studying multi-agent systems on coding-agent CLIs, these are worth reading:

- [anthropics/claude-cookbooks](https://github.com/anthropics/claude-cookbooks/tree/main/patterns/agents) — orchestrator-workers, evaluator-optimizer, parallelization patterns
- [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) — internal-data wiki pattern
- [liuyixin-louis/agentroom](https://github.com/liuyixin-louis/agentroom) — pixel-art office UI

## License

This project is MIT licensed (see [LICENSE](LICENSE)). The credits above are not a list of dependencies — they're acknowledgements of prior art that informed the design.
