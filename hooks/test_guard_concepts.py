"""Unit tests for guard_concepts.py. Stdlib-only.

Run: `python3 hooks/test_guard_concepts.py`
"""

from __future__ import annotations

import json
import os
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


def patch_payload(*operations: str, cwd: str | None = None) -> dict:
    payload = {
        "tool_name": "apply_patch",
        "tool_input": "*** Begin Patch\n" + "\n".join(operations) + "\n*** End Patch\n",
    }
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

    def test_malformed_json_is_rejected(self):
        r = run_hook("not json{")
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot inspect write input", r.stderr)

    def test_structured_edit_formats(self):
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"),
                          ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")):
            for path, code in (("hooks/example.py", 0), ("knowledge/index.md", 2)):
                with self.subTest(tool=tool, path=path):
                    r = run_hook({"tool_name": tool, "tool_input": {key: path}})
                    self.assertEqual(r.returncode, code)

    def test_checks_every_structured_path(self):
        r = run_hook({"tool_name": "Edit", "tool_input": {
            "file_path": "hooks/example.py", "notebook_path": "knowledge/index.md",
        }})
        self.assertEqual(r.returncode, 2)

    def test_unparseable_mutations_are_rejected(self):
        for tool_input in ({}, {"path": "knowledge/index.md"}, None, [], 1,
                           {"file_path": None}, {"file_path": ["hooks/example.py"]},
                           {"file_path": ""}, {"file_path": "bad\0path"}):
            with self.subTest(tool_input=tool_input):
                r = run_hook({"tool_name": "Edit", "tool_input": tool_input})
                self.assertEqual(r.returncode, 2)
                self.assertIn("cannot inspect write input", r.stderr)
        for payload in ([], None, 1, "null"):
            with self.subTest(payload=payload):
                self.assertEqual(run_hook(payload).returncode, 2)

    def test_invalid_cwd_is_rejected(self):
        for cwd in (None, 123, "", "relative/path"):
            with self.subTest(cwd=cwd):
                r = run_hook(write_payload("hooks/example.py") | {"cwd": cwd})
                self.assertEqual(r.returncode, 2)


