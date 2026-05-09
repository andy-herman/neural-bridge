"""Neural Bridge Discord bot daemon — multi-bot entry point.

Loads config + tokens, spawns one discord.Client per agent, registers slash
commands on the orchestrator (senior-pm) only, wires the on_message thread
listener for the intake state machine, and runs everything in one asyncio
event loop.

Usage:

    cd ~/Development/neural-bridge
    .venv/bin/python -m scripts.discord_bot.main

Or via launchd (PR-J).

Pre-req:
- macOS keychain has a `neural-bridge-discord-bot-<role>` token for every
  agent in `agents.json`
- `agents.json` has authorized_user_ids and guild_id filled in (no TODO_*)
- `discord.py>=2.3.0` installed (see requirements.txt)
- **Senior-pm** has Message Content Intent enabled in the Developer Portal
  (Application 1502038606905344162 → Bot tab → Privileged Gateway Intents).
  The other 8 bots do NOT need this — only the orchestrator reads
  thread messages for the PM intake flow.
"""

from __future__ import annotations

import asyncio
import sys

import discord
from discord import app_commands

from .client_registry import REGISTRY as CLIENT_REGISTRY
from .config import AgentConfig, BotConfig, load_config
from .handlers import (
    handle_close,
    handle_mention,
    handle_pm_summary,
    handle_pm_task,
    handle_squad_discuss,
    handle_triage,
    log,
    on_thread_message,
)
from .mention import is_mention_for_self
from .keychain import get_token


class AgentClient(discord.Client):
    """One discord.Client per Neural Bridge agent.

    The orchestrator client (senior-pm) registers slash commands AND listens
    to thread messages for PM intake. Speaker clients have no command tree
    and ignore messages — their job is to post when senior-pm hands off.
    """

    def __init__(self, agent: AgentConfig, config: BotConfig):
        intents = discord.Intents.default()
        # All agents now need message_content: senior-pm reads thread replies
        # for PM intake; specialists read mentions of themselves to respond
        # (PR-P-1). Andy must enable Message Content Intent on each of the 9
        # bots' Developer Portal pages (Application -> Bot -> Privileged
        # Gateway Intents -> Message Content Intent).
        intents.message_content = True
        super().__init__(intents=intents)
        self.agent = agent
        self.bot_config = config
        self.tree: app_commands.CommandTree | None = None
        if agent.is_orchestrator:
            self.tree = app_commands.CommandTree(self)
            self._register_commands()

    def _register_commands(self) -> None:
        assert self.tree is not None
        cfg = self.bot_config

        @self.tree.command(name="pm-task", description="Start a PM task draft (PM clarifies before creating an issue).")
        @app_commands.describe(request="Plain-English description of the task or need.")
        async def pm_task(interaction: discord.Interaction, request: str) -> None:
            await handle_pm_task(interaction, cfg, request)

        @self.tree.command(name="pm-summary", description="Get a senior-pm executive summary of tracked work.")
        async def pm_summary(interaction: discord.Interaction) -> None:
            await handle_pm_summary(interaction, cfg)

        @self.tree.command(name="squad-discuss", description="Open a multi-agent huddle on a topic. Use sparingly.")
        @app_commands.describe(topic="What the squad should discuss.")
        async def squad_discuss(interaction: discord.Interaction, topic: str) -> None:
            await handle_squad_discuss(interaction, cfg, topic)

        @self.tree.command(name="triage", description="Ask senior-pm to triage a specific GitHub issue.")
        @app_commands.describe(issue_number="The GitHub issue number.")
        async def triage(interaction: discord.Interaction, issue_number: int) -> None:
            await handle_triage(interaction, cfg, issue_number)

        @self.tree.command(name="close", description="Ask senior-pm to close a GitHub issue (requires per-thread auth).")
        @app_commands.describe(issue_number="The GitHub issue number.")
        async def close_cmd(interaction: discord.Interaction, issue_number: int) -> None:
            await handle_close(interaction, cfg, issue_number)

    async def setup_hook(self) -> None:
        if self.tree is not None:
            guild = discord.Object(id=int(self.bot_config.guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log(f"slash commands synced to guild {self.bot_config.guild_id}")

    async def on_ready(self) -> None:
        log(f"online: {self.agent.id} ({self.agent.display_name}) as {self.user}")

    async def on_message(self, message: discord.Message) -> None:
        # Don't respond to ourselves or to other bots (loop prevention).
        if message.author.bot:
            return

        # 1. Mention routing — every agent listens for its own @-mention.
        try:
            if is_mention_for_self(message.mentions, self.user):
                await handle_mention(self, message, self.bot_config)
                return
        except Exception as exc:
            log(f"on_message (mention) error: {type(exc).__name__}: {exc}")

        # 2. PM intake thread replies — only the orchestrator processes those.
        if not self.agent.is_orchestrator:
            return
        try:
            await on_thread_message(message, self.bot_config)
        except Exception as exc:
            log(f"on_message (intake) error: {type(exc).__name__}: {exc}")


def _resolve_token(agent: AgentConfig) -> str:
    token = get_token(agent.token_keychain_service)
    if not token:
        raise RuntimeError(
            f"missing keychain entry: {agent.token_keychain_service}. "
            f"Run: security add-generic-password -s '{agent.token_keychain_service}' -a \"$USER\" -w"
        )
    return token


async def run() -> None:
    config = load_config()
    log(f"loaded config: {len(config.agents)} agents, guild={config.guild_id}, "
        f"authorized_users={len(config.authorized_user_ids)}, default_repo={config.default_repo}")

    clients_and_tokens: list[tuple[AgentClient, str]] = []
    for agent in config.agents:
        try:
            token = _resolve_token(agent)
        except RuntimeError as exc:
            log(f"SKIP {agent.id}: {exc}")
            continue
        client = AgentClient(agent, config)
        CLIENT_REGISTRY.register(agent.id, client)
        clients_and_tokens.append((client, token))

    if not clients_and_tokens:
        log("ERROR: no agents have keychain tokens. Cannot start daemon.")
        return
    log(f"client registry: {len(CLIENT_REGISTRY)} agents registered")

    log(f"starting {len(clients_and_tokens)} agents...")
    await asyncio.gather(
        *[client.start(token) for client, token in clients_and_tokens],
        return_exceptions=False,
    )


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log("shutdown requested")
        return 0
    except Exception as exc:
        log(f"fatal: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
