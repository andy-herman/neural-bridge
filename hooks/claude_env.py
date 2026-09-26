"""Which account a spawned `claude -p` call bills to, decided in one place.

Standard library only, shared by the hooks, compile.py, lint.py and the
Discord daemon's claude_invoke (the same arrangement as wiki_recall).

Two routes exist on the Mac Mini:

- Proxy. The local copilot-api at localhost:4141, on Andy's Copilot
  subscription. The conversational fleet and, since 2026-09-25, the memory
  pipeline (flush, compile, lint) run here, on PIPELINE_MODEL. Only 4.x ids
  work on this route through Claude Code: every Claude 5 request comes back
  400, "This model does not support assistant message prefill" (verified
  2026-09-25 for claude-sonnet-5 and claude-opus-5; copilot-api's log held 456
  of them). Andy chose this route for the pipeline so it never draws on Max.

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
from collections.abc import Mapping

ROUTING_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_TOKEN",
)

DEFAULT_PROXY_BASE = "http://localhost:4141"
# copilot-api authenticates with its own cached GitHub token; this is only a
# placeholder so Claude Code has a key to send.
PROXY_PLACEHOLDER_KEY = "copilot-proxy"

# The memory pipeline's model on the proxy. copilot-api ids are dotted for 4.x.
PIPELINE_MODEL = "claude-opus-4.8"

_CLAUDE_5_RE = re.compile(r"^claude-(opus|sonnet|haiku)-5\b")


def direct_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of `env` that reaches Anthropic on the Claude Code login."""
    return {k: v for k, v in env.items() if k not in ROUTING_VARS}


def proxy_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of `env` that reaches the copilot-api proxy and nothing else.

    NB_COPILOT_API_BASE, read from `env` or the process environment, moves the
    proxy; nothing inherited can move the call off it.
    """
    base = env.get("NB_COPILOT_API_BASE") or os.environ.get("NB_COPILOT_API_BASE") or DEFAULT_PROXY_BASE
    out = direct_env(env)
    out["ANTHROPIC_BASE_URL"] = base
    out["ANTHROPIC_API_KEY"] = PROXY_PLACEHOLDER_KEY
    return out


def proxy_supports(model: str) -> bool:
    """False for model ids known to fail on the proxy through Claude Code."""
    return not _CLAUDE_5_RE.match(model)
