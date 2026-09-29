"""Synthetic lifecycle tests. No real models, credentials, bots, or runtime files."""

from __future__ import annotations

import asyncio
import copy
import json
import multiprocessing
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import env_file
from scripts.discord_bot import agent_telemetry as t, claude_invoke

REGISTRY = [
    {"id": name, "display_name": name.title(), "plugin_defined": True,
     "discord_registered": name != "loid"}
    for name in ("loid", "luna", "research")
]
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _child_writer(path, source, agent, ready, finish, results):
    writer = None
    try:
        writer = t.SnapshotWriter(Path(path), source, REGISTRY, {agent: True}, log=lambda _: None)
        writer.ready(agent)
        token = writer.begin(agent)
        ready.set()
        if not finish.wait(15):
            raise AssertionError("test synchronization timed out")
        writer.finish(token, "succeeded")
        writer.close()
        results.put("ok")
    except Exception as exc:
        results.put(type(exc).__name__)
        ready.set()
    finally:
        if writer:
            writer.close()


@unittest.skipIf(t.fcntl is None, "private writer requires POSIX file locks")
class TelemetryCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.path = self.root / "private" / "agent-telemetry.json"
        self.now = BASE
        self.logs = []

    def writer(self, source="discord", agents=None, registry=None):
        writer = t.SnapshotWriter(
            self.path, source, registry or REGISTRY,
            agents if agents is not None else {"research": True},
            clock=lambda: self.now, log=self.logs.append,
        )
        self.addCleanup(writer.close)
        return writer

    def read(self):
        data = json.loads(self.path.read_bytes())
        t.validate_snapshot(data, self.now)
        return data

    def source(self, source="discord"):
        return next(w for w in self.read()["writers"] if w["source"] == source)

    def row(self, agent="research", source="discord"):
        return next(a for a in self.source(source)["agents"] if a["id"] == agent)

    def fake_repo(self):
        repo = self.root / "repo"
        plugins = repo / "plugins/neural-bridge-core/agents"
        plugins.mkdir(parents=True)
        for name in ("research", "luna", "loid", "new-specialist"):
            (plugins / f"{name}.md").write_text("description: /home/private/person\nPRIVATE PROMPT")
        config = repo / "scripts/discord_bot"
        config.mkdir(parents=True)
        (config / "agents.json").write_text(json.dumps({
            "authorized_user_ids": ["private-user"],
            "agents": [
                {"id": "research", "display_name": "Research", "token_keychain_service": "private-secret"},
                {"id": "config-only", "display_name": "Config Only"},
            ],
        }))
        return repo


class TestRegistry(TelemetryCase):
    def test_discovery_includes_unregistered_and_config_only_without_prose(self):
        rows = t.discover_registry(self.fake_repo())
        self.assertEqual([row["id"] for row in rows],
                         ["config-only", "loid", "luna", "new-specialist", "research"])
        self.assertFalse(rows[0]["plugin_defined"])
        self.assertFalse(rows[1]["discord_registered"])
        text = json.dumps(rows)
        for excluded in ("private", "PROMPT", "token_keychain_service", "description"):
            self.assertNotIn(excluded, text)

    def test_actual_registry_matches_definition_and_config_sets(self):
        rows = t.discover_registry()
        definitions = {p.stem for p in (t.REPO_ROOT / "plugins/neural-bridge-core/agents").glob("*.md")}
        config = json.loads((t.REPO_ROOT / "scripts/discord_bot/agents.json").read_text())
        registered = {row["id"] for row in config["agents"]}
        self.assertEqual({row["id"] for row in rows}, definitions | registered)
        self.assertEqual({row["id"] for row in rows if row["plugin_defined"]}, definitions)
        self.assertEqual({row["id"] for row in rows if row["discord_registered"]}, registered)
        self.assertIn("loid", definitions - registered)

    def test_registry_export_is_unobserved_and_preserves_every_writer_value(self):
        repo = self.fake_repo()
        t.export_registry(self.path, root=repo, now=self.now)
        self.assertEqual(self.read()["writers"], [])
        writer = self.writer()
        writer.begin("research")
        before = self.read()["writers"]
        self.now += timedelta(seconds=60)
        t.export_registry(self.path, root=repo, now=self.now)
        data = self.read()
        self.assertEqual(data["writers"], before)
        self.assertEqual(data["generated_at"], t.timestamp(self.now))
        self.assertNotEqual(data["generated_at"], before[0]["heartbeat_at"])
        writer.publish()
        self.assertIn("new-specialist", {row["id"] for row in self.read()["agents"]})

    def test_invalid_registry_label_or_duplicate_refuses_export(self):
        repo = self.fake_repo()
        config = repo / "scripts/discord_bot/agents.json"
        for agents in (
            [{"id": "research", "display_name": "/home/private/path"}],
            [{"id": "research"}, {"id": "research"}],
            [{"id": "../escape"}],
        ):
            config.write_text(json.dumps({"agents": agents}))
            with self.assertRaises(t.TelemetryError):
                t.export_registry(self.path, root=repo, now=self.now)
        self.assertFalse(self.path.exists())


