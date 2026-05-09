"""Slash command handlers for senior-pm.

PR-H scope: handlers are stubs that confirm receipt and announce that the
real PM intake / triage / close logic ships in PR-I. The handlers DO enforce
the auth gate and DO log every invocation to stderr for observability.
"""

from __future__ import annotations

import sys

import discord

from .auth import REFUSAL_MESSAGE, is_authorized
from .config import BotConfig


def log(msg: str) -> None:
    print(f"[discord_bot] {msg}", file=sys.stderr, flush=True)


async def _gate(interaction: discord.Interaction, config: BotConfig, command: str) -> bool:
    """Auth gate. Returns True if the caller is authorized; replies with refusal otherwise."""
    user_id = str(interaction.user.id)
    if not is_authorized(user_id, config):
        log(f"REFUSED: /{command} from user_id={user_id} (not in authorized list)")
        await interaction.response.send_message(REFUSAL_MESSAGE, ephemeral=True)
        return False
    log(f"ACCEPTED: /{command} from user_id={user_id}")
    return True


async def handle_pm_task(interaction: discord.Interaction, config: BotConfig, request: str) -> None:
    if not await _gate(interaction, config, "pm-task"):
        return
    await interaction.response.send_message(
        f"Received pm-task draft: `{request[:200]}`\n\n"
        f"_PM intake state machine ships in PR-I. For now this is just a receipt — "
        f"no GitHub issue created, no clarification thread opened._",
        ephemeral=True,
    )


async def handle_pm_summary(interaction: discord.Interaction, config: BotConfig) -> None:
    if not await _gate(interaction, config, "pm-summary"):
        return
    await interaction.response.send_message(
        "_Executive summary ships in PR-I once `gh` integration lands. "
        "PR-H is foundation only._",
        ephemeral=True,
    )


async def handle_squad_discuss(interaction: discord.Interaction, config: BotConfig, topic: str) -> None:
    if not await _gate(interaction, config, "squad-discuss"):
        return
    await interaction.response.send_message(
        f"Received squad-discuss topic: `{topic[:200]}`\n\n"
        f"_Multi-agent huddle ships in a later PR. PR-H foundation only._",
        ephemeral=True,
    )


async def handle_triage(interaction: discord.Interaction, config: BotConfig, issue_number: int) -> None:
    if not await _gate(interaction, config, "triage"):
        return
    await interaction.response.send_message(
        f"Received triage request for issue #{issue_number}.\n\n"
        f"_senior-pm triage routing ships in PR-I once GitHub integration + per-issue threading land._",
        ephemeral=True,
    )


async def handle_close(interaction: discord.Interaction, config: BotConfig, issue_number: int) -> None:
    if not await _gate(interaction, config, "close"):
        return
    await interaction.response.send_message(
        f"Received close request for issue #{issue_number}.\n\n"
        f"_Per-thread authorization-to-close ships in PR-I. PR-H foundation only — no GitHub action taken._",
        ephemeral=True,
    )
