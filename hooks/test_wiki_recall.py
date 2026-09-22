"""Unit tests for hooks/wiki_recall.py, the read side of the wiki."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))

import wiki_recall as wr  # noqa: E402

CVE = """---
slug: cve-cwe-owasp-hierarchy
verdict: PROMOTE
---

# cve-cwe-owasp-hierarchy

> The CVE/CWE/OWASP hierarchy maps vulnerability instances to weakness classes to risk categories.

## Why this matters

Students confuse a CVE identifier with a CWE weakness class. OWASP Top Ten sits above both.
"""

SUBNET = """---
slug: subnet-math-cheat-sheet-for-midterm
verdict: PROMOTE
---

# subnet-math-cheat-sheet-for-midterm

> Subnet math for the midterm: CIDR prefix to host count, usable addresses, and broadcast.

Work the prefix length first. A /24 has 254 usable hosts. Practice with CIDR notation.
"""

CSP = """# csp-with-nonces-not-unsafe-inline

Content Security Policy with per-request nonces beats unsafe-inline for script control.
"""


class _Corpus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "cve-cwe-owasp-hierarchy.md").write_text(CVE, encoding="utf-8")
        (self.dir / "subnet-math-cheat-sheet-for-midterm.md").write_text(SUBNET, encoding="utf-8")
        (self.dir / "csp-with-nonces-not-unsafe-inline.md").write_text(CSP, encoding="utf-8")
        (self.dir / "_scratch.md").write_text("ignored", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()


class TestTokenize(unittest.TestCase):
    def test_lowercases_drops_stopwords_and_short_tokens(self):
        self.assertEqual(wr.tokenize("How do CVE and CWE relate to OWASP?"),
                         ["cve", "cwe", "relate", "owasp"])

    def test_keeps_identifiers_with_dashes_and_dots(self):
        self.assertIn("argon2id", wr.tokenize("use Argon2id in production"))
        self.assertIn("unsafe-inline", wr.tokenize("never use unsafe-inline"))


class TestLoad(_Corpus):
    def test_loads_concepts_and_skips_underscore_files(self):
        concepts = wr.load_concepts(self.dir)
        self.assertEqual([c.slug for c in concepts],
                         ["csp-with-nonces-not-unsafe-inline", "cve-cwe-owasp-hierarchy",
                          "subnet-math-cheat-sheet-for-midterm"])

    def test_summary_prefers_blockquote_then_first_paragraph(self):
        by_slug = {c.slug: c for c in wr.load_concepts(self.dir)}
        self.assertTrue(by_slug["cve-cwe-owasp-hierarchy"].summary.startswith("The CVE/CWE/OWASP"))
        self.assertTrue(by_slug["csp-with-nonces-not-unsafe-inline"].summary.startswith("Content Security Policy"))

    def test_missing_dir_is_empty_not_error(self):
        self.assertEqual(wr.load_concepts(self.dir / "nope"), [])


class TestRank(_Corpus):
    def test_matching_query_ranks_the_right_article_first(self):
        hits = wr.rank("how do CVE and CWE relate to OWASP", wr.load_concepts(self.dir))
        self.assertEqual(hits[0].slug, "cve-cwe-owasp-hierarchy")

    def test_unrelated_query_returns_nothing(self):
        hits = wr.rank("what is on my calendar tomorrow", wr.load_concepts(self.dir))
        self.assertEqual(hits, [])

    def test_slug_hits_outweigh_body_hits(self):
        # "midterm" is in the subnet slug; "students" only in the CVE body.
        hits = wr.rank("midterm students", wr.load_concepts(self.dir))
        self.assertEqual(hits[0].slug, "subnet-math-cheat-sheet-for-midterm")

    def test_single_body_term_is_not_evidence(self):
        # "students" appears once, in the CVE body only. One weak body term
        # must not inject an article into an unrelated turn.
        hits = wr.rank("tell the students about the schedule", wr.load_concepts(self.dir))
        self.assertEqual(hits, [])

    def test_two_body_terms_are_evidence(self):
        # "prefix" and "hosts" are both body-only terms of the subnet article.
        hits = wr.rank("prefix length and usable hosts", wr.load_concepts(self.dir))
        self.assertEqual([h.slug for h in hits], ["subnet-math-cheat-sheet-for-midterm"])

    def test_one_summary_term_is_evidence(self):
        # "broadcast" appears only in the subnet summary.
        hits = wr.rank("what about broadcast", wr.load_concepts(self.dir))
        self.assertEqual([h.slug for h in hits], ["subnet-math-cheat-sheet-for-midterm"])

    def test_top_k_and_determinism(self):
        concepts = wr.load_concepts(self.dir)
        a = wr.rank("cidr subnet cve owasp csp nonces", concepts, top_k=2)
        b = wr.rank("cidr subnet cve owasp csp nonces", concepts, top_k=2)
        self.assertEqual(len(a), 2)
        self.assertEqual([h.slug for h in a], [h.slug for h in b])

    def test_empty_query_or_corpus(self):
        self.assertEqual(wr.rank("", wr.load_concepts(self.dir)), [])
        self.assertEqual(wr.rank("cve", []), [])


class TestRender(_Corpus):
    def test_block_carries_markers_data_framing_and_paths(self):
        hits = wr.rank("subnet math midterm", wr.load_concepts(self.dir))
        block = wr.render_block(hits)
        self.assertTrue(block.startswith(wr.BLOCK_BEGIN))
        self.assertIn(wr.BLOCK_END, block)
        self.assertIn("DATA", block)
        self.assertIn("[[subnet-math-cheat-sheet-for-midterm]]", block)
        self.assertTrue(block.endswith("\n\n"))

    def test_no_hits_renders_nothing(self):
        self.assertEqual(wr.render_block([]), "")

    def test_budget_is_respected(self):
        hits = wr.rank("cidr subnet cve owasp csp nonces", wr.load_concepts(self.dir))
        block = wr.render_block(hits, budget_chars=220)
        self.assertLessEqual(len(block), 220 + 2)
        self.assertIn(wr.BLOCK_END, block)


class TestRecall(_Corpus):
    def test_recall_records_ok_with_chars_on_hit(self):
        with patch.object(wr, "_record") as rec:
            block, hits = wr.recall("cve cwe owasp", agent_id="research", concepts_dir=self.dir)
        self.assertTrue(block)
        self.assertEqual(hits[0].slug, "cve-cwe-owasp-hierarchy")
        rec.assert_called_once()
        self.assertTrue(rec.call_args.kwargs["ok"])
        self.assertEqual(rec.call_args.kwargs["chars"], len(block))

    def test_recall_records_ok_with_zero_chars_on_miss(self):
        # A miss is not a failure of the read path; the grounding report,
        # not the canary, is where "the wiki was irrelevant" shows up.
        with patch.object(wr, "_record") as rec:
            block, hits = wr.recall("calendar tomorrow", agent_id="luna", concepts_dir=self.dir)
        self.assertEqual(block, "")
        self.assertEqual(hits, [])
        self.assertTrue(rec.call_args.kwargs["ok"])
        self.assertEqual(rec.call_args.kwargs["chars"], 0)

    def test_recall_records_failure_when_corpus_missing(self):
        with patch.object(wr, "_record") as rec:
            block, hits = wr.recall("cve", concepts_dir=self.dir / "absent")
        self.assertEqual((block, hits), ("", []))
        self.assertFalse(rec.call_args.kwargs["ok"])

    def test_recall_never_raises(self):
        with patch.object(wr, "load_concepts", side_effect=RuntimeError("disk")), \
             patch.object(wr, "_record") as rec:
            self.assertEqual(wr.recall("cve", concepts_dir=self.dir), ("", []))
        self.assertFalse(rec.call_args.kwargs["ok"])

    def test_record_false_skips_telemetry(self):
        with patch.object(wr, "_record") as rec:
            wr.recall("cve", concepts_dir=self.dir, record=False)
        rec.assert_not_called()

    def test_real_corpus_smoke(self):
        # The checked-in wiki must be loadable and a known article findable.
        block, hits = wr.recall("CVE CWE OWASP hierarchy", record=False)
        self.assertTrue(any(h.slug == "cve-cwe-owasp-hierarchy" for h in hits))


class TestSkip(unittest.TestCase):
    def test_env_flag(self):
        self.assertTrue(wr.should_skip({wr.ENV_SKIP: "1"}))
        self.assertFalse(wr.should_skip({}))
        with patch.dict(os.environ, {wr.ENV_SKIP: "1"}):
            self.assertTrue(wr.should_skip())


class TestGroundingReport(unittest.TestCase):
    def _ev(self, agent, chars, ok=True, store="wiki_recall", stage="utilize"):
        return {"store": store, "stage": stage, "agent_id": agent, "ok": ok, "chars": chars}

    def test_counts_grounded_turns_per_agent(self):
        events = [
            self._ev("research", 400), self._ev("research", 0),
            self._ev("luna", 0), self._ev("luna", 0, ok=False),
            self._ev("x", 9, store="luna_notes"),  # other store, ignored
        ]
        s = wr.grounding_summary(events)
        self.assertEqual((s["turns"], s["grounded"]), (4, 1))
        self.assertEqual(s["by_agent"]["research"], [1, 2])
        self.assertEqual(s["by_agent"]["luna"], [0, 2])
        text = wr.format_grounding(s, 7)
        self.assertIn("1 of 4 agent turns", text)
        self.assertIn("research=1/2", text)

    def test_no_events_says_so(self):
        self.assertIn("no agent turns ran retrieval", wr.format_grounding(wr.grounding_summary([]), 7))


class TestCli(_Corpus):
    def test_query_prints_block_or_marker(self):
        with patch.object(wr, "CONCEPTS_DIR", self.dir):
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = wr.main(["what is on my calendar"])
        self.assertEqual(rc, 0)
        self.assertIn("no concept matched", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
