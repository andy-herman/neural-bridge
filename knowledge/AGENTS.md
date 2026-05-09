# Wiki Schema (Karpathy / Cole Medin pattern)

This wiki is the cross-agent memory layer of Neural Bridge. It is **maintained by LLM agents**, not by hand.

## Structure

| File / dir | Role |
|---|---|
| `index.md` | Content-oriented catalog. Always loaded into agent sessions on start. |
| `log.md` | Chronological append-only record (e.g., compile-pass entries). Read on demand, not always loaded. |
| `concepts/` | Cross-agent concept articles. One file per concept. Linked extensively. |
| `connections/` | Explicit cross-references between concepts (high-degree backlink hubs). |
| `agents/<name>/` | Per-agent memory subdirectory. Raw notes, session findings. |
| `quarantine/` | Articles that failed the filing gate. Human-reviewed, not auto-promoted. |

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
2. **Per-agent subdirectory convention.**
   - `research/`, `teaching-prep/`, `senior-pm/` — flat. Files at `agents/<role>/YYYY-MM-DD-<slug>.md`.
   - `content/`, `social/` — use a `drafts/` subdirectory: `agents/<role>/drafts/YYYY-MM-DD-<slug>.md`. Drafts are categorically different from research notes; the subfolder makes this explicit.
   - If a future agent's outputs split into categories (e.g., teaching-prep adding `insights/` later), document the convention here first.
3. **Concept articles** live in `concepts/`. Don't write them directly. Surface concept proposals in session content; `hooks/flush.py` extracts them into `daily-logs/<role>/`, and `scripts/compile.py` runs the filing gate before any concept article is created.
4. **Index updates** happen during compile. Don't edit `index.md` by hand unless you're patching a hand error.

## Routing-description style guide (for agent frontmatter)

The `description` field in each agent's frontmatter is the routing signal. Write it to make the parent's choice obvious.

- **Name what the agent does, not what it isn't.** "Researcher for technical deep-dives" beats "any question that isn't teaching or content."
- **Include one disambiguating phrase against sibling agents.** Especially when the domain overlaps. "Not for teaching materials (use teaching-prep)" is clearer than implying it.
- **Be specific about scope.** "INFO 310 only" beats "teaching prep" when the user might teach other courses later.
- **Avoid descriptions that route by exclusion.** Forces the parent to evaluate two negatives. Define positively.

## Sources

- [Karpathy's PRD](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [Cole Medin's claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler)
