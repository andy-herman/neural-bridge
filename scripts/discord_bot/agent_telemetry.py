"""Opt-in local model-execution observations; see docs/MOONBASE_TELEMETRY.md.

No transport payload enters this module. Each process owns one source lease
and merges only that source into a private, atomically replaced snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import re
import stat
import sys
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

try:
    import fcntl
except ImportError:
    fcntl = None

ENV_PATH = "NB_AGENT_TELEMETRY_PATH"
REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES = ("discord", "telegram-luna", "telegram-loid", "telegram-council")
HEARTBEAT_SECONDS = 10
STALE_SECONDS = 45
MAX_BYTES = 512 * 1024
MAX_AGENTS = 128
MAX_EVENTS = 64
MAX_ACTIVE = 256
MAX_INTEGER = 2**53 - 1
SKEW = timedelta(seconds=5)
OUTCOMES = ("succeeded", "failed", "cancelled", "interrupted")
STATUSES = ("starting", "idle", "working", "error", "disconnected", "disabled", "stopped")
EVENTS = (
    "writer_started", "writer_stopped", "agent_enabled", "agent_disabled",
    "agent_ready", "agent_disconnected", "job_started", "job_succeeded",
    "job_failed", "job_cancelled", "job_interrupted", "wait_cancelled",
)
SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)
LABEL = re.compile(r"[A-Za-z0-9 ._'-]+\Z", re.ASCII)
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z\Z", re.ASCII)


class TelemetryError(ValueError):
    """Fixed diagnostic code, never an exception containing private input."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(now: datetime) -> str:
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _require(condition: bool, code: str = "invalid_snapshot") -> None:
    if not condition:
        raise TelemetryError(code)


def _keys(value, keys: str) -> None:
    _require(isinstance(value, dict) and set(value) == set(keys.split()))


def _integer(value, maximum: int = MAX_INTEGER) -> None:
    _require(type(value) is int and 0 <= value <= maximum)


def _slug(value) -> None:
    _require(isinstance(value, str) and len(value) <= 64 and SLUG.fullmatch(value) is not None)


def _time(value, now: datetime) -> datetime:
    _require(isinstance(value, str) and TIMESTAMP.fullmatch(value) is not None)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TelemetryError("invalid_timestamp") from None
    _require(parsed <= now + SKEW, "future_timestamp")
    return parsed


def _unique(rows, field: str, maximum: int) -> None:
    _require(isinstance(rows, list) and len(rows) <= maximum)
    _require(all(isinstance(row, dict) and isinstance(row.get(field), str) for row in rows))
    _require(len({row[field] for row in rows}) == len(rows))


def validate_snapshot(data: dict, now: datetime) -> None:
    """Validate every field before preserving it in a merge; never copy extras."""
    _keys(data, "schema_version scope generated_at registry_generated_at agents writers")
    _require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    _require(data["scope"] == "model-subprocess")
    generated = _time(data["generated_at"], now)
    _require(_time(data["registry_generated_at"], now) <= generated + SKEW)
    _unique(data["agents"], "id", MAX_AGENTS)
    for row in data["agents"]:
        _keys(row, "id display_name plugin_defined discord_registered")
        _slug(row["id"])
        label = row["display_name"]
        _require(isinstance(label, str) and 0 < len(label) <= 80 and LABEL.fullmatch(label) is not None)
        _require(type(row["plugin_defined"]) is bool and type(row["discord_registered"]) is bool)
    _unique(data["writers"], "source", len(SOURCES))
    for writer in data["writers"]:
        _keys(writer, "source instance_id status started_at heartbeat_at heartbeat_interval_seconds "
                      "stale_after_seconds agents events")
        _require(writer["source"] in SOURCES and writer["status"] in ("running", "stopped"))
        try:
            _require(isinstance(writer["instance_id"], str))
            _require(str(uuid.UUID(writer["instance_id"])) == writer["instance_id"])
        except (ValueError, AttributeError):
            raise TelemetryError("invalid_instance") from None
        beat = _time(writer["heartbeat_at"], now)
        _require(_time(writer["started_at"], now) <= beat and beat <= generated + SKEW)
        _require(type(writer["heartbeat_interval_seconds"]) is int
                 and writer["heartbeat_interval_seconds"] == HEARTBEAT_SECONDS)
        _require(type(writer["stale_after_seconds"]) is int
                 and writer["stale_after_seconds"] == STALE_SECONDS)
        _unique(writer["agents"], "id", MAX_AGENTS)
        for row in writer["agents"]:
            _keys(row, "id enabled connected status active_jobs last_activity_at "
                       "last_outcome last_outcome_at counters")
            _slug(row["id"])
            _require(type(row["enabled"]) is bool)
            _require(row["connected"] is None or type(row["connected"]) is bool)
            _require(row["status"] in STATUSES)
            _integer(row["active_jobs"], MAX_ACTIVE)
            _require((row["status"] == "working") == (row["active_jobs"] > 0))
            if writer["status"] == "stopped":
                _require(row["status"] == "stopped")
            _require(row["last_outcome"] is None or row["last_outcome"] in OUTCOMES)
            _require((row["last_outcome"] is None) == (row["last_outcome_at"] is None))
            for key in ("last_activity_at", "last_outcome_at"):
                if row[key] is not None:
                    _require(_time(row[key], now) <= beat + SKEW)
            _keys(row["counters"], "started succeeded failed cancelled interrupted")
            for value in row["counters"].values():
                _integer(value)
            _require(row["counters"]["started"] ==
                     sum(row["counters"][key] for key in OUTCOMES) + row["active_jobs"])
        events = writer["events"]
        _require(isinstance(events, list) and len(events) <= MAX_EVENTS)
        previous = 0
        for event in events:
            _keys(event, "sequence at agent_id type active_jobs")
            _integer(event["sequence"])
            _require(event["sequence"] > previous)
            previous = event["sequence"]
            _require(_time(event["at"], now) <= beat + SKEW)
            if event["agent_id"] is not None:
                _slug(event["agent_id"])
            _require(event["type"] in EVENTS)
            _integer(event["active_jobs"], MAX_ACTIVE * MAX_AGENTS if event["agent_id"] is None else MAX_ACTIVE)


