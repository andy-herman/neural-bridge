"""Load KEY=value files into os.environ. Standard library only.

WHY THIS EXISTS

Nothing in this repo read a .env file. Every environment variable the daemons
need was set inline inside its launchd plist, which works for the scheduled run
and fails for every other one. Running `python -m scripts.telegram_bot.luna_checkin`
by hand exited with "no LUNA_TELEGRAM_ALLOWED_USERS configured" while the 06:30
job using the identical code path sent fine, because the config lived in the
scheduler rather than with the program. Config that only exists in one launcher
is config you cannot test.

PRECEDENCE

Anything already in os.environ wins. A plist that sets a variable explicitly
keeps overriding the file, so adding a value here cannot change what a running
scheduled job does. Pass override=True only when you mean to stomp the process
environment, which is almost never.

SECURITY

These files hold tokens. Values are never logged, never returned, and never put
in an exception message. The loader reports which KEYS it set and nothing more.
"""

from __future__ import annotations

import os
from pathlib import Path

# Searched in order. Later files do not override earlier ones, and neither
# overrides the real environment. ~/.hermes/.env is the shared secret store
# Andy already keeps for the Hermes side; the repo-local file is optional and
# gitignored, for per-checkout overrides.
DEFAULT_ENV_PATHS: tuple[Path, ...] = (
    Path.home() / ".hermes" / ".env",
    Path(__file__).resolve().parent.parent / ".env",
)


def parse_env_text(text: str) -> dict[str, str]:
    """Parse .env-style text into a dict. Pure, so it is testable without a file.

    Handles `KEY=value`, a leading `export `, surrounding single or double
    quotes, blank lines and `#` comments. Malformed lines are skipped rather
    than raising: a stray line in a 400-line secrets file should not take down
    a daemon.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or not (key[0].isalpha() or key[0] == "_"):
            continue
        if not all(c.isalnum() or c == "_" for c in key):
            continue
        value = value.strip()
        # Quoted values are taken literally, including any '#' inside them.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Unquoted: a ' #' starts a trailing comment. A '#' with no leading
            # space is treated as part of the value, since that shows up in
            # URLs and generated tokens.
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()
        out[key] = value
    return out


def load_env_file(path: str | Path, *, override: bool = False) -> list[str]:
    """Load one file into os.environ. Returns the KEY names actually set.

    Missing or unreadable files are not an error; they return []. The caller
    is usually a daemon starting up, and a missing optional override file must
    not be fatal.
    """
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    applied: list[str] = []
    for key, value in parse_env_text(text).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


def load_default_env(*, override: bool = False) -> list[str]:
    """Load DEFAULT_ENV_PATHS in order. Returns every KEY name set, in order.

    Call this once at the top of a `main()`. Safe to call more than once.
    """
    applied: list[str] = []
    for path in DEFAULT_ENV_PATHS:
        applied.extend(load_env_file(path, override=override))
    return applied
