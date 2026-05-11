"""Structured tool-use protocol for mentioned agents (PR-P-2.5).

Agents emit a single fenced ` ```actions ` block at the end of their
response containing a JSON array of action objects. The daemon extracts
the block, validates each action, executes it via the existing gh
wrappers, and reports back. The action block is stripped from the
visible reply.

Why this instead of giving agents Bash:
- Path/issue validation is possible (we control what gets executed)
- Audit trail: every action is logged with the agent that requested it
- No risk of `rm -rf` or arbitrary shell

Allowed actions: `create_issue`, `comment`, `add_label`, `remove_label`,
`close_issue`, `create_agent`. Cap at 5 actions per mention to bound impact and cost.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .agent_builder import validate_create_agent_payload

ALLOWED_ACTIONS = {
    "create_issue", "comment", "add_label", "remove_label", "close_issue",
    "create_agent",
    "open_pr_with_changes",  # staged action — requires Andy's chat approval before execution
    "search_conversation_memory",  # semantic search across the agent's own + shared archive
}
MAX_ACTIONS_PER_MENTION = 5
MAX_BODY_CHARS = 8000  # generous; gh accepts large bodies but stay sane

# Match ```actions\n...\n``` (or ``` actions, ```action, etc — be liberal)
ACTION_BLOCK_RE = re.compile(
    r"```\s*actions?\s*\n(.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class ParsedActionBlock:
    """Result of extract_actions: the response with action block stripped,
    and the parsed actions (or None if no block / parse error).

    `parse_error` is set if a block was found but malformed; the visible
    response in that case is the unmodified original (we don't want to
    swallow the block silently if parsing failed)."""
    visible_response: str
    actions: list[dict] | None  # None if no block found OR parse error
    parse_error: str | None


def extract_actions(response_text: str) -> ParsedActionBlock:
    """Find the action block, strip it from the response, parse the JSON."""
    match = ACTION_BLOCK_RE.search(response_text)
    if not match:
        return ParsedActionBlock(
            visible_response=response_text,
            actions=None,
            parse_error=None,
        )

    raw_json = match.group(1).strip()
    try:
        actions = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        # Leave the block visible so Andy can see what the agent emitted.
        return ParsedActionBlock(
            visible_response=response_text,
            actions=None,
            parse_error=f"json_decode: {exc.msg}",
        )

    if not isinstance(actions, list):
        return ParsedActionBlock(
            visible_response=response_text,
            actions=None,
            parse_error="actions block must be a JSON array",
        )

    # Strip the block from the visible response.
    visible = (response_text[: match.start()] + response_text[match.end():]).strip()

    return ParsedActionBlock(
        visible_response=visible,
        actions=actions,
        parse_error=None,
    )


@dataclass
class ValidationResult:
    ok: bool
    error: str | None = None
    action_type: str | None = None


def validate_action(action: Any) -> ValidationResult:
    """Validate a single action dict. Returns ok=True if it can be executed."""
    if not isinstance(action, dict):
        return ValidationResult(ok=False, error="action must be an object")
    action_type = action.get("action")
    if action_type not in ALLOWED_ACTIONS:
        return ValidationResult(
            ok=False, error=f"unknown action type: {action_type!r} (allowed: {sorted(ALLOWED_ACTIONS)})",
        )

    if action_type == "create_issue":
        if not isinstance(action.get("title"), str) or not action["title"].strip():
            return ValidationResult(ok=False, error="create_issue: title must be non-empty string", action_type=action_type)
        if not isinstance(action.get("body"), str):
            return ValidationResult(ok=False, error="create_issue: body must be string", action_type=action_type)
        if len(action["body"]) > MAX_BODY_CHARS:
            return ValidationResult(ok=False, error=f"create_issue: body exceeds {MAX_BODY_CHARS} chars", action_type=action_type)
        labels = action.get("labels", [])
        if not isinstance(labels, list) or not all(isinstance(l, str) for l in labels):
            return ValidationResult(ok=False, error="create_issue: labels must be list[str]", action_type=action_type)
        return ValidationResult(ok=True, action_type=action_type)

    if action_type == "comment":
        if not isinstance(action.get("issue_number"), int) or action["issue_number"] <= 0:
            return ValidationResult(ok=False, error="comment: issue_number must be positive int", action_type=action_type)
        if not isinstance(action.get("body"), str) or not action["body"].strip():
            return ValidationResult(ok=False, error="comment: body must be non-empty string", action_type=action_type)
        if len(action["body"]) > MAX_BODY_CHARS:
            return ValidationResult(ok=False, error=f"comment: body exceeds {MAX_BODY_CHARS} chars", action_type=action_type)
        return ValidationResult(ok=True, action_type=action_type)

    if action_type in ("add_label", "remove_label"):
        if not isinstance(action.get("issue_number"), int) or action["issue_number"] <= 0:
            return ValidationResult(ok=False, error=f"{action_type}: issue_number must be positive int", action_type=action_type)
        labels = action.get("labels", [])
        if not isinstance(labels, list) or not labels or not all(isinstance(l, str) for l in labels):
            return ValidationResult(ok=False, error=f"{action_type}: labels must be non-empty list[str]", action_type=action_type)
        return ValidationResult(ok=True, action_type=action_type)

    if action_type == "close_issue":
        if not isinstance(action.get("issue_number"), int) or action["issue_number"] <= 0:
            return ValidationResult(ok=False, error="close_issue: issue_number must be positive int", action_type=action_type)
        # Optional: comment string
        if "comment" in action:
            if not isinstance(action["comment"], str):
                return ValidationResult(ok=False, error="close_issue: comment must be string if present", action_type=action_type)
            if len(action["comment"]) > MAX_BODY_CHARS:
                return ValidationResult(ok=False, error=f"close_issue: comment exceeds {MAX_BODY_CHARS} chars", action_type=action_type)
        return ValidationResult(ok=True, action_type=action_type)

    if action_type == "create_agent":
        ok, err = validate_create_agent_payload(action)
        if not ok:
            return ValidationResult(ok=False, error=f"create_agent: {err}", action_type=action_type)
        return ValidationResult(ok=True, action_type=action_type)

    if action_type == "open_pr_with_changes":
        # Full per-agent + per-repo validation happens in handlers (which has the
        # agent_id and channel_id in scope). At this layer we only do shape
        # checks so a malformed action doesn't pollute the batch.
        for required in ("repo", "branch", "files", "commit_message", "pr_title"):
            if required not in action:
                return ValidationResult(
                    ok=False, error=f"open_pr_with_changes: missing required field {required!r}",
                    action_type=action_type,
                )
        if not isinstance(action["files"], list) or not action["files"]:
            return ValidationResult(
                ok=False, error="open_pr_with_changes: files must be non-empty list",
                action_type=action_type,
            )
        return ValidationResult(ok=True, action_type=action_type)

    if action_type == "search_conversation_memory":
        # Semantic search across the agent's archive via Ollama embeddings.
        # Validation is shape-only here; handlers.py runs the query with
        # agent_id in scope.
        if not isinstance(action.get("query"), str) or not action["query"].strip():
            return ValidationResult(
                ok=False, error="search_conversation_memory: query must be non-empty string",
                action_type=action_type,
            )
        top_n = action.get("top_n", 5)
        if not isinstance(top_n, int) or top_n < 1 or top_n > 20:
            return ValidationResult(
                ok=False, error="search_conversation_memory: top_n must be int in [1, 20]",
                action_type=action_type,
            )
        return ValidationResult(ok=True, action_type=action_type)

    # Should be unreachable given the ALLOWED_ACTIONS check above.
    return ValidationResult(ok=False, error=f"unhandled action: {action_type}", action_type=action_type)


def validate_action_batch(actions: list) -> tuple[bool, str | None, list[dict]]:
    """Validate the full batch. Returns (ok, error_msg, valid_actions)."""
    if len(actions) > MAX_ACTIONS_PER_MENTION:
        return False, f"too many actions ({len(actions)} > max {MAX_ACTIONS_PER_MENTION})", []
    valid: list[dict] = []
    for i, action in enumerate(actions):
        result = validate_action(action)
        if not result.ok:
            return False, f"action[{i}]: {result.error}", []
        valid.append(action)
    return True, None, valid
