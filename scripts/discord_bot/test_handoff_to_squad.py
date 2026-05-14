"""Unit tests for handoff_to_squad.py pure helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.handoff_to_squad import (  # noqa: E402
    MAX_POST_CHARS,
    build_dm_confirmation,
    build_handoff_post,
    resolve_mentions,
)


AGENTS_BY_ID = {
    "professor": "1502000000000000001",
    "docs-editor": "1502000000000000002",
    "research": "1502000000000000003",
    "luna": "1502000000000000099",
}


class TestResolveMentions(unittest.TestCase):
    def test_all_known(self):
        r = resolve_mentions(["professor", "docs-editor"], AGENTS_BY_ID)
        self.assertTrue(r.ok)
        self.assertEqual(r.client_ids, [
            "1502000000000000001", "1502000000000000002",
        ])
        self.assertEqual(r.unknown, [])

    def test_one_unknown(self):
        r = resolve_mentions(["professor", "ghost-agent"], AGENTS_BY_ID)
        self.assertFalse(r.ok)
        self.assertEqual(r.unknown, ["ghost-agent"])
        self.assertIn("ghost-agent", r.error)

    def test_multiple_unknown(self):
        r = resolve_mentions(["alpha", "beta"], AGENTS_BY_ID)
        self.assertFalse(r.ok)
        self.assertEqual(r.unknown, ["alpha", "beta"])

    def test_empty_input(self):
        r = resolve_mentions([], AGENTS_BY_ID)
        # No unknowns, so technically resolves successfully with an empty list.
        # The caller (actions.py validator) rejects empty mentions earlier.
        self.assertTrue(r.ok)
        self.assertEqual(r.client_ids, [])


class TestBuildHandoffPost(unittest.TestCase):
    def test_basic_shape(self):
        body = build_handoff_post(
            summary="Update lecture 13 review questions to drop lecture-12 items.",
            mention_client_ids=["111", "222"],
        )
        self.assertIn("📨 Handoff from Luna", body)
        self.assertIn("<@111> <@222>", body)
        self.assertIn("Update lecture 13", body)
        self.assertNotIn(">", body.split("Update lecture 13")[1])  # no excerpt block

    def test_with_excerpt(self):
        body = build_handoff_post(
            summary="x",
            mention_client_ids=["111"],
            dm_excerpt="Andy: please update lecture 13.",
        )
        self.assertIn("> Andy: please update lecture 13.", body)

    def test_empty_excerpt_omitted(self):
        body = build_handoff_post(
            summary="x",
            mention_client_ids=["111"],
            dm_excerpt="   ",
        )
        # No blockquote line should appear when the excerpt is whitespace-only.
        self.assertFalse(any(line.startswith("> ") for line in body.splitlines()))

    def test_truncates_when_over_cap(self):
        long_summary = "x" * (MAX_POST_CHARS + 500)
        body = build_handoff_post(
            summary=long_summary,
            mention_client_ids=["111"],
        )
        self.assertLessEqual(len(body), MAX_POST_CHARS)
        self.assertTrue(body.rstrip().endswith("…"))

    def test_mentions_and_header_preserved_under_truncation(self):
        body = build_handoff_post(
            summary="x" * (MAX_POST_CHARS + 500),
            mention_client_ids=["111", "222", "333"],
        )
        # Even with truncation, the @-mentions must survive — Discord won't fire
        # the on_message handlers without them.
        self.assertIn("<@111>", body)
        self.assertIn("<@222>", body)
        self.assertIn("<@333>", body)
        self.assertIn("📨 Handoff", body)


class TestBuildDmConfirmation(unittest.TestCase):
    def test_basic(self):
        text = build_dm_confirmation(
            mentions=["professor", "docs-editor"],
            squad_channel_id=1502587655680954458,
        )
        self.assertIn("<#1502587655680954458>", text)
        self.assertIn("`@professor`", text)
        self.assertIn("`@docs-editor`", text)
        self.assertNotIn("https://discord.com", text)

    def test_with_link(self):
        text = build_dm_confirmation(
            mentions=["professor"],
            squad_channel_id=1502587655680954458,
            message_id=9999999999,
            guild_id=1502587535384379533,
        )
        self.assertIn("https://discord.com/channels/1502587535384379533/1502587655680954458/9999999999", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
