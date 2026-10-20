"""The fleet's usage log (hooks/claude_usage.py, 2026-10-20).

The JSON shapes below are the ones Claude Code 2.1.304 printed for a success,
a turn cap, a spend cap and an unknown model; a missing session printed
nothing on stdout.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(HOOKS.parent))

import claude_usage as cu  # noqa: E402

USAGE = {"claude-haiku-4-5": {"inputTokens": 10, "outputTokens": 81, "cacheReadInputTokens": 8761,
                              "cacheCreationInputTokens": 20, "costUSD": 0.0012911}}


def cc(**fields) -> str:
    return json.dumps({"type": "result", "session_id": "s-1", "num_turns": 1,
                       "total_cost_usd": 0.0012911, "modelUsage": USAGE, **fields})


class TestRunResult(unittest.TestCase):
    def test_success_returns_the_reply_text(self):
        r = cu.run_result(0, cc(subtype="success", is_error=False, result="OK"), "")
        self.assertEqual((r.ok, r.text, r.error), (True, "OK", ""))

    def test_caps_name_themselves(self):
        for subtype in ("error_max_turns", "error_max_budget_usd"):
            r = cu.run_result(1, cc(subtype=subtype, is_error=True), "warning")
            self.assertEqual((r.ok, r.error), (False, f"exit_1:{subtype}"))

    def test_api_errors_keep_the_api_line(self):
        r = cu.run_result(1, cc(subtype="success", is_error=True,
                                result='API Error: 400 {"error":"bad"}'), "connectors warning")
        self.assertTrue(r.error.startswith("exit_1:API Error: 400"), r.error)

    def test_an_error_flag_on_exit_zero_still_fails(self):
        self.assertFalse(cu.run_result(0, cc(subtype="success", is_error=True, result="x"), "").ok)

    def test_a_missing_session_prints_only_to_stderr(self):
        r = cu.run_result(1, "", "No conversation found with session ID: 1234")
        self.assertEqual(r.error, "exit_1:No conversation found with session ID: 1234")
        self.assertEqual(r.meta, {})

    def test_plain_text_and_json_replies_pass_through(self):
        self.assertEqual(cu.run_result(0, "plain reply", "").text, "plain reply")
        reply = '{"decisions": []}'
        self.assertEqual(cu.run_result(0, reply, "").text, reply, "a reply that is JSON is not the result object")


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.when = datetime(2026, 10, 20, 19, 30, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        return [json.loads(l) for l in (self.dir / "claude-2026-10.jsonl").read_text().splitlines()]

    def test_one_line_per_call_without_prompt_or_reply(self):
        result = cu.run_result(0, cc(subtype="success", is_error=False, result="SECRET REPLY"), "")
        cu.record(job="luna_checkin", agent="luna", route="api", model="claude-opus-5", result=result,
                  seconds=2.04, usage_dir=self.dir, now=self.when)
        [line] = self.lines()
        self.assertEqual(line["job"], "luna_checkin")
        self.assertEqual(line["route"], "api")
        self.assertTrue(line["ok"])
        self.assertEqual(line["cost_usd"], 0.001291)
        self.assertEqual(line["tokens"], {"input": 10, "output": 81, "cache_read": 8761, "cache_write": 20})
        self.assertEqual(line["models"], {"claude-haiku-4-5": 0.001291})
        self.assertEqual(line["session_id"], "s-1")
        self.assertNotIn("SECRET", json.dumps(line))

    def test_a_timeout_has_unknown_cost(self):
        cu.record(job="agent_turn", agent="research", route="api", model="m", result=None, seconds=600,
                  error="timeout", max_budget_usd=5.0, max_turns=60, usage_dir=self.dir, now=self.when)
        [line] = self.lines()
        self.assertEqual((line["ok"], line["error"], line["cost_usd"]), (False, "timeout", None))
        self.assertEqual((line["max_budget_usd"], line["max_turns"]), (5.0, 60))

    def test_an_unwritable_log_never_raises(self):
        blocker = self.dir / "file"
        blocker.write_text("x")
        cu.record(job="j", agent="a", route="api", model="m", result=None, seconds=1,
                  usage_dir=blocker / "sub", now=self.when)

    def test_months_get_their_own_file(self):
        self.assertEqual(cu.path_for(self.when, self.dir).name, "claude-2026-10.jsonl")


class TestRoute(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(cu.route_of({"ANTHROPIC_API_KEY": "sk-ant-x"}), "api")
        self.assertEqual(cu.route_of({"ANTHROPIC_BASE_URL": "http://127.0.0.1:4142",
                                      "ANTHROPIC_API_KEY": "copilot-proxy"}), "proxy")
        self.assertEqual(cu.route_of({}), "login")
        self.assertEqual(cu.route_of({"ANTHROPIC_API_KEY": "copilot-proxy"}), "login")


class TestCallers(unittest.TestCase):
    """Each fleet path writes its line under its own job name."""

    def fake(self, stdout):
        return mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""))

    def logged(self):
        return [json.loads(l) for f in sorted(cu.USAGE_DIR.glob("*.jsonl")) for l in f.read_text().splitlines()]

    def test_call_claude_sync(self):
        from scripts.discord_bot import claude_invoke
        with mock.patch.object(claude_invoke.subprocess, "run",
                               self.fake(cc(subtype="success", is_error=False, result="hello"))) as run:
            ok, text, err = claude_invoke.call_claude_sync("hi", job="luna_checkin", agent_id="luna",
                                                           no_tools=True)
        self.assertEqual((ok, text, err), (True, "hello", ""))
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--output-format") + 1], "json")
        [line] = self.logged()
        self.assertEqual((line["job"], line["agent"], line["ok"]), ("luna_checkin", "luna", True))

    def test_unnamed_callers_are_labelled_by_agent(self):
        from scripts.discord_bot import claude_invoke
        with mock.patch.object(claude_invoke.subprocess, "run", self.fake("plain")):
            claude_invoke.call_claude_sync("hi", telemetry_agent_id="senior-pm")
        self.assertEqual(self.logged()[0]["job"], "other:senior-pm")

    def test_a_cap_is_an_error_the_caller_sees(self):
        from scripts.discord_bot import claude_invoke
        failed = subprocess.CompletedProcess([], 1, stdout=cc(subtype="error_max_budget_usd", is_error=True),
                                             stderr="")
        with mock.patch.object(claude_invoke.subprocess, "run", return_value=failed):
            ok, _, err = claude_invoke.call_claude_sync("hi", job="agent_turn", max_budget_usd=5.0)
        self.assertEqual((ok, err), (False, "exit_1:error_max_budget_usd"))
        self.assertEqual(self.logged()[0]["max_budget_usd"], 5.0)

    def test_a_timeout_is_logged(self):
        from scripts.discord_bot import claude_invoke
        with mock.patch.object(claude_invoke.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["claude"], 5)):
            self.assertEqual(claude_invoke.call_claude_sync("hi", job="agent_turn")[2], "timeout")
        self.assertEqual(self.logged()[0]["error"], "timeout")

    def test_flush(self):
        import flush
        with mock.patch.object(flush.subprocess, "run",
                               self.fake(cc(subtype="success", is_error=False, result="{}"))):
            flush.call_claude("p", "claude-opus-4-8", 5, agent="luna")
        line = self.logged()[0]
        self.assertEqual((line["job"], line["agent"]), ("flush", "luna"))

    def test_agent_turns_carry_caps(self):
        from scripts.discord_bot import agent_runtime
        self.assertEqual(agent_runtime.TURN_LIMITS,
                         {"job": "agent_turn", "max_turns": 60, "max_budget_usd": 5.0})
        text = (HOOKS.parent / "scripts/discord_bot/agent_runtime.py").read_text()
        self.assertEqual(text.count("**TURN_LIMITS,"), 3, "the first call, the model fallback and the retry")

    def test_every_named_job(self):
        root = HOOKS.parent
        for rel, job in (("scripts/telegram_bot/luna_checkin.py", "luna_checkin"),
                         ("scripts/echo/synthesize_profile.py", "echo_synthesis"),
                         ("scripts/publish/prepare_week.py", "publish_prep"),
                         ("scripts/telegram_bot/council_bridge.py", "council_router"),
                         ("scripts/compile.py", "compile"), ("scripts/lint.py", "lint"),
                         ("scripts/loop_engineer/executor.py", "loop_engineer")):
            with self.subTest(rel=rel):
                self.assertIn(f'job="{job}"' if "executor" not in rel else f'"job": "{job}"',
                              (root / rel).read_text())


if __name__ == "__main__":
    unittest.main()
