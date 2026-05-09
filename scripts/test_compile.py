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


if __name__ == "__main__":
    unittest.main(verbosity=2)
