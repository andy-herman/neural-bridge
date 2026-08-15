#!/usr/bin/env python3
"""guard_bash.py — PreToolUse hook: scope each agent's Bash to named commands.

Luna needs Bash to reach her calendar and inbox CLIs, because claude.ai MCP
connectors do not load in a headless `claude -p`. Granting her Bash grants her
the whole shell, which is a much larger permission than "read my calendar". This
hook closes that gap: Bash stays available, but only for the commands an agent
is actually supposed to run.

WHY A HOOK AND NOT `--allowedTools`

`--allowedTools` auto-approves a tool. It does not constrain what the tool is
asked to do, and it does not block anything. `Bash` on that list means any
command. A PreToolUse hook returning exit 2 is the only mechanical enforcement
point, and it is the same mechanism guard_concepts.py already uses here.

PER-AGENT, BECAUSE THE HOOK IS PROJECT-WIDE

Hooks in `.claude/settings.json` fire for every `claude -p` launched from this
repo. A single allowlist shaped for Luna would break Loid (synapse-journal) and
the loop engineer (which runs the test suite). So the daemon stamps
`NB_AGENT_ID` into the subprocess environment and this hook applies that agent's
list. An agent that is identified but has no entry gets no Bash at all.

THE FAIL-OPEN, STATED PLAINLY

When `NB_AGENT_ID` is unset the hook allows the command. That case is an
interactive human session in this repo (Andy, or Claude Code working with him),
where constraining the shell would be wrong and would break ordinary work. The
residual risk is real and worth naming: any automated path that forgets to stamp
NB_AGENT_ID silently gets unrestricted Bash. That is why the daemon sets it in
one place, `claude_invoke._subprocess_env`, rather than at each call site.

NO CHAINING

Shell metacharacters are rejected before matching. Without that,
`python -m scripts.luna.calendar today; rm -rf ~` would pass a prefix check.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Anything that could run a second command, redirect, or substitute. Rejected
# outright rather than parsed: a shell parser in a security hook is a liability.
_CHAINING = re.compile(r"[;&|`\n\r><]|\$\(|\$\{")

# Per-agent allowlists. Patterns match the FULL command after chain rejection.
# Optional leading path lets a venv interpreter through (.venv/bin/python).
_PY = r"(?:[\w./-]*/)?python3?(?:\.\d+)?"

AGENT_BASH_ALLOWLIST: dict[str, tuple[str, ...]] = {
    # Her executive-assistant CLIs, read-only by construction. Nothing else.
    "luna": (
        rf"^{_PY}\s+-m\s+scripts\.luna\.(?:calendar|inbox)(?:\s|$)",
    ),
    # The Synapse career database, his core function.
    "loid": (
        r"^(?:[\w./-]*/)?synapse-journal(?:\s|$)",
        rf"^{_PY}\s+-m\s+scripts\.loid\.synapse_journal(?:\s|$)",
    ),
    # Runs and verifies real code, so it needs the test runner and read-only
    # git. Write-side git (push, commit) is done by the daemon, not the agent.
    "loop-engineer": (
        rf"^{_PY}\s+-m\s+(?:unittest|pytest)\b",
        r"^(?:[\w./-]*/)?pytest\b",
        r"^git\s+(?:status|diff|log|show|rev-parse|branch|ls-files)\b",
    ),
}

# Written out rather than derived from the regexes: the first version rendered
# a pattern back into prose and told the agent its options were
# "python -m scripts.luna.(?:calendar|inbox)", which is not actionable.
AGENT_BASH_HELP: dict[str, str] = {
    "luna": "python -m scripts.luna.calendar <today|week|next|conflicts>; "
            "python -m scripts.luna.inbox <unread|search|thread|waiting>",
    "loid": "synapse-journal <args>",
    "loop-engineer": "python -m unittest ...; pytest ...; "
                     "git status|diff|log|show|rev-parse|branch|ls-files",
}


def decide(command: str, agent_id: str | None) -> tuple[bool, str]:
    """Return (allowed, reason). Pure, so the policy is testable."""
    if not agent_id:
        return True, "no NB_AGENT_ID: interactive session, not constrained"

    allowed_patterns = AGENT_BASH_ALLOWLIST.get(agent_id)
    if not allowed_patterns:
        return False, (
            f"agent '{agent_id}' has no Bash allowlist entry. Bash is granted "
            f"per agent in hooks/guard_bash.py; add an entry there if this agent "
            f"genuinely needs a shell command."
        )

    stripped = command.strip()
    if not stripped:
        return False, "empty command"

    if _CHAINING.search(stripped):
        return False, (
            "command chaining, redirection and substitution are not allowed "
            "(found one of ; & | ` newline < > $( ${ ). Run a single command."
        )

    for pattern in allowed_patterns:
        if re.match(pattern, stripped):
            return True, "matches allowlist"

    return False, (
        f"'{stripped[:80]}' is not on the Bash allowlist for '{agent_id}'. "
        f"Allowed: {AGENT_BASH_HELP.get(agent_id, '(none documented)')}"
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed payload: never block on our own bug

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not command:
        return 0

    agent_id = os.environ.get("NB_AGENT_ID") or None
    allowed, reason = decide(command, agent_id)
    if allowed:
        return 0

    print(f"BLOCKED by guard_bash: {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
