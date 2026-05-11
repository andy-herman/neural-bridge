"""Semantic search across the per-agent conversation archive.

Adds an embedding index over the markdown files written by
`conversation_log.append_turn`. Each turn (one `## <ts> — <author>`
section) gets embedded via Ollama's local `bge-m3` model and stored in a
sqlite-vec virtual table. Agents can query the index for top-N
semantically similar prior turns via the `search_conversation_memory`
action.

Why this exists: literal Grep across the archive misses synonyms and
paraphrase. If Andy mentions "the regulation we reviewed" in July and
the May conversation used a different name, Grep returns nothing. With
embeddings, the May turn is in the same vector neighborhood and
ranks high.

Architecture:
- **Embedding service:** local Ollama daemon, `bge-m3` model (1024-d,
  multilingual incl. Korean). No external API, no network egress, no
  per-call cost. Sub-second on M-series Macs. Requires Ollama running
  on localhost:11434.
- **Storage:** SQLite with the `sqlite-vec` extension at
  `~/Library/Application Support/neural-bridge/embeddings.db`. One
  virtual table per agent: `embeddings_<agent_id>`. Schema:
  `(turn_id, file_path, timestamp, content, embedding[1024])`.
- **Indexing:** `index_turn()` called from `conversation_log.append_turn`
  after a successful write. Synchronous but fast (~200ms).
  Idempotent via SHA-256 of file_path+content; re-indexing the same
  turn is a no-op.
- **Querying:** `search()` embeds the query, runs vec_distance against
  the agent's table, returns top-N rows ordered by similarity.

Graceful degradation: if Ollama is unreachable or sqlite-vec fails to
load, index_turn silently no-ops (logged at WARNING) and the conversation
flow continues. Search returns an empty list. The archive is still
intact on disk; missing embeddings just means the search misses some
results until the next index_turn succeeds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_logger = logging.getLogger("nb_discord.semantic_search")

# ---- Config ----

OLLAMA_URL = "http://localhost:11434/api/embeddings"
OLLAMA_MODEL = "bge-m3"
EMBEDDING_DIM = 1024  # bge-m3 native dimension
OLLAMA_TIMEOUT_S = 30

DB_DIR = Path.home() / "Library" / "Application Support" / "neural-bridge"
DB_PATH = DB_DIR / "embeddings.db"

# Section header pattern in the conversation log markdown files:
#   ## 2026-05-11 22:34:15Z — Andy
_TURN_HEADER_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z) — (.+?)$",
    re.MULTILINE,
)


@dataclass
class SearchResult:
    """One row returned by `search()`."""
    file_path: str
    timestamp: str
    author: str
    content: str
    distance: float  # smaller = more similar (cosine distance, 0 to 2)


# ---- Embedding client ----

def embed(text: str, *, timeout: float = OLLAMA_TIMEOUT_S) -> list[float] | None:
    """Call Ollama's embedding endpoint for `bge-m3`. Returns the 1024-d
    vector, or None if Ollama is unreachable or returns bad data.

    Never raises — failures log a warning and return None so callers can
    handle absent embeddings as a degraded-but-functional state.
    """
    if not text or not text.strip():
        return None
    payload = json.dumps({"model": OLLAMA_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError) as exc:
        _logger.warning("embed: Ollama unreachable: %s: %s", type(exc).__name__, exc)
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _logger.warning("embed: bad response: %s: %s", type(exc).__name__, exc)
        return None

    vec = data.get("embedding")
    if not isinstance(vec, list) or len(vec) != EMBEDDING_DIM:
        _logger.warning("embed: unexpected response shape (dim=%s, type=%s)",
                        len(vec) if isinstance(vec, list) else "?", type(vec).__name__)
        return None
    return vec


def _vec_to_bytes(vec: list[float]) -> bytes:
    """sqlite-vec stores vectors as packed float32 little-endian."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _turn_id(file_path: str, content: str) -> str:
    """Stable ID per (file × content) so we can dedup re-indexing."""
    h = hashlib.sha256()
    h.update(file_path.encode("utf-8"))
    h.update(b"\x00")
    h.update(content.encode("utf-8"))
    return h.hexdigest()[:32]


