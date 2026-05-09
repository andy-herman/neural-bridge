"""macOS keychain reader for Neural Bridge bot tokens.

Wraps `security find-generic-password`. Never prints tokens to stdout/stderr.
"""

from __future__ import annotations

import os
import subprocess

DEFAULT_TIMEOUT = 5


def get_token(service: str) -> str | None:
    """Return the token stored under `service` in the user's login keychain.

    Returns None if the service is missing, the keychain is locked, or the
    `security` CLI is not on PATH. Never raises.
    """
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return None

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", user, "-w"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None
