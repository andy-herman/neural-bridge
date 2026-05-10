# Neural Bridge

A personal AI substrate: nine specialized agents sharing a markdown wiki memory, reachable from a phone via Discord, with a filing gate that defends the shared memory against prompt injection and poisoning.

## What this actually is, today

V1 ships and runs. Nine specialists answer @-mentions in Discord. They read each other's notes, hand off to each other, and emit structured GitHub actions (file an issue, comment, label, close, recruit a new agent). Sessions flush to dated daily logs. A filing gate promotes (or rejects) candidate concepts before they reach the shared wiki. A weekly lint pass re-checks the wiki for drift.

The whole thing runs locally on a Mac Mini under `launchd`. There is no cloud infrastructure to manage, no service to pay for beyond a Claude Max subscription.

## The problem

Most personal AI workflows fragment into point tools — a chat tab, a coding session, a research workflow, each with its own short memory. Every project starts fresh. None of them compound.

Neural Bridge is the substrate where the work compounds.

## The substrate, in five layers

```
1. Agents          nine .md plugin files, each a specialist
2. Skills          inherited from user-level Claude Code settings
3. Transport       Discord (mention any agent from any device)
4. Shared state    knowledge/ wiki + daily-logs + filing gate + lint
5. Orchestration   Discord daemon + senior-pm + cross-agent handoff
```

## The nine agents

| Agent | Role |
|---|---|
| `research` | Deep reading, citations, threat model write-ups |
| `teaching-prep` | INFO 310 lecture material, slide outlines, exercises |
| `content` | Long-form drafts for the blog and LinkedIn |
| `social` | Short-form posts, X drafts, social copy |
| `senior-pm` | Issue triage, kanban moves, weekly summaries |
| `recruiter` | Designs and provisions new specialist agents |
| `automation-engineer` | Hooks, scripts, daemon work |
| `security-reviewer` | Audits prompts, flows, and PRs for prompt-injection / data-leak risks |
| `docs-editor` | Tightens prose, fixes drift in the wiki |

`@` any of them in `#neural-bridge` on Discord. They read the relevant context, respond, and can hand off to each other.

## Memory pipeline

Daily logs are cheap and per-agent. Concepts are expensive and cross-agent. Promoting a daily log entry into a concept article passes through a filing gate that asks one question: **PROMOTE, QUARANTINE, or REJECT?**

The gate checks for imperative AI-directed language (textbook prompt injection), untraceable claims, self-promotion, concept-worthiness, slug coherence, and adversarial signal in the source. If a candidate concept fails any of those, it never makes the wiki.

Background and threat model: [Memory Poisoning in Personal Agentic AI Substrates](https://neural-bridge.dev/research/memory-poisoning-in-personal-agentic-ai-substrates).

## Discord orchestrator

Nine bot identities, one daemon, one asyncio loop. Each agent has its own Discord application and Message Content Intent. The daemon:

- Routes `@agent` mentions to the right specialist
- Loads the agent's plugin definition into the prompt
- Calls `claude -p` with the right tool allowlist (Read / Write / Edit / WebSearch / WebFetch — never Bash)
- Extracts a single fenced ` ```actions ` block from the reply, validates it, executes via `gh`
- Posts the response back as the agent's bot
- Tracks per-channel turn budget so cross-agent chains can't run away

`senior-pm` also exposes slash commands: `/pm-task`, `/pm-summary`, `/triage`, `/squad-discuss`, `/close`.

## Repo map

```
.claude-plugin/marketplace.json    plugin marketplace declaration
plugins/neural-bridge-core/        the core plugin
  agents/                          nine specialist .md definitions
hooks/                             session_end, session_start, flush, schema
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
4. (Optional, for the Discord orchestrator) Create nine Discord applications, enable Message Content Intent on each, store the tokens in macOS keychain (`security add-generic-password ...`), populate `scripts/discord_bot/agents.json` with each application's client ID, then `./scripts/launchd/install.sh` to register the daemon with `launchd`.

## Build journal

[docs/STATUS.md](docs/STATUS.md) for chronological progress. Posts about the build and the threat model live at [neural-bridge.dev](https://neural-bridge.dev).

## License

MIT — see [LICENSE](LICENSE).

## Attribution

See [ATTRIBUTION.md](ATTRIBUTION.md) for credits and prior art (Karpathy's pattern, Cole Medin's claude-memory-compiler, AgentPoison and PoisonedRAG threat-model research).
