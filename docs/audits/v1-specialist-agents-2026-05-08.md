---
type: audit
project: Neural Bridge
status: agent-drafted
created: 2026-05-08
tags: [neural-bridge, audit, v1, agents]
tracks: ["#13"]
---

# V1 Specialist Agent Audit

> Drafted by a Claude Code subagent on 2026-05-08. Status: agent-drafted, awaiting human review and action. Recommendations are starting points, not final calls.

## Overall observations

All three agents share a clean, near-identical scaffold (read-broadly preamble, write-narrow rule, tone block, escalation block) which is good for consistency but means the same V1/V2 mismatches and routing weaknesses repeat across all three files. The biggest cross-cutting issue is that every agent instructs the model to file daily notes into `knowledge/agents/<role>/YYYY-MM-DD-<slug>.md` as a session-end action, but the V2 `SessionEnd` hook and `flush.py` that would normally do this don't exist yet, so the agent itself must write the file inline, and that's not stated. `settings.json` is empty (no permissions, no hooks), which is correct for a scaffold but leaves the "sensitive tools scoped to one agent" rule from `AGENTS.md` line 55 entirely unenforced.

## research.md

**Well-designed:**
- Tone block (line 26) matches project voice exactly: "Tight, sourced, opinionated... No false balance."
- Escalation block (lines 28-32) is concrete and role-appropriate, especially "research request that's actually a writing or decision task in disguise."
- Cross-agent read list at lines 17-18 explicitly names the other two agents' subdirs, which is the AGENTS.md rule made operational.
- Tool list is the only one of the three that includes WebSearch/WebFetch as primary tools, which fits the role.

**V2 assumptions to flag:**
- Line 22: "propose it in your daily log" assumes a daily-log artifact the V2 flush hook produces. In V1 there is no daily log unless the agent writes one itself. Either point at the same per-agent note (line 21) or note the V1 stopgap explicitly.
- Line 21: "End every session with a markdown note" implies session-end execution. Without `SessionEnd` hook (settings.json lines 6-9 are empty), the agent has to do this itself before the user closes the session — make that explicit or the file won't get written.

**Cross-agent memory:** OK. Lines 14-19 follow the AGENTS.md line 68 rule cleanly: read concepts, connections (missing — see below), own subdir, and other agents' subdirs. Write-narrow at line 21. One gap: doesn't mention `knowledge/connections/` even though `AGENTS.md` line 36 lists it.

**Routing description:** OK but generic. "any question that needs web search" will pull this agent in for trivial lookups that the parent could handle. Suggested rewrite: `Researcher for current events, papers, regulations, and technical deep-dives requiring multi-source synthesis or primary-source citation. Not for quick factual lookups or rewriting existing material.`

**Tool scope:** Has `Write` but no `Edit`. That's fine for the "create new note per session" pattern but means it can't update its own prior notes. Probably intentional, flag for confirmation. No `Bash`, which is correct.

**Edit recommendations (priority order):**
1. Resolve the daily-log ambiguity (line 22). Either rename "daily log" to "session note" everywhere in V1, or add a one-liner that says "in V1, your session note serves as your daily log until the flush hook lands."
2. Tighten the routing description per above.
3. Add `knowledge/connections/` to the read list at lines 14-19 to match AGENTS.md.
4. Decide whether `Edit` belongs in tools — currently absent.

## teaching-prep.md

**Well-designed:**
- Line 21: skill-preference rule (`info310-speaker-notes`, `info310-lecture-rebuild`) is exactly right, those skills exist at user level and encode INFO 310 voice. This is the only agent that proactively names its skills.
- Line 23: "propose them, don't apply them directly. Pedagogical decisions are the user's." Good guardrail for an agent touching course materials.
- Escalation block (lines 31-34) is the strongest of the three; "anything touching student grades, accommodations, or accessibility" is FERPA-aware without saying it.

**V2 assumptions to flag:**
- Line 22: "File teaching insights into `knowledge/agents/teaching-prep/YYYY-MM-DD-<slug>.md`", same V2 issue as research.md. No flush hook in V1.
- No `propose in your daily log` line here (unlike research line 22 and content line 23), which means concept-article promotion has no path for this agent. Either intentional (teaching content stays per-agent) or an oversight.

**Cross-agent memory:** OK. Lines 15-19 follow the rule. Same `connections/` omission.

**Routing description:** Strong because of the INFO 310 specificity. The list "lecture content, lab design, assessment, speaker notes, deck cleanup" maps cleanly to actual tasks. No rewrite needed.

