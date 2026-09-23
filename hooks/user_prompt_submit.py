#!/usr/bin/env python3
"""UserPromptSubmit hook: inject related wiki concepts into the turn.

Fires on every prompt in a Claude Code session rooted at this repo. Reads the
hook payload from stdin, ranks knowledge/concepts/ against the prompt text
with hooks/wiki_recall.py, and prints a compact context block to stdout, which
Claude Code appends to the model's context for that turn. Exit code is always
0: this hook may add context, never block a prompt.

Skips silently, printing nothing, when:
  - NB_SKIP_WIKI_RECALL=1 is set. The Discord daemon sets it because it injects
    the same block itself with the clean user message (the rendered daemon
    prompt would match on template boilerplate). flush.py and compile.py set
    it because the filing gate and concept writer must see exactly the prompt
    they were designed against; injecting wiki context into the gate would
    bias the verdicts on the very corpus it is gating.
  - the prompt already carries the recall block marker.
  - the prompt is empty or looks like a slash command.

Payload fields used: `user_input` (current docs), with `prompt` as a fallback
for older harness versions, and `cwd`. The wiki that is searched is the one
under this repo, regardless of cwd.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

import wiki_recall  # noqa: E402
from schema import KNOWN_AGENTS, UNATTRIBUTED  # noqa: E402

MAX_QUERY_CHARS = 4000


def resolve_agent(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    for key in ("NB_AGENT_ID", "NB_AGENT"):
        val = env.get(key, "").strip().lower()
        if val in KNOWN_AGENTS:
            return val
    return UNATTRIBUTED


def extract_prompt(payload: dict) -> str:
    for key in ("user_input", "prompt"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def build_context(payload: dict, env: dict[str, str] | None = None) -> str:
    """Pure entry point used by main() and the tests. Returns "" to inject nothing."""
    env = os.environ if env is None else env
    if wiki_recall.should_skip(env):
        return ""
    prompt = extract_prompt(payload)
    if not prompt or prompt.lstrip().startswith("/"):
        return ""
    if wiki_recall.BLOCK_BEGIN in prompt:
        return ""
    block, _hits = wiki_recall.recall(prompt[:MAX_QUERY_CHARS], agent_id=resolve_agent(env))
    return block


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        block = build_context(payload)
    except Exception:
        return 0
    if block:
        sys.stdout.write(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
