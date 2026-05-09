# scripts/launchd/

`launchd` user agent for the Neural Bridge Discord bot daemon.

The daemon launches at user login (Mac Mini boot for the deploy target), restarts on crash with a 30s throttle, logs to `~/Library/Logs/neural-bridge/`.

## What's here

- `com.andyherman.neural-bridge.discord-bot.plist` — the launchd plist
- `install.sh` — copies the plist into `~/Library/LaunchAgents/` and bootstraps it
- `uninstall.sh` — bootouts and removes the plist

## Pre-flight

Same as `scripts/discord_bot/README.md` pre-flight, plus:

- All 9 keychain bot tokens stored
- `agents.json` has real `authorized_user_ids`, `guild_id`, `default_repo`
- Senior-pm has Message Content Intent enabled in the Developer Portal
- venv exists at `~/Development/neural-bridge/.venv/` with `discord.py` installed
- Smoke-tested manually first (`.venv/bin/python -m scripts.discord_bot.main`) and confirmed all 9 bots come online

## Install

```bash
cd ~/Development/neural-bridge
chmod +x scripts/launchd/install.sh scripts/launchd/uninstall.sh
./scripts/launchd/install.sh
```

The install script:

1. Verifies `~/Development/neural-bridge/.venv/bin/python` exists (refuses to install otherwise — venv first)
2. Creates `~/Library/Logs/neural-bridge/` if missing
3. Copies the plist to `~/Library/LaunchAgents/`
4. If a previous version is loaded, `bootouts` it first
5. `bootstraps` the agent into the current GUI session
6. Verifies the agent loaded

## Verify

Tail the logs:

```bash
tail -f ~/Library/Logs/neural-bridge/discord-bot.stderr.log
```

Expected within a few seconds of install:

```
[discord_bot] loaded config: 9 agents, guild=..., authorized_users=1, default_repo=andy-herman/neural-bridge
[discord_bot] starting 9 agents...
[discord_bot] online: senior-pm (Senior PM) as ...
[discord_bot] online: research (Research) as ...
... (7 more)
[discord_bot] slash commands synced to guild ...
```

Then in Discord, `/pm-task request: launchd smoke test` should produce a clarification thread.

## Status

```bash
launchctl print "gui/$(id -u)/com.andyherman.neural-bridge.discord-bot" | head -20
```

Look for `state = running`. If it shows `state = exited` with a non-zero `last exit code`, check the stderr log.

## Uninstall

```bash
./scripts/launchd/uninstall.sh
```

This `bootouts` the agent and removes the plist from `~/Library/LaunchAgents/`. The logs at `~/Library/Logs/neural-bridge/` are kept; remove manually if you want.

## Manual control without uninstalling

Stop temporarily:

```bash
launchctl bootout "gui/$(id -u)/com.andyherman.neural-bridge.discord-bot"
```

Restart:

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.andyherman.neural-bridge.discord-bot.plist
```

## Plist hardcodes

The plist hardcodes `/Users/andyherman/...` because `~` does not expand inside plist values. If forking this repo to a different macOS user:

1. Find/replace `andyherman` with your username in the plist
2. Re-run `install.sh`

Environment variables set inside the plist:

- `USER=andyherman` — needed by `keychain.py`
- `HOME=/Users/andyherman` — for tilde expansion in `thread_map.py`
- `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` — so `gh` and `claude` resolve
- `PYTHONUNBUFFERED=1` — log lines flush immediately

## Troubleshooting

- **Logs file empty for 30+ seconds:** the daemon may be erroring before it gets to the first log line. `launchctl print` to confirm.
- **`exited with status 1` on every restart:** check stderr log for the Python traceback. Usually a missing keychain entry or `agents.json` validation error.
- **`PrivilegedIntentsRequired` error:** Senior-pm doesn't have Message Content Intent enabled. Developer Portal → Senior PM application → Bot tab → Privileged Gateway Intents → Message Content Intent → on.
- **Bots all show offline:** check that you actually invited them to the Neural Bridge server. The keychain has tokens but if the bots aren't in the server they have nowhere to be online.
