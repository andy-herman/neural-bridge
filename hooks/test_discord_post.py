"""Unit tests for hooks/discord_post.py.

Stdlib-only. No real network or keychain access — both are mocked.
Run: `python3 hooks/test_discord_post.py`
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

import discord_post  # noqa: E402


class _FakeKeychainResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestGetWebhookUrl(unittest.TestCase):
    def test_env_var_wins(self):
        with patch.dict("os.environ", {discord_post.ENV_VAR: "https://example/from-env", "USER": "andy"}):
            self.assertEqual(discord_post.get_webhook_url(), "https://example/from-env")

    def test_keychain_when_no_env(self):
        with patch.dict("os.environ", {discord_post.ENV_VAR: "", "USER": "andy"}):
            with patch(
                "discord_post.subprocess.run",
                return_value=_FakeKeychainResult(0, "https://example/from-keychain\n"),
            ):
                self.assertEqual(discord_post.get_webhook_url(), "https://example/from-keychain")

    def test_keychain_missing_returns_none(self):
        with patch.dict("os.environ", {discord_post.ENV_VAR: "", "USER": "andy"}):
            with patch(
                "discord_post.subprocess.run",
                return_value=_FakeKeychainResult(44, "", "The specified item could not be found"),
            ):
                self.assertIsNone(discord_post.get_webhook_url())

    def test_security_cli_missing(self):
        with patch.dict("os.environ", {discord_post.ENV_VAR: "", "USER": "andy"}):
            with patch("discord_post.subprocess.run", side_effect=FileNotFoundError()):
                self.assertIsNone(discord_post.get_webhook_url())

    def test_no_user_returns_none(self):
        with patch.dict("os.environ", {discord_post.ENV_VAR: "", "USER": "", "LOGNAME": ""}):
            self.assertIsNone(discord_post.get_webhook_url())


class TestTruncate(unittest.TestCase):
    def test_short_passes_through(self):
        self.assertEqual(discord_post.truncate_for_discord("hi"), "hi")

    def test_truncates_at_limit(self):
        long = "x" * 3000
        out = discord_post.truncate_for_discord(long)
        self.assertEqual(len(out), discord_post.DISCORD_MAX_CONTENT)

    def test_appends_suffix_when_room(self):
        text = "x" * 1000
        out = discord_post.truncate_for_discord(text, suffix=" [truncated]")
        # Short text + suffix shouldn't get truncated
        self.assertTrue(out.endswith(" [truncated]") or out == text)

    def test_truncates_with_suffix(self):
        text = "y" * 3000
        out = discord_post.truncate_for_discord(text, suffix=" [truncated]")
        self.assertTrue(out.endswith(" [truncated]"))
        self.assertLessEqual(len(out), discord_post.DISCORD_MAX_CONTENT)


class TestSend(unittest.TestCase):
    def test_no_webhook_returns_false(self):
        with patch.dict("os.environ", {discord_post.ENV_VAR: "", "USER": "x"}):
            with patch("discord_post.subprocess.run", return_value=_FakeKeychainResult(44)):
                self.assertFalse(discord_post.send("hello"))

    def test_explicit_url_skips_keychain(self):
        class _Resp:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        with patch("discord_post.request.urlopen", return_value=_Resp()):
            self.assertTrue(discord_post.send("hello", webhook_url="https://example/wh"))

    def test_url_error_returns_false(self):
        from urllib import error as url_error

        with patch("discord_post.request.urlopen", side_effect=url_error.URLError("boom")):
            self.assertFalse(discord_post.send("hello", webhook_url="https://example/wh"))

    def test_timeout_returns_false(self):
        with patch("discord_post.request.urlopen", side_effect=TimeoutError()):
            self.assertFalse(discord_post.send("hello", webhook_url="https://example/wh"))

    def test_http_2xx_via_httperror_returns_true(self):
        # Some Pythons raise HTTPError even for 2xx with no body
        from urllib import error as url_error

        exc = url_error.HTTPError("https://example/wh", 204, "No Content", {}, None)
        with patch("discord_post.request.urlopen", side_effect=exc):
            self.assertTrue(discord_post.send("hello", webhook_url="https://example/wh"))

    def test_http_4xx_returns_false(self):
        from urllib import error as url_error

        exc = url_error.HTTPError("https://example/wh", 401, "Unauthorized", {}, None)
        with patch("discord_post.request.urlopen", side_effect=exc):
            self.assertFalse(discord_post.send("hello", webhook_url="https://example/wh"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
