"""SQLite store for the review queue.

Local only, by design: the database holds titles and pointers, and it never
leaves the Mac. Four tables:

    items    current state of each item
    events   append-only: every create, decision, apply and failure
    renders  which Telegram messages show which item, so they can be edited
    meta     small key/values, e.g. the pusher's heartbeat

Producers call raise_item/clear/sync. They are idempotent, so a producer that
re-reports the same condition every run does not create duplicates:

  - incident items (alerts) are keyed by (source, key) while the condition
    holds. clear() resolves them; the same key raised later is a new item.
  - unique items (captures) are keyed by content. Once an item exists for a
    key, raising that key again is a no-op forever, so a working tree that
    still shows an already-filed file cannot bring it back.

State machine:

    open ──decide──▶ decided ──claim──▶ applying ──▶ applied | apply_failed
     │ ▲                                                  │
     │ └──wake── snoozed                    retry ◀───────┘
     └──clear──▶ superseded

Every transition is a conditional UPDATE, so two processes (the bridge and the
CLI) can never both decide or both apply the same item.

Several processes open this file (the Telegram bridge, the canary, the output
watchdog, auto-reload), so connections are short-lived and WAL-journaled with
a busy timeout.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

DB_ENV = "NB_REVIEW_QUEUE_DB"
DEFAULT_DB = Path.home() / "Library" / "Application Support" / "neural-bridge" / "review-queue.db"

# Kinds in the Phase 1 scope. Slice A produces capture and alert; probe is the
# health check's own item and is hidden from lists and stats.
CAPTURE, ALERT, QUESTION, ACTION, PR_REVIEW, PUBLISH, PROBE = (
    "capture", "alert", "question", "action", "pr_review", "publish", "probe")
KINDS = frozenset({CAPTURE, ALERT, QUESTION, ACTION, PR_REVIEW, PUBLISH, PROBE})

OPEN, SNOOZED, DECIDED, APPLYING, APPLIED, APPLY_FAILED, SUPERSEDED = (
    "open", "snoozed", "decided", "applying", "applied", "apply_failed", "superseded")
# Andy still owes these a decision (or a retry).
WAITING = (OPEN, SNOOZED, APPLY_FAILED)
# Nothing further will happen to these.
FINAL = (APPLIED, SUPERSEDED)

APPROVE, REJECT, ACKNOWLEDGE = "approve", "reject", "acknowledge"
VERBS = frozenset({APPROVE, REJECT, ACKNOWLEDGE, "edit", "answer", "rewind"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    source       TEXT NOT NULL,
    key          TEXT NOT NULL,
    agent        TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    ref          TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL DEFAULT '{}',
    state        TEXT NOT NULL,
    verb         TEXT NOT NULL DEFAULT '',
    result       TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    decided_at   INTEGER,
    snooze_until INTEGER,
    resolved_at  INTEGER,
    rev          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_source_key ON items(source, key);
CREATE INDEX IF NOT EXISTS items_state ON items(state);
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    at      INTEGER NOT NULL,
    actor   TEXT NOT NULL,
    event   TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_item ON events(item_id);
CREATE TABLE IF NOT EXISTS renders (
    item_id    TEXT NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    shown_at   INTEGER NOT NULL,
    shown_rev  INTEGER NOT NULL DEFAULT 0,
    closed_at  INTEGER,
    PRIMARY KEY (item_id, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

_COLUMNS = ("id", "kind", "source", "key", "agent", "title", "detail", "ref", "payload",
            "state", "verb", "result", "created_at", "updated_at", "decided_at",
            "snooze_until", "resolved_at", "rev")


@dataclass
class Item:
    id: str
    kind: str
    source: str
    key: str
    agent: str
    title: str
    detail: str
    ref: str
    payload: dict = field(default_factory=dict)
    state: str = OPEN
    verb: str = ""
    result: str = ""
    created_at: int = 0
    updated_at: int = 0
    decided_at: int | None = None
    snooze_until: int | None = None
    resolved_at: int | None = None
    # Bumped on every state change. A Telegram message records the rev it
    # drew, so "does this card need redrawing" never depends on two events
    # landing in different seconds.
    rev: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Item":
        data = {k: row[k] for k in _COLUMNS}
        try:
            data["payload"] = json.loads(data["payload"] or "{}")
        except json.JSONDecodeError:
            data["payload"] = {}
        return cls(**data)


@dataclass(frozen=True)
class Render:
    item_id: str
    chat_id: int
    message_id: int
    shown_at: int
    shown_rev: int
    closed_at: int | None


def db_path() -> Path:
    raw = os.environ.get(DB_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    if "unittest" in sys.modules or "pytest" in sys.modules:
        # A test that reaches a producer (the canary's main, say) must never
        # write into Andy's real queue: that would push fake cards to his
        # phone. Same rule as fleet_heartbeat._suppressed().
        return Path(tempfile.gettempdir()) / f"nb-review-queue-test-{os.getpid()}" / "review-queue.db"
    return DEFAULT_DB


def _now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else db_path()
        self._ready = False

    # ---------- plumbing ----------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            self._init()
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            yield conn
        finally:
            conn.close()

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        if fresh:
            # Titles only, but still nobody else's business.
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._ready = True

    @staticmethod
    def _event(conn: sqlite3.Connection, item_id: str, actor: str, event: str,
               detail: str = "", at: int | None = None) -> None:
        conn.execute("INSERT INTO events (item_id, at, actor, event, detail) VALUES (?, ?, ?, ?, ?)",
                     (item_id, at if at is not None else _now(), actor, event, detail))

    def _new_id(self, conn: sqlite3.Connection) -> str:
        # Six hex characters: short enough to type in the CLI, and it keeps
        # Telegram callback data ("q:<id>:<action>") far under its 64 bytes.
        while True:
            candidate = secrets.token_hex(3)
            if not conn.execute("SELECT 1 FROM items WHERE id = ?", (candidate,)).fetchone():
                return candidate

    # ---------- producers ----------

    def raise_item(self, *, source: str, key: str, kind: str, title: str,
                   detail: str = "", ref: str = "", agent: str = "",
                   payload: dict | None = None, unique: bool = False,
                   now: int | None = None) -> tuple[Item, bool]:
        """Create the item unless it already exists. Returns (item, created).

        A repeat raise of a live incident refreshes its detail (an alert's
        "overdue for 9 days" becomes "10 days") without logging an event: a
        producer that re-reports hourly must not flood the log.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        now = now if now is not None else _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if unique:
                    row = conn.execute(
                        "SELECT * FROM items WHERE source = ? AND key = ? ORDER BY created_at LIMIT 1",
                        (source, key)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM items WHERE source = ? AND key = ? AND resolved_at IS NULL "
                        "ORDER BY created_at DESC LIMIT 1", (source, key)).fetchone()
                if row is not None:
                    if not unique and (row["detail"] != detail or row["title"] != title
                                       or row["ref"] != ref):
                        conn.execute("UPDATE items SET title = ?, detail = ?, ref = ? WHERE id = ?",
                                     (title, detail, ref, row["id"]))
                        row = conn.execute("SELECT * FROM items WHERE id = ?", (row["id"],)).fetchone()
                    conn.execute("COMMIT")
                    return Item.from_row(row), False
                item_id = self._new_id(conn)
                conn.execute(
                    "INSERT INTO items (id, kind, source, key, agent, title, detail, ref, payload, "
                    "state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item_id, kind, source, key, agent, title, detail, ref,
                     json.dumps(payload or {}, sort_keys=True), OPEN, now, now))
                self._event(conn, item_id, source, "created", key, at=now)
                row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
                conn.execute("COMMIT")
                return Item.from_row(row), True
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def clear(self, *, source: str, key: str, now: int | None = None) -> Item | None:
        """The producer says the condition is gone. Resolves the live item.

        An item Andy has not decided on is superseded (nothing left to decide);
        one he already handled keeps its state and is only marked resolved.
        """
        now = now if now is not None else _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM items WHERE source = ? AND key = ? AND resolved_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1", (source, key)).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            new_state = SUPERSEDED if row["state"] in (OPEN, SNOOZED) else row["state"]
            conn.execute("UPDATE items SET resolved_at = ?, state = ?, updated_at = ?, rev = rev + 1 "
                         "WHERE id = ?",
                         (now, new_state, now, row["id"]))
            self._event(conn, row["id"], source, "resolved", new_state, at=now)
            row = conn.execute("SELECT * FROM items WHERE id = ?", (row["id"],)).fetchone()
            conn.execute("COMMIT")
            return Item.from_row(row)

    def sync(self, *, source: str, kind: str, entries: list[dict], unique: bool = False,
             now: int | None = None) -> tuple[list[Item], list[Item]]:
        """Make the source's live items match `entries` exactly.

        Each entry is {key, title, detail?, ref?, agent?, payload?}. Every entry
        is raised; every live item from this source whose key is absent is
        cleared. For a producer that knows its full current state (the output
        watchdog, the canary, the quarantine directory) this is the whole
        contract, and a check removed from its list resolves on its own.
        Returns (created, cleared).
        """
        now = now if now is not None else _now()
        created: list[Item] = []
        wanted = set()
        for e in entries:
            key = str(e["key"])
            wanted.add(key)
            item, is_new = self.raise_item(
                source=source, key=key, kind=kind, title=str(e["title"]),
                detail=str(e.get("detail", "")), ref=str(e.get("ref", "")),
                agent=str(e.get("agent", "")), payload=e.get("payload") or {},
                unique=unique, now=now)
            if is_new:
                created.append(item)
        with self._conn() as conn:
            live = [r["key"] for r in conn.execute(
                "SELECT key FROM items WHERE source = ? AND resolved_at IS NULL", (source,))]
        cleared = [c for c in (self.clear(source=source, key=k, now=now)
                               for k in sorted(set(live) - wanted)) if c is not None]
        return created, cleared

    # ---------- decisions ----------

    def decide(self, item_id: str, verb: str, *, actor: str, note: str = "",
               now: int | None = None) -> bool:
        """Record Andy's decision. False if the item is not waiting on one
        (already decided elsewhere, superseded, or unknown)."""
        if verb not in VERBS:
            raise ValueError(f"unknown verb {verb!r}")
        now = now if now is not None else _now()
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE items SET state = ?, verb = ?, decided_at = ?, updated_at = ?, "
                "snooze_until = NULL, rev = rev + 1 WHERE id = ? AND state IN (?, ?)",
                (DECIDED, verb, now, now, item_id, OPEN, SNOOZED))
            if cur.rowcount != 1:
                conn.execute("COMMIT")
                return False
            self._event(conn, item_id, actor, "decided", f"{verb}: {note}" if note else verb, at=now)
            conn.execute("COMMIT")
            return True

    def snooze(self, item_id: str, until: int, *, actor: str, now: int | None = None) -> bool:
        now = now if now is not None else _now()
        return self._transition(item_id, (OPEN,), SNOOZED, actor=actor, event="snoozed",
                                detail=str(until), now=now, extra={"snooze_until": until})

    def wake_due(self, now: int | None = None) -> list[str]:
        """Snoozed items whose time is up go back to open. Returns their ids."""
        now = now if now is not None else _now()
        with self._conn() as conn:
            due = [r["id"] for r in conn.execute(
                "SELECT id FROM items WHERE state = ? AND snooze_until <= ?", (SNOOZED, now))]
        return [i for i in due if self._transition(i, (SNOOZED,), OPEN, actor="queue",
                                                    event="reopened", now=now,
                                                    extra={"snooze_until": None})]

    def claim(self, item_id: str, *, actor: str, now: int | None = None) -> bool:
        """decided -> applying. Only one caller ever wins."""
        return self._transition(item_id, (DECIDED,), APPLYING, actor=actor,
                                event="applying", now=now)

    def finish(self, item_id: str, ok: bool, result: str, *, actor: str,
               resolve: bool = False, now: int | None = None) -> bool:
        """applying -> applied | apply_failed. `resolve` marks the underlying
        condition done too (a filed capture is gone from quarantine)."""
        now = now if now is not None else _now()
        extra: dict = {"result": result[:500]}
        if ok and resolve:
            extra["resolved_at"] = now
        return self._transition(item_id, (APPLYING,), APPLIED if ok else APPLY_FAILED,
                                actor=actor, event="applied" if ok else "apply_failed",
                                detail=result[:500], now=now, extra=extra)

    def retry(self, item_id: str, *, actor: str, now: int | None = None) -> bool:
        """apply_failed -> decided, so the applier runs again."""
        return self._transition(item_id, (APPLY_FAILED,), DECIDED, actor=actor,
                                event="retry", now=now)

    def fail_stale_applying(self, older_than: int, *, now: int | None = None) -> list[str]:
        """An apply that never finished (the process died mid-way) becomes a
        visible failure with a Retry button, never a silent hang."""
        now = now if now is not None else _now()
        with self._conn() as conn:
            stale = [r["id"] for r in conn.execute(
                "SELECT id FROM items WHERE state = ? AND updated_at < ?", (APPLYING, older_than))]
        return [i for i in stale if self._transition(
            i, (APPLYING,), APPLY_FAILED, actor="queue", event="apply_failed",
            detail="interrupted: the applier never finished", now=now,
            extra={"result": "interrupted: the applier never finished; retry is safe"})]

    def _transition(self, item_id: str, from_states: tuple[str, ...], to_state: str, *,
                    actor: str, event: str, detail: str = "", now: int | None = None,
                    extra: dict | None = None) -> bool:
        now = now if now is not None else _now()
        sets = {"state": to_state, "updated_at": now, **(extra or {})}
        assignments = ", ".join(f"{k} = ?" for k in sets)
        marks = ", ".join("?" for _ in from_states)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"UPDATE items SET {assignments}, rev = rev + 1 WHERE id = ? AND state IN ({marks})",
                (*sets.values(), item_id, *from_states))
            if cur.rowcount != 1:
                conn.execute("COMMIT")
                return False
            self._event(conn, item_id, actor, event, detail or to_state, at=now)
            conn.execute("COMMIT")
            return True

    def log_event(self, item_id: str, *, actor: str, event: str, detail: str = "") -> None:
        with self._conn() as conn:
            self._event(conn, item_id, actor, event, detail)

    # ---------- reads ----------

    def get(self, item_id: str) -> Item | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return Item.from_row(row) if row else None

    def items(self, *, states: tuple[str, ...] | None = None,
              kinds: tuple[str, ...] | None = None, include_probes: bool = False) -> list[Item]:
        sql, args = "SELECT * FROM items WHERE 1 = 1", []
        if states:
            sql += f" AND state IN ({', '.join('?' for _ in states)})"
            args.extend(states)
        if kinds:
            sql += f" AND kind IN ({', '.join('?' for _ in kinds)})"
            args.extend(kinds)
        if not include_probes:
            sql += " AND kind != ?"
            args.append(PROBE)
        sql += " ORDER BY created_at, rowid"
        with self._conn() as conn:
            return [Item.from_row(r) for r in conn.execute(sql, args)]

    def events(self, item_id: str | None = None, *, since: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1 = 1", []
        if item_id is not None:
            sql += " AND item_id = ?"
            args.append(item_id)
        if since is not None:
            sql += " AND at >= ?"
            args.append(since)
        sql += " ORDER BY seq"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    # ---------- Telegram renders ----------

    def add_render(self, item: Item, chat_id: int, message_id: int,
                   now: int | None = None) -> None:
        now = now if now is not None else _now()
        item_id = item.id
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO renders (item_id, chat_id, message_id, shown_at, "
                         "shown_rev) VALUES (?, ?, ?, ?, ?)",
                         (item_id, chat_id, message_id, now, item.rev))
            self._event(conn, item_id, "telegram", "rendered", f"chat {chat_id}", at=now)

    def open_renders(self, item_id: str | None = None) -> list[Render]:
        sql, args = "SELECT * FROM renders WHERE closed_at IS NULL", []
        if item_id is not None:
            sql += " AND item_id = ?"
            args.append(item_id)
        with self._conn() as conn:
            return [Render(**dict(r)) for r in conn.execute(sql, args)]

    def mark_shown(self, render: Render, rev: int, *, close: bool, now: int | None = None) -> None:
        """Record that `render` now shows the item as of `rev`. Never moves
        backwards, so a slow redraw cannot hide a newer change."""
        now = now if now is not None else _now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE renders SET shown_at = ?, shown_rev = MAX(shown_rev, ?), closed_at = ? "
                "WHERE item_id = ? AND chat_id = ? AND message_id = ?",
                (now, rev, now if close else None, render.item_id, render.chat_id,
                 render.message_id))

    def needing_render(self, kinds: tuple[str, ...]) -> list[Item]:
        """Open items of pushable kinds that no live message shows."""
        if not kinds:
            return []
        marks = ", ".join("?" for _ in kinds)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM items i WHERE i.state = ? AND i.kind IN ({marks}) AND NOT EXISTS "
                "(SELECT 1 FROM renders r WHERE r.item_id = i.id AND r.closed_at IS NULL) "
                "ORDER BY i.created_at, i.rowid", (OPEN, *kinds)).fetchall()
        return [Item.from_row(r) for r in rows]

    def stale_renders(self) -> list[tuple[Render, Item]]:
        """Live messages whose item changed since they were last drawn."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT r.item_id AS r_item, r.chat_id, r.message_id, r.shown_at, r.shown_rev, "
                "r.closed_at, i.* FROM renders r JOIN items i ON i.id = r.item_id "
                "WHERE r.closed_at IS NULL AND i.rev > r.shown_rev ORDER BY i.rowid").fetchall()
        return [(Render(r["r_item"], r["chat_id"], r["message_id"], r["shown_at"],
                        r["shown_rev"], r["closed_at"]), Item.from_row(r)) for r in rows]

    # ---------- meta ----------

    def meta_set(self, k: str, v: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))

    def meta_get(self, k: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else None

    # ---------- measurement ----------

    def stats(self, *, since: int, now: int | None = None) -> dict:
        """The Phase 1 evaluation numbers, from the event log.

        decisions: items Andy decided in the window, by kind
        median_hours_to_decision: created -> decided, per kind
        created: items created in the window, by source (a wired producer
                 that goes quiet shows up here as a zero)
        waiting: items still owed a decision, with the oldest age in days
        """
        now = now if now is not None else _now()
        with self._conn() as conn:
            decided = conn.execute(
                "SELECT kind, decided_at - created_at AS wait FROM items "
                "WHERE decided_at >= ? AND kind != ?", (since, PROBE)).fetchall()
            created = conn.execute(
                "SELECT source, COUNT(*) AS n FROM items WHERE created_at >= ? AND kind != ? "
                "GROUP BY source", (since, PROBE)).fetchall()
            waiting = conn.execute(
                f"SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM items "
                f"WHERE state IN ({', '.join('?' for _ in WAITING)}) AND kind != ?",
                (*WAITING, PROBE)).fetchone()
        by_kind: dict[str, list[int]] = {}
        for r in decided:
            by_kind.setdefault(r["kind"], []).append(max(0, int(r["wait"] or 0)))

        def _median(xs: list[int]) -> float:
            xs = sorted(xs)
            mid = len(xs) // 2
            return float(xs[mid]) if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2

        return {
            "window_days": round((now - since) / 86400, 1),
            "decisions": {k: len(v) for k, v in sorted(by_kind.items())},
            "median_hours_to_decision": {k: round(_median(v) / 3600, 1)
                                         for k, v in sorted(by_kind.items())},
            "created": {r["source"]: r["n"] for r in created},
            "waiting": waiting["n"],
            "oldest_waiting_days": (round((now - waiting["oldest"]) / 86400, 1)
                                    if waiting["oldest"] else None),
        }
