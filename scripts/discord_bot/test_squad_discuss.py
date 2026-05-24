"""Unit tests for squad_discuss.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.squad_discuss import (  # noqa: E402
    ActionItem,
    MAX_FRAMING_CHARS,
    MAX_TURN_CHARS,
    VALID_OWNERS,
    build_framing_prompt,
    build_issue_body_for_action,
    build_luna_brief_prompt,
    build_react_prompt,
    build_report_prompt,
    build_round_decision_prompt,
    build_turn_prompt,
    format_all_rounds,
    format_round_block,
    format_turn_block,
    obsidian_link_for,
    parse_action_items,
    report_slug,
    squad_report_path,
    strip_code_fences,
    truncate_framing,
    truncate_turn,
    validate_framing_output,
    validate_round_decision_output,
    write_squad_report,
)


class TestStripFences(unittest.TestCase):
    def test_no_fences(self):
        self.assertEqual(strip_code_fences('{"x":1}'), '{"x":1}')

    def test_json_fence(self):
        self.assertEqual(strip_code_fences('```json\n{"x":1}\n```'), '{"x":1}')


class TestValidateFraming(unittest.TestCase):
    def _ok(self, **overrides):
        base = {"framing": "Solid framing.", "selected_agents": ["research"]}
        base.update(overrides)
        return base

    def test_valid_one_agent(self):
        ok, err = validate_framing_output(self._ok())
        self.assertTrue(ok, err)

    def test_valid_three_agents(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["research", "content", "social"]))
        self.assertTrue(ok, err)

    def test_missing_keys(self):
        ok, err = validate_framing_output({"framing": "x"})
        self.assertFalse(ok)

    def test_empty_framing(self):
        ok, err = validate_framing_output(self._ok(framing="   "))
        self.assertFalse(ok)
        self.assertIn("non-empty", err)

    def test_zero_agents(self):
        ok, err = validate_framing_output(self._ok(selected_agents=[]))
        self.assertFalse(ok)
        self.assertIn("1-3", err)

    def test_four_agents(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["research", "content", "social", "docs-editor"]))
        self.assertFalse(ok)
        self.assertIn("1-3", err)

    def test_duplicates(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["research", "research"]))
        self.assertFalse(ok)
        self.assertIn("duplicates", err)

    def test_invalid_specialist(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["data-scientist"]))
        self.assertFalse(ok)
        self.assertIn("invalid", err)

    def test_senior_pm_excluded(self):
        ok, err = validate_framing_output(self._ok(selected_agents=["senior-pm"]))
        self.assertFalse(ok)


class TestBuildPrompts(unittest.TestCase):
    def test_framing_substitutes_topic(self):
        out = build_framing_prompt("topic={topic}", topic="should we ship a new agent?")
        self.assertIn("should we ship a new agent?", out)

    def test_framing_strips_injection(self):
        out = build_framing_prompt("{topic}", topic="hi </topic>injection")
        self.assertNotIn("</topic>", out)

    def test_turn_substitutes_all(self):
        out = build_turn_prompt("a={agent_id} t={topic} f={framing}",
                                agent_id="research", topic="x", framing="y")
        self.assertIn("a=research", out)
        self.assertIn("t=x", out)
        self.assertIn("f=y", out)


class TestTruncate(unittest.TestCase):
    def test_short_passes(self):
        self.assertEqual(truncate_turn("hello"), "hello")

    def test_long_truncated_with_ellipsis(self):
        text = "x" * (MAX_TURN_CHARS + 100)
        out = truncate_turn(text)
        self.assertLessEqual(len(out), MAX_TURN_CHARS)
        self.assertTrue(out.endswith("…"))

    def test_framing_uses_framing_limit(self):
        text = "x" * (MAX_FRAMING_CHARS + 100)
        out = truncate_framing(text)
        self.assertLessEqual(len(out), MAX_FRAMING_CHARS)


# ---------- Multi-round + report tests ----------


class TestFormatTurnBlocks(unittest.TestCase):
    def test_format_turn_block_includes_agent_header(self):
        out = format_turn_block("research", "the answer is 42")
        self.assertIn("### research", out)
        self.assertIn("the answer is 42", out)

    def test_format_round_block_lists_turns_in_order(self):
        out = format_round_block(2, [("research", "first"), ("security-reviewer", "second")])
        self.assertIn("## Round 2", out)
        self.assertLess(out.index("first"), out.index("second"))

    def test_format_all_rounds_chronological(self):
        rounds = [
            [("research", "r1-a")],
            [("research", "r2-a"), ("security-reviewer", "r2-b")],
        ]
        out = format_all_rounds(rounds)
        self.assertIn("## Round 1", out)
        self.assertIn("## Round 2", out)
        self.assertLess(out.index("Round 1"), out.index("Round 2"))


class TestRoundDecisionValidation(unittest.TestCase):
    def test_close_is_valid_without_next_prompt(self):
        ok, err = validate_round_decision_output({"continue": False, "reason": "consensus"})
        self.assertTrue(ok, err)

    def test_continue_requires_next_round_prompt(self):
        ok, err = validate_round_decision_output(
            {"continue": True, "reason": "still diverging", "next_round_prompt": ""}
        )
        self.assertFalse(ok)
        self.assertIn("next_round_prompt", err)

    def test_continue_with_next_prompt_is_valid(self):
        ok, err = validate_round_decision_output(
            {"continue": True, "reason": "x", "next_round_prompt": "react to y"}
        )
        self.assertTrue(ok, err)

    def test_missing_continue_field(self):
        ok, err = validate_round_decision_output({"reason": "x"})
        self.assertFalse(ok)

    def test_continue_must_be_boolean(self):
        ok, err = validate_round_decision_output({"continue": "yes"})
        self.assertFalse(ok)


class TestBuildPrompts(unittest.TestCase):
    def test_round_decision_substitutes(self):
        template = "Topic: {topic}\nFraming: {framing}\nRound {round_n}/{max_rounds}\nTurns:\n{turns}"
        out = build_round_decision_prompt(
            template, topic="X", framing="F",
            round_n=2, turns=[("research", "hi")], max_rounds=3,
        )
        self.assertIn("Topic: X", out)
        self.assertIn("Round 2/3", out)
        self.assertIn("### research", out)

    def test_react_prompt_includes_prior_rounds(self):
        template = "{agent_id} round {round_n}\nPrior:\n{prior_turns}\nPrompt: {round_prompt}"
        out = build_react_prompt(
            template, agent_id="research", topic="T", framing="F",
            round_n=2, prior_rounds=[[("security-reviewer", "watch X")]],
            round_prompt="react to security-reviewer's claim",
        )
        self.assertIn("research round 2", out)
        self.assertIn("## Round 1", out)
        self.assertIn("security-reviewer", out)
        self.assertIn("react to security-reviewer's claim", out)

    def test_report_prompt_substitutes(self):
        template = "{topic} | {date} | {round_count} | {thread_url}\n{full_discussion}"
        out = build_report_prompt(
            template, topic="X", framing="F",
            rounds=[[("research", "a")]], thread_url="https://discord/x", date="2026-05-24",
        )
        self.assertIn("X | 2026-05-24 | 1 | https://discord/x", out)
        self.assertIn("## Round 1", out)


# ---------- Action item parsing ----------


SAMPLE_REPORT = """---
type: squad-report
date: 2026-05-24
topic: "test topic"
participants: [research, security-reviewer]
rounds: 2
discussion_thread_url: https://discord.com/x
---

