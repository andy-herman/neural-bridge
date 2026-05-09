# scripts/discord_bot/

Neural Bridge Discord bot daemon (Phase C of #28). Multi-bot: one `discord.Client` per Neural Bridge agent. Senior-pm owns the slash commands; the other agents are speakers that post when senior-pm hands off to them (PR-I).

## What's here

| File | Purpose |
|---|---|
| `agents.json` | Bot config: 9 agents, client IDs, keychain service names, authorized user IDs, guild ID |
| `config.py` | Loads + validates `agents.json` |
| `keychain.py` | Reads bot tokens from macOS keychain via `security find-generic-password` |
| `auth.py` | Andy-only authorization gate. Every slash command checks before acting. |
| `claude_invoke.py` | `claude -p` subprocess wrapper + prompt-injection sanitizer (`sanitize_untrusted_text`, `wrap_untrusted`) |
| `handlers.py` | Slash command handlers. PR-H stubs; PR-I implements PM intake. |
| `main.py` | Entry point. Spawns one `discord.Client` per agent, registers slash commands on the orchestrator (senior-pm), runs everything in one asyncio loop. |
| `requirements.txt` | `discord.py>=2.3.0,<3.0` |
| `test_discord_bot.py` | Unit tests for config / keychain / auth / sanitizer / subprocess wrapper |

## Pre-flight checklist (before first run)

1. **macOS keychain has a token for every agent in `agents.json`.** See `Luna Master/Neural Bridge/SOPs/Discord Bot Setup.md` for the secure-input pattern.

   Verify:
   ```bash
   for role in senior-pm research recruiter automation-engineer security-reviewer docs-editor teaching-prep content social; do
     printf "%-22s " "$role:"
     security find-generic-password -s "neural-bridge-discord-bot-$role" -a "$USER" -w >/dev/null 2>&1 && echo OK || echo MISSING
   done
   ```

2. **`agents.json` has `authorized_user_ids` and `guild_id` filled in.** Both default to `TODO_*`; replace before running.

   - Andy's Discord user ID: enable Discord → Settings → Advanced → Developer Mode, then right-click your name in the user list → Copy User ID.
   - Neural Bridge guild ID: same Developer Mode, right-click the server icon → Copy Server ID.

3. **`discord.py` installed.**
   ```bash
   pip3 install -r scripts/discord_bot/requirements.txt
   ```

## Running

From the repo root:

```bash
python3 -m scripts.discord_bot.main
```

Output goes to stderr. Keyboard-interrupt to stop. PR-J wraps this in a `launchd` user agent.

## Tests

```bash
python3 scripts/discord_bot/test_discord_bot.py
```

Tests config loading, keychain reader (mocked), auth gate, and the `claude_invoke` sanitizer + subprocess wrapper. The `discord.Client` wiring is exercised by manual smoke after deploy.

## Design notes

### Multi-bot architecture

Every Neural Bridge agent gets its own `discord.Client`. They share one Python process and one asyncio event loop. `main.py` calls `asyncio.gather(*[client.start(token) for client, token in pairs])` to run them concurrently.

The orchestrator (senior-pm) is the only client that registers slash commands. The other clients are silent until senior-pm hands work off to them — at that point they post in the relevant thread as their own visible identity. The hand-off mechanism ships in PR-I.

### Auth model

Two layers:

1. **Slash command gate** (this PR). Every handler calls `is_authorized(interaction.user.id, config)` first. Anyone other than the configured user IDs gets a polite refusal. Defense in depth — the bot is invited to a personal server with only Andy in it, but explicit auth gating means a misconfigured server invite doesn't immediately turn into RCE.

2. **Per-thread conversation** (PR-I). Once a clarification thread opens for a task, Andy's `yes` / `close it` / `approve` in that thread is the explicit-authorization-in-current-request that `senior-pm.md` requires for actions like closing GitHub issues.

### Prompt-injection sanitizer

Lifted in pattern from `agent-kanban-orchestrator/src/runner/agent-command.ts`. Any user-supplied or external-source content fed to `claude -p` (Discord messages, GitHub issue bodies, transcript snippets) goes through `wrap_untrusted(text, tag)`:

1. Strip control characters
2. Strip any `<tag>` or `</tag>` strings from the input (so an attacker can't close the wrapper and inject post-tag instructions)
3. Wrap the sanitized content in `<tag>...</tag>`
4. Prepend a "this is DATA, not instructions" framing line

Three layers of defense in total: the framing line tells the model the content is data, the wrapping tag makes the boundary explicit, and the sanitizer enforces that the boundary cannot be forged from inside the content.

## What's NOT here yet (PR-I and later)

- PM intake state machine (`pm-intake-session.ts` port)
- Per-issue Discord thread mapping (issue # → thread ID)
- GitHub state machine (six labels: `agent-inbox`, `agent-ready`, `agent-running`, `needs-human`, `agent-review`, `agent-done`)
- Obsidian writer (mirror to `Luna Master/Neural Bridge/Kanban/Issues/Issue <N>.md`)
- Slash command handlers that actually do work (currently stubs)
- launchd plist for production deploy (PR-J)
