"""Load and validate scripts/discord_bot/agents.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "agents.json"


@dataclass
class AgentConfig:
    id: str
    client_id: str
    token_keychain_service: str
    is_orchestrator: bool
    display_name: str


@dataclass
class BotConfig:
    authorized_user_ids: list[str]
    guild_id: str
    agents: list[AgentConfig]

    def orchestrator(self) -> AgentConfig:
        for a in self.agents:
            if a.is_orchestrator:
                return a
        raise ValueError("No orchestrator agent configured (is_orchestrator: true required on exactly one)")

    def by_id(self, agent_id: str) -> AgentConfig | None:
        for a in self.agents:
            if a.id == agent_id:
                return a
        return None


def load_config(path: Path = CONFIG_PATH) -> BotConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))

    authorized = raw.get("authorized_user_ids", [])
    if not isinstance(authorized, list) or not authorized:
        raise ValueError("authorized_user_ids must be a non-empty list")
    for uid in authorized:
        if not isinstance(uid, str) or not uid or uid.startswith("TODO"):
            raise ValueError(f"invalid authorized_user_id: {uid!r} (set this before running the daemon)")

    guild_id = raw.get("guild_id", "")
    if not guild_id or guild_id.startswith("TODO"):
        raise ValueError("guild_id is unset (set this before running the daemon)")

    agents_raw = raw.get("agents", [])
    if not isinstance(agents_raw, list) or not agents_raw:
        raise ValueError("agents must be a non-empty list")

    agents: list[AgentConfig] = []
    orchestrator_count = 0
    for entry in agents_raw:
        agent = AgentConfig(
            id=entry["id"],
            client_id=entry["client_id"],
            token_keychain_service=entry["token_keychain_service"],
            is_orchestrator=bool(entry.get("is_orchestrator", False)),
            display_name=entry.get("display_name", entry["id"]),
        )
        if agent.is_orchestrator:
            orchestrator_count += 1
        agents.append(agent)

    if orchestrator_count != 1:
        raise ValueError(f"exactly one agent must have is_orchestrator: true (found {orchestrator_count})")

    return BotConfig(authorized_user_ids=authorized, guild_id=guild_id, agents=agents)
