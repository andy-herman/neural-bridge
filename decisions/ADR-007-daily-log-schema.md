---
type: adr
number: "007"
title: Daily-log file schema (per-agent session record)
status: accepted
created: 2026-05-09
tags: [neural-bridge, adr, v2, schema, daily-logs, hooks, compile]
tracks: ["#8"]
---

# ADR-007 — Daily-log file schema

> Drafted 2026-05-09. Status: accepted. Resolves the schema question flagged in issue #8 before the SessionEnd hook gets coded.

## Context

The SessionEnd hook (issue #8) and `flush.py` (issue #9 — folded from #10) write per-agent daily logs that `compile.py` later promotes into shared `knowledge/concepts/`. The schema question — *frontmatter? freeform? what sections?* — has to be answered before code lands, because:

- `compile.py` needs structure to extract Decisions, Findings, etc. as candidate concept material.
- `lint.py` (#11) validates structure as part of the adversarial-content check.
- Re-deriving the schema mid-implementation forces rewriting flush + compile in tandem.

Three options were considered:

1. **Freeform markdown** — flush.py just writes whatever the model produced. Cheapest to implement; expensive to compile against (compile.py becomes a freeform-text parser).
2. **Single YAML blob per session** — entire session record as one structured object. Easy to parse; awkward to read in Obsidian.
3. **Structured markdown with section headers and per-session frontmatter** — explicit `## Session N` blocks with YAML inline + named sections (Decisions / Findings / Open questions / Proposed concepts). Readable in Obsidian; parseable by compile.py via section headers.

## Decision

**Option 3.** Daily logs are structured markdown with one file per agent per day, multiple session blocks appended over the course of the day.

### File location and naming

```
daily-logs/<agent>/YYYY-MM-DD.md
```

One file per agent per day. UTC date in filename. The day rolls over at 00:00 UTC; a session that crosses midnight UTC stays in the file matching its **start** date.

### File-level frontmatter

```yaml
---
type: daily-log
agent: <role>            # research | teaching-prep | content | senior-pm
date: 2026-05-09
schema_version: "1.0"
session_count: <int>     # auto-incremented as session blocks are appended
last_flushed_at: <ISO 8601 UTC>
---
```

### Per-session block structure

Each session is a `## Session <N>` H2 block, separated from the previous block by `---`. The N is one greater than the last session in the file.

```markdown
## Session <N> — <HH:MM> UTC

```yaml
session_id: <claude session id>
transcript_path: <absolute path to transcript jsonl>
transcript_sha256: <sha256 hex>
started_at: 2026-05-09T18:23:05Z
ended_at: 2026-05-09T19:47:12Z
flush_version: "1.0"
hook_event: SessionEnd | PreCompact
```

### Decisions

- <bullet>

### Findings

- <bullet>

### Open questions

- <bullet>

### Proposed concepts

- <slug>: <one-line summary>
```

### Section semantics

- **Decisions** — choices the user explicitly made or agreed to. Quote-worthy commitments; not "considered but didn't pick."
- **Findings** — substantive new knowledge surfaced this session: a paper, an incident, a working code pattern, a stat with provenance. One bullet per finding; if a finding deserves a concept article, list it under *Proposed concepts* too.
- **Open questions** — genuinely unresolved items the user wants surfaced for next session. Not items the agent forgot to look up; only items the user marked as "park this."
- **Proposed concepts** — slug + one-liner. `compile.py` reads this section as the explicit signal for promotion candidates. Format: `cve-2026-12345-mitigation: How to harden against the X attack class`.

### Empty-session handling

If `flush.py` has nothing substantive to record (no decisions, no findings, no questions, no proposals — i.e. all four sections would be empty), it does NOT append a session block. Instead it writes a single line to `daily-logs/_queue.log` with status `skipped:empty` and exits 0.

### Failed-flush handling

If the model returns malformed output (JSON parse failure, missing required sections, schema_version mismatch), `flush.py` writes the raw model output verbatim to `daily-logs/<agent>/_failed/<session_id>.txt` and writes `failed:parse` (or specific reason) to `_queue.log`. Never silently drops a session.

### Queue log

`daily-logs/_queue.log` is append-only, line-oriented:

```
<ISO 8601 UTC> <agent> <session_id> <status>
```

Where `<status>` is `flushed` | `failed:<reason>` | `skipped:<reason>`. The hook writes the breadcrumb (`flush_started`); flush.py writes the outcome.

## Consequences

**Positive:**
- compile.py has a stable section contract to extract from. Pass-1 reads `## Session <N>` blocks, parses inline YAML, walks named sections.
- lint.py can validate structurally: missing required sections, malformed YAML, schema_version drift.
- Daily logs are readable in Obsidian without a special viewer. The `## Session N — HH:MM UTC` heading is a useful navigation anchor.
- Provenance (transcript_sha256, session_id, hook_event) is mandatory. A poisoned daily log is traceable to its source transcript.

**Negative / accepted trade-off:**
- The structure is mildly verbose for short sessions. A 5-minute "what's the URL for X" session still produces a session block (or an explicit skip line in `_queue.log`). Acceptable cost for the compile-side simplicity.
- `schema_version: "1.0"` commits us to versioned migrations if the schema changes meaningfully. flush.py and compile.py must check schema_version on read and fail loudly on mismatch.
- Section headers are fixed (`Decisions / Findings / Open questions / Proposed concepts`). New section types require a schema bump.

**Foreclosed:**
- Freeform markdown logs.
- One-file-per-session (clutters `daily-logs/<agent>/`; harder to scan a day's work).
- Unstructured catch-all "notes" sections; everything goes in one of the four named sections or it's not in the log.

## Open implementation details (resolved by reference, not by ADR)

- The flush prompt template that produces this output lives in `hooks/prompts/flush_v1.md` (created in PR for #8). The prompt instructs the model to output in this exact shape. Re-prompting on parse failure is one retry, then fall through to `_failed/`.
- Schema validation is a small pure-Python helper in `hooks/schema.py` shared between flush.py, compile.py, and lint.py. No external dependencies (`yaml` from stdlib via `PyYAML` is acceptable; nothing heavier).

## Affected issues / PRs

- Closes the schema question in #8.
- Constrains the flush.py implementation in #9 (Phase A).
- Constrains the lint.py structural checks in #11.
