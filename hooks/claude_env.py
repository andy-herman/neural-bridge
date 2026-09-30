"""Which account a spawned `claude -p` call bills to, decided in one place.

Standard library only, shared by the hooks, compile.py, lint.py and the
Discord daemon's claude_invoke (the same arrangement as wiki_recall).

Two routes exist on the Mac Mini:

- Proxy. Andy's Copilot subscription, reached through NB's model gateway
  (scripts/model_gateway.py, localhost:4142) in front of the local copilot-api
  (localhost:4141). The conversational fleet and, since 2026-09-25, the memory
  pipeline (flush, compile, lint) run here. Andy chose this route so the fleet
  never draws on Max.

  Until 2026-09-30 only 4.x ids worked on it: every Claude 5 request came back
  400, "This model does not support assistant message prefill". The cause was
  a `role: system` message Claude Code puts at the end of a Claude 5
  conversation, which copilot-api passes through and Copilot refuses. The
  gateway folds it into the system prompt, so Claude 5 ids work through it.
  When the gateway is down, calls go straight to copilot-api instead, where
  4.x still works and Claude 5 does not.

- Direct. Anthropic, on the Claude Code login (Max). The loop engineer uses it
  with dashed ids.

Both routes must start from an environment with NONE of the Anthropic routing
variables, because each one silently takes precedence:

- ANTHROPIC_BASE_URL sends the call elsewhere. A hook inherits it from
  whatever spawned the session: the proxy URL inside every agent turn, the
  desktop app's own URL inside a Claude desktop session.
- ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_TOKEN replace the login
  with a key. ~/.hermes/.env, which the Telegram bridges load since
  2026-08-16, holds a key with no credit, so a direct call spawned under it
  failed with "Credit balance is too low".

Until 2026-09-25 flush inherited the proxy URL from the agent turn it ran in
and called claude-sonnet-5 on it, so every flush of an agent turn failed and
the progress log and wiki never received a single entry.
"""
from __future__ import annotations

import os
import re
import socket
from collections.abc import Mapping
from urllib.parse import urlsplit

ROUTING_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_TOKEN",
)

DEFAULT_PROXY_BASE = "http://localhost:4141"
# NB's model gateway, in front of the proxy. See scripts/model_gateway.py.
DEFAULT_GATEWAY_BASE = "http://localhost:4142"
# copilot-api authenticates with its own cached GitHub token; this is only a
# placeholder so Claude Code has a key to send.
PROXY_PLACEHOLDER_KEY = "copilot-proxy"

# The memory pipeline's model on the proxy. copilot-api ids are dotted for 4.x.
PIPELINE_MODEL = "claude-opus-4.8"

# Agent and pipeline calls load no MCP server unless one is passed
# explicitly (the private grants, see scripts/discord_bot/private_tools.py).
# Without --strict-mcp-config every call inherited Andy's user-level servers:
# about 150 tool definitions per turn, paid tools (Kling) among them, and on
# Claude 5 enough prompt that a single file read failed "Prompt is too long".
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'


def direct_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of `env` that reaches Anthropic on the Claude Code login."""
    return {k: v for k, v in env.items() if k not in ROUTING_VARS}


def gateway_up(base: str | None = None, timeout: float = 0.25) -> bool:
    """Is the model gateway accepting connections? A TCP connect, nothing more."""
    parts = urlsplit(base or os.environ.get("NB_MODEL_GATEWAY_BASE") or DEFAULT_GATEWAY_BASE)
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def proxy_base(env: Mapping[str, str] | None = None) -> str:
    """Where a proxy-route call goes: an explicit NB_COPILOT_API_BASE, else the
    model gateway when it is up, else copilot-api directly."""
    env = env if env is not None else {}
    explicit = env.get("NB_COPILOT_API_BASE") or os.environ.get("NB_COPILOT_API_BASE")
    if explicit:
        return explicit
    gateway = env.get("NB_MODEL_GATEWAY_BASE") or os.environ.get("NB_MODEL_GATEWAY_BASE") or DEFAULT_GATEWAY_BASE
    return gateway if gateway_up(gateway) else DEFAULT_PROXY_BASE


def proxy_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of `env` that reaches the proxy route and nothing else.

    NB_COPILOT_API_BASE, read from `env` or the process environment, pins the
    base; otherwise it is the model gateway, or copilot-api when the gateway
    is down (proxy_base). Nothing inherited can move the call off the route.
    """
    base = proxy_base(env)
    out = direct_env(env)
    out["ANTHROPIC_BASE_URL"] = base
    out["ANTHROPIC_API_KEY"] = PROXY_PLACEHOLDER_KEY
    return out


_CLAUDE_5_RE = re.compile(r"^claude-(opus|sonnet|haiku)-5\b")


def proxy_supports(model: str) -> bool:
    """True when `model` works even on the fallback path, copilot-api with no
    gateway in front. Claude 5 ids work only through the gateway, so a default
    that must keep working while the gateway is down stays on 4.x."""
    return not _CLAUDE_5_RE.match(model)


def mcp_args(mcp_config: str | None = None) -> list[str]:
    """`claude -p` arguments that load exactly `mcp_config` (a path or JSON),
    or no MCP server at all."""
    return ["--strict-mcp-config", "--mcp-config", mcp_config or EMPTY_MCP_CONFIG]


# Warnings Claude Code prints on every proxy-route call. True, and never the
# reason a call failed: the fleet uses no claude.ai connectors, and the proxy's
# dotted model ids (claude-opus-4.8) read to Claude Code as the retired
# Claude Opus 4. On 2026-09-30 the second one, cut to "Claude Op", was all the
# daemon showed of a failure whose cause was an API error on stdout.
BENIGN_STDERR = (
    "claude.ai connectors are disabled",
    "was retired on",
)

ERROR_SNIPPET_LIMIT = 300


def error_snippet(stdout: str | None, stderr: str | None, *, limit: int = ERROR_SNIPPET_LIMIT) -> str:
    """One line naming why a `claude -p` call failed, for logs and Discord.

    Claude Code prints the API's own error on stdout ("API Error: 400 ...",
    or "Failed to authenticate. API Error: 403 forbidden" when the proxy's
    upstream token has gone stale); stderr carries CLI warnings. Preference
    order: the line carrying the API error, then
    stderr with the benign proxy-route warnings removed, then raw stderr,
    then the tail of stdout, then a marker. The 2026-09-25 flush outage and
    the 2026-09-30 Luna failure were both invisible for want of this.
    """
    out_lines = [l.strip() for l in (stdout or "").splitlines() if l.strip()]
    err_lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    api_error = next((l for l in out_lines if "API Error" in l), "")
    if api_error:
        text = api_error
    else:
        real = [l for l in err_lines if not any(b in l for b in BENIGN_STDERR)]
        text = " | ".join(real) or " | ".join(err_lines) or (out_lines[-1] if out_lines else "(no output)")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
