"""Discord outbound push — Phase B of #28.

Resolves a webhook URL from macOS keychain (or NB_DISCORD_WEBHOOK env
var fallback) and POSTs a message via stdlib urllib. Safe-fails: if no
webhook is configured or the post errors, returns False without raising
so the caller (flush.py / compile.py) is never blocked on Discord.

Phase C will replace this transport with bot-based posting from a
launchd-managed `discord.py` daemon. Callers should depend on `send()`,
not on the webhook implementation.

Keychain setup (one-time, by the user):

  security add-generic-password \\
    -s "neural-bridge-discord-webhook" \\
    -a "$USER" \\
    -w "https://discord.com/api/webhooks/<id>/<token>"

Verify:

  security find-generic-password -s "neural-bridge-discord-webhook" -a "$USER" -w
"""

from __future__ import annotations

import json
import os
import subprocess
from urllib import error, request

KEYCHAIN_SERVICE = "neural-bridge-discord-webhook"
ENV_VAR = "NB_DISCORD_WEBHOOK"
SUPPRESS_ENV_VAR = "NB_NO_DISCORD"  # set to "1" to silently skip all outbound posts
DEFAULT_TIMEOUT = 5
DISCORD_MAX_CONTENT = 2000  # hard limit per Discord webhook spec
SAFE_CONTENT_BUDGET = 1900   # leave room for our own framing


def get_webhook_url() -> str | None:
    """Resolve the webhook URL from keychain or env. Return None if missing."""
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return env

    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return None

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def truncate_for_discord(text: str, suffix: str = "") -> str:
    """Truncate text to Discord's 2000-char limit. Append suffix if it fits."""
    if not suffix:
        budget = DISCORD_MAX_CONTENT
    else:
        budget = DISCORD_MAX_CONTENT - len(suffix) - 1
    if len(text) <= budget:
        return text + (suffix if suffix and len(text) <= budget else "")
    return text[: budget - 1] + "…" + (suffix if suffix else "")


def send(content: str, *, webhook_url: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> bool:
    """POST a message to the Discord webhook. Return True on 2xx, False otherwise.

    Never raises. Returns False when:
      - NB_NO_DISCORD=1 in the environment (suppress flag, used by bot-spawned
        claude -p subprocesses to avoid double-posting their own activity)
      - No webhook URL configured (keychain empty, env var unset)
      - HTTP request fails
    """
    if os.environ.get(SUPPRESS_ENV_VAR) == "1":
        return False
    url = webhook_url if webhook_url is not None else get_webhook_url()
    if not url:
        return False

    payload = {"content": content[:DISCORD_MAX_CONTENT]}
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "neural-bridge/1.0"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except error.HTTPError as exc:
        # 204 No Content is normal for Discord webhooks but raises through HTTPError on some Pythons.
        if 200 <= exc.code < 300:
            return True
        return False
    except (error.URLError, TimeoutError, OSError):
        return False
