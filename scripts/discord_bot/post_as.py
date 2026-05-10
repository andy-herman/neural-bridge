"""Post a message to Discord AS one of the Neural Bridge agents.

Uses the agent's bot token (from macOS keychain) and Discord's REST API
directly — no discord.py Client spin-up, no WebSocket handshake. The
message appears in Discord with the agent's actual identity: their
avatar, their display name, their bot tag.

This is the architecturally-clean replacement for the webhook-with-
username-override hack used during the Echo build phase pings. The
webhook approach worked but posted from a webhook entity, not the bot
itself. This module posts AS the bot, which means avatar, presence,
and any future features that key on bot-identity (reactions, threads,
etc.) work correctly.

Token lookup: macOS keychain service `neural-bridge-discord-bot-<agent_id>`.
Same secret store the daemon uses. No new key infrastructure.

Usage (CLI):

    python -m scripts.discord_bot.post_as \\
        --agent luna \\
        --channel-id 1502587655680954458 \\
        --content "Build phase complete."

    # Read content from stdin (for piping long messages):
    echo "Long message body..." | python -m scripts.discord_bot.post_as \\
        --agent luna --channel-id <id>

    # Use a friendly channel name from agents.json:
    python -m scripts.discord_bot.post_as --agent luna --channel neural-bridge \\
        --content "Daemon restart complete."

Usage (programmatic):

    from scripts.discord_bot.post_as import post_as
    ok, err = post_as(agent_id="luna", channel_id=1502587655680954458,
                      content="hi from Luna")
    if not ok:
        print(f"failed: {err}")

Discord channel-message API:
  POST https://discord.com/api/v10/channels/{channel_id}/messages
  Authorization: Bot <token>
  Body: {"content": "<message>"}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_MAX_CONTENT = 2000
KEYCHAIN_SERVICE_PREFIX = "neural-bridge-discord-bot-"

# Optional friendly-name channel mapping. Edit/extend as new channels surface.
# Looking up via daemon log:
#   1502587655680954458 — #neural-bridge (where mentions land in default channel)
# Other channels seen in logs (uncomment + label as you confirm them):
#   1502587536004874465 — ???
#   1503060407894544424 — ???
#   1503073281895305378 — ???
CHANNEL_NAME_MAP: dict[str, int] = {
    "neural-bridge": 1502587655680954458,
}


def get_bot_token(agent_id: str) -> str | None:
    """Pull bot token from keychain for the given agent. Returns None if missing."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return None
    service = f"{KEYCHAIN_SERVICE_PREFIX}{agent_id}"
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", user, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def post_as(
    *,
    agent_id: str,
    channel_id: int,
    content: str,
    timeout: int = 10,
) -> tuple[bool, str | None]:
    """Post `content` to Discord channel `channel_id` as the bot for `agent_id`.

    Returns (ok, error). Never raises.
    """
    if not content or not content.strip():
        return False, "empty_content"
    if len(content) > DISCORD_MAX_CONTENT:
        return False, f"content_exceeds_2000_chars ({len(content)})"

    token = get_bot_token(agent_id)
    if not token:
        return False, f"keychain_token_missing:{KEYCHAIN_SERVICE_PREFIX}{agent_id}"

    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": f"neural-bridge-post-as/1.0 (agent={agent_id})",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, None
            return False, f"http_{resp.status}"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return False, f"http_{exc.code}:{body}"
    except urllib.error.URLError as exc:
        return False, f"network:{exc.reason}"
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"


def resolve_channel(channel_id: int | None, channel_name: str | None) -> tuple[int | None, str | None]:
    """Resolve a channel reference into a numeric ID.

    Either channel_id is provided (used as-is) or channel_name is looked up
    in CHANNEL_NAME_MAP. Returns (id, error).
    """
    if channel_id is not None:
        return channel_id, None
    if channel_name:
        cid = CHANNEL_NAME_MAP.get(channel_name)
        if cid is None:
            return None, f"unknown_channel_name:{channel_name} (known: {sorted(CHANNEL_NAME_MAP.keys())})"
        return cid, None
    return None, "must provide --channel-id or --channel"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post a Discord message AS one of the Neural Bridge agents.",
    )
    parser.add_argument("--agent", required=True,
                        help="Agent id whose bot token to use (e.g., luna, senior-pm).")
    parser.add_argument("--channel-id", type=int, default=None,
                        help="Discord channel ID. Numeric snowflake.")
    parser.add_argument("--channel", default=None,
                        help="Friendly channel name (from CHANNEL_NAME_MAP). "
                             "Mutually exclusive with --channel-id.")
    parser.add_argument("--content", default=None,
                        help="Message content. If omitted, read from stdin.")
    args = parser.parse_args(argv)

    channel_id, err = resolve_channel(args.channel_id, args.channel)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2

    if args.content is None:
        content = sys.stdin.read()
    else:
        content = args.content

    ok, post_err = post_as(
        agent_id=args.agent,
        channel_id=channel_id,
        content=content,
    )
    if ok:
        print(f"✓ posted as {args.agent} to channel {channel_id} (len={len(content)})", file=sys.stderr)
        return 0
    else:
        print(f"✗ post failed: {post_err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
