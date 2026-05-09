"""Process-wide registry of agent_id → discord.Client.

Populated by main.py as each AgentClient is constructed. Used by
handlers.py to post messages as a specific specialist bot — e.g., when
senior-pm hands off a triage to `research`, we want the actual research
bot to post in the thread, not senior-pm.

Doesn't import discord at module top so the test suite runs on a system
Python without the venv. Type-hints the client opaquely.
"""

from __future__ import annotations

from typing import Any, Optional


class ClientRegistry:
    """Singleton-style registry. main.py mutates it during startup."""

    def __init__(self) -> None:
        self._by_id: dict[str, Any] = {}

    def register(self, agent_id: str, client: Any) -> None:
        self._by_id[agent_id] = client

    def get(self, agent_id: str) -> Optional[Any]:
        return self._by_id.get(agent_id)

    def all_ids(self) -> list[str]:
        return sorted(self._by_id.keys())

    def __len__(self) -> int:
        return len(self._by_id)

    def reset(self) -> None:
        """For tests — clear the registry."""
        self._by_id.clear()


# Module-level singleton.
REGISTRY = ClientRegistry()


async def post_as_agent(
    agent_id: str,
    *,
    thread_id: int,
    content: str,
) -> tuple[bool, str | None]:
    """Post a message in `thread_id` as the bot identity for `agent_id`.

    Returns (ok, error). ok=False with error="agent_not_registered" if the
    bot for that role isn't in the registry (token missing, bot offline,
    etc.). All failures are non-fatal to the caller.
    """
    import discord  # local import — keeps module test-importable on system Python

    client = REGISTRY.get(agent_id)
    if client is None:
        return False, "agent_not_registered"

    channel = client.get_channel(thread_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden) as exc:
            return False, f"channel_{type(exc).__name__}"
        except Exception as exc:  # network glitch etc.
            return False, f"channel_fetch_{type(exc).__name__}"

    if not isinstance(channel, discord.Thread):
        return False, f"not_a_thread:{type(channel).__name__}"

    try:
        await channel.send(content)
    except discord.Forbidden:
        return False, "forbidden"
    except Exception as exc:
        return False, f"send_{type(exc).__name__}"
    return True, None
