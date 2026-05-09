"""Per-channel turn budget for bot-to-bot handoff chains (PR-P-3).

When one agent's response @-mentions another agent, Discord automatically
fires the second bot's on_message handler. Without a budget that loop
could run forever (or until rate-limited). This module tracks how many
back-to-back bot turns have happened in each channel/thread; an
authorized user message resets the counter.

The check is per-channel, not per-conversation, because Discord doesn't
expose a clean "conversation" abstraction; the channel is the surface.
For PM-intake threads or per-issue threads, each thread has its own
counter — they don't share with the parent channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_MAX_TURNS = 5


@dataclass
class HandoffBudget:
    max_turns: int = DEFAULT_MAX_TURNS
    _counts: dict[str, int] = field(default_factory=dict)

    def reset(self, channel_id: str) -> None:
        self._counts[str(channel_id)] = 0

    def consume(self, channel_id: str) -> bool:
        """Try to consume one turn. Returns True if allowed, False if exhausted.

        Increments only on success. Callers that aren't sure if they should
        consume yet should call `remaining()` first.
        """
        key = str(channel_id)
        current = self._counts.get(key, 0)
        if current >= self.max_turns:
            return False
        self._counts[key] = current + 1
        return True

    def remaining(self, channel_id: str) -> int:
        return max(0, self.max_turns - self._counts.get(str(channel_id), 0))

    def reset_all(self) -> None:
        """For tests."""
        self._counts.clear()


# Module-level singleton.
BUDGET = HandoffBudget()
