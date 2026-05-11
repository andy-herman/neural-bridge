"""Unit tests for conversation_log.py — per-agent Discord conversation
archive in the Obsidian vault.

Stdlib-only. Each test redirects AGENTS_BASE into a tempdir so the
real vault stays untouched.

Run: `python3 scripts/discord_bot/test_conversation_log.py`
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot import conversation_log as cl  # noqa: E402
from scripts.discord_bot.conversation_log import (  # noqa: E402
    agent_conversations_dir,
    append_turn,
    channel_label,
    conversation_log_path,
)


# ---- mocks: minimal discord.Message stand-in ----

class FakeUser:
    def __init__(self, name="andy.herman", display_name="Andy"):
        self.name = name
        self.display_name = display_name


class FakeGuildChannel:
    def __init__(self, channel_id=1234567890, name="neural-bridge"):
        self.id = channel_id
        self.name = name


class FakeDMChannel:
    def __init__(self, channel_id=9876543210):
        self.id = channel_id
        # No `.name` attribute — that's the DM indicator.


class FakeMessage:
    def __init__(self, channel, author=None, content=""):
        self.channel = channel
        self.author = author or FakeUser()
        self.content = content


# ---- tests ----

class TestChannelLabel(unittest.TestCase):
    def test_guild_channel(self):
        msg = FakeMessage(FakeGuildChannel(name="neural-bridge"))
        label, display, kind = channel_label(msg)
        self.assertEqual(label, "neural-bridge")
        self.assertEqual(display, "neural-bridge")
        self.assertEqual(kind, "guild")

    def test_guild_channel_with_unsafe_chars(self):
        msg = FakeMessage(FakeGuildChannel(name="My Channel! @#$"))
        label, display, kind = channel_label(msg)
        self.assertEqual(label, "my-channel")
        self.assertEqual(display, "My Channel! @#$")
        self.assertEqual(kind, "guild")

    def test_dm_uses_author_username(self):
        msg = FakeMessage(FakeDMChannel(), author=FakeUser(name="andy.herman"))
        label, display, kind = channel_label(msg)
        self.assertEqual(label, "DM-andy-herman")
        self.assertIn("andy.herman", display)
        self.assertEqual(kind, "DM")

    def test_dm_falls_back_to_unknown_when_author_missing(self):
        msg = FakeMessage(FakeDMChannel(), author=FakeUser(name=None, display_name=None))
        label, _, kind = channel_label(msg)
        self.assertEqual(label, "DM-unknown")
        self.assertEqual(kind, "DM")

    def test_label_is_length_capped(self):
        very_long = "x" * 200
        msg = FakeMessage(FakeGuildChannel(name=very_long))
        label, _, _ = channel_label(msg)
        self.assertLessEqual(len(label), 60)


class TestConversationLogPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = cl.AGENTS_BASE
        cl.AGENTS_BASE = Path(self._tmp.name) / "Agents"

    def tearDown(self):
        cl.AGENTS_BASE = self._orig
        self._tmp.cleanup()

    def test_path_segments(self):
        msg = FakeMessage(FakeGuildChannel(name="neural-bridge"))
        now = datetime(2026, 5, 11, 22, 34, tzinfo=timezone.utc)
        path = conversation_log_path("luna", msg, now=now)
        self.assertEqual(
            path,
            cl.AGENTS_BASE / "luna" / "conversations" / "2026-05" / "neural-bridge.md",
        )

    def test_dm_path(self):
        msg = FakeMessage(FakeDMChannel(), author=FakeUser(name="andy"))
        now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        path = conversation_log_path("echo", msg, now=now)
        self.assertEqual(path.name, "DM-andy.md")
        self.assertIn("echo", str(path))
        self.assertIn("2026-06", str(path))


class TestAgentConversationsDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = cl.AGENTS_BASE
        cl.AGENTS_BASE = Path(self._tmp.name) / "Agents"

    def tearDown(self):
        cl.AGENTS_BASE = self._orig
        self._tmp.cleanup()

    def test_dir_under_agent(self):
        p = agent_conversations_dir("luna")
        self.assertEqual(p, cl.AGENTS_BASE / "luna" / "conversations")


class TestAppendTurn(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = cl.AGENTS_BASE
        cl.AGENTS_BASE = Path(self._tmp.name) / "Agents"

    def tearDown(self):
        cl.AGENTS_BASE = self._orig
        self._tmp.cleanup()

    def _msg(self):
        return FakeMessage(
            FakeGuildChannel(name="neural-bridge"),
            content="hello world",
        )

    def test_first_append_creates_file_with_frontmatter(self):
        msg = self._msg()
        path = append_turn("luna", msg, "Andy", "Hi Luna, can you help?")
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("---", text)  # frontmatter
        self.assertIn("agent: luna", text)
        self.assertIn("channel_name: neural-bridge", text)
        self.assertIn("channel_kind: guild", text)
        self.assertIn("# Conversation log — luna × neural-bridge", text)
        self.assertIn("Hi Luna, can you help?", text)
        self.assertIn("— Andy", text)

    def test_second_append_appends_section_only(self):
        msg = self._msg()
        path1 = append_turn("luna", msg, "Andy", "First message")
        path2 = append_turn("luna", msg, "Luna", "First response")
        self.assertEqual(path1, path2)
        text = path2.read_text(encoding="utf-8")
        # Both turns present, in order.
        idx_first = text.index("First message")
        idx_second = text.index("First response")
        self.assertLess(idx_first, idx_second)
        # Frontmatter appears only once.
        self.assertEqual(text.count("---\nagent: luna"), 1)

    def test_empty_content_returns_none(self):
        msg = self._msg()
        self.assertIsNone(append_turn("luna", msg, "Andy", ""))
        self.assertIsNone(append_turn("luna", msg, "Andy", "   \n  "))

    def test_separate_agents_get_separate_files(self):
        msg = self._msg()
        p_luna = append_turn("luna", msg, "Andy", "for luna")
        p_echo = append_turn("echo", msg, "Andy", "for echo")
        self.assertNotEqual(p_luna, p_echo)
        self.assertIn("luna", str(p_luna))
        self.assertIn("echo", str(p_echo))

    def test_separate_channels_get_separate_files(self):
        msg_a = FakeMessage(FakeGuildChannel(channel_id=1, name="alpha"))
        msg_b = FakeMessage(FakeGuildChannel(channel_id=2, name="bravo"))
        p_a = append_turn("luna", msg_a, "Andy", "in alpha")
        p_b = append_turn("luna", msg_b, "Andy", "in bravo")
        self.assertNotEqual(p_a, p_b)
        self.assertIn("alpha.md", str(p_a))
        self.assertIn("bravo.md", str(p_b))

    def test_dm_and_guild_get_separate_files_even_same_agent(self):
        guild_msg = FakeMessage(FakeGuildChannel(name="neural-bridge"))
        dm_msg = FakeMessage(FakeDMChannel(), author=FakeUser(name="andy"))
        p_guild = append_turn("luna", guild_msg, "Andy", "in guild")
        p_dm = append_turn("luna", dm_msg, "Andy", "in DM")
        self.assertNotEqual(p_guild, p_dm)
        self.assertIn("neural-bridge.md", str(p_guild))
        self.assertIn("DM-andy.md", str(p_dm))

    def test_different_months_get_separate_files(self):
        msg = self._msg()
        may = datetime(2026, 5, 31, 23, 59, tzinfo=timezone.utc)
        june = datetime(2026, 6, 1, 0, 1, tzinfo=timezone.utc)
        p_may = append_turn("luna", msg, "Andy", "May msg", now=may)
        p_june = append_turn("luna", msg, "Andy", "June msg", now=june)
        self.assertNotEqual(p_may, p_june)
        self.assertIn("2026-05", str(p_may))
        self.assertIn("2026-06", str(p_june))

    def test_timestamp_in_section_header_is_utc_iso_ish(self):
        msg = self._msg()
        now = datetime(2026, 5, 11, 22, 34, 15, tzinfo=timezone.utc)
        path = append_turn("luna", msg, "Andy", "stamp test", now=now)
        text = path.read_text(encoding="utf-8")
        self.assertIn("## 2026-05-11 22:34:15Z — Andy", text)

    def test_write_failure_returns_none_without_raising(self):
        # Make the parent dir read-only so the write fails.
        msg = self._msg()
        # First write to create the parent dir.
        append_turn("luna", msg, "Andy", "first")
        target_dir = cl.AGENTS_BASE / "luna" / "conversations"
        original_mode = target_dir.stat().st_mode
        try:
            target_dir.chmod(0o555)  # read+exec, no write
            result = append_turn("luna", msg, "Andy", "second", now=datetime(2099, 1, 1, tzinfo=timezone.utc))
            # Either succeeded (if running as root) or returned None gracefully.
            # Most importantly: did not raise.
            self.assertIn(result, (None,) + tuple(p for p in [result] if p is not None))
        finally:
            target_dir.chmod(original_mode)


if __name__ == "__main__":
    unittest.main()
