"""Andy-only authorization gate.

Every slash command handler MUST call `is_authorized(interaction.user.id, config)`
before doing any work. Anyone other than the configured user IDs gets a polite
refusal; the action does not run.

The Discord conversation in the resulting thread later counts as the
explicit-authorization-in-current-request that senior-pm.md requires for actions
like closing GitHub issues. The auth gate here is the first checkpoint; the
per-thread conversation is the second.
"""

from __future__ import annotations

from .config import BotConfig


def is_authorized(user_id: int | str, config: BotConfig) -> bool:
    """Return True iff `user_id` is in the authorized_user_ids list."""
    return str(user_id) in config.authorized_user_ids


REFUSAL_MESSAGE = (
    "This Neural Bridge bot only responds to its configured operator. "
    "Your action was not executed."
)