class TestLifecycle(TelemetryCase):
    def test_heartbeat_does_not_invent_agent_activity(self):
        writer = self.writer(agents={"research": True, "luna": False})
        self.assertEqual(self.row()["status"], "starting")
        self.assertFalse(self.row("luna")["enabled"])
        self.assertEqual(self.row("luna")["status"], "disabled")
        writer.ready("research")
        self.assertEqual(self.row()["status"], "idle")
        self.assertTrue(self.row()["connected"])
        self.now += timedelta(seconds=300)
        writer.publish()
        self.assertIsNone(self.row()["last_activity_at"])
        self.assertEqual(self.row()["counters"]["started"], 0)
        self.assertEqual(self.source()["heartbeat_at"], t.timestamp(self.now))
        self.assertEqual(self.source()["heartbeat_interval_seconds"], 10)
        self.assertEqual(self.source()["stale_after_seconds"], 45)

    def test_overlap_disconnect_error_reconnect_and_recovery(self):
        writer = self.writer()
        writer.ready("research")
        first, second = writer.begin("research"), writer.begin("research")
        self.now += timedelta(seconds=1)
        writer.finish(first, "succeeded")
        self.assertEqual(self.row()["status"], "working")
        self.assertEqual(self.row()["active_jobs"], 1)
        writer.disconnected("research")
        self.assertEqual(self.row()["status"], "working")
        self.assertFalse(self.row()["connected"])
        writer.finish(second, "failed")
        self.assertEqual(self.row()["status"], "disconnected")
        writer.ready("research")
        self.assertEqual(self.row()["status"], "error")
        self.now += timedelta(seconds=1)
        writer.finish(writer.begin("research"), "succeeded")
        self.assertEqual(self.row()["status"], "idle")
        self.assertEqual(self.row()["counters"],
                         dict(started=3, succeeded=2, failed=1, cancelled=0, interrupted=0))

    def test_telegram_readiness_is_not_measured_connectivity(self):
        writer = self.writer("telegram-loid", {"loid": True})
        writer.ready("loid")
        row = self.row("loid", "telegram-loid")
        self.assertIsNone(row["connected"])
        self.assertEqual(row["status"], "idle")

    def test_duplicate_terminal_event_does_not_decrement_another_job(self):
        writer = self.writer()
        first = writer.begin("research")
        writer.begin("research")
        writer.finish(first, "succeeded")
        writer.finish(first, "failed")
        self.assertEqual(self.row()["active_jobs"], 1)
        self.assertEqual(self.row()["counters"]["failed"], 0)

    def test_stale_source_is_not_refreshed_by_other_writer_or_registry(self):
        self.writer().begin("research")
        before = self.source()
        self.now += timedelta(seconds=46)
        other = self.writer("telegram-luna", {"luna": True})
        other.ready("luna")
        self.assertEqual(self.source(), before)
        self.assertGreater((self.now - datetime.fromisoformat(before["heartbeat_at"])).total_seconds(), 45)
        self.assertEqual(self.read()["generated_at"], self.source("telegram-luna")["heartbeat_at"])

    def test_stop_records_lost_observation_not_successful_task(self):
        writer = self.writer()
        token = writer.begin("research")
        writer.close()
        self.assertEqual(self.source()["status"], "stopped")
        row = self.row()
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["last_outcome"], "interrupted")
        self.assertEqual(row["active_jobs"], 0)
        self.assertEqual(row["counters"]["succeeded"], 0)
        self.assertEqual(row["counters"]["interrupted"], 1)
        before = self.path.read_bytes()
        writer.finish(token, "succeeded")
        self.assertEqual(self.path.read_bytes(), before)

    def test_restart_loses_old_spans_but_preserves_counters_and_sequence(self):
        writer = self.writer()
        writer.finish(writer.begin("research"), "succeeded")
        writer.begin("research")
        writer.begin("research")
        old = self.source()
        # Simulate abrupt observer death, not a graceful stopped write.
        os.close(writer.lease_fd)
        writer.storage.close()
        writer._closed = True
        self.now += timedelta(seconds=1)
        replacement = self.writer()
        replacement.ready("research")
        new = self.source()
        self.assertNotEqual(new["instance_id"], old["instance_id"])
        self.assertEqual(self.row()["active_jobs"], 0)
        self.assertEqual(self.row()["status"], "error")
        self.assertEqual(self.row()["counters"],
                         dict(started=3, succeeded=1, failed=0, cancelled=0, interrupted=2))
        self.assertGreater(new["events"][-1]["sequence"], old["events"][-1]["sequence"])

    def test_events_bounded_and_strictly_ordered(self):
        writer = self.writer()
        for _ in range(40):
            writer.finish(writer.begin("research"), "succeeded")
        events = self.source()["events"]
        self.assertEqual(len(events), 64)
        sequence = [event["sequence"] for event in events]
        self.assertEqual(sequence, sorted(set(sequence)))
        self.assertGreater(sequence[0], 1)


