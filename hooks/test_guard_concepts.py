"""Unit tests for guard_concepts.py. Stdlib-only.

Run: `python3 hooks/test_guard_concepts.py`
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOKS_DIR.parent
SCRIPT = HOOKS_DIR / "guard_concepts.py"


def run_hook(payload) -> subprocess.CompletedProcess:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=data, capture_output=True, text=True,
    )


def write_payload(path: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}


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

    def test_blocks_traversal(self):
        r = run_hook(write_payload("daily-logs/../knowledge/concepts/evil.md"))
        self.assertEqual(r.returncode, 2)

    def test_allows_agent_subdir(self):
        r = run_hook(write_payload("knowledge/agents/research/note.md"))
        self.assertEqual(r.returncode, 0)

    def test_allows_daily_logs(self):
        r = run_hook(write_payload("daily-logs/research/2026-07-09.md"))
        self.assertEqual(r.returncode, 0)

    def test_allows_similar_prefix(self):
        r = run_hook(write_payload("knowledge/concepts-notes/x.md"))
        self.assertEqual(r.returncode, 0)

    def test_no_file_path_passes(self):
        r = run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(r.returncode, 0)

    def test_malformed_json_passes(self):
        r = run_hook("not json{")
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
