"""Unit tests for compile.py.

Stdlib-only. No real `claude -p` calls — subprocess is mocked.
Run: `python3 scripts/test_compile.py`
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import compile as cmp  # noqa: E402


SAMPLE_DAILY_LOG = """---
type: daily-log
agent: research
date: 2026-05-09
schema_version: "1.0"
session_count: 2
last_flushed_at: 2026-05-09T08:01:23Z
---

## Session 1 — 08:01 UTC

```yaml
session_id: smoke-1234567890
transcript_path: /tmp/transcript.jsonl
transcript_sha256: e8362336edb37eabde1116de83cdc8dc97489c2b14252ec7a0c55cabb9171ec4
started_at: 2026-05-09T08:01:17Z
ended_at: 2026-05-09T08:01:23Z
flush_version: "1.0"
hook_event: SessionEnd
```

### Decisions

- compile.py --dry-run flag defaults to True for the first two weeks
- Dry-run writes to docs/compile/<date>.md instead of knowledge/concepts/

### Findings

- (none)

### Open questions

- Should we publish a blog post about the filing gate design?

### Proposed concepts

- filing-gate-quarantine-vs-reject: Distinction between quarantine and reject outcomes
- dry-run-output-format: Provenance frontmatter even in dry-run mode for audit value

---

## Session 2 — 09:15 UTC

```yaml
session_id: sess-9999
transcript_path: /tmp/another.jsonl
transcript_sha256: ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
started_at: 2026-05-09T09:14:00Z
ended_at: 2026-05-09T09:15:30Z
flush_version: "1.0"
hook_event: SessionEnd
```

### Decisions

- (none)

### Findings

- ssh-keyscan ed25519,ecdsa,rsa adds GitHub host keys for SSH cloning

### Open questions

- (none)

### Proposed concepts

