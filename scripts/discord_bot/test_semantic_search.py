"""Unit tests for semantic_search.py.

Stdlib + unittest.mock + sqlite-vec (required for the EmbeddingIndex
tests). Ollama is mocked — we never hit the real localhost daemon.

Run: `python3 scripts/discord_bot/test_semantic_search.py`
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PKG_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG_DIR.parent.parent))

from scripts.discord_bot import semantic_search as ss  # noqa: E402
from scripts.discord_bot.semantic_search import (  # noqa: E402
    EMBEDDING_DIM,
    EmbeddingIndex,
    SearchResult,
    _turn_id,
    _vec_to_bytes,
    parse_turns_from_file,
)


def _mock_embed_response(vec: list[float]):
    """Build the urllib.urlopen context-manager mock for an embedding call."""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps({"embedding": vec}).encode("utf-8")
    cm = mock.MagicMock()
    cm.__enter__ = mock.MagicMock(return_value=resp)
    cm.__exit__ = mock.MagicMock(return_value=False)
    return cm


def _fake_vec(seed: float) -> list[float]:
    """Deterministic 1024-d vector from a seed. Used so similarity ordering
    is predictable in tests."""
    return [seed + (i * 0.0001) for i in range(EMBEDDING_DIM)]


class TestEmbedClient(unittest.TestCase):
    def test_returns_vector_on_happy_path(self):
        vec = [0.1] * EMBEDDING_DIM
        with mock.patch("urllib.request.urlopen", return_value=_mock_embed_response(vec)):
            out = ss.embed("hello world")
        self.assertEqual(out, vec)

    def test_returns_none_on_empty_input(self):
        self.assertIsNone(ss.embed(""))
        self.assertIsNone(ss.embed("   \n\t "))

    def test_returns_none_on_wrong_dim(self):
        with mock.patch("urllib.request.urlopen", return_value=_mock_embed_response([0.0] * 768)):
            self.assertIsNone(ss.embed("hi"))

    def test_returns_none_on_network_error(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conn refused")):
            self.assertIsNone(ss.embed("hi"))

    def test_returns_none_on_bad_json(self):
        resp = mock.MagicMock()
        resp.read.return_value = b"not json"
        cm = mock.MagicMock()
        cm.__enter__ = mock.MagicMock(return_value=resp)
        cm.__exit__ = mock.MagicMock(return_value=False)
        with mock.patch("urllib.request.urlopen", return_value=cm):
            self.assertIsNone(ss.embed("hi"))


class TestTurnId(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(
            _turn_id("/path.md", "content here"),
            _turn_id("/path.md", "content here"),
        )

    def test_differs_by_path(self):
        self.assertNotEqual(
            _turn_id("/a.md", "same"),
            _turn_id("/b.md", "same"),
        )

    def test_differs_by_content(self):
        self.assertNotEqual(
            _turn_id("/a.md", "one"),
            _turn_id("/a.md", "two"),
        )

    def test_length_is_32_chars(self):
        self.assertEqual(len(_turn_id("/a.md", "x")), 32)


class TestVecToBytes(unittest.TestCase):
    def test_packs_to_4_bytes_per_float(self):
        v = [1.0, 2.0, 3.0, 4.0]
        b = _vec_to_bytes(v)
        self.assertEqual(len(b), 4 * 4)  # 4 floats × 4 bytes each


class TestEmbeddingIndex(unittest.TestCase):
    """Real sqlite-vec round-trips with mocked Ollama. sqlite_vec must be
    installed for these to pass; they're the integration heart of the
    module."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test_embeddings.db"
        self.index = EmbeddingIndex(path=self.path)

    def tearDown(self):
        self.index.close()
        self._tmp.cleanup()

    def test_index_and_search_round_trip(self):
        # Three turns with distinct embeddings.
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                _mock_embed_response(_fake_vec(0.1)),   # index call
                _mock_embed_response(_fake_vec(0.5)),   # index call
                _mock_embed_response(_fake_vec(0.9)),   # index call
                _mock_embed_response(_fake_vec(0.1)),   # search query
            ]
            self.assertTrue(self.index.index_turn(
                "luna", "/path.md", "2026-05-11 22:00:00Z", "Andy", "first turn"))
            self.assertTrue(self.index.index_turn(
                "luna", "/path.md", "2026-05-11 22:05:00Z", "Luna", "second turn"))
            self.assertTrue(self.index.index_turn(
                "luna", "/path.md", "2026-05-11 22:10:00Z", "Andy", "third turn"))
            hits = self.index.search("luna", "query similar to first", top_n=3)
        self.assertEqual(len(hits), 3)
        # The first result should be the closest match (vector with seed 0.1).
        self.assertEqual(hits[0].content, "first turn")
        # Distances are monotonically non-decreasing.
        for i in range(1, len(hits)):
            self.assertGreaterEqual(hits[i].distance, hits[i - 1].distance)

    def test_index_dedup_skips_existing(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_embed_response(_fake_vec(0.5))
            inserted = self.index.index_turn(
                "luna", "/p.md", "ts", "Andy", "same content")
            self.assertTrue(inserted)
            skipped = self.index.index_turn(
                "luna", "/p.md", "ts", "Andy", "same content")
            self.assertFalse(skipped)

    def test_index_returns_false_when_ollama_unreachable(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("conn refused")):
            self.assertFalse(self.index.index_turn(
                "luna", "/p.md", "ts", "Andy", "content"))

    def test_index_returns_false_on_empty_content(self):
        self.assertFalse(self.index.index_turn(
            "luna", "/p.md", "ts", "Andy", ""))
        self.assertFalse(self.index.index_turn(
            "luna", "/p.md", "ts", "Andy", "   \n  "))

    def test_separate_agents_get_separate_tables(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_embed_response(_fake_vec(0.3))
            self.assertTrue(self.index.index_turn(
                "luna", "/p1.md", "t1", "Andy", "luna content"))
            self.assertTrue(self.index.index_turn(
                "echo", "/p2.md", "t2", "Andy", "echo content"))
        # Each agent only sees their own table.
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_embed_response(_fake_vec(0.3))
            luna_hits = self.index.search("luna", "anything", top_n=10)
        self.assertEqual(len(luna_hits), 1)
        self.assertEqual(luna_hits[0].content, "luna content")

    def test_search_returns_empty_for_unknown_agent(self):
        # No table created for "ghost" — should return empty, not raise.
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value = _mock_embed_response(_fake_vec(0.3))
            self.assertEqual(self.index.search("ghost", "anything"), [])

    def test_table_name_sanitizes_unsafe_chars(self):
        # Hyphens, slashes, etc. shouldn't appear in SQL identifiers.
        name = self.index._table_name("ux-designer/v2")
        self.assertNotIn("-", name)
        self.assertNotIn("/", name)
        self.assertTrue(name.startswith("embeddings_"))


class TestParseTurnsFromFile(unittest.TestCase):
    def test_extracts_each_turn(self):
        with tempfile.TemporaryDirectory() as td:
            md = Path(td) / "log.md"
            md.write_text(
                "---\nagent: luna\n---\n\n# Header\n\n"
                "## 2026-05-11 22:00:00Z — Andy\n\n"
                "First message\n\n"
                "## 2026-05-11 22:05:00Z — Luna\n\n"
                "Reply\n",
                encoding="utf-8",
            )
            turns = list(parse_turns_from_file(md))
        self.assertEqual(len(turns), 2)
        ts1, a1, c1 = turns[0]
        ts2, a2, c2 = turns[1]
        self.assertEqual(ts1, "2026-05-11 22:00:00Z")
        self.assertEqual(a1, "Andy")
        self.assertEqual(c1, "First message")
        self.assertEqual(a2, "Luna")
        self.assertEqual(c2, "Reply")

    def test_handles_file_with_no_turns(self):
        with tempfile.TemporaryDirectory() as td:
            md = Path(td) / "empty.md"
            md.write_text("just header, no turns yet", encoding="utf-8")
            self.assertEqual(list(parse_turns_from_file(md)), [])

    def test_missing_file_yields_nothing(self):
        self.assertEqual(list(parse_turns_from_file(Path("/nonexistent.md"))), [])


if __name__ == "__main__":
    unittest.main()
