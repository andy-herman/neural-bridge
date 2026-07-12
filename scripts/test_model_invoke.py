"""Unit tests for model_invoke.py. Stdlib-only; no real network calls.

Run: python3 scripts/test_model_invoke.py
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import model_invoke as mi  # noqa: E402


class TestFallbackConfig(unittest.TestCase):
    def test_unset_is_none(self):
        self.assertIsNone(mi.fallback_config({}))
        self.assertIsNone(mi.fallback_config({"NB_FALLBACK_BASE_URL": "   "}))

    def test_set_parses_and_strips_trailing_slash(self):
        cfg = mi.fallback_config({
            "NB_FALLBACK_BASE_URL": "https://api.example.test/v1/",
            "NB_FALLBACK_API_KEY": "sk-x",
            "NB_FALLBACK_MODEL": "m1",
        })
        self.assertEqual(cfg["base_url"], "https://api.example.test/v1")
        self.assertEqual(cfg["api_key"], "sk-x")
        self.assertEqual(cfg["model"], "m1")

    def test_available(self):
        self.assertFalse(mi.fallback_available({}))
        self.assertTrue(mi.fallback_available({"NB_FALLBACK_BASE_URL": "https://x"}))


class TestParseOpenAIResponse(unittest.TestCase):
    def test_valid(self):
        raw = json.dumps({"choices": [{"message": {"content": "hello"}}]})
        ok, text, err = mi.parse_openai_response(raw)
        self.assertTrue(ok, err)
        self.assertEqual(text, "hello")

    def test_bad_json(self):
        ok, _, err = mi.parse_openai_response("not json")
        self.assertFalse(ok)
        self.assertEqual(err, "fallback_bad_json")

    def test_no_content(self):
        ok, _, err = mi.parse_openai_response(json.dumps({"choices": []}))
        self.assertFalse(ok)
        self.assertEqual(err, "fallback_no_content")

    def test_empty_content(self):
        raw = json.dumps({"choices": [{"message": {"content": "   "}}]})
        ok, _, err = mi.parse_openai_response(raw)
        self.assertFalse(ok)
        self.assertEqual(err, "fallback_empty")


class TestFallbackText(unittest.TestCase):
    def test_unconfigured(self):
        ok, _, err = mi.fallback_text("p", 10, env={})
        self.assertFalse(ok)
        self.assertEqual(err, "no_fallback_configured")

    def test_model_unset(self):
        ok, _, err = mi.fallback_text("p", 10, env={"NB_FALLBACK_BASE_URL": "https://x"})
        self.assertFalse(ok)
        self.assertEqual(err, "fallback_model_unset")

    def test_success_and_request_shape(self):
        env = {"NB_FALLBACK_BASE_URL": "https://api.example.test/v1",
               "NB_FALLBACK_MODEL": "m1", "NB_FALLBACK_API_KEY": "sk-x"}
        body = json.dumps({"choices": [{"message": {"content": "the answer"}}]}).encode()

        class _Resp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("model_invoke.urllib.request.urlopen", return_value=_Resp()) as up:
            ok, text, err = mi.fallback_text("p", 10, env=env)
        self.assertTrue(ok, err)
        self.assertEqual(text, "the answer")
        req = up.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.example.test/v1/chat/completions")
        self.assertIn(b'"m1"', req.data)  # model in the payload

    def test_http_error(self):
        env = {"NB_FALLBACK_BASE_URL": "https://x", "NB_FALLBACK_MODEL": "m"}
        err500 = urllib.error.HTTPError("u", 500, "err", {}, None)
        with patch("model_invoke.urllib.request.urlopen", side_effect=err500):
            ok, _, err = mi.fallback_text("p", 10, env=env)
        self.assertFalse(ok)
        self.assertEqual(err, "http_500")

    def test_network_error_is_nonfatal(self):
        env = {"NB_FALLBACK_BASE_URL": "https://x", "NB_FALLBACK_MODEL": "m"}
        with patch("model_invoke.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            ok, _, err = mi.fallback_text("p", 10, env=env)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("urlerror"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
