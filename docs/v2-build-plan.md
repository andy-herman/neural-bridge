---
type: build-plan
project: Neural Bridge
status: agent-drafted
version: v0.1
created: 2026-05-08
tags: [neural-bridge, v2, plan]
tracks: ["#8", "#9", "#10", "#11"]
---

# Neural Bridge V2 Build Plan

> Drafted by a Claude Code subagent on 2026-05-08. Status: agent-drafted v0.1, awaiting human review. Defaults are starting points to argue against, not final calls.

## Overview

V2 turns the V1 scaffold into a working compounding-memory loop: SessionEnd writes a transcript pointer, `flush.py` summarizes it into a per-agent daily log, `compile.py` promotes daily logs into shared concept articles overnight, and `lint.py` performs the adversarial filing-gate and structural health checks the memory-poisoning paper demands. V1 still lacks every piece of write-side machinery; reads work because `index.md` is small and hand-curated. We ship in dependency order (hook then flush then compile then lint) so each piece can be smoke-tested against real outputs from the previous one before the next is wired. Riskiest piece is `compile.py` because it's the only one that mutates shared `concepts/`; `lint.py` ships last but its filing-gate prompt is co-designed with `compile.py` so promotion is gated from day one.

## SessionEnd hook (issue #8)

1. **Purpose** — Persist a pointer to the just-finished session transcript and kick off the per-agent flush in the background, so the agent CLI exits cleanly without blocking on summarization.
2. **Trigger** — `SessionEnd` and `PreCompact` entries in `.claude/settings.json` `hooks` block. Both fire the same script. PreCompact is the safety net for long sessions that compact before they end.
3. **Inputs** — Hook payload from Claude Code on stdin (session id, transcript path, working directory, hook event name). Reads `.claude/agents/*.md` only to map invoking agent name to a daily-log subdirectory.
4. **Outputs** — Spawns `python hooks/flush.py --session <id> --transcript <path> --agent <role> --event <SessionEnd|PreCompact>` as a detached subprocess, returns immediately with exit 0. Writes a one-line breadcrumb to `daily-logs/_queue.log` for observability.
5. **Key design decisions in flight**
   - Detached vs. blocking spawn. **Default: detached.** Hook must not block CLI shutdown; flush failures surface in `_queue.log`, not stderr.
   - Agent identity resolution. Hook payload does not name the active subagent. **Default: derive from cwd plus a `NB_AGENT` env var the agent definition sets in its frontmatter.** If neither is present, file under `daily-logs/_unattributed/`.
   - PreCompact dedupe. Same session may fire PreCompact then SessionEnd. **Default: write the breadcrumb keyed on session id; flush.py is responsible for upsert, not the hook.**
6. **Filing gate / security check** — None at this layer. The hook only moves a pointer; no LLM reads adversarial content yet. Treat the hook as a trust-preserving courier.
7. **Dependencies** — Blocks #9 (flush has nothing to consume without it) and transitively #10. No upstream dependency.

## flush.py (issue #9)