def discover_registry(root: Path = REPO_ROOT) -> list[dict]:
    directory = root / "plugins/neural-bridge-core/agents"
    _require(directory.is_dir(), "registry_unavailable")
    plugin_ids = {path.stem for path in directory.glob("*.md") if path.is_file()}
    _require(bool(plugin_ids), "registry_unavailable")
    config = root / "scripts/discord_bot/agents.json"
    _require(config.stat().st_size <= MAX_BYTES, "registry_too_large")
    raw = json.loads(config.read_text(encoding="utf-8"))
    _require(isinstance(raw, dict) and isinstance(raw.get("agents"), list), "invalid_registry")
    _unique(raw["agents"], "id", MAX_AGENTS)
    configured = {row["id"]: row.get("display_name", row["id"].replace("-", " ").title())
                  for row in raw["agents"]}
    ids = plugin_ids | configured.keys()
    _require(len(ids) <= MAX_AGENTS, "registry_too_large")
    rows = []
    for agent_id in sorted(ids):
        _slug(agent_id)
        label = configured.get(agent_id, agent_id.replace("-", " ").title())
        _require(isinstance(label, str) and 0 < len(label) <= 80
                 and LABEL.fullmatch(label) is not None, "invalid_registry_label")
        rows.append({"id": agent_id, "display_name": label, "plugin_defined": agent_id in plugin_ids,
                     "discord_registered": agent_id in configured})
    return rows


