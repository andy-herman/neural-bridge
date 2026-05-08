# Wiki Schema (Karpathy / Cole Medin pattern)

This wiki is the cross-agent memory layer of Neural Bridge. It is **maintained by LLM agents**, not by hand.

## Structure

| File / dir | Role |
|---|---|
| `index.md` | Content-oriented catalog. Always loaded into agent sessions on start. |
| `log.md` | Chronological append-only record. New entries prefixed with `## YYYY-MM-DD`. |
| `concepts/` | Cross-agent concept articles. One file per concept. Linked extensively. |
| `connections/` | Explicit cross-references between concepts (high-degree backlink hubs). |
| `agents/<name>/` | Per-agent memory subdirectory. Raw notes, session findings. |
| `quarantine/` | Articles that failed lint and weren't recovered. (Created on demand.) |

## Article conventions

- YAML frontmatter required: `type`, `created`, `tags`
- Wiki-links: `[[Page Name]]` (Obsidian-compatible)
- One topic per file
- Cross-link liberally — backlinks are the substrate of compounding knowledge

## Operations (per [Karpathy's PRD](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f))

| Operation | Trigger | Effect | Status |
|---|---|---|---|
| Ingest | New file in `raw/` | LLM summarizes and proposes concept articles | V3 |
| Flush | `SessionEnd` / `PreCompact` hook | Session transcript → daily log | V2 |
| Compile | Nightly cron | Daily logs → concept articles | V2 |
| Lint | Weekly | Find broken links, orphans, contradictions | V3 |
| Query | Any agent reading the wiki | Index-guided navigation, no vector DB | V1 ✅ |

## For agents writing to the wiki

1. **Per-agent notes** go in `agents/<your-role>/`. Free-form. You own your subdirectory.
2. **Concept articles** live in `concepts/`. Don't write them directly — propose them in your daily log and let the compile pass promote them.
3. **Index updates** happen automatically during compile. Don't edit `index.md` by hand unless you're patching a hand error.

## Sources

- [Karpathy's PRD](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [Cole Medin's claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler)