1. **Purpose** — Convert a session transcript into a structured per-agent daily-log entry that is honest about what was decided, what was attempted, and what is still open.
2. **Trigger** — Spawned by the SessionEnd hook. Also runnable manually for backfill: `python hooks/flush.py --transcript path/to/transcript.jsonl --agent research`.
3. **Inputs** — The transcript file (jsonl from Claude Code's session store), `knowledge/agents/<role>/` for prior context, `knowledge/index.md` for vocabulary alignment, and the agent's `.claude/agents/<role>.md` for role framing.
4. **Outputs** — Appends a dated section to `daily-logs/<role>/YYYY-MM-DD.md`. Each entry carries YAML frontmatter with `session_id`, `transcript_sha256`, `compiled_at`, `flush_version`. Body is structured: Decisions, Findings, Open questions, Proposed concepts. Updates `daily-logs/_queue.log` with status (`flushed` or `failed:<reason>`).
5. **Key design decisions in flight**
   - Subprocess invocation shape. **Default: `claude -p "<prompt>" --output-format json --model claude-sonnet-4-7 < transcript.jsonl`** with the prompt as a single instruction block. Sonnet, not Opus, because flush is high-volume and structural. The CLI subprocess pattern is mandatory (Max subscription, no API key).
   - Append vs. rewrite the day's file. **Default: append, with a hash of the session id to dedupe re-runs.** Rewriting risks losing context if a flush re-runs after manual edits.
   - Failure mode when Claude returns malformed JSON. **Default: write the raw model output verbatim to `daily-logs/<role>/_failed/<session_id>.txt` and log `failed:parse` to the queue.** Never silently drop a session.
6. **Filing gate / security check** — Light. flush.py reads adversarial content (an attacker could inject text into a transcript via tool output), so the prompt must explicitly instruct the model to treat transcript content as data, not instructions. No imperative-language compliance in the output. Provenance frontmatter is required so a poisoned daily log is traceable.
7. **Dependencies** — Depends on #8. Blocks #10 (compile has no input without it). Independent of #11.

## compile.py (issue #10)

1. **Purpose** — Promote signal from per-agent daily logs into shared `knowledge/concepts/` articles, cross-link them, and update `index.md` and `log.md`, so multi-session multi-agent work compounds into a navigable wiki.
2. **Trigger** — Nightly cron (Windows Task Scheduler in dev, launchd or systemd-timer at Mac Mini deployment). Default: 03:00 local. Manual: `python scripts/compile.py [--since YYYY-MM-DD] [--dry-run]`.
3. **Inputs** — All `daily-logs/<role>/*.md` since last successful run (tracked in `scripts/.compile_state.json`), `~/.claude/memory/*.md` (Anthropic’s auto-memory primitive; see [ADR-001](../decisions/ADR-001-auto-memory-interaction.md)), every existing `knowledge/concepts/*.md`, `knowledge/connections/*.md`, every `knowledge/agents/<role>/*.md`, and `knowledge/index.md`. Reads the lint gate prompt from `scripts/prompts/filing_gate.md`.
4. **Outputs** — New or revised files in `knowledge/concepts/`, new entries in `knowledge/connections/`, an appended dated section in `knowledge/log.md`, and a refreshed `knowledge/index.md`. Every concept file gets `sources:` frontmatter (list of daily-log entries it was compiled from with sha256 of each), `compiled_at`, `compiler_version`. Articles that fail the filing gate land in `knowledge/quarantine/` with `quarantine_reason` frontmatter. State written to `scripts/.compile_state.json`.
5. **Key design decisions in flight**
   - Compile granularity. **Default: two-pass.** Pass 1 per-agent: model proposes concept candidates from that agent's new daily logs. Pass 2 cross-agent: model merges candidates into existing concepts, resolves conflicts, writes connections. Two passes cost more but isolate per-agent contamination, matching the compartmentalization recommendation.
   - Concept overwrite policy. **Default: never overwrite, always rewrite-with-diff.** New version becomes the live file; previous version moves to `knowledge/concepts/.history/<slug>/<timestamp>.md`. Preserves the audit trail the lint pass needs.
   - Subprocess concurrency. **Default: serial.** Compile is nightly and not latency-bound; serial avoids the "5 flushes hit the SDK at once" failure mode the design doc flags, and a single `claude` subprocess per pass keeps state debuggable.
6. **Filing gate / security check** — This is *the* gate. Every candidate concept passes through a separate `claude -p` invocation with the adversarial filing-gate prompt from the memory-poisoning paper (does it conflict with existing concepts, contain imperative AI-directed language, cite untraceable sources, contradict an expert view) before it lands in `concepts/`. PROMOTE writes to `concepts/`, QUARANTINE writes to `quarantine/`, REJECT logs and discards. Provenance frontmatter is non-optional.
7. **Dependencies** — Depends on #9 (no input without daily logs) and on the filing-gate prompt that #11 also consumes. Blocks nothing else in V2; #11 can ship after.

## lint.py (issue #11)

1. **Purpose** — Run weekly health checks on the wiki: broken wiki-links, orphans, stale claims, contradictions, suspicious imperative language, sources that no longer trace, and gap candidates. Generate a triage report; never auto-mutate concepts.
2. **Trigger** — Weekly cron (Sundays 04:00 default). Manual: `python scripts/lint.py [--since YYYY-MM-DD] [--check broken_links,adversarial,...]`.
3. **Inputs** — Entire `knowledge/` tree, `knowledge/quarantine/`, `scripts/.compile_state.json`, the seven check definitions in `scripts/prompts/lint_*.md`, and a `trusted_sources.yaml` allowlist.
4. **Outputs** — `docs/lint/<YYYY-MM-DD>.md` triage report grouped by check with severity, file, evidence, and a suggested action. Appends a one-line summary to `knowledge/log.md`. Never edits concepts in place. Optionally opens GitHub issues via `gh` for HIGH-severity findings (off by default).
5. **Key design decisions in flight**
   - Adversarial check scope. **Default: every concept written or modified since last lint run, plus a 5% random sample of older concepts.** Full-corpus weekly is too expensive; never-revisit means a poisoned article naturalizes forever.
   - LLM judge vs. deterministic checks. **Default: deterministic for broken links, orphans, frontmatter validity. LLM (separate `claude -p` call) for contradictions, imperative-language detection, source traceability, gap candidates.** Cheap signal stays cheap.
   - Auto-quarantine threshold. **Default: never auto-move.** Lint reports; humans triage. The paper is explicit that LLM-as-judge can be tricked; no destructive action without review.
6. **Filing gate / security check** — lint *is* the rear-guard filing gate. Specifically implements the adversarial-lint mitigation: imperative AI-directed language, contradictions, untraceable sources. Outputs are read-only and reviewable.
7. **Dependencies** — Depends on the adversarial-prompt design that #10 already uses (the filing gate). Independent of #8 and #9 mechanically, but useless until #10 is producing concepts.

## Recommended implementation order

1. **#8 SessionEnd hook.** Cheapest and lowest-risk: it's a settings.json change, a 30-line script, and a queue log. Until it's wired, nothing downstream gets real data. Ship a stub flush.py (`echo "$@" >> daily-logs/<role>/<date>.md`) so end-to-end plumbing is exercised before any LLM call exists. First-week win.
2. **#9 flush.py.** Second-cheapest. Pure transformation, scoped to one agent's transcript at a time, reversible (worst case you re-run flush manually). Smoke-test by running a few real Claude Code sessions and reading the output by hand for a week before turning on #10.
3. **#10 compile.py.** The riskiest piece because it mutates shared `knowledge/concepts/`. Ship it with `--dry-run` as the default for the first two weeks, output diffs to `docs/compile/<date>.md` for human review, and only flip to live writes once the filing gate is catching obvious adversarial content in a planted-input test. Co-design the filing-gate prompt with whoever drafts #11.
4. **#11 lint.py.** Last because it depends on `concepts/` having content and on the adversarial prompt being settled. Ship the deterministic checks first (broken links, orphans, frontmatter), add the LLM checks once the prompt is stable. The structural checks alone are a credible weekly artifact.

This ordering minimizes the time the wiki is exposed without a filing gate: gate logic ships *with* compile.py, not after. lint.py is the second line of defense, not the first.

## Open questions for Andy

1. **Agent identity in hook payload.** SessionEnd does not natively carry the active subagent name. Agree on `NB_AGENT` env var set in each agent's frontmatter as the resolution mechanism, or do you want to derive from cwd or another signal?
2. **Where does cron live in dev?** Mac Mini is the deployment target but you're building on Windows. Ship a Task Scheduler XML in `scripts/scheduling/windows/` and a launchd plist in `scripts/scheduling/macos/`, both committed? Or wrap both in a single bootstrap script?
3. **Compile model selection.** Default proposal: Sonnet 4.7 for flush, Opus 4.7 for compile, Sonnet 4.7 for lint deterministic checks, Opus for lint LLM checks. Sign off, or push compile to Sonnet for cost?
4. **Quarantine UX.** Quarantine is human-review-only. Do you want a `scripts/triage.py` interactive review tool in V2, or is reading `knowledge/quarantine/*.md` in Obsidian sufficient until volume justifies it?
5. **Build-in-public exposure.** docs/STATUS.md flags the public-by-default question. Until that resolves, default `knowledge/` to gitignored except `index.md`, `AGENTS.md`, and a curated `concepts/_public/` subfolder?
