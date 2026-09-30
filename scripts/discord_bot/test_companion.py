"""Structural companion contracts, not generated-conversation quality tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scripts.discord_bot import agent_runtime as ar, companion, mention

ROOT = Path(__file__).resolve().parents[2]
STANDARD_TOOLS = "[Read, Write, Edit, Glob, Grep, WebSearch, WebFetch]"
EXPECTED_METADATA = {
    "automation-engineer": ("[Read, Write, Edit, Glob, Grep, Bash, WebSearch, WebFetch]", "claude-sonnet-4-6", "red"),
    "content": (STANDARD_TOOLS, "claude-sonnet-4-6", "orange"),
    "docs-editor": (STANDARD_TOOLS, "claude-sonnet-4-6", "white"),
    "echo": ("[Read, Glob, Grep, Write, Edit]", "claude-sonnet-4-6", "white"),
    "librarian": (STANDARD_TOOLS, "claude-sonnet-4-6", "magenta"),
    "loid": ("[Read, Write, Edit, Glob, Grep, Bash]", "claude-opus-4-8", "slate"),
    "luna": ("[Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Bash]", "claude-sonnet-4-6", "pink"),
    "recruiter": (STANDARD_TOOLS, "claude-sonnet-4-6", "yellow"),
    "research": ("[WebSearch, WebFetch, Read, Glob, Grep, Write]", "claude-sonnet-4-6", "blue"),
    "security-reviewer": ("[Read, Glob, Grep, Bash, WebSearch, WebFetch, Write]", "claude-sonnet-4-6", "pink"),
    "senior-pm": ("[Read, Glob, Grep, Bash, WebSearch, WebFetch, Write]", "claude-sonnet-4-6", "purple"),
    "social": (STANDARD_TOOLS, "claude-sonnet-4-6", "cyan"),
    "teaching-prep": (STANDARD_TOOLS, "claude-sonnet-4-6", "green"),
    "ux-designer": (STANDARD_TOOLS, "claude-sonnet-4-6", "cyan"),
}


def frontmatter(text: str) -> dict[str, str]:
    header = text.removeprefix("---\n").split("\n---\n", 1)[0]
    return dict(line.split(": ", 1) for line in header.splitlines())


class TestRequiredCompanionLoader(unittest.TestCase):
    def test_real_standard_is_full_body_without_metadata(self):
        text = companion.COMPANION_STANDARD_PATH.read_text(encoding="utf-8")
        body = text.split("\n---\n", 1)[1].strip()
        self.assertEqual(companion.load_companion_standard(), body)
        self.assertNotIn("description:", body)

    def test_missing_unreadable_and_invalid_contracts_are_not_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.md"
            with self.assertRaises(companion.CompanionSetupError) as raised:
                companion.load_companion_standard(missing)
            self.assertEqual(raised.exception.reason, "companion_standard_missing")
        cases = [
            (PermissionError("synthetic diagnostic"), "companion_standard_unreadable"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic"), "companion_standard_unreadable"),
            ("\n ", "companion_standard_empty"),
            ("---\nname: companion-standard\n", "companion_standard_invalid"),
            ("---\nname: companion-standard\n---\n ", "companion_standard_empty"),
        ]
        for value, reason in cases:
            with self.subTest(reason=reason, value_type=type(value).__name__):
                path = Mock(spec=Path)
                if isinstance(value, Exception):
                    path.read_text.side_effect = value
                else:
                    path.read_text.return_value = value
                with self.assertRaises(companion.CompanionSetupError) as raised:
                    companion.load_companion_standard(path)
                self.assertEqual(raised.exception.reason, reason)
                self.assertIn("No model was called", str(raised.exception))
                self.assertNotIn("synthetic diagnostic", str(raised.exception))

    def test_definition_cannot_skip_missing_standard(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(companion, "COMPANION_STANDARD_PATH", Path(tmp) / "missing.md"):
                with self.assertRaises(companion.CompanionSetupError):
                    mention.load_agent_definition("luna")


class TestNativeCompanionEntry(unittest.TestCase):
    def test_all_fourteen_preload_without_changing_tools_model_or_color(self):
        paths = list(mention.AGENTS_DIR.glob("*.md"))
        self.assertEqual({path.stem for path in paths}, set(EXPECTED_METADATA))
        for path in paths:
            with self.subTest(agent_id=path.stem):
                metadata = frontmatter(path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["skills"], f"[{companion.COMPANION_SKILL_ID}]")
                self.assertEqual(
                    tuple(metadata[key] for key in ("tools", "model", "color")),
                    EXPECTED_METADATA[path.stem],
                )
                self.assertEqual(set(metadata), {"description", "tools", "model", "color", "skills"})
        metadata = frontmatter(companion.COMPANION_STANDARD_PATH.read_text(encoding="utf-8"))
        self.assertEqual(metadata["name"], "companion-standard")
        self.assertEqual(set(metadata), {"name", "description"})
        manifest = ROOT / "plugins/neural-bridge-core/.claude-plugin/plugin.json"
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["name"], "neural-bridge-core")

    def test_recruiter_keeps_the_future_authoring_entry(self):
        recruiter = (mention.AGENTS_DIR / "recruiter.md").read_text(encoding="utf-8")
        self.assertIn(f"skills: [{companion.COMPANION_SKILL_ID}]", recruiter.split("\n---\n", 1)[1])


class TestActualCompanionPrompts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.standard = companion.load_companion_standard()
        self.context_reads = [
            self.stack.enter_context(patch.object(mention, name, return_value=""))
            for name in ("_wiki_recall_block", "_echo_voice_block", "_progress_block",
                         "_luna_notes_block", "_luna_live_state_block")
        ]
        self.context_reads.append(self.stack.enter_context(
            patch.object(mention.honcho_client, "get_peer_card_context", return_value="")
        ))
        self.stack.enter_context(patch.object(mention._mem, "record"))
        self.grants = self.stack.enter_context(patch.object(ar, "grant_for", return_value=None))
        self.store = self.stack.enter_context(patch.object(ar, "SESSION_STORE"))
        self.store.get_or_create.return_value = (SimpleNamespace(session_id="existing-session"), True)
        self.store.reset.return_value = SimpleNamespace(session_id="fresh-session")
        self.model = self.stack.enter_context(
            patch.object(ar, "call_claude", new_callable=AsyncMock, return_value=(True, "reply", ""))
        )
        self.log = Mock()

    def request(self, agent_id="luna", **kwargs):
        return ar.TurnRequest(
            agent_id=agent_id, conversation_key=24680, message_content="A synthetic request.",
            **kwargs,
        )

    def assert_full_standard(self, prompt):
        self.assertIn(f"<companion-standard>\n{self.standard}\n</companion-standard>", prompt)
        self.assertEqual(prompt.count(self.standard), 1)
        for token in ("{agent_definition}", "{transport}", "{surface_capabilities}", "{response_char_cap}"):
            self.assertNotIn(token, prompt)

    def assert_no_work_started(self):
        self.model.assert_not_awaited()
        self.grants.assert_not_called()
        self.assertEqual(self.store.mock_calls, [])
        for reader in self.context_reads:
            reader.assert_not_called()

    async def test_actual_prompts_for_all_roles_on_both_surfaces(self):
        for agent_id in EXPECTED_METADATA:
            for transport in ("discord", "telegram"):
                with self.subTest(agent_id=agent_id, transport=transport):
                    result = await ar.run_agent_turn(self.request(agent_id, transport=transport))
                    self.assertTrue(result.ok)
                    call = self.model.await_args
                    prompt = call.args[0]
                    self.assert_full_standard(prompt)
                    definition = mention.load_agent_definition(agent_id)
                    role_body = definition.split("</companion-standard>", 1)[1].strip()
                    self.assertIn(role_body, prompt)
                    self.assertIn(f"This is a {transport.title()} DM turn.", prompt)
                    self.assertIn(mention.SURFACE_CAPABILITIES[transport], prompt)
                    self.assertIn(f"ceiling: {mention.max_response_chars_for(agent_id)} characters", prompt)
                    self.assertNotIn("model", call.kwargs)
                    self.assertEqual(call.kwargs["allowed_tools"], mention.allowed_tools_for(agent_id))
                    self.assertEqual(call.kwargs["add_dirs"], mention.add_dirs_for(agent_id))
                    self.assertEqual(call.kwargs["timeout"], mention.timeout_for(agent_id))
                    self.assertEqual(call.kwargs["effort"], mention.effort_for(agent_id))
                    self.grants.assert_called_with(agent_id, False)
                    self.store.get_or_create.assert_called_with(24680, agent_id)

    async def test_resumed_turn_keeps_key_grants_and_full_prompt(self):
        self.store.get_or_create.return_value = (SimpleNamespace(session_id="existing-session"), False)
        result = await ar.run_agent_turn(self.request(transport="telegram"))
        self.assertTrue(result.ok)
        self.assert_full_standard(self.model.await_args.args[0])
        self.assertTrue(self.model.await_args.kwargs["resume"])
        self.assertEqual(self.model.await_args.kwargs["session_id"], "existing-session")
        self.store.touch.assert_called_once_with(24680, "luna")
        self.store.reset.assert_not_called()

    async def test_resume_failure_retries_once_with_identical_contract_and_settings(self):
        self.store.get_or_create.return_value = (SimpleNamespace(session_id="existing-session"), False)
        self.model.side_effect = [(False, "", "synthetic resume failure"), (True, "reply", "")]
        result = await ar.run_agent_turn(self.request(transport="telegram"))
        self.assertTrue(result.ok)
        self.assertTrue(result.resume_retried)
        first, second = self.model.await_args_list
        self.assert_full_standard(first.args[0])
        self.assertEqual(first.args, second.args)
        self.assertTrue(first.kwargs["resume"])
        self.assertFalse(second.kwargs["resume"])
        self.assertEqual(second.kwargs["session_id"], "fresh-session")
        for key in ("allowed_tools", "add_dirs", "timeout", "effort", "mcp_config"):
            self.assertEqual(first.kwargs[key], second.kwargs[key])
        self.store.reset.assert_called_once_with(24680, "luna")

    async def test_stateless_keeps_pin_and_skips_store_and_retry(self):
        for ok in (True, False):
            with self.subTest(ok=ok):
                self.model.reset_mock()
                self.model.return_value = (ok, "reply" if ok else "", "" if ok else "synthetic failure")
                result = await ar.run_agent_turn(self.request(
                    "loid", transport="telegram", stateless=True, model="existing-model-pin",
                    channel_kind="COUNCIL (shared Telegram room with Andy and Yor)",
                ))
                self.assertEqual(result.ok, ok)
                self.assertFalse(result.resume_retried)
                self.assert_full_standard(self.model.await_args.args[0])
                self.assertEqual(self.model.await_args.kwargs["model"], "existing-model-pin")
                self.assertFalse(self.model.await_args.kwargs["resume"])
                self.model.assert_awaited_once()
                self.assertEqual(self.store.mock_calls, [])

    async def test_fresh_failure_does_not_retry(self):
        self.model.return_value = (False, "", "synthetic fresh failure")
        result = await ar.run_agent_turn(self.request())
        self.assertFalse(result.ok)
        self.assertFalse(result.resume_retried)
        self.assert_full_standard(self.model.await_args.args[0])
        self.model.assert_awaited_once()
        self.store.reset.assert_not_called()
        self.store.touch.assert_not_called()

    async def test_prefix_and_cap_override_still_apply(self):
        self.model.return_value = (True, "x" * 50, "")
        result = await ar.run_agent_turn(self.request(prompt_prefix="EXISTING PREFIX\n", max_response_chars=17))
        prompt = self.model.await_args.args[0]
        self.assertTrue(prompt.startswith("EXISTING PREFIX\n"))
        self.assert_full_standard(prompt)
        self.assertIn("ceiling: 17 characters", prompt)
        self.assertEqual(len(result.response), 17)

    async def test_template_setup_failures_use_safe_user_visible_diagnostics(self):
        cases = [
            (None, "prompt_template_missing"),
            (PermissionError("synthetic diagnostic"), "prompt_template_unreadable"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic diagnostic"), "prompt_template_unreadable"),
        ]
        for error, reason in cases:
            with self.subTest(reason=reason, error_type=type(error).__name__):
                path = Mock(spec=Path)
                path.exists.return_value = error is not None
                path.read_text.side_effect = error
                with patch.object(ar, "MENTION_PROMPT_PATH", path):
                    result = await ar.run_agent_turn(self.request(transport="telegram"), log=self.log)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_reason, reason)
                self.assertIn("No model was called", result.setup_error)
                self.assertNotIn("synthetic diagnostic", result.setup_error)
                self.assertIn(reason, self.log.call_args.args[0])
                self.assertNotIn("synthetic diagnostic", self.log.call_args.args[0])
                if error is not None:
                    self.assertIn(type(error).__name__, result.setup_error)
                else:
                    path.read_text.assert_not_called()
                self.assert_no_work_started()

    async def test_required_contract_failures_are_logged_before_any_work(self):
        cases = [
            (FileNotFoundError("synthetic missing file"), "companion_standard_missing"),
            (PermissionError("synthetic diagnostic"), "companion_standard_unreadable"),
            (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "synthetic"), "companion_standard_unreadable"),
            (" \n", "companion_standard_empty"),
            ("---\nname: companion-standard\n", "companion_standard_invalid"),
        ]
        for value, reason in cases:
            with self.subTest(reason=reason):
                path = Mock(spec=Path)
                if isinstance(value, Exception):
                    path.read_text.side_effect = value
                else:
                    path.read_text.return_value = value
                with patch.object(companion, "COMPANION_STANDARD_PATH", path):
                    result = await ar.run_agent_turn(self.request(transport="telegram"), log=self.log)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_reason, reason)
                self.assertIn("No model was called", result.setup_error)
                self.assertNotIn("synthetic diagnostic", result.setup_error)
                self.assert_no_work_started()
                self.log.assert_called_with(f"TURN setup FAILED: agent=luna err={reason}")

    async def test_role_read_failure_is_safe_and_does_not_call_model(self):
        with patch.object(ar, "load_agent_definition", side_effect=PermissionError("synthetic diagnostic")):
            result = await ar.run_agent_turn(self.request(), log=self.log)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_reason, "agent_definition_unreadable")
        self.assertIn("PermissionError", result.setup_error)
        self.assertNotIn("synthetic diagnostic", result.setup_error)
        self.assert_no_work_started()
        self.assertIn("agent_definition_unreadable", self.log.call_args.args[0])

    async def test_unsupported_transport_is_setup_failure_not_a_dictionary_error(self):
        result = await ar.run_agent_turn(self.request(transport="unsupported"), log=self.log)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_reason, "prompt_transport_invalid")
        self.assertIn("No model was called", result.setup_error)
        self.assert_no_work_started()

    def test_prompt_builder_rejects_unknown_surface_before_context_reads(self):
        with self.assertRaisesRegex(ValueError, "Unsupported conversation transport"):
            mention.build_mention_prompt(
                "{message}", agent_id="luna", agent_definition="definition",
                channel_kind="DM", history=[], message_content="hello", transport="unsupported",
            )
        self.assert_no_work_started()


class TestCompanionDesignCases(unittest.TestCase):
    def test_synthetic_examples_trace_to_authored_clauses_not_model_runs(self):
        path = Path(__file__).parent / "fixtures/companion_design_cases.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["test_kind"], "synthetic-behavioral-design")
        self.assertFalse(fixture["model_outputs_measured"])
        self.assertIn("not empirical", fixture["scope"])
        standard = companion.load_companion_standard()
        seen = set()
        categories = set()
        positive_roles = set()
        for case in fixture["cases"]:
            with self.subTest(case_id=case["id"]):
                self.assertNotIn(case["id"], seen)
                seen.add(case["id"])
                categories.add(case["category"])
                self.assertIn(case["agent_id"], EXPECTED_METADATA)
                self.assertTrue(case["expected_behavior"])
                self.assertTrue(case["failure_signals"])
                self.assertTrue(case["illustrative_reply"])
                role = (mention.AGENTS_DIR / f"{case['agent_id']}.md").read_text(encoding="utf-8")
                for source, anchor in case["anchors"]:
                    self.assertIn(anchor, standard if source == "standard" else role)
                if case["category"] == "positive-role":
                    positive_roles.add(case["agent_id"])
                if case["agent_id"] in ("loid", "teaching-prep"):
                    self.assertNotIn("\u2014", case["illustrative_reply"])
                if case["agent_id"] == "loid":
                    self.assertIn("대표님", case["illustrative_reply"])
        self.assertEqual(positive_roles, {"luna", "loid", "teaching-prep"})
        self.assertTrue({
            "greeting", "overwhelm", "disagreement", "unknown-context", "contradictory-context",
            "correction", "no-bookkeeping", "ending", "unavailable-handoff",
            "unsupported-action", "uncertain-source", "course-boundary", "positive-role",
        }.issubset(categories))


if __name__ == "__main__":
    unittest.main()
