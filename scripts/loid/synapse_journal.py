"""Loid's CLI for reading and writing Synapse's career-intelligence journal.

Loid invokes this via Bash. The CLI is also useful for Andy directly from a
terminal.

Reads/writes the `journal_entries` table in Synapse's SQLite DB at
`~/Development/Synapse/data/agent_i.db` (override with $SYNAPSE_DB_PATH).

Usage:
    synapse-journal read [--tag T] [--source S] [--since YYYY-MM-DD] [--limit N]
    synapse-journal write "content" [--tags a,b,c] [--source loid]

The `read` subcommand outputs markdown blocks Loid can quote directly. The
`write` subcommand inserts a new row with `source='loid'` by default, generates
a fresh UUID-style id, and stamps the current date / timestamp. Confirms the
row was written by echoing the assigned id.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / "Development" / "Synapse" / "data" / "agent_i.db"


def resolve_db_path(arg_path: Path | None) -> Path:
    if arg_path is not None:
        return arg_path.expanduser()
    env_path = os.environ.get("SYNAPSE_DB_PATH")
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_DB_PATH


def open_conn(db_path: Path, *, writable: bool = False) -> sqlite3.Connection:
    if writable:
        conn = sqlite3.connect(str(db_path))
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(t) for t in value] if isinstance(value, list) else []


def format_entry(row: sqlite3.Row) -> str:
    tags = parse_tags(row["tags"])
    src = row["source"] or "direct"
    lines: list[str] = []
    lines.append(f"## {row['date']} — {src} `{row['id']}`")
    if tags:
        lines.append(f"**Tags:** {', '.join(tags)}")
    if row["artifact_id"]:
        lines.append(f"**Source artifact:** `{row['artifact_id']}`")
    lines.append("")
    content = row["content"] or ""
    for line in content.splitlines() or [""]:
        lines.append(f"> {line}".rstrip())
    lines.append("")
    return "\n".join(lines)


def cmd_read(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args.db)
    if not db_path.exists():
        print(f"error: synapse db not found at {db_path}", file=sys.stderr)
        return 2

    where: list[str] = []
    params: list[object] = []

    if args.tag:
        where.append("tags LIKE ?")
        params.append(f'%"{args.tag}"%')
    if args.source:
        where.append("source = ?")
        params.append(args.source)
    if args.since:
        where.append("date >= ?")
        params.append(args.since)
    if args.until:
        where.append("date <= ?")
        params.append(args.until)

    sql = "SELECT id, date, created_at, content, tags, source, artifact_id FROM journal_entries"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(args.limit))

    conn = open_conn(db_path, writable=False)
    try:
        rows = list(conn.execute(sql, params))
    finally:
        conn.close()

    if not rows:
        print("(no entries matched)", file=sys.stderr)
        return 0

    print(f"# Synapse journal — {len(rows)} entries\n")
    for row in rows:
        print(format_entry(row))
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(args.db)
    if not db_path.exists():
        print(f"error: synapse db not found at {db_path}", file=sys.stderr)
        return 2

    content = args.content.strip()
    if not content:
        print("error: refusing to write empty entry", file=sys.stderr)
        return 2

    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    entry_id = uuid.uuid4().hex[:8]
    today = datetime.now().strftime("%Y-%m-%d")
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")

    conn = open_conn(db_path, writable=True)
    try:
        conn.execute(
            "INSERT INTO journal_entries (id, date, created_at, content, tags, source, artifact_id, insights_extracted) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, '[]')",
            (entry_id, today, created_at, content, json.dumps(tags), args.source),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"✓ wrote entry id={entry_id} source={args.source} tags={tags}", file=sys.stderr)
    print(entry_id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synapse-journal",
        description="Read/write Synapse's career-intelligence journal.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to Synapse agent_i.db (default: ~/Development/Synapse/data/agent_i.db).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    read_p = sub.add_parser("read", help="Read journal entries with optional filters.")
    read_p.add_argument("--tag", help="Filter by tag (substring match in JSON tags field).")
    read_p.add_argument("--source", help="Filter by source (meeting, document, email, direct, voice, chat, ado, mobile, loid).")
    read_p.add_argument("--since", help="Only entries on or after this date (YYYY-MM-DD).")
    read_p.add_argument("--until", help="Only entries on or before this date (YYYY-MM-DD).")
    read_p.add_argument("--limit", type=int, default=20, help="Max entries to return (default: 20).")
    read_p.set_defaults(func=cmd_read)

    write_p = sub.add_parser("write", help="Insert a new journal entry.")
    write_p.add_argument("content", help="The entry text (verbatim, in Andy's voice).")
    write_p.add_argument("--tags", default="", help="Comma-separated tags (e.g. 'governance,leadership').")
    write_p.add_argument("--source", default="loid", help="Source attribution (default: loid).")
    write_p.set_defaults(func=cmd_write)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