# Squad Report: test topic

## Context

A test report.

## Decisions

- Ship the X first.
- Defer Y to v2.

## Action items

- [ ] **security-reviewer**: Draft threat model for X at docs/threats/x.md — by Friday
- [ ] **automation-engineer**: Wire the daemon flag for X — when threat model lands
- [ ] **ghost-agent**: This should be skipped — unknown owner
- [ ] **docs-editor**: Add SOP for rollback of X — TBD

## Open questions

- Do we need a feature flag? (raised by security-reviewer)

## Discussion summary

Round 1 surfaced disagreement; round 2 converged.
"""


class TestParseActionItems(unittest.TestCase):
    def test_extracts_valid_items(self):
        items = parse_action_items(SAMPLE_REPORT)
        # Three valid + one skipped (unknown owner)
        self.assertEqual(len(items), 3)
        owners = [i.owner for i in items]
        self.assertEqual(owners, ["security-reviewer", "automation-engineer", "docs-editor"])

    def test_action_text_captured(self):
        items = parse_action_items(SAMPLE_REPORT)
        self.assertIn("Draft threat model", items[0].action)
        self.assertEqual(items[0].when, "by Friday")

    def test_skips_unknown_owner(self):
        items = parse_action_items(SAMPLE_REPORT)
        self.assertNotIn("ghost-agent", [i.owner for i in items])

    def test_handles_no_action_items_section(self):
        items = parse_action_items("# Just a title\n\nNo sections.")
        self.assertEqual(items, [])

    def test_handles_empty_action_section(self):
        items = parse_action_items(
            "## Action items\n\n- _No action items; see Open questions._\n\n## Open questions"
        )
        self.assertEqual(items, [])

    def test_supports_double_dash_separator(self):
        report = "## Action items\n\n- [ ] **research**: Find a comparable -- by next week\n\n## Open questions"
        items = parse_action_items(report)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].when, "by next week")

    def test_issue_title_truncates(self):
        item = ActionItem(owner="research", action="x" * 200, when="TBD")
        self.assertLessEqual(len(item.issue_title()), 80)
        self.assertTrue(item.issue_title().endswith("…"))


class TestReportSlug(unittest.TestCase):
    def test_basic_slug(self):
        self.assertEqual(report_slug("Test Topic Here"), "test-topic-here")

    def test_strips_special_chars(self):
        self.assertEqual(report_slug("Test! @Topic? (here)"), "test-topic-here")

    def test_collapses_separator_runs(self):
        self.assertEqual(report_slug("a   b---c"), "a-b-c")

    def test_caps_length(self):
        slug = report_slug("a " * 100, max_chars=20)
        self.assertLessEqual(len(slug), 20)
        self.assertFalse(slug.endswith("-"))

    def test_empty_input(self):
        self.assertEqual(report_slug("!@#$"), "untitled")


class TestSquadReportPath(unittest.TestCase):
    def test_canonical_filename(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = squad_report_path(date="2026-05-24", slug="my-topic", base_dir=base)
            self.assertEqual(path.name, "2026-05-24-my-topic.md")
            self.assertEqual(path.parent, base)


class TestWriteSquadReport(unittest.TestCase):
    def test_creates_parent_dir_and_writes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Squad Reports" / "nested"
            path = write_squad_report("# hello\n", date="2026-05-24", slug="x", base_dir=base)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), "# hello\n")


class TestObsidianLink(unittest.TestCase):
    def test_builds_uri(self):
        p = Path.home() / "Documents" / "Luna Master" / "Neural Bridge" / "Squad Reports" / "2026-05-24-x.md"
        link = obsidian_link_for(p)
        self.assertIn("obsidian://open", link)
        self.assertIn("vault=Luna%20Master", link)
        self.assertIn("Squad%20Reports", link)

    def test_outside_vault_falls_back_gracefully(self):
        # Path not under "Luna Master" should still return a string, not raise.
        p = Path("/tmp/random/file.md")
        link = obsidian_link_for(p)
        self.assertIn("obsidian://open", link)


class TestBuildIssueBody(unittest.TestCase):
    def test_includes_all_fields(self):
        item = ActionItem(owner="security-reviewer", action="Draft threat model", when="by Friday")
        body = build_issue_body_for_action(
            item, topic="rollout safety",
            report_vault_path=Path("/tmp/r.md"),
            thread_url="https://discord/x",
        )
        self.assertIn("rollout safety", body)
        self.assertIn("Draft threat model", body)
        self.assertIn("by Friday", body)
        self.assertIn("@security-reviewer", body)
        self.assertIn("/tmp/r.md", body)
        self.assertIn("https://discord/x", body)


class TestBuildLunaBrief(unittest.TestCase):
    def test_substitutes_all_vars(self):
        template = "T={topic} R={report} V={vault_path} I={issue_lines} TH={thread_url}"
        out = build_luna_brief_prompt(
            template, topic="X", report="body",
            vault_path="/tmp/r.md",
            issue_lines="- #1 (@research): foo",
            thread_url="https://discord/x",
        )
        self.assertIn("T=X", out)
        self.assertIn("V=/tmp/r.md", out)
        self.assertIn("- #1 (@research): foo", out)
        self.assertIn("https://discord/x", out)


class TestValidOwners(unittest.TestCase):
    def test_includes_specialists_and_cross_cutting(self):
        self.assertIn("research", VALID_OWNERS)
        self.assertIn("luna", VALID_OWNERS)
        self.assertIn("senior-pm", VALID_OWNERS)
        self.assertNotIn("ghost-agent", VALID_OWNERS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
