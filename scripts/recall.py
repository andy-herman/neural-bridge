#!/usr/bin/env python3
"""recall.py — Level 3 semantic memory: local vector search over session
notes, daily logs, decisions, and vault memory folders.

Fills the deferred Level 3 slot in the Claude Code memory framework
(Levels 1/2/5 shipped — see AGENTS.md). Embeds markdown chunks on-device
with ChromaDB's bundled ONNX MiniLM model (all-MiniLM-L6-v2, 384 dims) —
the same stack Bellwether uses. No cloud round-trip; the DB lives under
data/recall/ (gitignored, machine-local per the local-only DB rule).

Usage:
  python3 scripts/recall.py index              # incremental (mtime-based)
  python3 scripts/recall.py index --full       # rebuild from scratch
  python3 scripts/recall.py search "query" [-k 8] [--source TAG]
  python3 scripts/recall.py status             # corpus counts, last run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
HOME = Path.home()
VAULT = HOME / "Documents" / "Luna Master"
DATA_DIR = REPO_ROOT / "data" / "recall"
MANIFEST_FILE = DATA_DIR / "manifest.json"
COLLECTION_NAME = "recall"

# source tag -> root directory. All markdown under each root is indexed
# (recursively), minus EXCLUDE_DIRS. Vault "Neural Bridge/_Memory" is a
# curated copy of claude-memory and is skipped to avoid duplicate hits.
SOURCES: dict[str, Path] = {
    "vault-sessions": VAULT / "AI Agents - Copilot" / "Sessions",
    "vault-memory": VAULT / "AI Agents - Copilot" / "Memory",
    "vault-daily": VAULT / "Daily",
    "vault-meetings": VAULT / "Meetings",
    "nb-knowledge": REPO_ROOT / "knowledge",
    "nb-daily-logs": REPO_ROOT / "daily-logs",
    "nb-decisions": REPO_ROOT / "decisions",
    "claude-memory": HOME
    / ".claude"
    / "projects"
    / "-Users-andyherman-Desktop-Andy-Herman"
    / "memory",
}

EXCLUDE_DIRS = {".obsidian", ".git", ".trash", "Attachments", "__pycache__"}
MAX_FILE_BYTES = 1_000_000
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200


# ---------------------------------------------------------------- chunking


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections on ##/### headings.

    Content before the first heading gets heading "". Frontmatter is kept
    with the preamble — it often carries the only metadata a note has.
    """
    sections: list[tuple[str, str]] = []
    heading = ""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("### "):
            if lines:
                sections.append((heading, "\n".join(lines).strip()))
            heading = line.lstrip("#").strip()
            lines = []
        else:
            lines.append(line)
    if lines:
        sections.append((heading, "\n".join(lines).strip()))
    return [(h, b) for h, b in sections if b]


