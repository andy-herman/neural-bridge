# Bounded companion standard

The first pass strengthens Luna (operations colleague), Loid (career record,
story and preparation) and Professor (`teaching-prep`, INFO 310A-only instructional
peer). All fourteen roles keep their IDs, frontmatter tools/models/colors and
distinct specialties. Yor on external Hermes remains the primary thinking
companion; her invocation path is unchanged.

## One source, two loading paths

The sole shared contract is
`plugins/neural-bridge-core/skills/companion-standard/SKILL.md`.

**NB wrappers:** `mention.load_agent_definition` loads the full skill body and
role body, stripping metadata. `run_agent_turn` uses that definition for Discord,
Luna/Loid Telegram and Council Loid, including stateless turns and the existing
single fresh-session retry after resume failure. Missing, unreadable or empty
contracts, or incomplete metadata fences, return a logged, user-visible error
before model invocation or session mutation. Setup diagnostics use fixed reasons
and exception types, not raw file contents. Telegram callers identify their surface and
report actual setup failures, not a misleading "mention prompt missing" label.
Luna check-ins load the same contract on their separate path and surface setup
failure through stderr, failure telemetry and a nonzero exit; they do not call
the model or send a check-in on failure.

**Direct Claude plugin:** install/enable `neural-bridge-core` and select the
desired plugin role (for example, `neural-bridge-core:luna`). Each charter contains
`skills: [neural-bridge-core:companion-standard]`, and the agent provisioner emits
that entry for future charters. Claude's documented subagent skill preloading
injects the full skill at startup; a body-level "read this policy" ritual or
dynamic shell glue is not needed. The shared skill is also addressable as
`/neural-bridge-core:companion-standard` in a plugin-enabled Claude session.

Native Claude does **not** provide the NB fail-closed guarantee: missing or
disabled preload skills can be skipped with a debug warning. Verify the enabled
installed plugin's skill and charter entries before relying on that path.
Source/static tests cannot establish native installed-plugin preload.

References:
- [Claude subagent skill preloading](https://code.claude.com/docs/en/sub-agents#preload-skills-into-subagents)
- [Claude plugin packaging](https://code.claude.com/docs/en/plugins-reference)

## Boundaries that did not change

The standard guides attention, honest continuity, respectful disagreement,
concrete repair and pressure-free endings. It grants no new tools, models,
effort, timeouts, private access, memory sharing, schedules or automatic actions.
Role frontmatter is not a declaration of deployed NB model/tool grants.

Discord action/attachment execution and approval remain transport-owned.
Telegram conversation paths are text-only; a suggested handoff or draft is
owner-delivered, not dispatched. Luna's calendar/inbox CLIs remain read-only and
Drive overflow does not authorize sharing changes. Loid keeps formal Korean,
verbatim confirmed Synapse writes and owner-delivered handoffs. Professor keeps
immutable Section A, authorized Section B/research/alignment notes and their
existing update conventions. Project Husky or a generic INFO 310 label does not
authorize another course; a future review-only adapter is not implemented.

## Evidence and acceptance limits

Regression tests assert full real-contract inclusion, setup/no-model behavior,
surface declarations, retry/stateless preservation, charter metadata and
provisioner entries. `scripts/discord_bot/fixtures/companion_design_cases.json`
contains synthetic **behavioral design tests**, including positive role-specific
outputs and observable failure criteria. Tests check that those criteria trace to
authored prompt clauses; they do not generate or score model replies.

Neither static inclusion nor a design example proves companionship quality,
perfect memory, well-being benefits or native preload. No live private-service
call, paid model trial, deployment, restart, push or automatic handoff is part of
this implementation.
