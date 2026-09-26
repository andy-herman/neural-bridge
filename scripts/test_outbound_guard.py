"""Unit tests for outbound_guard.py: synthetic positive and negative controls.

The repo is public, so every marked note, marking phrase and folder name used
here is invented (see outbound_guard_testing.py). Stdlib-only; no real vault,
no network. The one test that runs Gemma GRC's real corpus gate does so on a
synthetic temp vault and is skipped when gemma-grc is not checked out.

Run: python3 scripts/test_outbound_guard.py
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import outbound_guard as og  # noqa: E402
from scripts.outbound_guard_testing import (  # noqa: E402
    BOILERPLATE, FILLER, MARKED_NOTE, MARKING_PHRASE, MARKING_TAG, SECRET_WORDS,
    SyntheticGuard, marked_excerpt, planted,
)

GUARD_SCRIPT = SCRIPTS_DIR / "outbound_guard.py"
REAL_GATE = Path.home() / "Development" / "gemma-grc" / "scripts"


def _check(text: str) -> og.Verdict:
    return og.check(text, surface="test")


class TestNormalization(unittest.TestCase):
    """Same normalization as leak_scan.py, so reformatting cannot hide a passage."""

    def test_case_punctuation_and_markdown_vanish(self):
        self.assertEqual(og.norm_words("**Hello**, _World_!\n> - [x] done"), ["hello", "world", "x", "done"])

    def test_hyphens_and_line_breaks_split_words(self):
        self.assertEqual(og.norm_words("halyard-restricted\nnote"), ["halyard", "restricted", "note"])


class TestControls(unittest.TestCase):
    """Positive controls must block; negative controls must pass."""

    @classmethod
    def setUpClass(cls):
        cls.guard = SyntheticGuard().install()

    @classmethod
    def tearDownClass(cls):
        cls.guard.remove()

    # --- positive controls (leak_scan.py: 40- and 20-word excerpts, whole notes) ---

    def test_40_word_excerpt_in_filler_is_blocked(self):
        v = _check(planted(marked_excerpt(40)))
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "shingles")
        self.assertEqual(v.shingle_hits, 29)

    def test_20_word_excerpt_is_blocked(self):
        v = _check(planted(marked_excerpt(20)))
        self.assertFalse(v.allowed)
        self.assertEqual(v.shingle_hits, 9)

    def test_whole_note_is_blocked(self):
        v = _check(MARKED_NOTE)
        self.assertFalse(v.allowed)
        self.assertEqual((v.notes_hit, v.categories), (1, ("tag",)))

    def test_14_words_is_the_floor(self):
        v = _check(planted(marked_excerpt(14)))
        self.assertFalse(v.allowed)
        self.assertEqual(v.shingle_hits, og.THRESHOLD)

    def test_reformatting_does_not_hide_an_excerpt(self):
        words = marked_excerpt(20).split()
        reformatted = "**" + " ".join(words[:7]).upper() + "**\n> " + ",  ".join(words[7:14]) + "\n- " + " ".join(words[14:])
        self.assertFalse(_check(reformatted).allowed)

    def test_excerpt_split_across_parts_still_counts(self):
        # Callers join title, body and files into one item; a passage split
        # between the title and the body is still one run of words.
        words = marked_excerpt(20).split()
        joined = " ".join(words[:10]) + "\n\n" + " ".join(words[10:])
        self.assertFalse(_check(joined).allowed)

    def test_marking_phrase_blocks_in_any_formatting(self):
        for text in (f"Heads up: {MARKING_PHRASE}.", "fyi **ZEPHYRINE internal\nonly** memo",
                     "Zephyrine-Internal-Only", f"tagged #{MARKING_TAG}", "see HALYARD RESTRICTED"):
            with self.subTest(text=text):
                v = _check(text)
                self.assertFalse(v.allowed)
                self.assertEqual(v.reason, "marking")
                self.assertEqual(v.marking_hits, 1)

    def test_excerpt_and_marking_together(self):
        v = _check(f"{MARKING_PHRASE}\n{marked_excerpt(20)}")
        self.assertEqual(v.reason, "shingles+marking")

    # --- negative controls ---

    def test_13_words_is_below_the_floor(self):
        # The documented miss: 13 consecutive words make 2 shingles.
        v = _check(planted(marked_excerpt(13)))
        self.assertTrue(v.allowed)
        self.assertEqual(v.shingle_hits, 2)

    def test_unrelated_text_passes(self):
        v = _check(FILLER)
        self.assertTrue(v.allowed)
        self.assertEqual((v.shingle_hits, v.marking_hits), (0, 0))

    def test_marked_text_that_also_sits_in_an_unmarked_note_stays_protected(self):
        # No discount: the leak check dropped shingles found in unmarked notes,
        # but agents write unmarked notes, so a discount would launder quotes.
        self.assertFalse(_check(BOILERPLATE).allowed)

    def test_marking_words_apart_pass(self):
        self.assertTrue(_check("Zephyrine sent the internal memo only on Friday.").allowed)

    def test_empty_text_passes(self):
        self.assertTrue(_check("").allowed)


class TestFailClosed(unittest.TestCase):
    """An index the guard cannot trust blocks everything, even harmless text."""

    def setUp(self):
        self.guard = SyntheticGuard().install()
        og._CACHE.clear()

    def tearDown(self):
        self.guard.remove()
        og._CACHE.clear()

    def assertBlocked(self, reason: str):
        v = _check("a harmless sentence about sourdough")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, reason)
        self.assertFalse(og.readiness().allowed)
        self.assertIn("failing closed", v.describe())
        return v

    def test_missing_index(self):
        self.guard.index_path.unlink()
        self.assertBlocked("no-index")

    def test_stale_index(self):
        later = time.time() + (og.MAX_AGE_HOURS + 1) * 3600
        with mock.patch.object(og.time, "time", return_value=later):
            v = self.assertBlocked("stale-index")
        self.assertGreater(v.index_age_hours, og.MAX_AGE_HOURS)

    def test_cached_index_still_ages_out(self):
        self.assertTrue(_check("warm the cache").allowed)
        later = time.time() + (og.MAX_AGE_HOURS + 1) * 3600
        with mock.patch.object(og.time, "time", return_value=later):
            self.assertBlocked("stale-index")

    def test_corrupt_payload(self):
        blob = bytearray(self.guard.index_path.read_bytes())
        blob[-3] ^= 0xFF
        self.guard.index_path.write_bytes(bytes(blob))
        self.assertBlocked("bad-index")

    def test_garbage_file(self):
        self.guard.index_path.write_bytes(b"not an index at all")
        self.assertBlocked("bad-index")

    def test_truncated_file(self):
        blob = self.guard.index_path.read_bytes()
        self.guard.index_path.write_bytes(blob[: len(blob) // 2])
        self.assertBlocked("bad-index")

    def test_missing_key(self):
        self.guard.key_path.unlink()
        self.assertBlocked("no-key")

    def test_unreadable_key(self):
        self.guard.key_path.write_text("not hex\n", encoding="ascii")
        self.assertBlocked("no-key")

    def test_different_key(self):
        self.guard.key_path.write_text(os.urandom(32).hex() + "\n", encoding="ascii")
        self.assertBlocked("key-mismatch")

    def test_unexpected_error_blocks(self):
        with mock.patch.object(og.Index, "check", side_effect=RuntimeError("boom")):
            v = _check("anything")
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "error:RuntimeError")

    def test_enforce_raises_with_counts_only(self):
        with self.assertRaises(og.OutboundBlocked) as ctx:
            og.enforce(planted(marked_excerpt(20)), surface="test")
        self.assertEqual(ctx.exception.verdict.reason, "shingles")
        self.assertIn("9 shingle(s)", str(ctx.exception))

    def test_rebuild_recovers(self):
        self.guard.index_path.unlink()
        self.assertFalse(_check("x").allowed)
        self.guard.rebuild()
        self.assertTrue(_check("x").allowed)


class TestNeverText(unittest.TestCase):
    """The index, the audit log and every message carry counts and digests only."""

    def setUp(self):
        self.guard = SyntheticGuard().install()

    def tearDown(self):
        self.guard.remove()

    def test_index_holds_no_text(self):
        blob = self.guard.index_path.read_bytes().lower()
        for word in SECRET_WORDS:
            self.assertNotIn(word.encode(), blob)

    def test_audit_and_descriptions_hold_no_text(self):
        texts = [planted(marked_excerpt(40)), MARKED_NOTE, f"{MARKING_PHRASE} {MARKING_TAG}", FILLER]
        descriptions = [_check(t).describe() for t in texts]
        audit = self.guard.audit_path.read_text(encoding="utf-8").lower()
        for word in SECRET_WORDS + ("sourdough", "routing", "pallets"):
            self.assertNotIn(word, audit)
            for d in descriptions:
                self.assertNotIn(word, d.lower())

    def test_audit_records_counts_per_check(self):
        _check(planted(marked_excerpt(20)))
        _check(FILLER)
        records = self.guard.audit_records()
        self.assertEqual([r["allowed"] for r in records], [False, True])
        self.assertEqual(records[0]["shingle_hits"], 9)
        self.assertEqual(records[0]["surface"], "test")
        self.assertRegex(records[0]["ref"], r"^[0-9a-f]{16}$")

    def test_same_text_same_ref_across_checks(self):
        self.assertEqual(_check("repeatable").ref, _check("repeatable").ref)

    def test_digests_are_keyed(self):
        phrase = " ".join(og.norm_words(MARKING_PHRASE))
        self.assertNotEqual(og._digest(b"k" * 32, phrase), og._digest(b"j" * 32, phrase))

    def test_policy_errors_do_not_echo_the_policy(self):
        self.guard.policy_file.write_text(json.dumps({"marking_phrases": [MARKING_PHRASE, "..."],
                                                      "policy_folders": []}), encoding="utf-8")
        with self.assertRaises(og.PolicyError) as ctx:
            og.load_policy()
        self.assertNotIn("zephyrine", str(ctx.exception).lower())


class TestPolicyAndKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "outbound-guard.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_valid_policy(self):
        self._write({"marking_phrases": ["Alpha Beta"], "policy_folders": ["Some Folder", "Other/"]})
        p = og.load_policy(self.path)
        self.assertEqual(p.policy_folders, ("Some Folder/", "Other/"))

    def test_missing_policy(self):
        with self.assertRaises(og.PolicyError):
            og.load_policy(self.path)

    def test_empty_marking_phrases_rejected(self):
        # An empty list would silently switch the marking check off.
        self._write({"marking_phrases": [], "policy_folders": []})
        with self.assertRaises(og.PolicyError):
            og.load_policy(self.path)

    def test_missing_policy_folders_key_rejected(self):
        self._write({"marking_phrases": ["Alpha Beta"]})
        with self.assertRaises(og.PolicyError):
            og.load_policy(self.path)

    def test_key_created_private_and_reused(self):
        key_file = Path(self.tmp.name) / "outbound-guard.key"
        key = og.load_or_create_key(key_file)
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertEqual(og.load_or_create_key(key_file), key)

    def test_unreadable_key_is_not_replaced(self):
        key_file = Path(self.tmp.name) / "outbound-guard.key"
        key_file.write_text("garbage\n", encoding="ascii")
        with self.assertRaises(og.IndexUnavailable):
            og.load_or_create_key(key_file)
        self.assertEqual(key_file.read_text(encoding="ascii"), "garbage\n")


def _stub_gate():
    """A stand-in for Gemma GRC's gate: frontmatter `marked: <category>` marks a
    note, and clean() resolves [[target|alias]] links to their alias, as
    vault_ingest.clean() does."""
    import re

    def gate_note(raw):
        for line in raw.splitlines():
            if line.startswith("marked: "):
                return False, line.split(": ", 1)[1].strip(), None
        return True, None, "unlabelled"

    def clean(raw):
        body = "\n".join(l for l in raw.splitlines() if not l.startswith("marked: "))
        return re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", body)

    return SimpleNamespace(gate_note=gate_note, read_note=lambda p: p.read_text(encoding="utf-8"), clean=clean)


# Invented, link-dense marked text: every few words is an aliased wikilink.
LINKED_NOTE = ("marked: label\nThe [[Harbour Survey Index|harbour]] survey found the [[Gauge Log|tidal gauges]] "
               "drift every winter after the [[Storm Notes|storms]] shift the silt banks toward the eastern "
               "[[Channel Markers|channel markers]], so the [[Pilot Rota|pilots]] now check them twice a month.")


class TestBuildFromVault(unittest.TestCase):
    def setUp(self):
        self.guard = SyntheticGuard(policy_folders=["committee drafts"]).install()  # case differs on disk
        self.vault = self.guard.root / "vault"
        self._note("Work/memo.md", "marked: tag\n" + MARKED_NOTE)
        self._note("Work/linked.md", LINKED_NOTE)
        self._note("Committee Drafts/item.md", "Unmarked by the gate but inside a policy folder. " + FILLER)
        self._note("Notes/garden.md", "Unmarked. The garden note. " + BOILERPLATE)
        self._note(".trash/old.md", "marked: tag\nThe quick brown fox jumps over the lazy dog while the "
                                    "patient heron waits beside the silver pond for a careless minnow.")

    def tearDown(self):
        self.guard.remove()
        og._CACHE.clear()

    def _note(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_walk_marks_gate_hits_and_policy_folders(self):
        counts = og.build_from_vault(vault=self.vault, gate=_stub_gate())
        self.assertEqual(counts["marked_by_category"], {"label": 1, "policy-folder": 1, "tag": 1})
        self.assertEqual(counts["notes_scanned"], 4)  # .trash is skipped, as in leak_scan.py
        self.assertEqual(counts["policy_folder_notes"], [1])
        og._CACHE.clear()
        gate_hit = _check(marked_excerpt(20))
        self.assertFalse(gate_hit.allowed)
        self.assertEqual(gate_hit.categories, ("tag",))
        policy_hit = _check(" ".join(FILLER.split()[:20]))  # FILLER sits in the policy folder here
        self.assertFalse(policy_hit.allowed)
        self.assertEqual(policy_hit.categories, ("policy-folder",))
        trash = ("quick brown fox jumps over the lazy dog while the patient heron waits beside the "
                 "silver pond for a careless minnow")
        self.assertTrue(_check(trash).allowed)  # 20 words that would block if .trash were indexed
        self.assertFalse(_check(BOILERPLATE).allowed)  # also in an unmarked note: still protected

    def test_an_agent_quoting_a_marked_note_does_not_unprotect_it(self):
        # The daemon archives Discord turns into unmarked vault notes. A quote
        # landing there must not clear the passage at the next rebuild.
        self._note("Agents/luna/conversations/2026-09-25.md", "Luna: here is the memo, " + marked_excerpt(40))
        og.build_from_vault(vault=self.vault, gate=_stub_gate())
        og._CACHE.clear()
        v = _check(marked_excerpt(40))  # not planted: FILLER is marked in this vault
        self.assertFalse(v.allowed)
        self.assertEqual(v.shingle_hits, 29)

    def test_raw_markdown_and_rendered_quotes_both_block(self):
        og.build_from_vault(vault=self.vault, gate=_stub_gate())
        og._CACHE.clear()
        raw = LINKED_NOTE.split("\n", 1)[1]
        rendered = _stub_gate().clean(raw)
        for form in (raw, rendered):
            with self.subTest(form=form[:30]):
                self.assertFalse(_check(planted(form)).allowed)

    def test_policy_folder_matching_nothing_fails_the_build_without_naming_it(self):
        self.guard.policy_file.write_text(json.dumps({"marking_phrases": [MARKING_PHRASE],
                                                      "policy_folders": ["Committee Drafts", "Nowhere Folder"]}),
                                          encoding="utf-8")
        with self.assertRaises(og.BuildError) as ctx:
            og.build_from_vault(vault=self.vault, gate=_stub_gate())
        self.assertIn("2 of 2", str(ctx.exception))
        self.assertNotIn("nowhere", str(ctx.exception).lower())

    def test_missing_vault_refuses(self):
        with self.assertRaises(og.BuildError):
            og.build_from_vault(vault=self.guard.root / "nope", gate=_stub_gate())

    def test_vault_without_marked_notes_refuses(self):
        # An index that marks nothing would clear everything: fail closed.
        empty = self.guard.root / "plain-vault"
        (empty / "a.md").parent.mkdir(parents=True)
        (empty / "a.md").write_text("nothing marked here", encoding="utf-8")
        with self.assertRaises(og.BuildError):
            og.build_from_vault(vault=empty, policy=og.Policy(("alpha beta",), ()), gate=_stub_gate())

    def test_missing_gate_refuses(self):
        with self.assertRaises(og.BuildError):
            og.build_from_vault(vault=self.vault)  # ENV_GATE points nowhere

    def test_index_is_private_and_no_temp_files_are_left(self):
        og.build_from_vault(vault=self.vault, gate=_stub_gate())
        self.assertEqual(stat.S_IMODE(self.guard.index_path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.guard.state_dir.iterdir() if p.name.endswith(".tmp")), [])


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.guard = SyntheticGuard().install()

    def tearDown(self):
        self.guard.remove()

    def _run(self, *args, stdin=""):
        return subprocess.run([sys.executable, str(GUARD_SCRIPT), *args], input=stdin,
                              capture_output=True, text=True, timeout=60, env=dict(os.environ))

    def test_check_blocks_with_counts_only(self):
        proc = self._run("check", stdin=planted(marked_excerpt(40)))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("29 shingle(s)", proc.stdout)
        for word in SECRET_WORDS:
            self.assertNotIn(word, proc.stdout.lower())

    def test_check_clears_filler(self):
        self.assertEqual(self._run("check", stdin=FILLER).returncode, 0)

    def test_status(self):
        proc = self._run("status")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(json.loads(proc.stdout)["ready"])

    def test_build_failure_is_reported_not_raised(self):
        proc = self._run("build")  # ENV_VAULT points nowhere
        self.assertEqual(proc.returncode, 1)
        self.assertIn("vault not found", proc.stderr)


@unittest.skipUnless((REAL_GATE / "vault_ingest.py").is_file(), "gemma-grc corpus gate not checked out")
class TestRealCorpusGate(unittest.TestCase):
    """End to end through the CLI with Gemma GRC's real gate, on a synthetic vault.

    Runs in a child process so vault_ingest's imports stay out of this one.
    """

    NOTES = {
        "Work/labelled.md": ("---\nclassification: internal\n---\n", "labelled note about the Marrowgate "
                             "harbour survey and the tidal gauges that drift every winter after the storms "
                             "shift the silt banks toward the eastern channel markers."),
        "Work/banner.md": ("Zephyrine Confidential\n\n", "banner note about the Quillfeather ledger migration, "
                           "the reconciliation scripts that run every night, and the three accounts that "
                           "never balance because of a rounding rule nobody wrote down."),
        "Work/tagged.md": ("---\ntags: [private]\n---\n", "tagged note about Oriel Street, where the parking "
                           "study counted cars every fifteen minutes for a fortnight and found the evening "
                           "peak moved an hour earlier once the market closed."),
        "Committee Drafts/draft.md": ("", "committee draft about repainting the Harrowgate footbridge, "
                                      "which has to wait for three dry days in a row and a lane closure "
                                      "that the council only grants outside the school holidays."),
        "Notes/open.md": ("", "open note about the Kestrel Lane allotments, the rain barrels that overflow "
                          "every April, and the compost rota that the volunteers keep forgetting to update "
                          "on the noticeboard by the gate."),
        "Notes/public.md": ("---\nclassification: public\n---\n", "public note about the Linden Row "
                            "library hours, which change in summer when the reading room closes early "
                            "on Fridays for the volunteer training sessions."),
    }

    def setUp(self):
        self.guard = SyntheticGuard(policy_folders=["Committee Drafts"]).install()
        vault = self.guard.root / "vault"
        for rel, (head, body) in self.NOTES.items():
            (vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (vault / rel).write_text(head + body + "\n", encoding="utf-8")
        env = dict(os.environ, **{og.ENV_VAULT: str(vault), og.ENV_GATE: str(REAL_GATE)})
        self.proc = subprocess.run([sys.executable, str(GUARD_SCRIPT), "build"], capture_output=True,
                                   text=True, timeout=120, env=env)
        og._CACHE.clear()

    def tearDown(self):
        self.guard.remove()
        og._CACHE.clear()

    def _excerpt(self, rel):
        return " ".join(self.NOTES[rel][1].split()[2:22])

    def test_real_gate_marks_labels_banners_tags_and_policy_folders(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertIn("4 marked notes", self.proc.stdout)
        for rel in ("Work/labelled.md", "Work/banner.md", "Work/tagged.md", "Committee Drafts/draft.md"):
            with self.subTest(note=rel):
                self.assertFalse(_check(planted(self._excerpt(rel))).allowed)
        for rel in ("Notes/open.md", "Notes/public.md"):
            with self.subTest(note=rel):
                self.assertTrue(_check(planted(self._excerpt(rel))).allowed)


def _git(cwd: Path, *args: str) -> tuple[bool, str]:
    proc = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                          cwd=str(cwd), capture_output=True, text=True, timeout=30)
    return proc.returncode == 0, proc.stdout


class TestCheckPush(unittest.TestCase):
    """What a push would publish: unpushed commit messages, paths and added lines."""

    def setUp(self):
        self.guard = SyntheticGuard().install()
        root = self.guard.root
        self.work = root / "work"
        _git(root, "-c", "init.defaultBranch=main", "init", "--bare", "remote.git")
        _git(root, "-c", "init.defaultBranch=main", "init", "work")
        _git(self.work, "remote", "add", "origin", str(root / "remote.git"))
        self._commit("README.md", "hello\n", "initial")
        ok, _ = _git(self.work, "push", "-u", "origin", "main")
        self.assertTrue(ok)
        _git(self.work, "checkout", "-b", "feature")

    def tearDown(self):
        self.guard.remove()

    def _commit(self, name, content, message):
        (self.work / name).write_text(content, encoding="utf-8")
        _git(self.work, "add", "--", name)
        ok, _ = _git(self.work, "commit", "-m", message)
        self.assertTrue(ok)

    def _run_git(self, args):
        return _git(self.work, *args)

    def test_clean_commit_passes(self):
        self._commit("notes.md", FILLER + "\n", "add baking notes")
        self.assertTrue(og.check_push(self._run_git, surface="test").allowed)

    def test_marked_excerpt_in_new_file_blocks(self):
        self._commit("notes.md", planted(marked_excerpt(20)) + "\n", "add notes")
        v = og.check_push(self._run_git, surface="test")
        self.assertFalse(v.allowed)
        self.assertEqual(v.shingle_hits, 9)

    def test_marking_in_commit_message_blocks(self):
        self._commit("notes.md", "fine\n", f"notes ({MARKING_PHRASE})")
        self.assertEqual(og.check_push(self._run_git, surface="test").reason, "marking")

    def test_earlier_unpushed_commit_counts_even_if_later_removed(self):
        self._commit("notes.md", planted(marked_excerpt(20)) + "\n", "add")
        self._commit("notes.md", "scrubbed\n", "scrub")  # history still carries the first commit
        self.assertFalse(og.check_push(self._run_git, surface="test").allowed)

    def test_already_pushed_content_is_not_rescanned(self):
        _git(self.work, "checkout", "main")
        self._commit("old.md", planted(marked_excerpt(20)) + "\n", "old")
        _git(self.work, "push", "origin", "main")
        _git(self.work, "checkout", "-b", "feature2")
        self._commit("new.md", "fine\n", "new")
        self.assertTrue(og.check_push(self._run_git, surface="test").allowed)

    def test_extra_text_is_screened_with_the_push(self):
        self._commit("notes.md", "fine\n", "fine")
        self.assertFalse(og.check_push(self._run_git, surface="test", extra=marked_excerpt(20)).allowed)

    def test_content_only_in_a_merge_commit_is_screened(self):
        self._commit("a.md", "fine\n", "a")
        _git(self.work, "checkout", "-b", "side")
        self._commit("b.md", "also fine\n", "b")
        _git(self.work, "checkout", "feature")
        _git(self.work, "merge", "--no-ff", "--no-commit", "side")
        (self.work / "c.md").write_text(planted(marked_excerpt(20)) + "\n", encoding="utf-8")
        _git(self.work, "add", "--", "c.md")
        ok, _ = _git(self.work, "commit", "-m", "merge side")
        self.assertTrue(ok)
        self.assertFalse(og.check_push(self._run_git, surface="test").allowed)

    def test_commits_already_on_another_remote_still_count(self):
        _git(self.guard.root, "-c", "init.defaultBranch=main", "init", "--bare", "backup.git")
        _git(self.work, "remote", "add", "backup", str(self.guard.root / "backup.git"))
        self._commit("notes.md", planted(marked_excerpt(20)) + "\n", "add")
        ok, _ = _git(self.work, "push", "backup", "feature")
        self.assertTrue(ok)  # on backup, not on origin: a push to origin still publishes it
        self.assertFalse(og.check_push(self._run_git, surface="test").allowed)

    def test_diff_attributes_cannot_hide_content(self):
        (self.work / ".gitattributes").write_text("notes.md -diff\n", encoding="utf-8")
        _git(self.work, "add", "--", ".gitattributes")
        self._commit("notes.md", planted(marked_excerpt(20)) + "\n", "add")
        self.assertFalse(og.check_push(self._run_git, surface="test").allowed)

    def test_git_failure_blocks(self):
        v = og.check_push(lambda args: (False, ""), surface="test")
        self.assertEqual((v.allowed, v.reason), (False, "error:git"))

    def test_git_exception_blocks(self):
        def undecodable(args):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        v = og.check_push(undecodable, surface="test")
        self.assertEqual((v.allowed, v.reason), (False, "error:git"))
        self.assertIn("could not screen", v.describe())


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.guard = SyntheticGuard().install()
        og._CACHE.clear()

    def tearDown(self):
        self.guard.remove()
        og._CACHE.clear()

    def test_fresh_index_is_not_rebuilt(self):
        with mock.patch.object(og, "refresh") as refresh:
            ok, _ = og.ensure_fresh()
        self.assertTrue(ok)
        refresh.assert_not_called()

    def test_old_or_missing_index_is_rebuilt(self):
        with mock.patch.object(og, "refresh", return_value=(True, "built")) as refresh:
            later = time.time() + 2 * 3600
            with mock.patch.object(og.time, "time", return_value=later):
                og.ensure_fresh(max_age_hours=1)
            self.guard.index_path.unlink()
            og.ensure_fresh()
        self.assertEqual(refresh.call_count, 2)

    def test_refresh_reports_build_failure(self):
        ok, detail = og.refresh()  # ENV_VAULT points nowhere
        self.assertFalse(ok)
        self.assertIn("vault not found", detail)

    def test_refresh_timeout(self):
        with mock.patch.object(og.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            ok, detail = og.refresh(timeout=1)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