**Tool scope:** Has `Write` AND `Edit`, correct, since deck/lecture iteration requires editing existing files. Has `WebSearch`/`WebFetch` for finding current security incidents to use as examples, appropriate. No issues.

**Edit recommendations (priority order):**
1. Same daily-log clarification as research.md.
2. Decide whether teaching-prep should be allowed to propose concept articles (currently silent on this). If yes, add a line matching research line 22.
3. Consider adding `info310` to the description so the routing is unambiguous when other coursework comes up later: e.g. `Teaching prep specialist for INFO 310 only (Information Assurance and Cybersecurity, UW iSchool)...`. The "only" matters once Andy teaches anything else.

## content.md

**Well-designed:**
- Line 21: "Drafts only. The user reviews and posts; you never publish." Correct, and matches the build-in-public posture in Andy's research paper (write narrow, no autonomous external action).
- Line 22: "Show the work, don't summarize at it", exact phrasing of the build-in-public voice.
- Line 27: "No em dashes (this user dislikes them)." Concrete, enforceable, correct.
- Line 19's parenthetical "(cross-agent context matters; the research agent's findings often become content angles)" is the only place in any of the three agents where the *why* of cross-agent reading is explained. That's good documentation.

**V2 assumptions to flag:**
- Line 23: "Every draft goes in `knowledge/agents/content/drafts/YYYY-MM-DD-<slug>.md`." Note this is a *subdirectory* of the agent's subdir (`/drafts/`), which neither AGENTS.md nor knowledge/AGENTS.md specifies. Not wrong, but inconsistent with the other two agents, flag for normalization.
- Line 24: "propose a concept article in your daily log", same daily-log V2 issue.

**Cross-agent memory:** OK. Same `connections/` omission.

**Routing description:** Weakest of the three. "any external-facing writing that isn't teaching or research" is defined by exclusion, which forces the parent to evaluate two negatives. Suggested rewrite: `Drafts blog posts, video scripts, and social posts for Neural Bridge build-in-public content. Audience analysis and idea-pipeline work. Not for teaching materials (teaching-prep) or source synthesis (research).`

**Tool scope:** Has Write/Edit/WebSearch/WebFetch. Reasonable. But: this is the agent most likely to be asked to "post this to X" or "schedule this." With the empty settings.json allow-list (line 3), there's no enforced boundary preventing some future MCP `send_email` / posting tool from being available to this agent. AGENTS.md line 55 flags this; settings.json doesn't enforce it. Lock down via `disallowedTools` once any such tool exists.

**Edit recommendations (priority order):**
1. Tighten the routing description per above.
2. Decide whether `/drafts/` subfolder is the convention or whether content notes should sit flat in `knowledge/agents/content/` like the other two. Pick one and document in `knowledge/AGENTS.md`.
3. Same daily-log clarification.

## Cross-cutting recommendations

1. **V1 daily-log paragraph in all three agents.** Add (or replace the existing line) with a single explicit sentence: "In V1 there is no flush hook. Write your session note inline before the session ends; the V2 flush pipeline will replace this." Without this, agents will reference a "daily log" artifact that never gets created.

2. **Add `knowledge/connections/` to all three read lists.** AGENTS.md line 36 and knowledge/AGENTS.md line 12 list it as part of the wiki, but no agent reads from it. Either drop `connections/` from the schema for V1 or have the agents read it.

3. **`settings.json` is doing nothing.** AGENTS.md line 55 says "Sensitive tools (e.g. `send_email`) are scoped to one agent only via `disallowedTools` on the others", but `settings.json` has empty `permissions.allow` and empty hooks. For V1 this is fine (no sensitive tools yet), but add a comment-block or `README.md` next to `settings.json` recording the intended V2 enforcement model so it's not forgotten.

4. **Normalize the per-agent file path.** Two agents write to `knowledge/agents/<role>/YYYY-MM-DD-<slug>.md`, content writes to `knowledge/agents/content/drafts/YYYY-MM-DD-<slug>.md`. Pick one. If `/drafts/` is right for content (it probably is, drafts are categorically different from research notes), then teaching-prep likely needs `/insights/` or similar and the convention should land in `knowledge/AGENTS.md`.

5. **Routing-description style guide.** The strongest description (teaching-prep) names the specific domain. The weakest (content) defines by exclusion. Worth a one-sentence convention in `AGENTS.md`: "Descriptions should name what the agent does, not what it isn't, and should include one disambiguating phrase against sibling agents."

6. **No agent currently reads `knowledge/log.md`.** The wiki schema (knowledge/AGENTS.md line 8) calls it "always loaded" alongside `index.md`. Either the agents are wrong to skip it or the schema is wrong to claim it's always loaded. Reconcile.