class TestPrivateStorage(TelemetryCase):
    def test_new_private_modes_do_not_change_existing_ancestors(self):
        ancestor_mode = stat.S_IMODE(self.root.stat().st_mode)
        self.writer()
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        for path in self.path.parent.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), ancestor_mode)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_unsafe_existing_directory_is_rejected_not_chmodded(self):
        self.path.parent.mkdir(mode=0o755)
        self.path.parent.chmod(0o755)
        with self.assertRaisesRegex(t.TelemetryError, "unsafe_destination"):
            self.writer()
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o755)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_unsafe_existing_snapshot_is_not_read_or_overwritten(self):
        self.path.parent.mkdir(mode=0o700)
        self.path.write_text("PRIVATE CONTENT")
        self.path.chmod(0o644)
        writer = self.writer()
        self.assertFalse(writer.publish())
        self.assertIn("unsafe_destination", self.logs[0])
        self.assertNotIn("PRIVATE CONTENT", " ".join(self.logs))
        self.assertEqual(self.path.read_text(), "PRIVATE CONTENT")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_checkout_relative_and_symlink_destinations_are_rejected(self):
        with self.assertRaises(t.TelemetryError):
            t.SnapshotFile(Path("relative.json"))
        repo = self.fake_repo()
        (repo / ".git").mkdir()
        with self.assertRaisesRegex(t.TelemetryError, "destination_inside_checkout"):
            t.SnapshotFile(repo / "new-dir" / "snapshot.json")
        self.assertFalse((repo / "new-dir").exists())
        self.path.parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(t.TelemetryError):
            self.writer()

    def test_snapshot_symlink_fifo_and_hardlink_are_refused(self):
        self.path.parent.mkdir(mode=0o700)
        outside = self.root / "outside"
        outside.write_text("PRIVATE OUTSIDE")
        outside.chmod(0o600)
        storage = t.SnapshotFile(self.path)
        self.addCleanup(storage.close)
        for make in (
            lambda: self.path.symlink_to(outside),
            lambda: os.mkfifo(self.path, 0o600),
            lambda: os.link(outside, self.path),
        ):
            make()
            with self.assertRaises((OSError, t.TelemetryError)):
                storage.read(self.now)
            self.path.unlink()
        self.assertEqual(outside.read_text(), "PRIVATE OUTSIDE")

    def test_foreign_owner_rejected(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid() + 1, st_nlink=1)
        with self.assertRaises(t.TelemetryError):
            t._private(info)

    def test_bad_or_oversized_document_is_not_reset(self):
        writer = self.writer()
        for raw in ("{broken", "x" * (t.MAX_BYTES + 1), '{"schema_version":1,"schema_version":1}'):
            self.path.write_text(raw)
            self.assertFalse(writer.publish())
            self.assertEqual(self.path.read_text(), raw)
        self.assertTrue(self.logs)

    def test_atomic_failure_preserves_file_and_retry_recovers(self):
        writer = self.writer()
        before = self.path.read_bytes()
        with patch.object(t.os, "replace", side_effect=OSError("private location")):
            token = writer.begin("research")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.assertNotIn("private location", " ".join(self.logs))
        self.assertTrue(writer.publish())
        self.assertEqual(self.row()["active_jobs"], 1)
        writer.finish(token, "succeeded")

    def test_replaced_lock_does_not_allow_a_second_write(self):
        writer = self.writer()
        before = self.path.read_bytes()
        lease = self.path.with_name(self.path.name + ".discord.lock")
        lease.unlink()
        lease.touch(mode=0o600)
        self.assertFalse(writer.publish())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(any("lock_replaced" in message or "unsafe_destination" in message for message in self.logs))

    def test_failed_first_restart_write_does_not_repeat_recovery(self):
        writer = self.writer()
        writer.begin("research")
        writer.close()
        self.now += timedelta(seconds=1)
        with patch.object(t.os, "replace", side_effect=OSError("disk full")):
            replacement = self.writer()
        self.assertTrue(replacement.publish())
        self.assertEqual(self.row()["counters"]["interrupted"], 1)

    def test_contended_merge_defers_work_and_export_does_not_clobber(self):
        writer = self.writer()
        other_storage = t.SnapshotFile(self.path)
        self.addCleanup(other_storage.close)
        before = self.path.read_bytes()
        with other_storage.locked():
            token = writer.begin("research")
            self.assertEqual(self.path.read_bytes(), before)
            with self.assertRaisesRegex(t.TelemetryError, "snapshot_busy"):
                t.export_registry(self.path, root=self.fake_repo(), now=self.now)
        self.assertIn("snapshot_busy", " ".join(self.logs))
        writer.publish()
        self.assertEqual(self.row()["active_jobs"], 1)
        writer.finish(token, "succeeded")

    def test_contention_at_start_preserves_pending_activity(self):
        old = self.writer()
        old.begin("research")
        os.close(old.lease_fd)
        old.storage.close()
        old._closed = True
        storage = t.SnapshotFile(self.path)
        self.addCleanup(storage.close)
        self.now += timedelta(seconds=1)
        with storage.locked():
            replacement = self.writer()
            token = replacement.begin("research")
        replacement.publish()
        self.assertEqual(self.row()["counters"]["started"], 2)
        self.assertEqual(self.row()["counters"]["interrupted"], 1)
        self.assertEqual(self.row()["active_jobs"], 1)
        replacement.finish(token, "succeeded")

    def test_recovery_compares_timestamp_values_not_fractional_string_order(self):
        old = self.writer()
        old.finish(old.begin("research"), "failed")
        os.close(old.lease_fd)
        old.storage.close()
        old._closed = True
        data = self.read()
        row = data["writers"][0]["agents"][0]
        row["last_activity_at"] = row["last_outcome_at"] = "2026-01-01T00:00:00Z"
        self.path.write_text(json.dumps(data))
        storage = t.SnapshotFile(self.path)
        self.addCleanup(storage.close)
        self.now += timedelta(milliseconds=1)
        with storage.locked():
            replacement = self.writer()
            replacement.finish(replacement.begin("research"), "succeeded")
        replacement.publish()
        self.assertEqual(self.row()["last_outcome"], "succeeded")
        self.assertEqual(self.row()["last_outcome_at"], t.timestamp(self.now))

    def test_duplicate_source_cannot_steal_even_stale_lease(self):
        self.writer()
        before = self.path.read_bytes()
        self.now += timedelta(seconds=500)
        with self.assertRaisesRegex(t.TelemetryError, "source_busy"):
            self.writer()
        self.assertEqual(self.path.read_bytes(), before)

    def test_actual_processes_preserve_each_others_active_and_stopped_slots(self):
        self.now = t.utc_now()
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        processes, releases = [], []
        try:
            for source, agent in (("discord", "research"), ("telegram-loid", "loid")):
                ready, release = context.Event(), context.Event()
                process = context.Process(target=_child_writer, args=(
                    str(self.path), source, agent, ready, release, results))
                process.start()
                processes.append(process)
                releases.append(release)
                self.assertTrue(ready.wait(10))
                self.assertTrue(process.is_alive())
            self.now = t.utc_now()
            data = self.read()
            self.assertEqual(len(data["writers"]), 2)
            self.assertTrue(all(w["agents"][0]["active_jobs"] == 1 for w in data["writers"]))
            with self.assertRaisesRegex(t.TelemetryError, "source_busy"):
                self.writer()
            releases[0].set()
            processes[0].join(10)
            self.assertFalse(processes[0].is_alive())
            self.now = t.utc_now()
            self.assertEqual(self.source()["status"], "stopped")
            self.assertEqual(self.row("loid", "telegram-loid")["active_jobs"], 1)
            releases[1].set()
            processes[1].join(10)
            self.assertFalse(processes[1].is_alive())
            self.assertEqual([results.get(timeout=5), results.get(timeout=5)], ["ok", "ok"])
        finally:
            for release in releases:
                release.set()
            for process in processes:
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()

    def test_concurrent_reader_only_sees_whole_documents(self):
        writer = self.writer()
        stop = threading.Event()
        errors = []

        def read():
            while not stop.is_set():
                try:
                    t.validate_snapshot(json.loads(self.path.read_bytes()), self.now)
                except Exception as exc:
                    errors.append(type(exc).__name__)

        reader = threading.Thread(target=read)
        reader.start()
        try:
            for _ in range(20):
                writer.finish(writer.begin("research"), "succeeded")
        finally:
            stop.set()
            reader.join(5)
        self.assertEqual(errors, [])


