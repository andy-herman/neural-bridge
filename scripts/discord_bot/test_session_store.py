"""Unit tests for session_store.py — per (channel × agent) claude session
ID persistence with TTL.

Stdlib-only. Each test redirects the store's file path into a tempdir so
the real ~/Library/Application Support/neural-bridge/sessions.json stays
untouched.

Run: `python3 scripts/discord_bot/test_session_store.py`
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot.session_store import (  # noqa: E402
    SESSION_TTL_SECONDS,
    SessionRecord,
    SessionStore,
)


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except (ValueError, TypeError):
        return False


class TestSessionStoreBasics(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sessions.json"
        self.store = SessionStore(path=self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_returns_none_for_unknown(self):
        self.assertIsNone(self.store.get(123, "luna"))

    def test_get_or_create_returns_is_new_true_on_first_call(self):
        rec, is_new = self.store.get_or_create(123, "luna")
        self.assertTrue(is_new)
        self.assertTrue(_is_uuid(rec.session_id))
        self.assertEqual(rec.turn_count, 0)

    def test_get_or_create_returns_same_record_on_second_call(self):
        rec1, _ = self.store.get_or_create(123, "luna")
        rec2, is_new = self.store.get_or_create(123, "luna")
        self.assertFalse(is_new)
        self.assertEqual(rec1.session_id, rec2.session_id)

    def test_separate_channels_get_separate_sessions(self):
        rec1, _ = self.store.get_or_create(123, "luna")
        rec2, _ = self.store.get_or_create(456, "luna")
        self.assertNotEqual(rec1.session_id, rec2.session_id)

    def test_separate_agents_in_same_channel_get_separate_sessions(self):
        rec1, _ = self.store.get_or_create(123, "luna")
        rec2, _ = self.store.get_or_create(123, "content")
        self.assertNotEqual(rec1.session_id, rec2.session_id)

    def test_touch_increments_turn_count_and_updates_last_used(self):
        rec, _ = self.store.get_or_create(123, "luna")
        original_last_used = rec.last_used_at
        time.sleep(0.01)  # ensure timestamp moves
        self.store.touch(123, "luna")
        rec_after = self.store.get(123, "luna")
        self.assertEqual(rec_after.turn_count, 1)
        self.assertGreater(rec_after.last_used_at, original_last_used)

    def test_touch_on_missing_session_is_noop(self):
        # Doesn't raise; just silently ignores.
        self.store.touch(999, "ghost")
        self.assertIsNone(self.store.get(999, "ghost"))

    def test_reset_produces_new_uuid(self):
        rec1, _ = self.store.get_or_create(123, "luna")
        rec2 = self.store.reset(123, "luna")
        self.assertNotEqual(rec1.session_id, rec2.session_id)
        # The store now holds the new one.
        rec3 = self.store.get(123, "luna")
        self.assertEqual(rec2.session_id, rec3.session_id)

    def test_reset_works_when_no_prior_session(self):
        # Reset on a non-existent key should just create one.
        rec = self.store.reset(789, "fresh")
        self.assertTrue(_is_uuid(rec.session_id))


class TestSessionStorePersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sessions.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_persist_across_store_instances(self):
        store_a = SessionStore(path=self.path)
        rec_a, _ = store_a.get_or_create(123, "luna")

        store_b = SessionStore(path=self.path)
        rec_b = store_b.get(123, "luna")
        self.assertIsNotNone(rec_b)
        self.assertEqual(rec_a.session_id, rec_b.session_id)

    def test_corrupt_json_starts_fresh_without_raising(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        store = SessionStore(path=self.path)
        # Should silently start fresh, not raise.
        self.assertIsNone(store.get(123, "luna"))
        # And we can use it normally after.
        rec, is_new = store.get_or_create(123, "luna")
        self.assertTrue(is_new)

    def test_malformed_record_skipped_without_raising(self):
        # Mix of one valid record and one missing required fields. The valid
        # one uses a recent timestamp so TTL doesn't expire it before the test
        # gets to check it.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        valid = f'"123:luna": {{"session_id": "abc", "created_at": {now}, "last_used_at": {now}, "turn_count": 0}}'
        broken = '"999:bad": {"session_id": "x"}'  # missing fields
        self.path.write_text("{" + valid + ", " + broken + "}", encoding="utf-8")
        store = SessionStore(path=self.path)
        rec = store.get(123, "luna")
        self.assertIsNotNone(rec)
        self.assertIsNone(store.get(999, "bad"))


class TestSessionStoreTTL(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sessions.json"
        self.store = SessionStore(path=self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_returns_none_for_expired_session(self):
        # Backdate the record to before the TTL window.
        rec, _ = self.store.get_or_create(123, "luna")
        rec.last_used_at = time.time() - SESSION_TTL_SECONDS - 60
        # Force a persist so the get path sees the old timestamp.
        self.store._persist()
        # Re-fetch through a fresh store to bypass in-memory caching.
        fresh = SessionStore(path=self.path)
        self.assertIsNone(fresh.get(123, "luna"))

    def test_get_or_create_after_expiry_returns_new_uuid(self):
        rec_old, _ = self.store.get_or_create(123, "luna")
        rec_old.last_used_at = time.time() - SESSION_TTL_SECONDS - 60
        self.store._persist()

        fresh = SessionStore(path=self.path)
        rec_new, is_new = fresh.get_or_create(123, "luna")
        self.assertTrue(is_new)
        self.assertNotEqual(rec_old.session_id, rec_new.session_id)

    def test_prune_expired_returns_count(self):
        rec_alive, _ = self.store.get_or_create(1, "luna")
        rec_dead, _ = self.store.get_or_create(2, "luna")
        rec_dead.last_used_at = time.time() - SESSION_TTL_SECONDS - 60
        self.store._persist()

        fresh = SessionStore(path=self.path)
        count = fresh.prune_expired()
        self.assertEqual(count, 1)
        self.assertIsNotNone(fresh.get(1, "luna"))
        self.assertIsNone(fresh.get(2, "luna"))


class TestSessionStoreAtomicWrite(unittest.TestCase):
    """Sanity check that the tempfile-then-rename write path doesn't leave
    junk behind in the parent directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sessions.json"
        self.store = SessionStore(path=self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_temp_files_left_after_writes(self):
        for i in range(5):
            self.store.get_or_create(i, "luna")
        leftover = [p for p in self.path.parent.iterdir() if p.name.startswith(".sessions.")]
        self.assertEqual(leftover, [], f"unexpected temp files: {leftover}")


if __name__ == "__main__":
    unittest.main()