def _private(info: os.stat_result, *, directory: bool = False) -> None:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    _require(kind(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == mode
             and (directory or info.st_nlink == 1), "unsafe_destination")


def _outside_checkout(directory: Path) -> None:
    for ancestor in (directory, *directory.parents):
        marker = ancestor / ".git"
        _require(not marker.exists() and not marker.is_symlink(), "destination_inside_checkout")


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


class SnapshotFile:
    def __init__(self, path: Path):
        _require(fcntl is not None, "unsupported_platform")
        _require(path.is_absolute() and bool(path.name), "absolute_path_required")
        _outside_checkout(path.parent)
        _outside_checkout(path.parent.resolve())
        missing = []
        directory = path.parent
        while not directory.exists():
            _require(not directory.is_symlink(), "unsafe_destination")
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass  # Another producer can create the same private directory.
            _private(directory.lstat(), directory=True)
        _private(path.parent.lstat(), directory=True)
        self.path = path
        self.directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _private(os.fstat(self.directory_fd), directory=True)
            self.lock_fd = self._open(path.name + ".lock", os.O_RDWR | os.O_CREAT)
        except BaseException:
            os.close(self.directory_fd)
            raise

    def _open(self, name: str, flags: int) -> int:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.directory_fd)
        try:
            _private(os.fstat(fd))
        except BaseException:
            os.close(fd)
            raise
        return fd

    def check_directory(self) -> None:
        current = self.path.parent.lstat()
        opened = os.fstat(self.directory_fd)
        _private(current, directory=True)
        _require((current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino), "destination_changed")
        _outside_checkout(self.path.parent.resolve())

    def check_lock(self, fd: int, name: str) -> None:
        opened = os.fstat(fd)
        current = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        _private(opened)
        _private(current)
        _require((current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino), "lock_replaced")

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.check_directory()
        self.check_lock(self.lock_fd, self.path.name + ".lock")
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TelemetryError("snapshot_busy") from None
        try:
            yield
        finally:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    def acquire_source(self, source: str) -> int:
        _require(source in SOURCES, "invalid_source")
        fd = self._open(self.path.name + f".{source}.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise TelemetryError("source_busy") from None
        except BaseException:
            os.close(fd)
            raise
        return fd

    def read(self, now: datetime) -> dict | None:
        try:
            fd = self._open(self.path.name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            _require(os.fstat(stream.fileno()).st_size <= MAX_BYTES, "snapshot_too_large")
            raw = stream.read(MAX_BYTES + 1)
        _require(len(raw) <= MAX_BYTES, "snapshot_too_large")
        try:
            data = json.loads(raw, object_pairs_hook=_json_object)
        except (ValueError, UnicodeError):
            raise TelemetryError("invalid_snapshot") from None
        validate_snapshot(data, now)
        return data

    def write(self, data: dict, now: datetime) -> None:
        validate_snapshot(data, now)
        raw = (json.dumps(data, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        _require(len(raw) <= MAX_BYTES, "snapshot_too_large")
        name = self.path.name + f".{uuid.uuid4().hex}.tmp"
        fd = self._open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self.check_directory()
            os.replace(name, self.path.name, src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
        finally:
            try:
                os.unlink(name, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass

    def close(self) -> None:
        os.close(self.lock_fd)
        os.close(self.directory_fd)


def _envelope(registry: list[dict], now: datetime) -> dict:
    return {"schema_version": 1, "scope": "model-subprocess", "generated_at": timestamp(now),
            "registry_generated_at": timestamp(now), "agents": copy.deepcopy(registry), "writers": []}


def export_registry(path: Path, *, root: Path = REPO_ROOT, now: datetime | None = None) -> None:
    registry = discover_registry(root)
    now = now or utc_now()
    storage = SnapshotFile(path)
    try:
        with storage.locked():
            data = storage.read(now) or _envelope(registry, now)
            data.update(agents=registry, registry_generated_at=timestamp(now), generated_at=timestamp(now))
            storage.write(data, now)
    finally:
        storage.close()


def _observation(agent_id: str, enabled: bool) -> dict:
    return {"id": agent_id, "enabled": enabled, "connected": None,
            "status": "starting" if enabled else "disabled", "active_jobs": 0,
            "last_activity_at": None, "last_outcome": None, "last_outcome_at": None,
            "counters": {key: 0 for key in ("started", *OUTCOMES)}}


def _report(log: Callable[[str], None], exc: Exception) -> None:
    code = str(exc) if isinstance(exc, TelemetryError) else "io_failure" if isinstance(exc, OSError) else "observer_failure"
    try:
        log(f"agent telemetry: {code}")
    except Exception:
        logging.getLogger(__name__).warning("agent telemetry: logging_failure")


class SnapshotWriter:
    """One process/source. Explicit instances allow isolated tests without env opt-in."""

    def __init__(self, path: Path, source: str, registry: list[dict], agents: dict[str, bool],
                 *, clock: Callable[[], datetime] = utc_now,
                 log: Callable[[str], None] = logging.getLogger(__name__).warning):
        _require(source in SOURCES, "invalid_source")
        self.clock, self.log = clock, log
        self.registry = copy.deepcopy(registry)
        self.ids = {row["id"] for row in registry}
        _require(set(agents) <= self.ids, "unknown_agent")
        _require(all(type(enabled) is bool for enabled in agents.values()), "invalid_enablement")
        self.source = source
        self.started_at = timestamp(clock())
        self.instance_id = str(uuid.uuid4())
        self.agents = {key: _observation(key, enabled) for key, enabled in agents.items()}
        self.events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self.sequence = 0
        self.jobs: dict[str, str] = {}
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._initialized = False
        self._published = False
        self._predecessor_id: str | None = None
        self._closed = False
        self._broken = False
        self.status = "running"
        validate_snapshot(_envelope(registry, clock()), clock())
        self.storage = SnapshotFile(path)
        try:
            self.lease_fd = self.storage.acquire_source(source)
        except BaseException:
            self.storage.close()
            raise
        self._event("writer_started", None, at=self.started_at)
        for agent_id, enabled in agents.items():
            self._event("agent_enabled" if enabled else "agent_disabled", agent_id, at=self.started_at)
        self.publish()

    def _event(self, kind: str, agent_id: str | None, *, at: str | None = None) -> None:
        self.sequence += 1
        _integer(self.sequence)
        active = self.agents[agent_id]["active_jobs"] if agent_id else sum(
            row["active_jobs"] for row in self.agents.values())
        self.events.append({"sequence": self.sequence, "at": at or timestamp(self.clock()),
                            "agent_id": agent_id, "type": kind, "active_jobs": active})

    def _recover(self, old: dict | None) -> None:
        if old:
            pending = list(self.events)
            self.events = deque(old["events"], maxlen=MAX_EVENTS)
            self.sequence = self.events[-1]["sequence"] if self.events else 0
            for prior in old["agents"]:
                row = self.agents.setdefault(prior["id"], _observation(prior["id"], False))
                for key in row["counters"]:
                    row["counters"][key] += prior["counters"][key]
                if prior["active_jobs"]:
                    row["counters"]["interrupted"] += prior["active_jobs"]
                    prior = {**prior, "last_activity_at": self.started_at,
                             "last_outcome": "interrupted", "last_outcome_at": self.started_at}
                    self._event("job_interrupted", row["id"], at=self.started_at)
                    self.events[-1]["active_jobs"] = 0
                for time_key in ("last_activity_at", "last_outcome_at"):
                    if prior[time_key] and (
                        not row[time_key] or datetime.fromisoformat(prior[time_key]) >
                        datetime.fromisoformat(row[time_key])
                    ):
                        row[time_key] = prior[time_key]
                        if time_key == "last_outcome_at":
                            row["last_outcome"] = prior["last_outcome"]
            for event in pending:
                self.sequence += 1
                event["sequence"] = self.sequence
                self.events.append(event)
        self._initialized = True

    def _status(self, row: dict) -> str:
        if self.status == "stopped":
            return "stopped"
        if row["active_jobs"]:
            return "working"
        if not row["enabled"]:
            return "disabled"
        if row["connected"] is False:
            return "disconnected"
        if row["last_outcome"] in ("failed", "interrupted"):
            return "error"
        return "starting" if row["status"] == "starting" else "idle"

    def publish(self) -> bool:
        with self._mutex:
            if self._closed or self._broken:
                return False
            try:
                self.storage.check_lock(self.lease_fd, self.storage.path.name + f".{self.source}.lock")
                with self.storage.locked():
                    now = self.clock()
                    data = self.storage.read(now) or _envelope(self.registry, now)
                    old = next((w for w in data["writers"] if w["source"] == self.source), None)
                    if not self._initialized:
                        self._predecessor_id = old["instance_id"] if old else None
                        self._recover(old)
                        if datetime.fromisoformat(data["registry_generated_at"]) <= datetime.fromisoformat(self.started_at):
                            data.update(agents=copy.deepcopy(self.registry), registry_generated_at=self.started_at)
                    elif old and old["instance_id"] != self.instance_id and (
                        self._published or old["instance_id"] != self._predecessor_id
                    ):
                        raise TelemetryError("source_replaced")
                    for row in self.agents.values():
                        row["status"] = self._status(row)
                    writer = {
                        "source": self.source, "instance_id": self.instance_id, "status": self.status,
                        "started_at": self.started_at, "heartbeat_at": timestamp(now),
                        "heartbeat_interval_seconds": HEARTBEAT_SECONDS, "stale_after_seconds": STALE_SECONDS,
                        "agents": sorted(self.agents.values(), key=lambda a: a["id"]), "events": list(self.events),
                    }
                    data["writers"] = sorted(
                        [w for w in data["writers"] if w["source"] != self.source] + [writer],
                        key=lambda w: w["source"],
                    )
                    data["generated_at"] = timestamp(now)
                    self.storage.write(data, now)
                    self._published = True
                return True
            except Exception as exc:
                _report(self.log, exc)
                return False

    def _change(self, action: Callable[[], object]):
        with self._mutex:
            if self._closed or self._broken:
                return None
            try:
                result = action()
            except Exception as exc:
                self._broken = True
                _report(self.log, exc)
                return None
            self.publish()
            return result

    def ready(self, agent_id: str) -> None:
        def change():
            row = self.agents[agent_id]
            row["connected"] = True if self.source == "discord" else None
            row["status"] = "idle"
            self._event("agent_ready", agent_id)
        self._change(change)

    def disconnected(self, agent_id: str) -> None:
        def change():
            self.agents[agent_id]["connected"] = False
            self._event("agent_disconnected", agent_id)
        self._change(change)

    def begin(self, agent_id: str) -> str | None:
        def change():
            _require(agent_id in self.ids, "unknown_agent")
            row = self.agents.setdefault(agent_id, _observation(agent_id, True))
            if not row["enabled"]:
                row["enabled"] = True
                self._event("agent_enabled", agent_id)
            _require(row["active_jobs"] < MAX_ACTIVE, "active_limit")
            token = uuid.uuid4().hex
            self.jobs[token] = agent_id
            row["active_jobs"] += 1
            row["counters"]["started"] += 1
            row["last_activity_at"] = timestamp(self.clock())
            row["status"] = "working"
            self._event("job_started", agent_id)
            return token
        return self._change(change)

    def finish(self, token: str, outcome: str) -> None:
        def change():
            if token not in self.jobs:
                return
            _require(outcome in OUTCOMES, "invalid_outcome")
            row = self.agents[self.jobs.pop(token)]
            row["active_jobs"] -= 1
            row["counters"][outcome] += 1
            row["last_outcome"] = outcome
            row["last_outcome_at"] = row["last_activity_at"] = timestamp(self.clock())
            self._event("job_" + outcome, row["id"])
        self._change(change)

    def wait_cancelled(self, token: str) -> None:
        def change():
            if token in self.jobs:
                self._event("wait_cancelled", self.jobs[token])
        self._change(change)

    def start_heartbeat(self) -> None:
        def tick():
            while not self._stop.wait(HEARTBEAT_SECONDS):
                self.publish()
        self._thread = threading.Thread(target=tick, name="nb-agent-telemetry", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._mutex:
            if self._closed:
                return
            try:
                for token in list(self.jobs):
                    self.finish(token, "interrupted")
                self.status = "stopped"
                self._event("writer_stopped", None)
                self.publish()
            except Exception as exc:
                _report(self.log, exc)
            finally:
                self._closed = True
                os.close(self.lease_fd)
                self.storage.close()
        if self._thread:
            self._thread.join(timeout=1)


_writer: SnapshotWriter | None = None


@contextmanager
def runtime(source: str, agents: dict[str, bool], *, log: Callable[[str], None]) -> Iterator[None]:
    global _writer
    path = os.environ.get(ENV_PATH)
    if not path or "unittest" in sys.modules or "pytest" in sys.modules:
        yield
        return
    writer = None
    try:
        _require(_writer is None, "observer_already_started")
        writer = SnapshotWriter(Path(path), source, discover_registry(), agents, log=log)
        _writer = writer
        writer.start_heartbeat()
    except Exception as exc:
        _report(log, exc)
    try:
        yield
    finally:
        if writer:
            _writer = None
            try:
                writer.close()
            except Exception as exc:
                _report(log, exc)


def ready(agent_id: str) -> None:
    if _writer:
        _writer.ready(agent_id)


def disconnected(agent_id: str) -> None:
    if _writer:
        _writer.disconnected(agent_id)


class Invocation:
    """Shared with the async waiter; only the worker owns start/finish."""

    def __init__(self, agent_id: str | None):
        self.writer = _writer if agent_id else None
        self.agent_id = agent_id
        self.token: str | None = None
        self.outcome = "failed"
        self._cancelled_wait = False
        self._done = False
        self._mutex = threading.Lock()

    def __enter__(self) -> Invocation:
        with self._mutex:
            if self.writer and self.agent_id:
                self.token = self.writer.begin(self.agent_id)
                if self.token and self._cancelled_wait:
                    self.writer.wait_cancelled(self.token)
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        with self._mutex:
            if exc_type and issubclass(exc_type, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                self.outcome = "cancelled"
            if self.writer and self.token:
                self.writer.finish(self.token, self.outcome)
            self._done = True

    def cancel_wait(self) -> None:
        with self._mutex:
            self._cancelled_wait = True
            if self.writer and self.token and not self._done:
                self.writer.wait_cancelled(self.token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-registry", help="Export actual identities without starting a bot")
    export.add_argument("--path", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        export_registry(args.path)
    except Exception as exc:
        _report(lambda message: print(message, file=sys.stderr), exc)
        return 1
    print("Registry exported; existing writer observations preserved. No runtime was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
