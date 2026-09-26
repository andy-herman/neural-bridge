"""Unit tests for guard_concepts.py. Stdlib-only.

Run: `python3 hooks/test_guard_concepts.py`
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOKS_DIR.parent
SCRIPT = HOOKS_DIR / "guard_concepts.py"


def run_hook(payload, script: Path = SCRIPT) -> subprocess.CompletedProcess:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(script)],
        input=data, capture_output=True, text=True,
    )


def write_payload(path: str, cwd: str | None = None) -> dict:
    payload = {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


class TestGuardConcepts(unittest.TestCase):
    def test_blocks_concepts_absolute(self):
        r = run_hook(write_payload(str(REPO_ROOT / "knowledge" / "concepts" / "evil.md")))
        self.assertEqual(r.returncode, 2)
        self.assertIn("BLOCKED", r.stderr)
        self.assertIn("filing gate", r.stderr)

    def test_blocks_concepts_relative(self):
        r = run_hook(write_payload("knowledge/concepts/evil.md"))
        self.assertEqual(r.returncode, 2)

    def test_blocks_quarantine(self):
        r = run_hook(write_payload(str(REPO_ROOT / "knowledge" / "quarantine" / "x.md")))
        self.assertEqual(r.returncode, 2)

    def test_blocks_every_other_tracked_wiki_path(self):
        # Before 2026-09-26 these were writable, and went public on the next
        # manual commit without the filing gate or the outbound guard.
        for rel in ("knowledge/connections/a--b.md", "knowledge/index.md", "knowledge/log.md",
                    "knowledge/AGENTS.md", "knowledge/new-area/x.md"):
            with self.subTest(path=rel):
                r = run_hook(write_payload(rel))
                self.assertEqual(r.returncode, 2)
                self.assertIn(rel, r.stderr)

    def test_blocks_traversal(self):
        r = run_hook(write_payload("daily-logs/../knowledge/concepts/evil.md"))
        self.assertEqual(r.returncode, 2)

    def test_blocks_case_variants(self):
        # The Mac's filesystem ignores case; the hook must too.
        r = run_hook(write_payload(str(REPO_ROOT / "Knowledge" / "Connections" / "x.md")))
        self.assertEqual(r.returncode, 2)

    def test_relative_paths_resolve_against_the_session_cwd(self):
        r = run_hook(write_payload("connections/x.md", cwd=str(REPO_ROOT / "knowledge")))
        self.assertEqual(r.returncode, 2)
        r = run_hook(write_payload("research/x.md", cwd=str(REPO_ROOT / "daily-logs")))
        self.assertEqual(r.returncode, 0)

    def test_allows_agent_subdir(self):
        r = run_hook(write_payload("knowledge/agents/research/note.md"))
        self.assertEqual(r.returncode, 0)

    def test_blocks_a_sibling_that_only_looks_like_the_agent_dir(self):
        r = run_hook(write_payload("knowledge/agents-notes/x.md"))
        self.assertEqual(r.returncode, 2)

    def test_allows_daily_logs(self):
        r = run_hook(write_payload("daily-logs/research/2026-07-09.md"))
        self.assertEqual(r.returncode, 0)

    def test_allows_similar_prefix_outside_knowledge(self):
        r = run_hook(write_payload("knowledge-archive/x.md"))
        self.assertEqual(r.returncode, 0)

    def test_no_file_path_passes(self):
        r = run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(r.returncode, 0)

    def test_malformed_json_passes(self):
        r = run_hook("not json{")
        self.assertEqual(r.returncode, 0)


class TestSymlinks(unittest.TestCase):
    """Run a copy of the hook in a throwaway tree, so no link is ever created
    inside the real checkout's knowledge/."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "hooks").mkdir()
        self.script = self.root / "hooks" / "guard_concepts.py"
        shutil.copy2(SCRIPT, self.script)
        for d in ("knowledge/concepts", "knowledge/agents/research", "knowledge/agents/content", "vault/Drafts"):
            (self.root / d).mkdir(parents=True)

    def test_link_from_agent_notes_into_concepts_is_blocked(self):
        (self.root / "knowledge/agents/research/sneaky").symlink_to(self.root / "knowledge/concepts")
        r = run_hook(write_payload(str(self.root / "knowledge/agents/research/sneaky/evil.md")),
                     script=self.script)
        self.assertEqual(r.returncode, 2)

    def test_link_out_to_the_vault_is_allowed(self):
        # knowledge/agents/content/drafts is a symlink into the vault on the Mac.
        (self.root / "knowledge/agents/content/drafts").symlink_to(self.root / "vault/Drafts")
        r = run_hook(write_payload(str(self.root / "knowledge/agents/content/drafts/post.md")),
                     script=self.script)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
