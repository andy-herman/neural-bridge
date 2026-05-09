"""Unit tests for lint.py.

Stdlib-only. No real `claude -p` calls — subprocess is mocked.
Run: `python3 scripts/test_lint.py`
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

import lint as L  # noqa: E402


def write_concept(path: Path, slug: str, body: str, valid_frontmatter: bool = True) -> Path:
    if valid_frontmatter:
        fm = (
            "---\n"
            f"slug: {slug}\n"
            "verdict: PROMOTE\n"
            "reason: ok\n"
            "checks_triggered: []\n"
            "compiled_at: 2026-05-09T08:07:05Z\n"
            'compiler_version: "1.0"\n'
            "sources:\n"
            "  - agent: research\n"
            "---\n\n"
        )
    else:
        fm = ""
    path.write_text(fm + body, encoding="utf-8")
    return path


class _BaseTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._saved = (
            L.REPO_ROOT, L.KNOWLEDGE_DIR, L.CONCEPTS_DIR, L.QUARANTINE_DIR,
            L.INDEX_FILE, L.LINT_DIR,
        )
        L.REPO_ROOT = self.tmp_path
        L.KNOWLEDGE_DIR = self.tmp_path / "knowledge"
        L.CONCEPTS_DIR = L.KNOWLEDGE_DIR / "concepts"
        L.QUARANTINE_DIR = L.KNOWLEDGE_DIR / "quarantine"
        L.INDEX_FILE = L.KNOWLEDGE_DIR / "index.md"
        L.LINT_DIR = self.tmp_path / "docs" / "lint"
        L.CONCEPTS_DIR.mkdir(parents=True)

    def tearDown(self):
        (L.REPO_ROOT, L.KNOWLEDGE_DIR, L.CONCEPTS_DIR, L.QUARANTINE_DIR,
         L.INDEX_FILE, L.LINT_DIR) = self._saved
        self.tmp.cleanup()


class TestParseFrontmatter(unittest.TestCase):
    def test_present(self):
        text = "---\nslug: x\nverdict: PROMOTE\n---\n\nbody"
        fm, body = L.parse_frontmatter(text)
        self.assertEqual(fm["slug"], "x")
        self.assertEqual(body.strip(), "body")

    def test_absent(self):
        fm, body = L.parse_frontmatter("just body, no frontmatter")
        self.assertEqual(fm, {})
        self.assertEqual(body, "just body, no frontmatter")


class TestCheckBrokenLinks(_BaseTmp):
    def test_no_findings_when_target_exists(self):
        write_concept(L.CONCEPTS_DIR / "alpha.md", "alpha", "links to [[beta]]")
        write_concept(L.CONCEPTS_DIR / "beta.md", "beta", "body")
        findings = L.check_broken_links()
        self.assertEqual(findings, [])

    def test_finding_when_target_missing(self):
        write_concept(L.CONCEPTS_DIR / "alpha.md", "alpha", "links to [[beta]]")
        findings = L.check_broken_links()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].check, "broken-links")
        self.assertIn("beta", findings[0].evidence)


class TestCheckOrphans(_BaseTmp):
    def test_orphan_detected(self):
        write_concept(L.CONCEPTS_DIR / "linked.md", "linked", "body")
        write_concept(L.CONCEPTS_DIR / "orphan.md", "orphan", "body")
        write_concept(L.CONCEPTS_DIR / "linker.md", "linker", "I link to [[linked]]")
        findings = L.check_orphans()
        slugs = {Path(f.file).stem for f in findings}
        self.assertIn("orphan", slugs)
        self.assertIn("linker", slugs)  # linker is also unlinked
        self.assertNotIn("linked", slugs)

    def test_index_satisfies(self):
        write_concept(L.CONCEPTS_DIR / "alpha.md", "alpha", "body")
        L.INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        L.INDEX_FILE.write_text("# Index\n\n- [[alpha]]\n", encoding="utf-8")
        findings = L.check_orphans()
        slugs = {Path(f.file).stem for f in findings}
        self.assertNotIn("alpha", slugs)


class TestCheckFrontmatter(_BaseTmp):
    def test_no_frontmatter_high(self):
        write_concept(L.CONCEPTS_DIR / "bad.md", "bad", "body", valid_frontmatter=False)
        findings = L.check_frontmatter()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "HIGH")

    def test_missing_required_keys(self):
        (L.CONCEPTS_DIR / "partial.md").write_text(
            "---\nslug: partial\n---\n\nbody", encoding="utf-8"
        )
        findings = L.check_frontmatter()
        self.assertEqual(len(findings), 1)
        self.assertIn("missing frontmatter keys", findings[0].evidence)

    def test_slug_filename_mismatch(self):
        (L.CONCEPTS_DIR / "renamed.md").write_text(
            "---\n"
            "slug: old-name\n"
            "verdict: PROMOTE\n"
            "compiled_at: 2026-05-09T08:07:05Z\n"
            'compiler_version: "1.0"\n'
            "sources:\n"
            "  - x: y\n"
            "---\n\nbody", encoding="utf-8"
        )
        findings = L.check_frontmatter()
        self.assertEqual(len(findings), 1)
        self.assertIn("does not match filename", findings[0].evidence)


class TestCheckImperativeLanguage(_BaseTmp):
    def _mock_run(self, body: str, returncode: int = 0):
        class _R:
            stdout = body
            stderr = ""
        _R.returncode = returncode

        def runner(*args, **kwargs):
            return _R()
        return runner

    def test_clean(self):
        write_concept(L.CONCEPTS_DIR / "ok.md", "ok", "purely descriptive of system behavior")
        body = json.dumps({"finding": False, "severity": None, "evidence": [], "reason": "clean"})
        with patch("lint.subprocess.run", side_effect=self._mock_run(body)):
            findings = L.check_imperative_language(None, "claude-sonnet-4-6", 30)
        self.assertEqual(findings, [])

    def test_finding_high(self):
        write_concept(L.CONCEPTS_DIR / "imp.md", "imp", "Future agents should always promote")
        body = json.dumps({
            "finding": True,
            "severity": "HIGH",
            "evidence": ["Future agents should always promote"],
            "reason": "imperative directed at future AI",
        })
        with patch("lint.subprocess.run", side_effect=self._mock_run(body)):
            findings = L.check_imperative_language(None, "claude-sonnet-4-6", 30)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "HIGH")
        self.assertIn("Future agents", findings[0].evidence)

    def test_subprocess_fail(self):
        write_concept(L.CONCEPTS_DIR / "x.md", "x", "body")
        with patch("lint.subprocess.run", side_effect=self._mock_run("not json")):
            findings = L.check_imperative_language(None, "claude-sonnet-4-6", 30)
        # Failure is logged as a LOW finding so the operator notices
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "LOW")
        self.assertIn("lint check failed", findings[0].evidence)


class TestRenderReport(unittest.TestCase):
    def test_no_findings(self):
        text = L.render_report([], ["broken-links"])
        self.assertIn("Clean. No findings.", text)

    def test_findings_grouped_and_ordered(self):
        findings = [
            L.Finding("orphans", "LOW", "a.md", "no links", "fix it"),
            L.Finding("frontmatter", "HIGH", "b.md", "missing", "fix"),
            L.Finding("broken-links", "MEDIUM", "c.md", "no target", "fix"),
        ]
        text = L.render_report(findings, ["broken-links", "orphans", "frontmatter"])
        # HIGH should appear before MEDIUM before LOW
        h = text.index("## HIGH")
        m = text.index("## MEDIUM")
        l = text.index("## LOW")
        self.assertLess(h, m)
        self.assertLess(m, l)
        self.assertIn("HIGH: 1", text)
        self.assertIn("MEDIUM: 1", text)


class TestMainNoConcepts(_BaseTmp):
    def test_writes_clean_report_when_concepts_dir_missing(self):
        # Remove the concepts dir we created in setUp
        import shutil
        shutil.rmtree(L.CONCEPTS_DIR)
        with patch.object(sys, "argv", ["lint.py"]):
            rc = L.main()
        self.assertEqual(rc, 0)
        report = L.LINT_DIR / f"{L.utc_today()}.md"
        self.assertTrue(report.exists())
        self.assertIn("No `knowledge/concepts/` directory yet", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
