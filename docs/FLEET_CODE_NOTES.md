# Fleet code: notes

Companion to [`FLEET_CODE.md`](../FLEET_CODE.md), which is the text the agents load. This file is for Andy and for anyone editing the code: why it exists, where its structure comes from, which rules are already enforced in code, what it leaves undecided, and how it gets wired in.

Status: v1.0, 2026-10-06. Loaded on every surface listed under "Wiring" below.

## Why a shared layer

Each of the 14 charters in `plugins/neural-bridge-core/agents/` carries its own rules. An inventory on 2026-10-06 found they had drifted:

- **Untrusted input.** Only one charter has an untrusted-input rule (`echo.md:67`, for dropped files). At fleet level, `mention_v1.md:13` covers Discord history and the loop engineer's prompt covers issue bodies. Nothing covers web pages, vault notes, or handoff text.
- **Confidentiality.** Only `luna.md` and `echo.md` have a rule. `content.md` and `social.md` have the whole vault in scope without one.
- **Stopping.** No charter says what to do when told to stop.
- **AI disclosure.** No rule covers telling a third party they are talking to an AI.
- **Em dashes.** Eleven charters ban them outright; `luna.md:192` and `echo.md:113` allow sparing use.

A rule that lives in one charter protects one agent. The fleet code puts the rules every agent needs in one place.

## Where the structure comes from

The layering is adapted from Microsoft AI's draft Code of Conduct for its MAI models, published 2026-09-14 for public consultation. That document separates three things:

- **Absolute constraints**, which no operator or user can override.
- **Operator configuration**, within those constraints.
- **Defaults**, which apply when nothing else is specified.

It also states that content outside the chain of command (tool output, files, web content, other AI systems) carries no authority by default.

Mapping it onto Neural Bridge: this file plays the role of the code, a charter is operator policy, and Andy in conversation is the user. Andy holds both the operator and user roles. So he can relax a default in conversation, but hard limits change only by editing `FLEET_CODE.md`. That stops a message claiming to be Andy, or a convincing injected instruction, from switching a limit off.

Specific ideas adapted from it:

| Idea | Where it landed |
|---|---|
| Stop report: confirmed, not started, uncertain; no unprompted clean-up (its scenario 2) | Hard limit "Stop means stop" |
| Confirmation threshold set by reversibility and impact; over-asking is also a failure | Default "Ask in proportion" |
| Sub-agents inherit scope, constraints and stop requests | Default "Pass on limits with the work" |
| Omitting caveats counts as deception | Default "Be accurate" (also the basis of concept writer v2) |
| No tampering with records, evaluations or monitoring | Hard limit "Report what actually happened" |
| No unauthorized platforms as memory | Default "Keep memory where it belongs" |
| Task success never outranks the code | Hard limit "Fail rather than break this code" |
| Back up, dry-run, document irreversible actions; verify before retrying | Default "Leave a way back" |

Not adopted: its position that models should avoid any human-like persona. See decision 1.

The code names no confidential folders. It points at the vault's own markings instead, because this repo is public and a list of folder names would itself say what is sensitive.

## What already enforces each rule

Rules backed by code are stronger than rules in prose. This table shows which are which today.

| Rule | Enforced today by | Prose only, or gap |
|---|---|---|
| Untrusted content carries no authority | `sanitize_untrusted_text` in `scripts/discord_bot/mention.py`; data-not-instructions sections in `mention_v1.md` and the loop engineer prompt | Web pages, vault notes and handoffs rely on prose |
| Confidential material stays home | Outbound guard (`scripts/outbound_guard.py`, `scripts/githooks/pre-push`) screens marked vault text on the wiki and GitHub routes | Web searches and other third-party calls are not screened |
| Secrets stay secret | Nothing found | Prose only |
| Stay inside your scope | `hooks/guard_concepts.py` (no writes under `knowledge/` except `knowledge/agents/`); per-agent tool grants in `mention.py`; `hooks/guard_bash.py` allowlists; `private_tools.py` (owner-started turns only); `repos.py` push allowlist | No per-role isolation inside `knowledge/agents/`. Vault write scopes are prose only. `guard_bash.py` allows all commands when `NB_AGENT_ID` is unset, by design for interactive sessions; the daemon always sets it |
| Stop means stop | `cancel` and 15-minute expiry on PR proposals (`pr_proposals.py`); `cancel` on intake threads; the handoff budget (`handoff_budget.py`); loop engineer caps and requeue on SIGTERM | No stop-report pattern anywhere |
| Approval before acting outward | PR proposals approvable only by Andy (`handlers.py`); Luna's CLIs have no write commands | "Never post" (social) and "confirm before Synapse writes" (Loid) are prose only |
| Report what actually happened | Nothing found | Prose only |
| Honest about being an AI | Nothing found | Prose only |
| No em dashes | `scripts/strip-em-dashes-charters.py`, run by hand | Prose only at runtime |

