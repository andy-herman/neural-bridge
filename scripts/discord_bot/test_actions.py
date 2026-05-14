"""Unit tests for actions.py — structured tool-use protocol parsing
and validation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.actions import (  # noqa: E402
    MAX_ACTIONS_PER_MENTION,
    extract_actions,
    validate_action,
    validate_action_batch,
)


class TestExtractActions(unittest.TestCase):
    def test_no_block_passes_through(self):
        result = extract_actions("Just a regular response, no action block.")
        self.assertEqual(result.visible_response, "Just a regular response, no action block.")
        self.assertIsNone(result.actions)
        self.assertIsNone(result.parse_error)

    def test_extracts_block(self):
        text = (
            "Here's my analysis. The papers are interesting.\n\n"
            "```actions\n"
            '[{"action": "create_issue", "title": "Track AgentPoison", "body": "..."}]\n'
            "```\n"
        )
        result = extract_actions(text)
        self.assertEqual(result.parse_error, None)
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0]["title"], "Track AgentPoison")
        self.assertNotIn("```actions", result.visible_response)
        self.assertIn("Here's my analysis", result.visible_response)

    def test_handles_action_singular_keyword(self):
        text = "x\n\n```action\n[{\"action\": \"comment\", \"issue_number\": 1, \"body\": \"hi\"}]\n```\n"
        result = extract_actions(text)
        self.assertIsNotNone(result.actions)
        self.assertEqual(result.actions[0]["action"], "comment")

    def test_invalid_json_returns_parse_error(self):
        text = "x\n\n```actions\nthis is not json\n```"
        result = extract_actions(text)
        self.assertIsNone(result.actions)
        self.assertIsNotNone(result.parse_error)
        # Visible response should be unchanged so Andy can see what happened.
        self.assertIn("not json", result.visible_response)

    def test_non_array_root_rejected(self):
        text = '```actions\n{"action": "comment", "issue_number": 1, "body": "x"}\n```'
        result = extract_actions(text)
        self.assertIsNone(result.actions)
        self.assertIn("array", result.parse_error)


class TestValidateAction(unittest.TestCase):
    def test_create_issue_valid(self):
        r = validate_action({"action": "create_issue", "title": "x", "body": "y"})
        self.assertTrue(r.ok, r.error)

    def test_create_issue_missing_title(self):
        r = validate_action({"action": "create_issue", "body": "y"})
        self.assertFalse(r.ok)
        self.assertIn("title", r.error)

    def test_create_issue_with_labels(self):
        r = validate_action({"action": "create_issue", "title": "x", "body": "y", "labels": ["a", "b"]})
        self.assertTrue(r.ok)

    def test_create_issue_bad_labels(self):
        r = validate_action({"action": "create_issue", "title": "x", "body": "y", "labels": "not-a-list"})
        self.assertFalse(r.ok)

    def test_comment_valid(self):
        r = validate_action({"action": "comment", "issue_number": 14, "body": "hello"})
        self.assertTrue(r.ok)

    def test_comment_negative_issue(self):
        r = validate_action({"action": "comment", "issue_number": -1, "body": "x"})
        self.assertFalse(r.ok)

    def test_add_label_valid(self):
        r = validate_action({"action": "add_label", "issue_number": 1, "labels": ["bug"]})
        self.assertTrue(r.ok)

    def test_add_label_empty_labels(self):
        r = validate_action({"action": "add_label", "issue_number": 1, "labels": []})
        self.assertFalse(r.ok)

    def test_close_issue_no_comment(self):
        r = validate_action({"action": "close_issue", "issue_number": 1})
        self.assertTrue(r.ok)

    def test_close_issue_with_comment(self):
        r = validate_action({"action": "close_issue", "issue_number": 1, "comment": "done"})
        self.assertTrue(r.ok)

    def test_unknown_action_type(self):
        r = validate_action({"action": "delete_repo", "name": "x"})
        self.assertFalse(r.ok)
        self.assertIn("unknown", r.error)

    def test_non_dict_action(self):
        r = validate_action("not a dict")
        self.assertFalse(r.ok)

    # ---------- handoff_to_squad ----------

    def test_handoff_to_squad_valid(self):
        r = validate_action({
            "action": "handoff_to_squad",
            "summary": "Loop in @professor and @editor on lecture 13 review questions.",
            "mentions": ["professor", "docs-editor"],
        })
        self.assertTrue(r.ok, r.error)

    def test_handoff_to_squad_valid_with_excerpt(self):
        r = validate_action({
            "action": "handoff_to_squad",
            "summary": "x",
            "mentions": ["professor"],
            "dm_excerpt": "Andy said: please pull lecture 13 questions.",
        })
        self.assertTrue(r.ok, r.error)

    def test_handoff_to_squad_missing_summary(self):
        r = validate_action({"action": "handoff_to_squad", "mentions": ["professor"]})
        self.assertFalse(r.ok)
        self.assertIn("summary", r.error)

    def test_handoff_to_squad_empty_summary(self):
        r = validate_action({"action": "handoff_to_squad", "summary": "  ", "mentions": ["professor"]})
        self.assertFalse(r.ok)

    def test_handoff_to_squad_empty_mentions(self):
        r = validate_action({"action": "handoff_to_squad", "summary": "x", "mentions": []})
        self.assertFalse(r.ok)
        self.assertIn("mentions", r.error)

    def test_handoff_to_squad_too_many_mentions(self):
        r = validate_action({
            "action": "handoff_to_squad", "summary": "x",
            "mentions": ["a", "b", "c", "d"],
        })
        self.assertFalse(r.ok)
        self.assertIn("capped", r.error)

    def test_handoff_to_squad_duplicate_mentions(self):
        r = validate_action({
            "action": "handoff_to_squad", "summary": "x",
            "mentions": ["professor", "professor"],
        })
        self.assertFalse(r.ok)
        self.assertIn("unique", r.error)

    def test_handoff_to_squad_excerpt_too_long(self):
        r = validate_action({
            "action": "handoff_to_squad", "summary": "x",
            "mentions": ["professor"], "dm_excerpt": "y" * 2001,
        })
        self.assertFalse(r.ok)
        self.assertIn("dm_excerpt", r.error)

    def test_handoff_to_squad_wrong_mentions_type(self):
        r = validate_action({
            "action": "handoff_to_squad", "summary": "x",
            "mentions": "professor",
        })
        self.assertFalse(r.ok)


class TestValidateBatch(unittest.TestCase):
    def test_empty_batch(self):
        ok, err, valid = validate_action_batch([])
        self.assertTrue(ok)
        self.assertEqual(valid, [])

    def test_under_limit(self):
        actions = [{"action": "comment", "issue_number": i, "body": "x"} for i in range(1, 4)]
        ok, err, valid = validate_action_batch(actions)
        self.assertTrue(ok)
        self.assertEqual(len(valid), 3)

    def test_over_limit(self):
        actions = [{"action": "comment", "issue_number": i, "body": "x"} for i in range(1, MAX_ACTIONS_PER_MENTION + 2)]
        ok, err, valid = validate_action_batch(actions)
        self.assertFalse(ok)
        self.assertIn("too many", err)

    def test_one_invalid_aborts_batch(self):
        actions = [
            {"action": "comment", "issue_number": 1, "body": "ok"},
            {"action": "comment", "issue_number": 2, "body": ""},  # empty body invalid
            {"action": "comment", "issue_number": 3, "body": "ok"},
        ]
        ok, err, valid = validate_action_batch(actions)
        self.assertFalse(ok)
        self.assertIn("[1]", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
