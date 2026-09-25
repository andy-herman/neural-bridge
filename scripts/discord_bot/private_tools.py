"""Private MCP tools: granted only on the owner's machine, only on the owner's own mentions.

Some tools reach private material on the Mac and must never be reachable by anyone
else: not by readers of this public repo, not by agent-to-agent handoffs, not by
other processes that happen to run `claude`. So the grant does not live here.

It lives in a file outside the repo, ~/.config/neural-bridge/private-tools.json
(override with NB_PRIVATE_TOOLS):

    {
      "mcp_config": "~/.config/neural-bridge/private-mcp.json",
      "agents": {"research": ["mcp__<server>__<tool>"], ...}
    }

`mcp_config` is a standard Claude Code MCP config file (`{"mcpServers": {...}}`).
It is passed per call with --mcp-config, so the private server is loaded only into
sessions that received the grant, and the listed tools are appended to that call's
--allowedTools. Everything fails closed: no file, a malformed file, an unlisted
agent, a missing MCP config, a tool name that is not an MCP tool, or a turn not
started by an authorized human all mean no private tool anywhere.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

PRIVATE_TOOLS_FILE = Path(
    os.environ.get("NB_PRIVATE_TOOLS", "~/.config/neural-bridge/private-tools.json")
).expanduser()

_MCP_TOOL = re.compile(r"mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class PrivateGrant:
    mcp_config: str   # path passed to --mcp-config
    tools: str        # comma-separated, appended to --allowedTools


def grant_for(agent_id: str, owner_invoked: bool, path: Path | None = None) -> PrivateGrant | None:
    """Return the private grant for this turn, or None. Never raises."""
    if not owner_invoked:
        return None
    try:
        data = json.loads((path or PRIVATE_TOOLS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tools = (data.get("agents") or {}).get(agent_id) if isinstance(data.get("agents"), dict) else None
    if not isinstance(tools, list) or not tools:
        return None
    if not all(isinstance(t, str) and _MCP_TOOL.fullmatch(t) for t in tools):
        return None
    config = Path(str(data.get("mcp_config", ""))).expanduser()
    if not str(data.get("mcp_config", "")).strip() or not config.is_file():
        return None
    return PrivateGrant(mcp_config=str(config), tools=",".join(tools))


def merge_tools(base: str | None, extra: str) -> str:
    """Append private tools to an agent's --allowedTools value."""
    return ",".join(t for t in (base, extra) if t)