# ---- Index store ----

class EmbeddingIndex:
    """Per-agent embedding store backed by sqlite-vec.

    One virtual table per agent (`embeddings_<sanitized_agent_id>`). Tables
    are created lazily on first index_turn for that agent. The schema is:

        CREATE VIRTUAL TABLE embeddings_<agent> USING vec0(
            turn_id TEXT PRIMARY KEY,
            file_path TEXT,
            timestamp TEXT,
            author TEXT,
            content TEXT,
            embedding float[1024]
        );

    The connection enables_load_extension on construction and loads
    sqlite-vec. If sqlite-vec fails to load (missing extension, etc.),
    the constructor raises — callers should treat that as "indexing
    disabled," not a fatal error.
    """

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.enable_load_extension(True)
        try:
            import sqlite_vec
            sqlite_vec.load(self._db)
        except Exception as exc:
            self._db.close()
            raise RuntimeError(f"sqlite-vec load failed: {exc}") from exc
        self._db.enable_load_extension(False)
        # Track which agent tables we've already ensured exist this session,
        # so we don't run a CREATE TABLE on every index call.
        self._ensured_agents: set[str] = set()

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    def _table_name(self, agent_id: str) -> str:
        """Sanitize agent_id into a safe SQL identifier."""
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", agent_id)
        return f"embeddings_{safe}"

    def _ensure_table(self, agent_id: str) -> str:
        table = self._table_name(agent_id)
        if agent_id in self._ensured_agents:
            return table
        self._db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
            f"  turn_id TEXT PRIMARY KEY,"
            f"  file_path TEXT,"
            f"  timestamp TEXT,"
            f"  author TEXT,"
            f"  content TEXT,"
            f"  embedding float[{EMBEDDING_DIM}]"
            f")"
        )
        self._db.commit()
        self._ensured_agents.add(agent_id)
        return table

    def index_turn(self, agent_id: str, file_path: str, timestamp: str,
                   author: str, content: str) -> bool:
        """Embed + insert one turn. Idempotent: re-indexing the same turn
        (same file_path + content) is a no-op via PRIMARY KEY conflict.

        Returns True if a new row was inserted, False otherwise (already
        indexed, empty content, or embedding service unavailable).
        """
        content = (content or "").strip()
        if not content:
            return False
        turn_id = _turn_id(file_path, content)
        # Cheap dedup before paying the embed cost.
        try:
            table = self._ensure_table(agent_id)
            row = self._db.execute(
                f"SELECT turn_id FROM {table} WHERE turn_id = ?", (turn_id,),
            ).fetchone()
            if row is not None:
                return False
        except sqlite3.DatabaseError as exc:
            _logger.warning("index_turn: dedup check failed: %s", exc)
            return False

        vec = embed(content)
        if vec is None:
            return False  # Ollama unavailable; logged inside embed()

        try:
            self._db.execute(
                f"INSERT INTO {table} (turn_id, file_path, timestamp, author, content, embedding) "
                f"VALUES (?, ?, ?, ?, ?, ?)",
                (turn_id, file_path, timestamp, author, content, _vec_to_bytes(vec)),
            )
            self._db.commit()
            return True
        except sqlite3.IntegrityError:
            # Race: another caller inserted the same turn_id between dedup and insert.
            return False
        except sqlite3.DatabaseError as exc:
            _logger.warning("index_turn: insert failed: %s", exc)
            return False

    def search(self, agent_id: str, query: str, *, top_n: int = 5) -> list[SearchResult]:
        """Return top-N turns most similar to `query`. Empty list if the
        agent has no table yet, the query embeds to nothing, or any
        database failure occurs."""
        query = (query or "").strip()
        if not query:
            return []
        vec = embed(query)
        if vec is None:
            return []
        table = self._table_name(agent_id)
        try:
            rows = self._db.execute(
                f"SELECT file_path, timestamp, author, content, distance "
                f"FROM {table} "
                f"WHERE embedding MATCH ? "
                f"ORDER BY distance "
                f"LIMIT ?",
                (_vec_to_bytes(vec), top_n),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            _logger.debug("search: query failed (table may not exist): %s", exc)
            return []
        return [SearchResult(*r) for r in rows]


# Module-level singleton, lazily constructed so module import doesn't
# touch the filesystem.
_STORE: EmbeddingIndex | None = None


def get_store() -> EmbeddingIndex | None:
    """Return the shared EmbeddingIndex, or None if sqlite-vec can't load.
    Constructed on first call so import-time failures don't poison the
    whole daemon."""
    global _STORE
    if _STORE is not None:
        return _STORE
    try:
        _STORE = EmbeddingIndex()
        return _STORE
    except Exception as exc:  # noqa: BLE001 — degraded mode is fine
        _logger.warning("get_store: EmbeddingIndex unavailable: %s: %s",
                        type(exc).__name__, exc)
        return None


# ---- Helpers for indexing turns from append_turn ----

def index_turn_from_append(agent_id: str, file_path: Path, author: str,
                           content: str, timestamp: str) -> bool:
    """Convenience wrapper called from conversation_log.append_turn.

    Best-effort: returns False (and logs a warning) on any failure. Never
    raises. Safe to call from the daemon's append-turn path; total
    overhead is dominated by the Ollama embed call (~200ms on M-series).
    """
    store = get_store()
    if store is None:
        return False
    try:
        return store.index_turn(
            agent_id=agent_id,
            file_path=str(file_path),
            timestamp=timestamp,
            author=author,
            content=content,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("index_turn_from_append: %s: %s", type(exc).__name__, exc)
        return False


# ---- Backfill from existing markdown files ----

def parse_turns_from_file(path: Path) -> Iterable[tuple[str, str, str]]:
    """Walk a conversation-log markdown file and yield (timestamp, author,
    content) for each `## <ts> — <author>` section. Used by backfill.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    # Find each section header and the body until the next header.
    matches = list(_TURN_HEADER_RE.finditer(text))
    for i, m in enumerate(matches):
        ts, author = m.group(1), m.group(2).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[body_start:body_end].strip()
        if content:
            yield ts, author, content


def backfill_agent_archive(agent_id: str, agents_base: Path,
                           *, log_every: int = 50) -> tuple[int, int]:
    """Walk `agents_base/<agent_id>/conversations/**/*.md` and index every
    turn that isn't already indexed. Returns (rows_inserted, rows_skipped).

    Slow on first run (embedding cost) but bounded: typical archive at
    Neural Bridge launch is <2000 turns total, so ~5 min full backfill at
    most. Subsequent calls skip already-indexed turns via the SHA-256
    dedup, so re-running is cheap.
    """
    store = get_store()
    if store is None:
        _logger.warning("backfill: index store unavailable, skipping agent=%s", agent_id)
        return 0, 0
    conv_dir = agents_base / agent_id / "conversations"
    if not conv_dir.exists():
        return 0, 0

    inserted = 0
    skipped = 0
    for md_file in sorted(conv_dir.rglob("*.md")):
        for ts, author, content in parse_turns_from_file(md_file):
            ok = store.index_turn(agent_id, str(md_file), ts, author, content)
            if ok:
                inserted += 1
            else:
                skipped += 1
            if (inserted + skipped) % log_every == 0:
                _logger.info("backfill agent=%s progress: inserted=%d skipped=%d",
                             agent_id, inserted, skipped)
    return inserted, skipped


def backfill_shared_archive(agents_base: Path) -> tuple[int, int]:
    """Walk `_shared/conversations/**/*.md` and index every turn under the
    pseudo-agent key `_shared`. Lets agents query cross-agent context."""
    return backfill_agent_archive("_shared", agents_base)