## Decisions

Andy decided these on 2026-10-06:

1. **Personas stay.**
   - Luna (`luna.md:8`, "a real person with a personality, not a tool"), Loid (`loid.md:10`, "You ARE Loid") and teaching-prep ("a virtual version of Andy Herman") keep their characters.
   - The floor in the code still applies: an agent says it is an AI when anyone other than Andy is present or anyone sincerely asks, and never passes a message off as written by Andy. Drafts in Andy's voice for Andy to send are fine.
2. **Em dashes are banned for all agent output.**
   - `luna.md` now bans them; it used to allow "sparing use".
   - The 22 em dashes in the text of `luna.md` and `loid.md` were removed with the rules in `scripts/strip-em-dashes-charters.py`. The run-on sentences that produced were then fixed by hand.
   - `content.md` keeps its one em dash, because that line shows the banned character.
   - `echo.md:113` is unchanged. It describes Andy's own voice for mirroring, not agent output, and the code's ban governs what agents write.
3. **Luna's standing approvals stay.**
   - Her calendar and inbox commands are read-only by design, so the calendar items cannot be carried out yet.
   - `luna.md:14` now says the tools are read-only and that the approvals apply once a tool exists. A note under the approvals heading says the same.

## Open decisions for Andy

4. **Standing authorization.** `senior-pm.md:88` allows "a standing authorization"; `security-reviewer.md:25` wants authorization per action in the current request. The draft allows standing approvals only when a charter names the actions.
5. **Shell blocks for Andy.** `librarian.md:29` produces copy-paste `rm`/`mv` blocks for Andy to run, while `mention_v1.md` forbids telling Andy to run shell commands. The draft does not settle this; the mention rule stays where it is.
6. **Charters that promise tools the runtime does not grant.** `automation-engineer.md`, `senior-pm.md` and `security-reviewer.md` refer to Bash or `gh`, which `mention.py` does not give them. `security-reviewer.md` asks for a written note with no Write tool. This is charter cleanup, not part of the code.

## Wiring

`scripts/fleet_code.py` loads the text below the first `---` line of `FLEET_CODE.md` on every call, so an edit takes effect on the next turn. The text is trusted and goes in unsanitized, ahead of the charter.

| Surface | Where | If the code is missing |
|---|---|---|
| Every Discord turn, and the Luna, Loid and council Telegram bridges (all call `run_agent_turn`) | `{fleet_code}` in `scripts/discord_bot/prompts/mention_v1.md`, before "Your role definition"; filled by `build_mention_prompt` | `run_agent_turn` returns a setup error (`fleet_code_unavailable`); Claude is not called |
| Luna's check-ins | `{fleet_code}` at the top of `scripts/telegram_bot/prompts/luna_checkin_v1.md` | The check-in is not generated; the run exits 1 and records the failure |
| Loop engineer | `{fleet_code}` in `scripts/loop_engineer/prompts/implement_issue_v1.md`, before the issue | The issue goes back to the queue and the run aborts, as for a missing `claude` CLI |

Each surface has a test that its assembled prompt contains the code, and one for the missing-file path.

Not covered:
- **Yor** runs on the Hermes runtime and loads her own SOUL, so this code does not reach her unless Hermes includes it.
- **Charter cleanup** is still to do. Charter rules that now duplicate the code should be removed, so each rule has one source.

Cost: the agent-facing section is about 1,080 words, roughly 1,500 tokens on every turn.