class TestValidation(TelemetryCase):
    def test_documented_synthetic_example_is_valid(self):
        path = t.REPO_ROOT / "docs/examples/moonbase-telemetry-v1.json"
        t.validate_snapshot(json.loads(path.read_text()), BASE + timedelta(seconds=10))

    def test_exact_active_limit_and_registry_limit(self):
        writer = self.writer()
        with patch.object(writer, "publish", return_value=True):
            for _ in range(t.MAX_ACTIVE):
                self.assertIsNotNone(writer.begin("research"))
        self.assertTrue(writer.publish())
        self.assertEqual(self.row()["active_jobs"], 256)
        before = self.path.read_bytes()
        self.assertIsNone(writer.begin("research"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("active_limit", " ".join(self.logs))

        rows = [{"id": f"agent-{i}", "display_name": f"Agent {i}",
                 "plugin_defined": True, "discord_registered": False} for i in range(128)]
        t.validate_snapshot(t._envelope(rows, self.now), self.now)
        rows.append({"id": "too-many", "display_name": "Too Many",
                     "plugin_defined": True, "discord_registered": False})
        with self.assertRaises(t.TelemetryError):
            t.validate_snapshot(t._envelope(rows, self.now), self.now)

    def test_every_timestamp_enforces_fixed_future_allowance(self):
        writer = self.writer()
        writer.finish(writer.begin("research"), "succeeded")
        original = self.read()
        for target, key in (
            ((), "generated_at"), ((), "registry_generated_at"),
            (("writers", 0), "heartbeat_at"), (("writers", 0), "started_at"),
            (("writers", 0, "agents", 0), "last_activity_at"),
            (("writers", 0, "agents", 0), "last_outcome_at"),
            (("writers", 0, "events", 0), "at"),
        ):
            data = copy.deepcopy(original)
            row = data
            for part in target:
                row = row[part]
            row[key] = t.timestamp(self.now + timedelta(seconds=6))
            with self.subTest(target=target, key=key), self.assertRaises(t.TelemetryError):
                t.validate_snapshot(data, self.now)
        t.validate_snapshot(original, self.now - timedelta(seconds=5))
        with self.assertRaises(t.TelemetryError):
            t.validate_snapshot(original, self.now - timedelta(seconds=6))

    def test_unknown_fields_bad_types_and_impossible_counts_are_refused(self):
        self.writer().begin("research")
        original = self.read()
        mutations = (
            lambda d: d.update(schema_version=True),
            lambda d: d.update(prompt="private"),
            lambda d: d["agents"][0].update(display_name="/home/private"),
            lambda d: d["agents"].append(d["agents"][0]),
            lambda d: d["writers"][0].update(instance_id="session-id"),
            lambda d: d["writers"][0].update(source="unknown"),
            lambda d: d["writers"][0].update(stale_after_seconds=999999),
            lambda d: d["writers"][0]["agents"][0].update(active_jobs=True),
            lambda d: d["writers"][0]["agents"][0].update(active_jobs=257),
            lambda d: d["writers"][0]["agents"][0].update(status="idle"),
            lambda d: d["writers"][0]["agents"][0]["counters"].update(started=3),
            lambda d: d["writers"][0]["events"][0].update(sequence=t.MAX_INTEGER + 1),
            lambda d: d["writers"][0]["events"].reverse(),
            lambda d: d["writers"][0]["events"].extend([d["writers"][0]["events"][0]] * 65),
        )
        for mutate in mutations:
            data = copy.deepcopy(original)
            mutate(data)
            with self.subTest(mutate=mutate), self.assertRaises(t.TelemetryError):
                t.validate_snapshot(data, self.now)

    def test_invalid_input_preserved_and_error_does_not_leak_it(self):
        writer = self.writer()
        data = self.read()
        data["private_prompt"] = "FAKE SENSITIVE PROMPT"
        self.path.write_text(json.dumps(data))
        before = self.path.read_bytes()
        self.assertFalse(writer.publish())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("FAKE", " ".join(self.logs))


class TestInvocation(TelemetryCase):
    def test_results_exceptions_and_telemetry_only_identity(self):
        writer = self.writer()
        for result, exception, outcome in (
            (SimpleNamespace(returncode=0, stdout="private response", stderr=""), None, "succeeded"),
            (SimpleNamespace(returncode=1, stdout="", stderr="private stderr"), None, "failed"),
            (SimpleNamespace(returncode=-15, stdout="", stderr=""), None, "cancelled"),
            (None, subprocess.TimeoutExpired("private prompt", 1), "failed"),
            (None, FileNotFoundError("private path"), "failed"),
            (None, RuntimeError("unexpected execution error"), "failed"),
        ):
            with patch.object(t, "_writer", writer), patch.object(
                claude_invoke.subprocess, "run", return_value=result, side_effect=exception,
            ) as run, patch.dict(os.environ, {}, clear=True):
                if isinstance(exception, RuntimeError):
                    with self.assertRaises(RuntimeError):
                        claude_invoke.call_claude_sync("private prompt", telemetry_agent_id="research")
                else:
                    claude_invoke.call_claude_sync("private prompt", telemetry_agent_id="research")
                self.assertNotIn("NB_AGENT_ID", run.call_args.kwargs["env"])
            self.assertEqual(self.row()["last_outcome"], outcome)
            self.assertEqual(self.row()["active_jobs"], 0)
        raw = self.path.read_text()
        for excluded in ("private prompt", "private response", "private stderr", "private path", "unexpected execution"):
            self.assertNotIn(excluded, raw)

    def test_cancelled_async_waiter_leaves_worker_active(self):
        writer = self.writer()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        finish = writer.finish

        def fake_run(*_args, **_kw):
            entered.set()
            if not release.wait(5):
                raise AssertionError("worker not released")
            return SimpleNamespace(returncode=0, stdout="answer", stderr="")

        def finish_then_signal(*args):
            finish(*args)
            finished.set()

        async def scenario():
            with patch.object(t, "_writer", writer), patch.object(writer, "finish", side_effect=finish_then_signal), \
                 patch.object(claude_invoke.subprocess, "run", side_effect=fake_run):
                task = asyncio.create_task(claude_invoke.call_claude("fake", agent_id="research"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    row = self.row()
                    self.assertEqual(row["active_jobs"], 1)
                    self.assertEqual(row["status"], "working")
                    self.assertEqual(row["counters"]["cancelled"], 0)
                    self.assertIsNone(row["last_outcome"])
                    self.assertEqual(self.source()["events"][-1]["type"], "wait_cancelled")
                finally:
                    release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 5))
                self.assertEqual(self.row()["last_outcome"], "succeeded")
                self.assertEqual(self.row()["active_jobs"], 0)
        asyncio.run(scenario())

    def test_cancellation_while_queued_does_not_invent_execution(self):
        writer = self.writer()
        release = threading.Event()

        async def scenario():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
            blocker = loop.run_in_executor(None, release.wait, 5)
            with patch.object(t, "_writer", writer), patch.object(claude_invoke.subprocess, "run") as run:
                task = asyncio.create_task(claude_invoke.call_claude("fake", agent_id="research"))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
                await blocker
                await loop.shutdown_default_executor()
                run.assert_not_called()
        asyncio.run(scenario())
        self.assertEqual(self.row()["counters"]["started"], 0)

    def test_cancel_start_race_and_confirmed_boundary_cancellation(self):
        writer = self.writer()
        with patch.object(t, "_writer", writer):
            span = t.Invocation("research")
            span.cancel_wait()
            with self.assertRaises(asyncio.CancelledError):
                with span:
                    self.assertEqual(self.row()["active_jobs"], 1)
                    self.assertEqual(self.source()["events"][-1]["type"], "wait_cancelled")
                    raise asyncio.CancelledError()
        self.assertEqual(self.row()["last_outcome"], "cancelled")

    def test_disabled_and_unattributed_calls_do_not_export(self):
        with patch.object(t, "_writer", None), patch.object(
            claude_invoke.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok", stderr=""),
        ):
            claude_invoke.call_claude_sync("fake", agent_id="research")
        self.assertFalse(self.path.exists())
        writer = self.writer()
        before = self.path.read_bytes()
        with patch.object(t, "_writer", writer), patch.object(
            claude_invoke.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok", stderr=""),
        ):
            claude_invoke.call_claude_sync("router classification")
        self.assertEqual(self.path.read_bytes(), before)

    def test_optional_write_failure_does_not_change_model_result(self):
        writer = self.writer()
        with patch.object(t, "_writer", writer), patch.object(t.os, "replace", side_effect=OSError("disk")), \
             patch.object(claude_invoke.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0, stdout="answer", stderr="")):
            result = claude_invoke.call_claude_sync("fake", agent_id="research")
        self.assertEqual(result, (True, "answer", ""))
        self.assertTrue(self.logs)

    def test_test_runner_suppression_and_absent_config_do_not_create_writer(self):
        for path in ("", str(self.path)):
            with patch.dict(os.environ, {t.ENV_PATH: path}), patch.object(t, "SnapshotWriter") as constructor:
                with t.runtime("discord", {"research": True}, log=self.logs.append):
                    constructor.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_runtime_initialization_failure_logs_without_blocking_body(self):
        with patch.object(t, "sys", SimpleNamespace(modules={})), \
             patch.dict(os.environ, {t.ENV_PATH: "relative.json"}):
            with t.runtime("discord", {"research": True}, log=self.logs.append):
                self.assertIsNone(t._writer)
        self.assertIn("absolute_path_required", " ".join(self.logs))


class TestRuntimeWiring(TelemetryCase):
    def setUp(self):
        super().setUp()
        self.shared_env = self.root / "shared.env"
        self.local_env = self.root / "local.env"
        self.enterContext(patch.object(
            env_file, "DEFAULT_ENV_PATHS", (self.shared_env, self.local_env),
        ))

    def test_discord_main_loads_only_telemetry_before_observer_setup(self):
        from scripts.discord_bot import main

        unrelated = {
            "ANTHROPIC_API_KEY": "fake-secret",
            "ANTHROPIC_BASE_URL": "https://example.invalid",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "LUNA_TELEGRAM_ALLOWED_USERS": "123",
            "NB_AGENT_ID": "loid",
        }
        file_text = "".join(f"{key}={value}\n" for key, value in unrelated.items())
        local = str(self.path)
        shared = str(self.root / "shared.json")
        inherited = str(self.root / "inherited.json")
        for name, shared_value, local_value, inherited_value, expected in (
            ("local", None, local, None, local),
            ("shared", shared, local, None, shared),
            ("inherited", shared, local, inherited, inherited),
            ("inherited-empty", shared, local, "", ""),
            ("shared-empty", "", local, None, ""),
            ("local-empty", None, "", None, ""),
            ("unset", None, None, None, None),
        ):
            for inherit_unrelated in (False, True):
                with self.subTest(case=name, inherit_unrelated=inherit_unrelated):
                    self.logs.clear()
                    for path, value in ((self.shared_env, shared_value), (self.local_env, local_value)):
                        path.write_text(file_text + ("" if value is None else f"{t.ENV_PATH}={value}\n"))
                    environment = ({key: "inherited" for key in unrelated} if inherit_unrelated else {})
                    if inherited_value is not None:
                        environment[t.ENV_PATH] = inherited_value
                    expected_environment = dict(environment)
                    if expected is not None:
                        expected_environment[t.ENV_PATH] = expected

                    async def run():
                        self.assertEqual(dict(os.environ), expected_environment)
                        with t.runtime("discord", {"research": True}, log=self.logs.append):
                            self.assertEqual(t._writer is not None, bool(expected))

                    with patch.dict(os.environ, environment, clear=True), \
                         patch.object(t, "sys", SimpleNamespace(modules={})), \
                         patch.object(t, "_writer", None), \
                         patch.object(t, "discover_registry", return_value=REGISTRY) as registry, \
                         patch.object(t, "SnapshotWriter") as constructor, \
                         patch.object(main, "_configure_logging") as logging_setup, \
                         patch.object(main, "log", self.logs.append), \
                         patch.object(main, "run", side_effect=run) as runner:
                        self.assertEqual(main.main(), 0, self.logs)
                        self.assertEqual(dict(os.environ), expected_environment)
                        logging_setup.assert_called_once_with()
                        runner.assert_awaited_once_with()
                        if expected:
                            registry.assert_called_once_with()
                            constructor.assert_called_once_with(
                                Path(expected), "discord", REGISTRY, {"research": True},
                                log=self.logs.append,
                            )
                            constructor.return_value.start_heartbeat.assert_called_once_with()
                            constructor.return_value.close.assert_called_once_with()
                        else:
                            registry.assert_not_called()
                            constructor.assert_not_called()
                        self.assertIsNone(t._writer)
        self.assertFalse(self.path.exists())

    def test_discord_uses_real_setup_and_connection_events(self):
        from scripts.discord_bot import main
        from scripts.discord_bot.config import AgentConfig, BotConfig

        research = AgentConfig("research", "1", "fake", False, "Research")
        luna = AgentConfig("luna", "2", "fake", False, "Luna")
        config = BotConfig(["3"], "4", "example/repo", [research, luna])
        captured = []
        client = main.AgentClient(research, config)
        self.addCleanup(lambda: asyncio.run(client.close()))

        async def start(_token):
            self.assertTrue(self.row()["enabled"])
            self.assertFalse(self.row("luna")["enabled"])
            await client.on_ready()
            self.assertTrue(self.row()["connected"])
            await client.on_disconnect()
            self.assertFalse(self.row()["connected"])
            await client.on_resumed()
            captured.append(self.row())

        with patch.object(t, "sys", SimpleNamespace(modules={})), \
             patch.dict(os.environ, {t.ENV_PATH: str(self.path)}), \
             patch.object(t, "utc_now", return_value=self.now), \
             patch.object(t, "discover_registry", return_value=REGISTRY), \
             patch.object(main, "load_config", return_value=config), \
             patch.object(main, "_resolve_token", side_effect=["fake", RuntimeError("missing")]), \
             patch.object(main, "AgentClient", return_value=client), \
             patch.object(main, "CLIENT_REGISTRY", MagicMock()), \
             patch.object(main.inflight, "drain", return_value=[]), \
             patch.object(main, "INTERRUPTED", []), \
             patch.object(main, "fleet_set_state"), patch.object(main, "fleet_log_event"), \
             patch.object(main, "outbound_guard_refresh_loop", AsyncMock()), \
             patch.object(main, "log", self.logs.append), \
             patch.object(client, "start", side_effect=start):
            # The constructor's default clock is intentionally injected, not
            # overridden through a production environment escape hatch.
            constructor = t.SnapshotWriter
            with patch.object(t, "SnapshotWriter", side_effect=lambda *a, **kw: constructor(
                *a, **kw, clock=lambda: self.now,
            )):
                asyncio.run(main.run())
        self.assertEqual(captured[0]["status"], "idle")
        self.assertTrue(captured[0]["connected"])
        self.assertIsNone(captured[0]["last_activity_at"])
        self.assertEqual(self.source()["status"], "stopped")

    def test_telegram_startup_is_unmeasured_and_preserves_luna_queue_hooks(self):
        from contextlib import ExitStack
        from scripts.telegram_bot import council_bridge, loid_bridge, luna_bridge

        for module, source, agent in (
            (luna_bridge, "telegram-luna", "luna"),
            (loid_bridge, "telegram-loid", "loid"),
            (council_bridge, "telegram-council", "loid"),
        ):
            with self.subTest(source=source), ExitStack() as stack:
                app = MagicMock()
                builder = MagicMock()
                builder.token.return_value = builder
                builder.post_init.return_value = builder
                builder.post_shutdown.return_value = builder
                builder.build.return_value = app
                observed = []
                queue = MagicMock(start=AsyncMock(), stop=AsyncMock())

                def poll(**kwargs):
                    init = builder.post_init.call_args.args[0]
                    asyncio.run(init(app))
                    observed.append(self.row(agent, source))
                    if module is luna_bridge:
                        self.assertEqual(kwargs["allowed_updates"], ["message", "callback_query"])
                        shutdown = builder.post_shutdown.call_args.args[0]
                        asyncio.run(shutdown(app))

                app.run_polling.side_effect = poll
                stack.enter_context(patch.object(t, "sys", SimpleNamespace(modules={})))
                stack.enter_context(patch.dict(os.environ, {t.ENV_PATH: str(self.path)}))
                stack.enter_context(patch.object(t, "discover_registry", return_value=REGISTRY))
                constructor = t.SnapshotWriter
                stack.enter_context(patch.object(t, "SnapshotWriter", side_effect=lambda *a, **kw: constructor(
                    *a, **kw, clock=lambda: self.now,
                )))
                stack.enter_context(patch.object(module, "ApplicationBuilder", return_value=builder))
                load_env = stack.enter_context(patch.object(module, "load_default_env"))
                stack.enter_context(patch.object(module, "_configure_logging"))
                stack.enter_context(patch.object(module, "_allowed_user_ids", return_value={123}))
                stack.enter_context(patch.object(module, "get_token", return_value="fake"))
                stack.enter_context(patch.object(module, "log", self.logs.append))
                if module is luna_bridge:
                    stack.enter_context(patch.object(module.review_store, "Store"))
                    stack.enter_context(patch.object(module, "QueueSurface", return_value=queue))
                module.main()
                load_env.assert_called_once_with()
                self.assertIsNone(observed[0]["connected"])
                self.assertIsNone(observed[0]["last_activity_at"])
                self.assertEqual(observed[0]["status"], "idle")
                self.assertEqual(self.source(source)["status"], "stopped")
                if module is luna_bridge:
                    queue.start.assert_awaited_once_with(app)
                    queue.stop.assert_awaited_once_with(app)
                    queue.install.assert_called_once_with(app)


if __name__ == "__main__":
    unittest.main()