def chunk_text(body: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Hard-split an oversized section into overlapping windows."""
    if len(body) <= size:
        return [body]
    chunks = []
    start = 0
    while start < len(body):
        chunks.append(body[start : start + size])
        if start + size >= len(body):
            break
        start += size - overlap
    return chunks


MIN_CONTENT_CHARS = 40


def has_content(body: str) -> bool:
    """True if a section holds real prose, not just list bullets/dashes.

    Session-note templates leave empty scaffolding sections ("- —"); one
    note was found with 39 identical empty sections, which turn into
    identical junk vectors that crowd out real results.
    """
    stripped = "".join(ch for ch in body if ch.isalnum() or ch == " ")
    return len(stripped.strip()) >= MIN_CONTENT_CHARS


def chunk_file(path: Path, text: str) -> list[tuple[str, str]]:
    """Yield (chunk_id_suffix, embed_text) pairs for one markdown file.

    Each chunk is prefixed with the note title and section heading so the
    embedding carries document context, not just the paragraph. Near-empty
    sections are skipped and repeated sections are deduplicated.
    """
    title = path.stem
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    n = 0
    for heading, body in split_sections(text):
        if not has_content(body):
            continue
        for piece in chunk_text(body):
            key = f"{heading}\x00{piece}"
            if key in seen:
                continue
            seen.add(key)
            label = f"{title} — {heading}" if heading else title
            out.append((str(n), f"{label}\n\n{piece}"))
            n += 1
    return out


def doc_id(path: Path, suffix: str) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:16] + "#" + suffix


# ---------------------------------------------------------------- manifest


def load_manifest() -> dict[str, int]:
    if MANIFEST_FILE.exists():
        return json.loads(MANIFEST_FILE.read_text())
    return {}


def save_manifest(manifest: dict[str, int]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=0, sort_keys=True))


def iter_markdown(root: Path):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.md")):
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


# ---------------------------------------------------------------- chroma


def get_client():
    import chromadb
    from chromadb.config import Settings

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(DATA_DIR / "chroma"),
        settings=Settings(anonymized_telemetry=False),
    )


def get_collection(client):
    from chromadb.utils import embedding_functions

    return client.get_or_create_collection(
        COLLECTION_NAME,
        embedding_function=embedding_functions.DefaultEmbeddingFunction(),
        metadata={"hnsw:space": "cosine"},
    )


def display_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(HOME))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- commands


def cmd_index(full: bool, quiet: bool) -> int:
    client = get_client()
    if full:
        try:
            client.delete_collection(COLLECTION_NAME)
        except Exception:
            pass
        manifest: dict[str, int] = {}
    else:
        manifest = load_manifest()
    collection = get_collection(client)

    seen: set[str] = set()
    changed_files = 0
    changed_chunks = 0

    for source, root in SOURCES.items():
        for path in iter_markdown(root):
            key = str(path)
            seen.add(key)
            mtime = path.stat().st_mtime_ns
            if manifest.get(key) == mtime:
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            collection.delete(where={"path": key})
            chunks = chunk_file(path, text)
            if chunks:
                collection.upsert(
                    ids=[doc_id(path, s) for s, _ in chunks],
                    documents=[t for _, t in chunks],
                    metadatas=[
                        {
                            "path": key,
                            "source": source,
                            "modified": datetime.fromtimestamp(
                                mtime / 1e9, tz=timezone.utc
                            ).strftime("%Y-%m-%d"),
                        }
                        for _ in chunks
                    ],
                )
            manifest[key] = mtime
            changed_files += 1
            changed_chunks += len(chunks)
            if not quiet:
                print(f"  indexed {display_path(path)} ({len(chunks)} chunks)")

    removed = [k for k in manifest if k not in seen]
    for key in removed:
        collection.delete(where={"path": key})
        del manifest[key]

    save_manifest(manifest)
    if not quiet or changed_files:
        print(
            f"recall index: {changed_files} files updated "
            f"({changed_chunks} chunks), {len(removed)} removed, "
            f"{len(manifest)} files total"
        )
    return 0


def cmd_search(query: str, k: int, source: str | None) -> int:
    collection = get_collection(get_client())
    where = {"source": source} if source else None
    res = collection.query(query_texts=[query], n_results=k, where=where)
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = res["distances"][0]
    if not docs:
        print("no results")
        return 1
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
        score = 1 - dist
        first, _, rest = doc.partition("\n\n")
        snippet = " ".join(rest.split())[:300]
        print(f"[{i}] {score:.3f}  {meta['source']}  {display_path(Path(meta['path']))}")
        print(f"    {first}  (modified {meta['modified']})")
        print(f"    {snippet}")
        print()
    return 0


def cmd_status() -> int:
    manifest = load_manifest()
    collection = get_collection(get_client())
    print(f"files indexed:  {len(manifest)}")
    print(f"chunks stored:  {collection.count()}")
    for source, root in SOURCES.items():
        n = sum(1 for k in manifest if k.startswith(str(root) + "/"))
        state = "" if root.is_dir() else "  (missing)"
        print(f"  {source:16} {n:5}  {display_path(root)}{state}")
    if MANIFEST_FILE.exists():
        ts = datetime.fromtimestamp(MANIFEST_FILE.stat().st_mtime)
        print(f"last indexed:   {ts:%Y-%m-%d %H:%M}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="index new/changed files")
    p_index.add_argument("--full", action="store_true", help="rebuild from scratch")
    p_index.add_argument("-q", "--quiet", action="store_true")

    p_search = sub.add_parser("search", help="semantic search")
    p_search.add_argument("query")
    p_search.add_argument("-k", type=int, default=8, help="results to return")
    p_search.add_argument("--source", choices=sorted(SOURCES), default=None)

    sub.add_parser("status", help="corpus counts")

    args = parser.parse_args(argv)
    if args.cmd == "index":
        return cmd_index(full=args.full, quiet=args.quiet)
    if args.cmd == "search":
        return cmd_search(args.query, args.k, args.source)
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
