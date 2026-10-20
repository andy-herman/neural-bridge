"""Which account a spawned `claude -p` call bills to, decided in one place.

Standard library only, shared by the hooks, compile.py, lint.py and the
Discord daemon's claude_invoke (the same arrangement as wiki_recall).

Three routes exist on the Mac Mini:

- API (the fleet's route since 2026-10-16). The Anthropic API on Andy's own
  API key, read from a private key file outside the repo (api_key). GitHub
  began refusing the proxy's Copilot token exchange on 2026-10-16, after
  which Copilot rejected every Claude 5 request, so the fleet moved here.
  fleet_env picks this route whenever the key file exists; set
  NB_MODEL_ROUTE=proxy to go back to the proxy.

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
  2026-08-16, held a key with no credit until 2026-10-16, so a direct call
  spawned under it failed with "Credit balance is too low". The API route
  sets its key itself and never relies on an inherited one.

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

# The memory pipeline's model. Model ids in this repo are written in the API's
# dashed form; route_model() converts 4.x ids to copilot-api's dotted form
# when a call goes to the proxy.
PIPELINE_MODEL = "claude-opus-4-8"

# The API route's key: NB_ANTHROPIC_API_KEY when set, else the first line of
# this file (mode 0600, never in the repo). NB_ANTHROPIC_KEY_FILE moves it.
DEFAULT_API_KEY_FILE = "~/.config/neural-bridge/anthropic-api-key"

ROUTE_API = "api"
ROUTE_PROXY = "proxy"

# Inherited from a Claude desktop or SDK session (flush runs inside those),
# CLAUDE_CODE_ENTRYPOINT makes a nested `claude -p` sign in through the host
# session and ignore ANTHROPIC_API_KEY: verified 2026-10-16 with an invalid
# key, which a clean environment rejects and the desktop environment did not.
# The API route drops it so the call bills the key.
HOST_AUTH_VARS = ("CLAUDE_CODE_ENTRYPOINT",)

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


def _setting(name: str, env: Mapping[str, str] | None) -> str:
    return ((env or {}).get(name) or os.environ.get(name) or "").strip()


def api_key(env: Mapping[str, str] | None = None) -> str | None:
    """The API route's key, or None when none is configured.

    NB_ANTHROPIC_API_KEY (from `env` or the process environment) wins;
    otherwise the first non-comment line of the key file. An inherited
    ANTHROPIC_API_KEY is deliberately ignored: it is whatever the parent
    process happened to load.
    """
    explicit = _setting("NB_ANTHROPIC_API_KEY", env)
    if explicit:
        return explicit
    path = os.path.expanduser(_setting("NB_ANTHROPIC_KEY_FILE", env) or DEFAULT_API_KEY_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return None


def route(env: Mapping[str, str] | None = None) -> str:
    """ROUTE_API or ROUTE_PROXY. NB_MODEL_ROUTE picks one explicitly;
    otherwise the API route whenever a key is configured."""
    choice = _setting("NB_MODEL_ROUTE", env).lower()
    if choice in (ROUTE_API, ROUTE_PROXY):
        return choice
    return ROUTE_API if api_key(env) else ROUTE_PROXY


def api_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of `env` that reaches the Anthropic API on the configured key.

    Raises RuntimeError when no key is configured, rather than letting Claude
    Code fall back to the Max login."""
    key = api_key(env)
    if not key:
        raise RuntimeError("no Anthropic API key: set NB_ANTHROPIC_API_KEY or create "
                           f"{DEFAULT_API_KEY_FILE}")
    out = {k: v for k, v in direct_env(env).items() if k not in HOST_AUTH_VARS}
    out["ANTHROPIC_API_KEY"] = key
    return out


def fleet_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment for a fleet or memory-pipeline `claude -p` call: the API
    route (api_env) or the proxy route (proxy_env), as route() decides."""
    return api_env(env) if route(env) == ROUTE_API else proxy_env(env)


_DOTTED_4X_RE = re.compile(r"^(claude-(?:opus|sonnet|haiku)-4)\.(\d+)$")
_DASHED_4X_RE = re.compile(r"^(claude-(?:opus|sonnet|haiku)-4)-(\d+)$")


def route_model(model: str, env: Mapping[str, str] | None = None) -> str:
    """`model` in the id form the chosen route accepts: dashed for the API
    (claude-opus-4-8), dotted for copilot-api (claude-opus-4.8). Claude 5 ids
    have no minor version and pass through unchanged."""
    if route(env) == ROUTE_API:
        return _DOTTED_4X_RE.sub(r"\1-\2", model)
    return _DASHED_4X_RE.sub(r"\1.\2", model)


_CLAUDE_5_RE = re.compile(r"^claude-(opus|sonnet|haiku)-5\b")


def proxy_supports(model: str) -> bool:
    """True when `model` works even when the route degrades. On the API route
    every model does. On the proxy route Claude 5 ids work only through the
    gateway (copilot-api alone rejects them), so a default that must keep
    working while the gateway is down stays on 4.x."""
    if route() == ROUTE_API:
        return True
    return not _CLAUDE_5_RE.match(model)


# For a one-shot `claude -p` whose prompt carries everything it needs: no
# built-in tools, no skills or slash commands. Without these, every call loads
# Claude Code's tool definitions and skill listing whether it uses them or not:
# a one-line probe on 2026-10-20 (Claude Code 2.1.304, Haiku 4.5) took 25,096
# input tokens as the fleet called it and 8,777 with these, the same answer.
# Never on an agent turn, or on a call whose prompt asks it to read files.
NO_TOOLS_ARGS = ("--tools", "", "--disable-slash-commands")


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
