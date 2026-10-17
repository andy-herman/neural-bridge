"""The fleet's first token-cost fixes on the API key (2026-10-17):

- templates named their own placeholders in their header line, so the code's
  str.replace filled the header too and every flush sent its transcript twice;
- stateless jobs (check-ins, publish prep, Echo, lint, the loop engineer)
  spawned a flush nothing reads after every run;
- agentic runs had no turn or spend cap.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "hooks"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

PLACEHOLDER_IN_BACKTICKS = re.compile(r"`\{[a-z_]+\}`")


def templates() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*prompts/*.md"], cwd=REPO_ROOT,
                         capture_output=True, text=True).stdout.split()
    return [REPO_ROOT / f for f in out]


class TestTemplates(unittest.TestCase):
    def test_no_header_names_a_placeholder_in_braces(self):
        offenders = []
        for path in templates():
            lines = path.read_text(encoding="utf-8").split("\n")
            end = next((i for i, line in enumerate(lines) if line.strip() == "---"), None)
            if end is None:
                continue  # no header block: everything is prompt
            offenders += [f"{path.relative_to(REPO_ROOT)}:{i + 1}" for i in range(end)
                          if PLACEHOLDER_IN_BACKTICKS.search(lines[i])]
        self.assertEqual(offenders, [], "a header that names {x} gets x substituted into it too")

    def test_the_expensive_slots_appear_once(self):
        once = {
            "hooks/prompts/flush_v1.md": ["{transcript}"],
            "scripts/discord_bot/prompts/mention_v1.md": ["{agent_definition}", "{discord_history}", "{message}"],
            "scripts/publish/prompts/linkedin_v1.md": ["{voice_corpus}", "{blog_body}"],
            "scripts/prompts/filing_gate_v1.md": ["{session_excerpt}"],
            "scripts/prompts/concept_writer_v1.md": ["{session_excerpt}"],
        }
        for rel, slots in once.items():
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for slot in slots:
                with self.subTest(template=rel, slot=slot):
                    self.assertEqual(text.count(slot), 1)

    def test_a_flush_prompt_carries_the_transcript_once(self):
        import flush
        template = flush.PROMPT_TEMPLATE.read_text(encoding="utf-8")
        prompt = flush.build_prompt(template, "luna", "s1", "SessionEnd", "TRANSCRIPT-SENTINEL")
        self.assertEqual(prompt.count("TRANSCRIPT-SENTINEL"), 1)


class TestCallClaudeSync(unittest.TestCase):
    def run_call(self, **kwargs):
        from scripts.discord_bot import claude_invoke
        seen = {}

        def fake_run(args, **kw):
            seen["args"], seen["env"] = args, kw["env"]
            return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

        with mock.patch.object(claude_invoke.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NB_SKIP_FLUSH", None)
            ok, _, _ = claude_invoke.call_claude_sync("hi", model="claude-sonnet-5-5", **kwargs)
        self.assertTrue(ok)
        return seen

    def test_flush_false_tells_the_session_hook_to_skip(self):
        self.assertEqual(self.run_call(flush=False)["env"].get("NB_SKIP_FLUSH"), "1")
        self.assertNotIn("NB_SKIP_FLUSH", self.run_call()["env"], "agent turns still flush")

    def test_turn_and_spend_caps_reach_the_cli(self):
        args = self.run_call(max_turns=40, max_budget_usd=1.5)["args"]
        self.assertEqual(args[args.index("--max-turns") + 1], "40")
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "1.50")
        self.assertNotIn("--max-turns", self.run_call()["args"])


class TestStatelessCallersSkipFlush(unittest.TestCase):
    def test_check_in(self):
        from scripts.telegram_bot import luna_checkin
        with mock.patch.object(luna_checkin, "call_claude_sync_with_fallback",
                               return_value=(True, "x", "", "m")) as call:
            luna_checkin.generate("prompt")
        self.assertIs(call.call_args.kwargs["flush"], False)

    def test_publish_prep(self):
        from scripts.publish import prepare_week
        from datetime import date
        cand = prepare_week.Candidate(file_path=Path("x.mdx"), slug="x", title="X", description="",
                                      pub_date=date(2026, 10, 19), content_type="posts", body="b")
        with mock.patch.object(prepare_week, "call_claude_sync_with_fallback",
                               return_value=(True, "post", "", "m")) as call, \
                mock.patch.object(prepare_week, "build_linkedin_prompt", return_value="p"):
            prepare_week.generate_linkedin_variant(cand, dry_run=False)
        self.assertIs(call.call_args.kwargs["flush"], False)

    def test_echo_synthesis(self):
        from scripts.echo import synthesize_profile
        with mock.patch.object(synthesize_profile, "call_claude_sync", return_value=(True, "x", "")) as call:
            synthesize_profile.invoke_synthesis("p")
        self.assertIs(call.call_args.kwargs["flush"], False)

    def test_lint_and_the_loop_engineer_set_the_skip_in_their_env(self):
        for rel in ("scripts/lint.py", "scripts/loop_engineer/executor.py"):
            with self.subTest(rel=rel):
                self.assertIn('"NB_SKIP_FLUSH": "1"', (REPO_ROOT / rel).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
