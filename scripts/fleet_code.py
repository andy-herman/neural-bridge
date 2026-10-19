"""The fleet code: FLEET_CODE.md, the shared rules above every agent charter.

One loader for every surface that builds an agent prompt, so all of them carry
the same text:

- the mention path (discord_bot.mention.build_mention_prompt), which serves
  every Discord turn and the Luna, Loid and council Telegram bridges;
- Luna's proactive check-ins (telegram_bot.luna_checkin);
- the loop engineer (loop_engineer.executor).

The text goes into prompts as trusted instructions. The charter, by contrast,
passes through sanitize_untrusted_text in build_mention_prompt.

Missing or malformed raises FleetCodeError instead of returning an empty
string. An agent that silently loses its hard limits is worse than one that
fails to answer. Read on every call, so an edit takes effect on the next turn
without a restart.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET_CODE_PATH = REPO_ROOT / "FLEET_CODE.md"

# The agent-facing text starts with this heading, after the human header and
# its `---` line. Tests on each surface look for it in the assembled prompt.
TITLE = "# Fleet code"


class FleetCodeError(RuntimeError):
    """FLEET_CODE.md is missing, unreadable, or not in the expected shape."""


def load_fleet_code(path: Path = FLEET_CODE_PATH) -> str:
    """Return the agent-facing text: everything after the first `---` line."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FleetCodeError(f"fleet code unreadable at {path}: {exc}") from exc
    _, sep, body = text.partition("\n---\n")
    body = body.strip()
    if not sep or not body.startswith(TITLE):
        raise FleetCodeError(
            f"fleet code at {path} needs a `---` line followed by `{TITLE}`"
        )
    return body
