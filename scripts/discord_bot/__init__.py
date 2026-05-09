"""Neural Bridge Discord bot daemon (#28 Phase C foundation).

Multi-bot daemon: one discord.Client per Neural Bridge agent. Senior-pm
owns the slash commands (/pm-task, /pm-summary, /squad-discuss, /triage,
/close); other agents are speakers that post when senior-pm hands work
off to them.

PR-H scope: foundation. Bots come online, slash commands register,
Andy-only auth gate enforced, claude -p wrapper with prompt-injection
sanitizer. Slash command handlers are stubs that return "Phase B work"
until PR-I lands the PM intake state machine and per-issue thread
mapping.

Entry point: scripts/discord_bot/main.py
"""
