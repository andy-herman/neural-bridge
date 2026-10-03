# Neural Bridge

A personal AI substrate: fourteen specialized agents sharing a markdown wiki memory, reachable from a phone via Discord and Telegram, with a filing gate that defends the shared memory against prompt injection and poisoning.

## What this actually is, today

V1 ships and runs. Fourteen specialists are defined in the plugin; thirteen identities are registered in the Discord configuration. Luna and Loid have Telegram bridges, and Loid also participates in Council; Loid is not currently registered in the Discord configuration. The specialists read each other's notes, hand off to each other, and emit structured GitHub actions (file an issue, comment, label, close, recruit a new agent). Sessions flush to dated daily logs. A filing gate promotes (or rejects) candidate concepts before they reach the shared wiki. A weekly lint pass re-checks the wiki for drift.

The whole thing runs locally on a Mac Mini under `launchd`. There is no cloud infrastructure to manage, no service to pay for beyond a Claude Max subscription.

## The problem

Most personal AI workflows fragment into point tools — a chat tab, a coding session, a research workflow, each with its own short memory. Every project starts fresh. None of them compound.

Neural Bridge is the substrate where the work compounds.

## The substrate, in five layers

```
1. Agents          fourteen .md plugin files, each a specialist
2. Skills          plugin-shipped standards/skills + user-level skills
3. Transport       Discord (mention any agent from any device)
4. Shared state    knowledge/ wiki + daily-logs + filing gate + lint
5. Orchestration   Discord daemon + senior-pm + cross-agent handoff
```

## The fourteen agents

| Agent | Role |
|---|---|
| `luna` | AI operations colleague: read-only calendar/inbox CLIs, recommendations and approved handoffs |
| `research` | Deep reading, citations, threat model write-ups |
| `teaching-prep` | Professor: INFO 310A-only instructional peer, source-grounded review and authorized corpus notes |
| `content` | Long-form drafts for the blog and LinkedIn |
| `social` | Short-form posts, X drafts, social copy |
| `senior-pm` | Issue triage, kanban moves, weekly summaries |
| `recruiter` | Designs and provisions new specialist agents |
| `automation-engineer` | Hooks, scripts, daemon work |
| `security-reviewer` | Audits prompts, flows, and PRs for prompt-injection / data-leak risks |
| `docs-editor` | Tightens prose, fixes drift in the wiki |
| `librarian` | Maintains the Luna Master Obsidian vault: INDEX, audits, structure |
| `echo` | Voice-double: keeps Andy's voice profile, flags AI-sounding drafts |
| `loid` | Career strategist on Telegram and Council, backed by the Synapse DB |
| `ux-designer` | Look and feel for neural-bridge-blog and other web surfaces |

`@` the registered identities in `#neural-bridge` on Discord. They read the relevant context, respond, and can hand off to each other (including multi-round `/squad-discuss` sessions).

### Companion standard

All fourteen charters preload one [plugin-shipped conversation standard](plugins/neural-bridge-core/skills/companion-standard/SKILL.md). NB wrappers also load its full body explicitly for Discord, Luna/Loid Telegram, Council Loid and Luna check-ins; missing or unreadable contract setup stops before model invocation. Luna, Loid and Professor keep distinct voices and current authority. Yor's external Hermes path is unchanged. [Loading, boundaries and design-test limits](docs/COMPANION_STANDARD.md) distinguish source behavior from native-plugin or generated-output verification.

## Memory pipeline

Daily logs are cheap and per-agent. Concepts are expensive and cross-agent. Promoting a daily log entry into a concept article passes through a filing gate that asks one question: **PROMOTE, QUARANTINE, or REJECT?**

The gate checks for imperative AI-directed language (textbook prompt injection), untraceable claims, self-promotion, concept-worthiness, slug coherence, and adversarial signal in the source. If a candidate concept fails any of those, it never makes the wiki.

Background and threat model: [Memory Poisoning in Personal Agentic AI Substrates](https://neural-bridge.dev/research/memory-poisoning-in-personal-agentic-ai-substrates).

## Discord orchestrator

Thirteen bot identities, one daemon, one asyncio loop. Each agent has its own Discord application and Message Content Intent. Separate Telegram bridges carry Luna DMs and Loid (transcribed voice and text); an available Honcho card supplies bounded context, not identical full memory across agents or Hermes. The daemon:

- Routes `@agent` mentions to the right specialist
- Loads the shared companion standard and role body into the prompt
- Calls `claude -p` with the existing per-agent tool allowlist (most roles have no Bash; Luna/Loid CLI grants remain narrow)
- Extracts a single fenced ` ```actions ` block from the reply, validates it, executes via `gh`
- Posts the response back as the agent's bot
- Tracks per-channel turn budget so cross-agent chains can't run away

`senior-pm` also exposes slash commands: `/pm-task`, `/pm-summary`, `/triage`, `/squad-discuss`, `/close`.

### Private specialist observations

[Moonbase telemetry](docs/MOONBASE_TELEMETRY.md) is an optional local JSON
snapshot of the discovered registry and actual model-subprocess execution.
It is disabled unless `NB_AGENT_TELEMETRY_PATH` names a private file outside
Git checkouts. Registry-only export shows every defined specialist as
unobserved, without starting a bot. Writer freshness, gateway connectivity,
and model activity remain separate; this is not a task-completion feed or the
application-level Fleet heartbeat. No operational endpoint is added.

## Repo map

```
.claude-plugin/marketplace.json    plugin marketplace declaration
plugins/neural-bridge-core/        the core plugin
  agents/                          fourteen specialist .md definitions
  skills/companion-standard/       single shared conversation contract
hooks/                             session_start, user_prompt_submit, session_end, flush, wiki_recall, guards, schema
scripts/                           compile (filing gate), lint, discord_bot/
  discord_bot/                     daemon, mention routing, GitHub actions
  launchd/                         persistence under launchd
knowledge/                         the LLM-maintained wiki
  agents/                          per-agent session notes
  concepts/                        cross-agent concept articles (filed via gate)
  quarantine/                      concepts the gate refused (with reason)
  index.md                         always-loaded starting point
daily-logs/                        per-agent session summaries
decisions/                         ADRs
docs/                              STATUS.md, lint reports, audits
```

## Setup

This repo ships as a Claude Code plugin marketplace plus a Discord daemon.

1. Clone the repo and install [Claude Code](https://docs.claude.com/en/docs/claude-code).
2. Install the plugin:
   ```
   /plugin marketplace add andy-herman/neural-bridge
   /plugin install neural-bridge-core@neural-bridge
   ```
3. (Optional) Open the repo as an [Obsidian](https://obsidian.md/) vault for graph view + backlinks.
4. (Optional, for the Discord orchestrator) Create one Discord application per agent (thirteen today), enable Message Content Intent on each, store the tokens in macOS keychain (`security add-generic-password ...`), populate `scripts/discord_bot/agents.json` with each application's client ID, then `./scripts/launchd/install.sh` to register the daemon with `launchd`.

## Build journal

[docs/STATUS.md](docs/STATUS.md) for chronological progress. Posts about the build and the threat model live at [neural-bridge.dev](https://neural-bridge.dev).

## License

MIT — see [LICENSE](LICENSE).

## Attribution

See [ATTRIBUTION.md](ATTRIBUTION.md) for credits and prior art (Karpathy's pattern, Cole Medin's claude-memory-compiler, AgentPoison and PoisonedRAG threat-model research).