- filing-gate-quarantine-vs-reject: Same slug from a second session (dedupe test)
"""


class TestParseDailyLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.agent_dir = self.tmp_path / "research"
        self.agent_dir.mkdir()
        self.log_file = self.agent_dir / "2026-05-09.md"
        self.log_file.write_text(SAMPLE_DAILY_LOG, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_yields_two_sessions(self):
        records = cmp.parse_daily_log(self.log_file)
        self.assertEqual(len(records), 2)

    def test_first_session_metadata(self):
        records = cmp.parse_daily_log(self.log_file)
        rec = records[0]
        self.assertEqual(rec.agent, "research")
        self.assertEqual(rec.session_n, 1)
        self.assertEqual(rec.session_id, "smoke-1234567890")
        self.assertEqual(
            rec.transcript_sha256,
            "e8362336edb37eabde1116de83cdc8dc97489c2b14252ec7a0c55cabb9171ec4",
        )

    def test_first_session_decisions(self):
        records = cmp.parse_daily_log(self.log_file)
        self.assertEqual(len(records[0].decisions), 2)
        self.assertIn("dry-run flag defaults", records[0].decisions[0])

    def test_first_session_findings_none(self):
        records = cmp.parse_daily_log(self.log_file)
        self.assertEqual(records[0].findings, [])

    def test_first_session_proposed_concepts(self):
        records = cmp.parse_daily_log(self.log_file)
        concepts = records[0].proposed_concepts
        self.assertEqual(len(concepts), 2)
        self.assertEqual(concepts[0]["slug"], "filing-gate-quarantine-vs-reject")
        self.assertIn("quarantine and reject", concepts[0]["summary"])

    def test_second_session_findings(self):
        records = cmp.parse_daily_log(self.log_file)
        self.assertEqual(len(records[1].findings), 1)
        self.assertIn("ssh-keyscan", records[1].findings[0])


class TestGatherCandidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.agent_dir = self.tmp_path / "research"
        self.agent_dir.mkdir()
        self.log_file = self.agent_dir / "2026-05-09.md"
        self.log_file.write_text(SAMPLE_DAILY_LOG, encoding="utf-8")
        self._orig_repo_root = cmp.REPO_ROOT
        cmp.REPO_ROOT = self.tmp_path

    def tearDown(self):
        cmp.REPO_ROOT = self._orig_repo_root
        self.tmp.cleanup()

    def test_gather_dedupes_by_slug(self):
        cands = cmp.gather_candidates([self.log_file])
        slugs = sorted(c.slug for c in cands)
        self.assertEqual(slugs, ["dry-run-output-format", "filing-gate-quarantine-vs-reject"])

    def test_gather_collects_multi_source(self):
        cands = cmp.gather_candidates([self.log_file])
        target = next(c for c in cands if c.slug == "filing-gate-quarantine-vs-reject")
        self.assertEqual(len(target.sources), 2)
        session_ids = sorted(s["session_id"] for s in target.sources)
        self.assertEqual(session_ids, ["sess-9999", "smoke-1234567890"])

    def test_excerpt_contains_decisions(self):
        cands = cmp.gather_candidates([self.log_file])
        # First session for filing-gate-quarantine-vs-reject is session 1
        target = next(c for c in cands if c.slug == "dry-run-output-format")
        self.assertIn("Decisions:", target.excerpt)
        self.assertIn("--dry-run flag", target.excerpt)


class TestStripCodeFences(unittest.TestCase):
    def test_no_fences(self):
        self.assertEqual(cmp.strip_code_fences('{"x": 1}'), '{"x": 1}')

    def test_json_fence(self):
        self.assertEqual(cmp.strip_code_fences('```json\n{"x": 1}\n```'), '{"x": 1}')

    def test_bare_fence(self):
        self.assertEqual(cmp.strip_code_fences('```\n{"x": 1}\n```'), '{"x": 1}')


class TestCallFilingGate(unittest.TestCase):
    def _mock_run(self, stdout_text: str, returncode: int = 0):
        class _Result:
            stdout = stdout_text
            stderr = ""
        _Result.returncode = returncode

        def runner(*args, **kwargs):
            return _Result()
        return runner

    def test_promote_parsed(self):
        body = json.dumps({
            "verdict": "PROMOTE",
            "reason": "concrete design distinction",
            "checks_triggered": [],
        })
        with patch("compile.subprocess.run", side_effect=self._mock_run(body)):
            ok, gate, err = cmp.call_filing_gate("prompt", "claude-sonnet-4-6", 30)
        self.assertTrue(ok, err)
        self.assertEqual(gate["verdict"], "PROMOTE")

    def test_invalid_verdict(self):
        body = json.dumps({"verdict": "MAYBE", "reason": "x", "checks_triggered": []})
        with patch("compile.subprocess.run", side_effect=self._mock_run(body)):
            ok, gate, err = cmp.call_filing_gate("prompt", "claude-sonnet-4-6", 30)
        self.assertFalse(ok)
        self.assertIn("bad_verdict", err)

    def test_missing_reason(self):
        body = json.dumps({"verdict": "PROMOTE", "checks_triggered": []})
        with patch("compile.subprocess.run", side_effect=self._mock_run(body)):
            ok, gate, err = cmp.call_filing_gate("prompt", "claude-sonnet-4-6", 30)
        self.assertFalse(ok)
        self.assertIn("missing_reason", err)

    def test_subprocess_nonzero_exit(self):
        with patch("compile.subprocess.run", side_effect=self._mock_run("", returncode=1)):
            ok, gate, err = cmp.call_filing_gate("prompt", "claude-sonnet-4-6", 30)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("exit_1"))

    def test_invalid_json(self):
        with patch("compile.subprocess.run", side_effect=self._mock_run("not json")):
            ok, gate, err = cmp.call_filing_gate("prompt", "claude-sonnet-4-6", 30)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("json_decode"))


class TestWriteOutputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._saved = (
            cmp.REPO_ROOT, cmp.CONCEPTS_DIR, cmp.QUARANTINE_DIR, cmp.DRY_RUN_DIR
        )
        cmp.REPO_ROOT = self.tmp_path
        cmp.CONCEPTS_DIR = self.tmp_path / "knowledge" / "concepts"
        cmp.QUARANTINE_DIR = self.tmp_path / "knowledge" / "quarantine"
        cmp.DRY_RUN_DIR = self.tmp_path / "docs" / "compile"

    def tearDown(self):
        cmp.REPO_ROOT, cmp.CONCEPTS_DIR, cmp.QUARANTINE_DIR, cmp.DRY_RUN_DIR = self._saved
        self.tmp.cleanup()

    def _candidate(self):
        return cmp.ConceptCandidate(
            slug="filing-gate-quarantine-vs-reject",
            summary="Distinction between quarantine and reject",
            sources=[{
                "agent": "research",
                "session_id": "sess-1",
                "transcript_sha256": "abc",
                "source_log": "daily-logs/research/2026-05-09.md",
                "session_n": 1,
            }],
            excerpt="Decisions:\n- did the thing",
        )

    def test_write_concept_dry_run_goes_to_docs(self):
        cand = self._candidate()
        gate = {"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []}
        target = cmp.write_concept(cand, gate, dry_run=True)
        self.assertTrue(target.exists())
        self.assertIn("docs/compile", str(target))
        self.assertIn("PROMOTE", target.name)

    def test_write_concept_real_goes_to_concepts(self):
        cand = self._candidate()
        gate = {"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []}
        target = cmp.write_concept(cand, gate, dry_run=False)
        self.assertTrue(target.exists())
        self.assertIn("knowledge/concepts", str(target))
        self.assertEqual(target.name, "filing-gate-quarantine-vs-reject.md")

    def test_concept_frontmatter_contains_provenance(self):
        cand = self._candidate()
        gate = {"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []}
        target = cmp.write_concept(cand, gate, dry_run=False)
        text = target.read_text(encoding="utf-8")
        self.assertIn(f"slug: {cand.slug}", text)
        self.assertIn("verdict: PROMOTE", text)
        self.assertIn("transcript_sha256: abc", text)
        self.assertIn("source_log: daily-logs/research/2026-05-09.md", text)

    def test_write_quarantine_includes_reason(self):
        cand = self._candidate()
        gate = {"verdict": "QUARANTINE", "reason": "summary overclaims", "checks_triggered": ["untraceable-claims"]}
        target = cmp.write_quarantine(cand, gate, dry_run=False)
        text = target.read_text(encoding="utf-8")
        self.assertIn("**Quarantined**", text)
        self.assertIn("summary overclaims", text)
        self.assertIn("untraceable-claims", text)


class TestMainWithMockedGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._saved = (
            cmp.REPO_ROOT, cmp.DAILY_LOGS_DIR, cmp.CONCEPTS_DIR,
            cmp.QUARANTINE_DIR, cmp.DRY_RUN_DIR, cmp.COMPILE_STATE_FILE,
        )
        cmp.REPO_ROOT = self.tmp_path
        cmp.DAILY_LOGS_DIR = self.tmp_path / "daily-logs"
        cmp.CONCEPTS_DIR = self.tmp_path / "knowledge" / "concepts"
        cmp.QUARANTINE_DIR = self.tmp_path / "knowledge" / "quarantine"
        cmp.DRY_RUN_DIR = self.tmp_path / "docs" / "compile"
        cmp.COMPILE_STATE_FILE = self.tmp_path / ".compile_state.json"
        # Create a daily log
        agent_dir = cmp.DAILY_LOGS_DIR / "research"
        agent_dir.mkdir(parents=True)
        (agent_dir / "2026-05-09.md").write_text(SAMPLE_DAILY_LOG, encoding="utf-8")

    def tearDown(self):
        (cmp.REPO_ROOT, cmp.DAILY_LOGS_DIR, cmp.CONCEPTS_DIR,
         cmp.QUARANTINE_DIR, cmp.DRY_RUN_DIR, cmp.COMPILE_STATE_FILE) = self._saved
        self.tmp.cleanup()

    def _run_main(self, argv: list[str]) -> int:
        with patch.object(sys, "argv", ["compile.py", *argv]):
            return cmp.main()

    def _mock_promote(self, *args, **kwargs):
        class _R:
            stdout = json.dumps({"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []})
            stderr = ""
            returncode = 0
        return _R()

    def _mock_quarantine(self, *args, **kwargs):
        class _R:
            stdout = json.dumps({"verdict": "QUARANTINE", "reason": "overclaim", "checks_triggered": ["untraceable-claims"]})
            stderr = ""
            returncode = 0
        return _R()

    def test_dry_run_writes_to_docs_not_concepts(self):
        with patch("compile.subprocess.run", side_effect=self._mock_promote):
            rc = self._run_main(["--dry-run"])
        self.assertEqual(rc, 0)
        promoted = list(cmp.DRY_RUN_DIR.glob("*-PROMOTE-*.md"))
        self.assertEqual(len(promoted), 2)  # two unique candidates in SAMPLE_DAILY_LOG
        self.assertFalse(cmp.CONCEPTS_DIR.exists())

    def test_no_dry_run_writes_to_concepts(self):
        with patch("compile.subprocess.run", side_effect=self._mock_promote):
            rc = self._run_main(["--no-dry-run"])
        self.assertEqual(rc, 0)
        promoted = list(cmp.CONCEPTS_DIR.glob("*.md"))
        self.assertEqual(len(promoted), 2)

    def test_quarantine_path(self):
        with patch("compile.subprocess.run", side_effect=self._mock_quarantine):
            rc = self._run_main(["--no-dry-run"])
        self.assertEqual(rc, 0)
        quarantined = list(cmp.QUARANTINE_DIR.glob("*.md"))
        self.assertEqual(len(quarantined), 2)

    def test_state_persisted_after_real_run(self):
        with patch("compile.subprocess.run", side_effect=self._mock_promote):
            self._run_main(["--no-dry-run"])
        self.assertTrue(cmp.COMPILE_STATE_FILE.exists())
        state = json.loads(cmp.COMPILE_STATE_FILE.read_text(encoding="utf-8"))
        self.assertIn("last_run_at", state)
        self.assertEqual(len(state["compiled_concepts"]), 2)

    def test_state_NOT_persisted_after_dry_run(self):
        with patch("compile.subprocess.run", side_effect=self._mock_promote):
            self._run_main(["--dry-run"])
        # State file should not exist (dry-run never persists state)
        # Actually we DO write a fresh state object into memory but never call write_compile_state
        # Test: file should not exist
        self.assertFalse(cmp.COMPILE_STATE_FILE.exists())


# ============================================================================
# Phase B core: concept writer, archive history, log/index refresh
# ============================================================================


def _writer_response(body: str):
    class _R:
        stdout = body
        stderr = ""
        returncode = 0
    return _R()


class TestConceptWriter(unittest.TestCase):
    def test_call_returns_body_and_strips_trailing_whitespace(self):
        with patch("compile.subprocess.run",
                   side_effect=lambda *a, **k: _writer_response("Some body text\n\n")):
            ok, body, err = cmp.call_concept_writer("prompt", "model", 60)
        self.assertTrue(ok, err)
        self.assertEqual(body, "Some body text\n")

    def test_strips_fences(self):
        with patch("compile.subprocess.run",
                   side_effect=lambda *a, **k: _writer_response("```\nbody\n```")):
            ok, body, err = cmp.call_concept_writer("prompt", "model", 60)
        self.assertTrue(ok, err)
        self.assertEqual(body, "body\n")

    def test_strips_leading_h1(self):
        with patch("compile.subprocess.run",
                   side_effect=lambda *a, **k: _writer_response("# title-it-shouldnt-have-emitted\n\nReal body\n")):
            ok, body, err = cmp.call_concept_writer("prompt", "model", 60)
        self.assertTrue(ok, err)
        self.assertEqual(body, "Real body\n")

    def test_empty_body_fails(self):
        with patch("compile.subprocess.run",
                   side_effect=lambda *a, **k: _writer_response("   \n")):
            ok, body, err = cmp.call_concept_writer("prompt", "model", 60)
        self.assertFalse(ok)
        self.assertEqual(err, "empty_body")

    def test_subprocess_nonzero_exit_fails(self):
        class _R:
            stdout = ""
            stderr = "boom"
            returncode = 1
        with patch("compile.subprocess.run", side_effect=lambda *a, **k: _R()):
            ok, body, err = cmp.call_concept_writer("prompt", "model", 60)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("exit_1"))

    def test_render_combines_frontmatter_and_body(self):
        cand = cmp.ConceptCandidate(
            slug="my-slug",
            summary="One-liner summary",
            sources=[{"agent": "research", "session_id": "s1",
                      "transcript_sha256": "abc",
                      "source_log": "daily-logs/research/2026-05-09.md", "session_n": 1}],
            excerpt="...",
        )
        gate = {"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []}
        rendered = cmp.render_concept_article(cand, gate, "Body paragraph.\n")
        self.assertTrue(rendered.startswith("---\n"))
        self.assertIn("slug: my-slug", rendered)
        self.assertIn("\n# my-slug\n\n", rendered)
        self.assertIn("Body paragraph.", rendered)


class TestArchiveExistingConcept(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._saved = (cmp.CONCEPTS_DIR, cmp.HISTORY_DIR)
        cmp.CONCEPTS_DIR = self.tmp_path / "knowledge" / "concepts"
        cmp.HISTORY_DIR = cmp.CONCEPTS_DIR / ".history"
        cmp.CONCEPTS_DIR.mkdir(parents=True)

    def tearDown(self):
        cmp.CONCEPTS_DIR, cmp.HISTORY_DIR = self._saved
        self.tmp.cleanup()

    def test_archive_moves_existing_to_history(self):
        existing = cmp.CONCEPTS_DIR / "foo.md"
        existing.write_text("old version\n", encoding="utf-8")
        archived = cmp.archive_existing_concept("foo", dry_run=False)
        self.assertIsNotNone(archived)
        self.assertFalse(existing.exists())
        self.assertTrue(archived.exists())
        self.assertIn(".history/foo/", str(archived))
        self.assertEqual(archived.read_text(encoding="utf-8"), "old version\n")

    def test_archive_when_no_existing_returns_none(self):
        archived = cmp.archive_existing_concept("nonexistent", dry_run=False)
        self.assertIsNone(archived)

    def test_archive_dry_run_does_nothing(self):
        existing = cmp.CONCEPTS_DIR / "foo.md"
        existing.write_text("old version\n", encoding="utf-8")
        archived = cmp.archive_existing_concept("foo", dry_run=True)
        self.assertIsNone(archived)
        self.assertTrue(existing.exists())  # untouched


class TestAppendToLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._saved = cmp.WIKI_LOG
        cmp.WIKI_LOG = self.tmp_path / "log.md"
        cmp.WIKI_LOG.write_text(
            "---\ntype: log\n---\n\n# Log\n\n## 2026-05-08\n\n- old entry\n",
            encoding="utf-8",
        )

    def tearDown(self):
        cmp.WIKI_LOG = self._saved
        self.tmp.cleanup()

    def test_creates_new_dated_section(self):
        # cmp.utc_today() returns today's UTC date; that date is not in setUp's log
        cmp.append_to_log("ran a thing", ["- detail"], dry_run=False)
        text = cmp.WIKI_LOG.read_text(encoding="utf-8")
        # Check that a new ## YYYY-MM-DD section was added (not the 2026-05-08 one)
        date_headings = cmp.LOG_DATE_HEADING_RE.findall(text)
        self.assertEqual(len(date_headings), 2)  # old + new

    def test_dry_run_does_nothing(self):
        original = cmp.WIKI_LOG.read_text(encoding="utf-8")
        cmp.append_to_log("ran a thing", [], dry_run=True)
        self.assertEqual(cmp.WIKI_LOG.read_text(encoding="utf-8"), original)

    def test_when_log_missing_does_nothing(self):
        cmp.WIKI_LOG.unlink()
        # Should not raise
        cmp.append_to_log("ran", [], dry_run=False)
        self.assertFalse(cmp.WIKI_LOG.exists())


class TestRefreshIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._saved = cmp.WIKI_INDEX
        cmp.WIKI_INDEX = self.tmp_path / "index.md"
        cmp.WIKI_INDEX.write_text(
            "---\ntype: index\n---\n\n# Index\n\n## Concepts\n\n_None yet._\n\n"
            "## Per-agent memory\n\n- something\n",
            encoding="utf-8",
        )

    def tearDown(self):
        cmp.WIKI_INDEX = self._saved
        self.tmp.cleanup()

    def test_adds_new_slugs(self):
        cmp.refresh_index(["filing-gate-quarantine-vs-reject", "memory-poisoning-defense"], dry_run=False)
        text = cmp.WIKI_INDEX.read_text(encoding="utf-8")
        self.assertIn("- [[filing-gate-quarantine-vs-reject]]", text)
        self.assertIn("- [[memory-poisoning-defense]]", text)
        self.assertIn("## Per-agent memory", text)  # other sections preserved

    def test_idempotent_for_existing(self):
        cmp.refresh_index(["foo"], dry_run=False)
        cmp.refresh_index(["foo"], dry_run=False)
        text = cmp.WIKI_INDEX.read_text(encoding="utf-8")
        self.assertEqual(text.count("[[foo]]"), 1)

    def test_dry_run_does_nothing(self):
        original = cmp.WIKI_INDEX.read_text(encoding="utf-8")
        cmp.refresh_index(["foo"], dry_run=True)
        self.assertEqual(cmp.WIKI_INDEX.read_text(encoding="utf-8"), original)

    def test_when_index_missing_does_nothing(self):
        cmp.WIKI_INDEX.unlink()
        cmp.refresh_index(["foo"], dry_run=False)
        self.assertFalse(cmp.WIKI_INDEX.exists())


class TestMainPhaseB(unittest.TestCase):
    """Integration tests: writer + archive + log/index refresh wired through main()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._saved = (
            cmp.REPO_ROOT, cmp.DAILY_LOGS_DIR, cmp.CONCEPTS_DIR, cmp.HISTORY_DIR,
            cmp.QUARANTINE_DIR, cmp.DRY_RUN_DIR, cmp.COMPILE_STATE_FILE,
            cmp.WIKI_LOG, cmp.WIKI_INDEX,
        )
        cmp.REPO_ROOT = self.tmp_path
        cmp.DAILY_LOGS_DIR = self.tmp_path / "daily-logs"
        cmp.CONCEPTS_DIR = self.tmp_path / "knowledge" / "concepts"
        cmp.HISTORY_DIR = cmp.CONCEPTS_DIR / ".history"
        cmp.QUARANTINE_DIR = self.tmp_path / "knowledge" / "quarantine"
        cmp.DRY_RUN_DIR = self.tmp_path / "docs" / "compile"
        cmp.COMPILE_STATE_FILE = self.tmp_path / ".compile_state.json"
        cmp.WIKI_LOG = self.tmp_path / "knowledge" / "log.md"
        cmp.WIKI_INDEX = self.tmp_path / "knowledge" / "index.md"
        # Daily log
        agent_dir = cmp.DAILY_LOGS_DIR / "research"
        agent_dir.mkdir(parents=True)
        (agent_dir / "2026-05-09.md").write_text(SAMPLE_DAILY_LOG, encoding="utf-8")
        # Wiki seed
        cmp.WIKI_LOG.parent.mkdir(parents=True, exist_ok=True)
        cmp.WIKI_LOG.write_text("---\ntype: log\n---\n\n# Log\n", encoding="utf-8")
        cmp.WIKI_INDEX.write_text(
            "---\ntype: index\n---\n\n# Index\n\n## Concepts\n\n_None yet._\n",
            encoding="utf-8",
        )

    def tearDown(self):
        (cmp.REPO_ROOT, cmp.DAILY_LOGS_DIR, cmp.CONCEPTS_DIR, cmp.HISTORY_DIR,
         cmp.QUARANTINE_DIR, cmp.DRY_RUN_DIR, cmp.COMPILE_STATE_FILE,
         cmp.WIKI_LOG, cmp.WIKI_INDEX) = self._saved
        self.tmp.cleanup()

    def _run_main(self, argv: list[str]) -> int:
        with patch.object(sys, "argv", ["compile.py", *argv]):
            return cmp.main()

    def test_no_rich_body_uses_stub_and_skips_writer(self):
        """With --no-rich-body, only the filing gate is called, not the writer."""
        call_count = {"n": 0}

        def gate_only(*args, **kwargs):
            call_count["n"] += 1

            class _R:
                stdout = json.dumps({"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []})
                stderr = ""
                returncode = 0
            return _R()

        with patch("compile.subprocess.run", side_effect=gate_only):
            rc = self._run_main(["--no-dry-run", "--no-rich-body", "--no-discord"])
        self.assertEqual(rc, 0)
        # 2 candidates, only filing-gate calls (no writer)
        self.assertEqual(call_count["n"], 2)
        # Concepts written with stub format
        for path in cmp.CONCEPTS_DIR.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("_Promoted on", text)  # stub footer signature

    def test_rich_body_calls_writer_per_promote(self):
        """Default behavior: 1 filing-gate call + 1 writer call per candidate."""
        gate_response = json.dumps({"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []})
        responses = [gate_response, "Rich article body about the concept.\n",
                     gate_response, "Another rich article body.\n"]

        def alternating(*args, **kwargs):
            class _R:
                stdout = responses.pop(0)
                stderr = ""
                returncode = 0
            return _R()

        with patch("compile.subprocess.run", side_effect=alternating):
            rc = self._run_main(["--no-dry-run", "--no-discord"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(responses), 0)  # all consumed
        # Concepts have rich bodies, no stub footer
        promoted = list(cmp.CONCEPTS_DIR.glob("*.md"))
        self.assertEqual(len(promoted), 2)
        for path in promoted:
            text = path.read_text(encoding="utf-8")
            self.assertIn("Rich article body", text) if "Rich" in path.read_text() else self.assertIn("Another rich", text)
            self.assertNotIn("_Promoted on", text)  # no stub footer

    def test_archive_runs_on_re_promote(self):
        """Re-promoting a concept moves the existing version to .history."""
        # Pre-seed an existing concept
        cmp.CONCEPTS_DIR.mkdir(parents=True)
        existing = cmp.CONCEPTS_DIR / "filing-gate-quarantine-vs-reject.md"
        existing.write_text("OLD VERSION\n", encoding="utf-8")

        gate_response = json.dumps({"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []})

        def gate_only(*args, **kwargs):
            class _R:
                stdout = gate_response
                stderr = ""
                returncode = 0
            return _R()

        with patch("compile.subprocess.run", side_effect=gate_only):
            self._run_main(["--no-dry-run", "--no-rich-body", "--no-discord"])

        # Archived copy exists in .history
        history_files = list((cmp.HISTORY_DIR / "filing-gate-quarantine-vs-reject").glob("*.md"))
        self.assertEqual(len(history_files), 1)
        self.assertEqual(history_files[0].read_text(encoding="utf-8"), "OLD VERSION\n")
        # Live file replaced with new version (stub format)
        self.assertNotEqual(existing.read_text(encoding="utf-8"), "OLD VERSION\n")
        self.assertIn("filing-gate-quarantine-vs-reject", existing.read_text(encoding="utf-8"))

    def test_index_and_log_refreshed_on_live_run(self):
        gate_response = json.dumps({"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []})

        def gate_only(*args, **kwargs):
            class _R:
                stdout = gate_response
                stderr = ""
                returncode = 0
            return _R()

        with patch("compile.subprocess.run", side_effect=gate_only):
            self._run_main(["--no-dry-run", "--no-rich-body", "--no-discord"])

        index_text = cmp.WIKI_INDEX.read_text(encoding="utf-8")
        self.assertIn("[[filing-gate-quarantine-vs-reject]]", index_text)
        self.assertIn("[[dry-run-output-format]]", index_text)
        self.assertNotIn("_None yet._", index_text)  # placeholder replaced

        log_text = cmp.WIKI_LOG.read_text(encoding="utf-8")
        self.assertIn("PROMOTE=2", log_text)
        # New dated section was appended
        self.assertTrue(cmp.LOG_DATE_HEADING_RE.search(log_text) is not None)

    def test_index_and_log_NOT_touched_on_dry_run(self):
        gate_response = json.dumps({"verdict": "PROMOTE", "reason": "ok", "checks_triggered": []})
        original_index = cmp.WIKI_INDEX.read_text(encoding="utf-8")
        original_log = cmp.WIKI_LOG.read_text(encoding="utf-8")

        def gate_only(*args, **kwargs):
            class _R:
                stdout = gate_response
                stderr = ""
                returncode = 0
            return _R()

        with patch("compile.subprocess.run", side_effect=gate_only):
            self._run_main(["--dry-run", "--no-rich-body", "--no-discord"])

        self.assertEqual(cmp.WIKI_INDEX.read_text(encoding="utf-8"), original_index)
        self.assertEqual(cmp.WIKI_LOG.read_text(encoding="utf-8"), original_log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
