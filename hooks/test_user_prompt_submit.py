"""Unit tests for hooks/user_prompt_submit.py."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

import user_prompt_submit as ups  # noqa: E402
import wiki_recall  # noqa: E402

BLOCK = wiki_recall.BLOCK_BEGIN + "\n- [[x]]\n" + wiki_recall.BLOCK_END + "\n\n"


class TestExtractPrompt(unittest.TestCase):
    def test_user_input_is_preferred(self):
        self.assertEqual(ups.extract_prompt({"user_input": "a", "prompt": "b"}), "a")

    def test_prompt_is_the_fallback(self):
        # Older harness versions delivered the text under `prompt`.
        self.assertEqual(ups.extract_prompt({"prompt": "b"}), "b")

    def test_empty_or_wrong_type(self):
        self.assertEqual(ups.extract_prompt({"user_input": "   "}), "")
        self.assertEqual(ups.extract_prompt({"user_input": 42}), "")
        self.assertEqual(ups.extract_prompt({}), "")


class TestResolveAgent(unittest.TestCase):
    def test_agent_id_then_agent_then_unattributed(self):
        self.assertEqual(ups.resolve_agent({"NB_AGENT_ID": "luna"}), "luna")
        self.assertEqual(ups.resolve_agent({"NB_AGENT": "research"}), "research")
        self.assertEqual(ups.resolve_agent({"NB_AGENT": "compile"}), ups.UNATTRIBUTED)
        self.assertEqual(ups.resolve_agent({}), ups.UNATTRIBUTED)


class TestBuildContext(unittest.TestCase):
    def test_injects_block_for_a_matching_prompt(self):
        with patch.object(wiki_recall, "recall", return_value=(BLOCK, ["hit"])) as rec:
            out = ups.build_context({"user_input": "how do CVE and CWE relate"}, env={"NB_AGENT_ID": "research"})
        self.assertEqual(out, BLOCK)
        self.assertEqual(rec.call_args.kwargs["agent_id"], "research")

    def test_skip_env_injects_nothing_and_does_not_search(self):
        with patch.object(wiki_recall, "recall") as rec:
            out = ups.build_context({"user_input": "cve"}, env={wiki_recall.ENV_SKIP: "1"})
        self.assertEqual(out, "")
        rec.assert_not_called()

    def test_slash_commands_and_empty_prompts_are_ignored(self):
        with patch.object(wiki_recall, "recall") as rec:
            self.assertEqual(ups.build_context({"user_input": "/compact"}, env={}), "")
            self.assertEqual(ups.build_context({"user_input": ""}, env={}), "")
        rec.assert_not_called()

    def test_prompt_already_carrying_the_block_is_not_reinjected(self):
        with patch.object(wiki_recall, "recall") as rec:
            self.assertEqual(ups.build_context({"user_input": BLOCK + "question"}, env={}), "")
        rec.assert_not_called()

    def test_long_prompts_are_truncated_before_ranking(self):
        with patch.object(wiki_recall, "recall", return_value=("", [])) as rec:
            ups.build_context({"user_input": "x" * 10000}, env={})
        self.assertEqual(len(rec.call_args.args[0]), ups.MAX_QUERY_CHARS)


class TestMain(unittest.TestCase):
    def _run(self, raw: str) -> tuple[int, str]:
        buf = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(raw)), patch.object(sys, "stdout", buf):
            rc = ups.main()
        return rc, buf.getvalue()

    def test_prints_block_to_stdout(self):
        with patch.object(ups, "build_context", return_value=BLOCK):
            rc, out = self._run(json.dumps({"user_input": "cve"}))
        self.assertEqual(rc, 0)
        self.assertEqual(out, BLOCK)

    def test_bad_payload_is_silent_and_exit_zero(self):
        self.assertEqual(self._run("{not json"), (0, ""))
        self.assertEqual(self._run("[1,2]"), (0, ""))
        self.assertEqual(self._run(""), (0, ""))

    def test_internal_error_never_blocks_the_prompt(self):
        with patch.object(ups, "build_context", side_effect=RuntimeError("boom")):
            self.assertEqual(self._run(json.dumps({"user_input": "cve"})), (0, ""))


if __name__ == "__main__":
    unittest.main()
