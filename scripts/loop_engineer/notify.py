"""Discord notifications for the loop, so Andy can watch it without ssh.

Resolves a dedicated `#engineering-loop` webhook first (keychain service
`neural-bridge-loop-webhook` or env `NB_LOOP_DISCORD_WEBHOOK`) and falls back to
the main NB webhook if that isn't configured. Mirrors hooks/discord_post.py:
stdlib urllib, never raises, honours the NB_NO_DISCORD suppress flag.

Message builders are pure so their formatting is unit-tested; `notify` is the
only side-effecting function.
"""

from __future__ import annotations

import json
import os
import subprocess
from urllib import error, request

LOOP_KEYCHAIN_SERVICE = "neural-bridge-loop-webhook"
MAIN_KEYCHAIN_SERVICE = "neural-bridge-discord-webhook"
LOOP_ENV_VAR = "NB_LOOP_DISCORD_WEBHOOK"
MAIN_ENV_VAR = "NB_DISCORD_WEBHOOK"
SUPPRESS_ENV_VAR = "NB_NO_DISCORD"
DISCORD_MAX_CONTENT = 2000
DEFAULT_TIMEOUT = 5


def _keychain(service: str) -> str | None:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", user, "-w"],
            capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get_webhook_url() -> str | None:
    """Prefer the loop channel; fall back to the main NB webhook."""
    for env_var, service in (
        (LOOP_ENV_VAR, LOOP_KEYCHAIN_SERVICE),
        (MAIN_ENV_VAR, MAIN_KEYCHAIN_SERVICE),
    ):
        env = os.environ.get(env_var, "").strip()
        if env:
            return env
        kc = _keychain(service)
        if kc:
            return kc
    return None


def notify(content: str, *, webhook_url: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> bool:
    """POST to Discord. Returns True on 2xx. Never raises."""
    if os.environ.get(SUPPRESS_ENV_VAR) == "1":
        return False
    url = webhook_url if webhook_url is not None else get_webhook_url()
    if not url:
        return False
    payload = {"content": content[:DISCORD_MAX_CONTENT]}
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "neural-bridge-loop/1.0"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except error.HTTPError as exc:
        return 200 <= exc.code < 300
    except (error.URLError, TimeoutError, OSError):
        return False


# ---------- Pure message builders ----------

_PREFIX = "🔧 **loop**"


def claimed_msg(number: int, title: str) -> str:
    return f"{_PREFIX} claimed #{number}: {title}"


def pr_opened_msg(number: int, pr_url: str, files: int, lines: int) -> str:
    return (
        f"{_PREFIX} ✅ PR opened for #{number} → {pr_url}\n"
        f"tests green · {files} file(s), {lines} line(s) changed · draft, awaiting your review"
    )


def blocked_msg(number: int, reason: str, detail: str = "") -> str:
    tail = f" — {detail}" if detail else ""
    return f"{_PREFIX} ⏸️ #{number} needs a human: {reason}{tail}"


def escalated_msg(number: int, reason: str, detail: str = "") -> str:
    tail = f"\n```\n{detail[:600]}\n```" if detail else ""
    return f"{_PREFIX} 🚨 #{number} escalated: {reason}{tail}"


def run_summary_msg(opened: int, blocked: int, escalated: int, considered: int) -> str:
    return (
        f"{_PREFIX} run complete — considered {considered}, "
        f"opened {opened} PR(s), blocked {blocked}, escalated {escalated}"
    )