class TestPatchInputs(unittest.TestCase):
    def test_allows_mixed_safe_operations(self):
        r = run_hook(patch_payload(
            "*** Add File: hooks/new file.py\n+first\n+",
            "*** Update File: hooks/old.py\n*** Move to: hooks/renamed.py\n"
            "@@\n-old\n+new\n*** End of File",
            "*** Delete File: hooks/obsolete.py",
        ))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_blocks_add_update_and_delete(self):
        for operation in ("Add", "Update", "Delete"):
            for path in ("knowledge/index.md", "knowledge/concepts/a.md",
                         "knowledge/connections/a.md", "knowledge/quarantine/a.md"):
                with self.subTest(operation=operation, path=path):
                    content = "\n+x" if operation == "Add" else "\n@@\n-x\n+y" if operation == "Update" else ""
                    r = run_hook(patch_payload(f"*** {operation} File: {path}{content}"))
                    self.assertEqual(r.returncode, 2)
                    self.assertIn("filing gate", r.stderr)

    def test_checks_all_files_in_a_batch(self):
        safe = "*** Add File: hooks/safe.py\n+x"
        blocked = "*** Add File: knowledge/concepts/blocked.md\n+x"
        for operations in ((safe, blocked), (blocked, safe)):
            with self.subTest(operations=operations):
                self.assertEqual(run_hook(patch_payload(*operations)).returncode, 2)

    def test_checks_move_source_and_destination(self):
        for source, destination in (
            ("hooks/safe.py", "knowledge/index.md"),
            ("knowledge/index.md", "hooks/safe.py"),
            ("knowledge/agents/research/note.md", "knowledge/concepts/note.md"),
            ("hooks/safe.py", "daily-logs/../knowledge/index.md"),
        ):
            with self.subTest(source=source, destination=destination):
                r = run_hook(patch_payload(
                    f"*** Update File: {source}\n*** Move to: {destination}\n@@\n-x\n+y"
                ))
                self.assertEqual(r.returncode, 2)

    def test_move_only_and_update_only_envelopes(self):
        for operation in (
            "*** Update File: hooks/a.py",
            "*** Update File: hooks/a.py\n*** Move to: hooks/b.py",
        ):
            with self.subTest(operation=operation):
                r = run_hook(patch_payload(operation))
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_patch_content_cannot_be_confused_with_metadata(self):
        r = run_hook(patch_payload(
            "*** Add File: hooks/example.txt\n+*** Delete File: knowledge/index.md\n+*** End Patch",
            "*** Update File: hooks/example.py\n@@\n"
            " *** Update File: knowledge/index.md\n-*** Move to: knowledge/index.md\n+safe",
        ))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_relative_paths_and_case_variants(self):
        for path, cwd in (
            ("connections/a.md", str(REPO_ROOT / "knowledge")),
            ("daily-logs/../knowledge/index.md", str(REPO_ROOT)),
            (str(REPO_ROOT / "Knowledge/Connections/a.md"), str(REPO_ROOT)),
        ):
            with self.subTest(path=path, cwd=cwd):
                r = run_hook(patch_payload(f"*** Add File: {path}\n+x", cwd=cwd))
                self.assertEqual(r.returncode, 2)

    def test_allows_agent_notes_and_session_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in ("knowledge/agents/research/note.md", str(Path(directory) / "plan.md")):
                with self.subTest(path=path):
                    r = run_hook(patch_payload(f"*** Add File: {path}\n+x"))
                    self.assertEqual(r.returncode, 0, r.stderr)

    def test_rejects_malformed_patch_envelopes(self):
        valid = patch_payload("*** Add File: hooks/a.py\n+x")["tool_input"]
        for patch in (
            "", "not a patch", "*** Begin Patch\n*** End Patch",
            valid.replace("*** Begin Patch", "*** Start Patch"),
            valid.replace("*** End Patch\n", ""), valid + "trailing data\n",
            valid.replace("*** Add File: hooks/a.py", "*** Unknown: hooks/a.py"),
            valid.replace("+x", ""), valid.replace("+x", "unprefixed content"),
            valid.replace("hooks/a.py", ""), valid.replace("hooks/a.py", " hooks/a.py"),
            valid.replace("hooks/a.py", "hooks/a.py\0"),
            "*** Begin Patch\n*** Update File: hooks/a.py\n*** End of File\n*** End Patch\n",
            "*** Begin Patch\n*** Move to: knowledge/index.md\n*** End Patch\n",
            "*** Begin Patch\n*** Update File: hooks/a.py\n*** Move to: hooks/b.py\n"
            "*** Move to: knowledge/index.md\n*** End Patch\n",
        ):
            with self.subTest(patch=patch):
                r = run_hook({"tool_name": "apply_patch", "tool_input": patch})
                self.assertEqual(r.returncode, 2)
                self.assertIn("cannot inspect write input", r.stderr)


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

    def test_patch_add_and_move_through_symlink_are_blocked(self):
        (self.root / "knowledge/agents/research/sneaky").symlink_to(self.root / "knowledge/concepts")
        target = self.root / "knowledge/agents/research/sneaky/evil.md"
        for operation in (
            f"*** Add File: {target}\n+x",
            f"*** Update File: {self.root / 'hooks/a.py'}\n*** Move to: {target}",
            f"*** Update File: {target}\n*** Move to: {self.root / 'hooks/a.py'}",
        ):
            with self.subTest(operation=operation):
                r = run_hook(patch_payload(operation), script=self.script)
                self.assertEqual(r.returncode, 2)

    def test_patch_link_out_to_vault_retains_existing_policy(self):
        (self.root / "knowledge/agents/content/drafts").symlink_to(self.root / "vault/Drafts")
        r = run_hook(patch_payload(
            f"*** Add File: {self.root / 'knowledge/agents/content/drafts/post.md'}\n+x",
        ), script=self.script)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_symlink_loop_is_rejected(self):
        (self.root / "loop").symlink_to(self.root / "loop")
        r = run_hook(patch_payload(f"*** Add File: {self.root / 'loop/a.md'}\n+x"),
                     script=self.script)
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot inspect write input", r.stderr)

    def test_dangling_link_into_protected_directory_is_blocked(self):
        (self.root / "knowledge/agents/research/dangling").symlink_to(
            self.root / "knowledge/new-area"
        )
        r = run_hook(patch_payload(
            f"*** Add File: {self.root / 'knowledge/agents/research/dangling/a.md'}\n+x"
        ), script=self.script)
        self.assertEqual(r.returncode, 2)
        self.assertIn("filing gate", r.stderr)


@unittest.skipIf(os.name == "nt", "launcher commands require a POSIX shell or Git Bash")
class TestHookLaunchers(unittest.TestCase):
    def test_root_resolution_preserves_guard_decisions(self):
        settings = json.loads((REPO_ROOT / ".claude/settings.json").read_text())
        commands = {
            entry["matcher"]: entry["hooks"][0]["command"]
            for entry in settings["hooks"]["PreToolUse"]
        }
        write_command = commands["Write|Edit|MultiEdit|NotebookEdit"]
        bash_command = commands["Bash"]
        for project_dir in (None, "", str(REPO_ROOT)):
            for cwd in (REPO_ROOT, HOOKS_DIR):
                env = os.environ.copy()
                if project_dir is None:
                    env.pop("CLAUDE_PROJECT_DIR", None)
                else:
                    env["CLAUDE_PROJECT_DIR"] = project_dir
                env["NB_AGENT_ID"] = "luna"
                cases = (
                    (write_command, write_payload("hooks/example.py", str(REPO_ROOT)), 0),
                    (write_command, write_payload("knowledge/index.md", str(REPO_ROOT)), 2),
                    (write_command, patch_payload("*** Add File: hooks/example.py\n+x",
                                                  cwd=str(REPO_ROOT)), 0),
                    (write_command, patch_payload("*** Add File: knowledge/index.md\n+x",
                                                  cwd=str(REPO_ROOT)), 2),
                    (bash_command, {"tool_input": {"command": "python3 -m scripts.luna.calendar today"}}, 0),
                    (bash_command, {"tool_input": {"command": "git status"}}, 2),
                )
                for command, payload, code in cases:
                    with self.subTest(project_dir=project_dir, cwd=cwd, payload=payload):
                        r = subprocess.run(
                            ["/bin/sh", "-c", command], input=json.dumps(payload),
                            capture_output=True, text=True, env=env, cwd=cwd,
                        )
                        self.assertEqual(r.returncode, code, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
