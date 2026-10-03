"""Synthetic contract, real bounded script, and mocked research tests. No models."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import moonbase_bridge as bridge
from scripts import moonbase_project_check as check
from scripts.discord_bot import claude_invoke

JOB = "11111111-1111-4111-8111-111111111111"
MODEL_JOB = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def no_live_model_or_telemetry(monkeypatch):
    monkeypatch.setenv("NB_MOONBASE_PUBLIC_RESEARCH", "0")
    monkeypatch.delenv("NB_AGENT_TELEMETRY_PATH", raising=False)
    monkeypatch.delenv("NB_CLAUDE_DIRECT", raising=False)


@pytest.fixture
def project(tmp_path):
    root = tmp_path.resolve() / "project"
    root.mkdir()
    (root / "README.md").write_text("Synthetic project.\n")
    (root / "main.py").write_text("raise RuntimeError('must never execute this file')\n")
    (root / "package.json").write_text(json.dumps({
        "name": "synthetic", "scripts": {"test": "touch MUST_NOT_EXIST"},
        "secret": "synthetic-secret-must-not-leak",
    }))
    return root


def request(root: Path) -> dict:
    return {"protocolVersion": 1, "jobId": JOB, "profileId": "project-check",
            "agentId": "automation-engineer", "projectRoot": str(root)}


def research_request() -> dict:
    return {"protocolVersion": 1, "jobId": MODEL_JOB, "profileId": "public-research",
            "agentId": "research", "brief": "Compare two public standards and cite primary sources."}


def exchange(data: bytes, *, action: str = "", capture: list | None = None):
    input_r, input_w = os.pipe()
    output_r, output_w = os.pipe()
    frames = []
    failures = []
    real_popen = subprocess.Popen

    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        if capture is not None:
            capture.append(process)
        return process

    def client():
        writer_open = True
        try:
            os.write(input_w, data)
            with os.fdopen(output_r, "rb") as stream:
                for line in stream:
                    frame = bridge.decode(line)
                    frames.append(frame)
                    if frame["type"] == "started":
                        if action == "cancel":
                            os.write(input_w, bridge.encode({"protocolVersion": 1, "type": "cancel", "jobId": JOB}))
                        elif action == "disconnect":
                            os.close(input_w)
                            writer_open = False
                        elif action == "secondJob":
                            os.write(input_w, data)
                        elif action == "partialControl":
                            os.write(input_w, b'{"unexpected":')
                        elif action == "signal":
                            os.kill(os.getpid(), signal.SIGTERM)
                    if frame["type"] == "terminal" and writer_open:
                        # Normal clients close stdin AFTER terminal, not before.
                        os.close(input_w)
                        writer_open = False
        except (OSError, ValueError) as exc:
            failures.append(exc)
        finally:
            if writer_open:
                os.close(input_w)

    thread = threading.Thread(target=client)
    thread.start()
    try:
        with patch.object(bridge.subprocess, "Popen", side_effect=spawn):
            code = bridge.run(input_r, output_w)
    finally:
        os.close(input_r)
        os.close(output_w)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures
    return code, frames


def assert_stream(code, frames, outcome):
    assert frames
    assert [f["sequence"] for f in frames] == list(range(1, len(frames) + 1))
    assert all(set(f) == {"protocolVersion", "jobId", "sequence", "type", "payload"} for f in frames)
    assert all(f["protocolVersion"] == 1 for f in frames)
    assert [f["type"] for f in frames].count("terminal") == 1
    terminal = frames[-1]["payload"]
    assert terminal["outcome"] == outcome
    assert terminal["exitCode"] == code == bridge.REASONS[terminal["reasonCode"]]
    assert terminal["cleanupConfirmed"] or outcome == "interrupted"
    assert len(frames) <= bridge.LIMITS["eventCount"]
    assert all(len(bridge.encode(f)) <= bridge.LIMITS["eventBytes"] for f in frames)
    assert sum(len(bridge.encode(f)) for f in frames) <= bridge.LIMITS["totalEventBytes"]
    reports = [f["payload"] for f in frames if f["type"] == "result"]
    assert [r["chunkIndex"] for r in reports] == list(range(len(reports)))
    assert all(len(r["text"].encode("utf-8")) <= bridge.CHUNK_BYTES for r in reports)
    assert sum(len(r["text"].encode("utf-8")) for r in reports) <= bridge.LIMITS["resultBytes"]


def test_real_script_start_progress_result_and_reap(project):
    before = {p.name: p.read_bytes() for p in project.iterdir()}
    children = []
    code, frames = exchange(bridge.encode(request(project)), capture=children)
    assert_stream(code, frames, "succeeded")
    assert frames[0]["type"] == "started"
    assert frames[0]["payload"]["executionKind"] == "script"
    assert [f["payload"]["completed"] for f in frames if f["type"] == "progress"] == [3]
    text = "".join(f["payload"]["text"] for f in frames if f["type"] == "result")
    assert "Visited entries: 3\nSelected files: 3\n" in text
    assert "Validated metadata files: 1" in text
    assert "Findings: none" in text
    assert "No project commands or model were run." in text
    assert str(project) not in text and "synthetic-secret" not in text and "RuntimeError" not in text
    assert before == {p.name: p.read_bytes() for p in project.iterdir()}
    assert len(children) == 1
    assert children[0].returncode == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)
    assert bridge._group_gone(children[0].pid)


def test_actual_module_cli(project):
    process = subprocess.Popen(
        [sys.executable, "-m", "scripts.moonbase_bridge", "run"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=bridge.ROOT,
    )
    try:
        process.stdin.write(bridge.encode(request(project)))
        process.stdin.flush()
        frames = []
        for line in process.stdout:
            frames.append(bridge.decode(line))
            if frames[-1]["type"] == "terminal":
                process.stdin.close()
        assert_stream(process.wait(timeout=5), frames, "succeeded")
        assert process.stderr.read() == b""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


@pytest.mark.parametrize("action,code,outcome,reason", [
    ("cancel", 6, "cancelled", "cancelled"),
    ("disconnect", 7, "interrupted", "inputClosed"),
    ("secondJob", 2, "failed", "invalidControl"),
    ("partialControl", 2, "failed", "invalidControl"),
    ("signal", 6, "cancelled", "cancelled"),
])
def test_real_active_job_control_and_reap(project, action, code, outcome, reason):
    for index in range(1800):
        (project / f"file{index}.py").write_text("pass\n")
    children = []
    got, frames = exchange(bridge.encode(request(project)), action=action, capture=children)
    assert got == code
    assert_stream(got, frames, outcome)
    assert frames[0]["type"] == "started"
    assert frames[-1]["payload"]["reasonCode"] == reason
    assert len(children) == 1 and children[0].returncode is not None
    assert bridge._group_gone(children[0].pid)
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)


def test_real_timeout_reaps_child(project, monkeypatch):
    monkeypatch.setitem(bridge.PROFILES, "project-check", ("automation-engineer", "script", 0.001))
    children = []
    code, frames = exchange(bridge.encode(request(project)), capture=children)
    assert_stream(code, frames, "timed_out")
    assert code == 5 and children[0].returncode is not None
    assert bridge._group_gone(children[0].pid)


def test_cancellation_before_spawn(project):
    data = bridge.encode(request(project)) + bridge.encode({"protocolVersion": 1, "type": "cancel", "jobId": JOB})
    children = []
    code, frames = exchange(data, capture=children)
    assert_stream(code, frames, "cancelled")
    assert children == [] and len(frames) == 1


def test_buffered_partial_control_prevents_success_even_when_fd_not_readable(project):
    code, frames = exchange(bridge.encode(request(project)) + b'{"unexpected":')
    assert_stream(code, frames, "failed")
    assert code == 2 and frames[-1]["payload"]["reasonCode"] == "invalidControl"
    assert not any(row["type"] == "result" for row in frames)


def test_unknown_cleanup_is_never_cancelled(project):
    actual = bridge.stop_owned
    with patch.object(bridge, "stop_owned", side_effect=lambda p, **kw: actual(p, **kw) and False):
        code, frames = exchange(bridge.encode(request(project)))
    assert_stream(code, frames, "interrupted")
    assert code == 7 and frames[-1]["payload"]["reasonCode"] == "cleanupUnknown"
    assert not frames[-1]["payload"]["cleanupConfirmed"]


@pytest.mark.parametrize("change", [
    {"protocolVersion": True}, {"protocolVersion": "1"}, {"protocolVersion": 2},
    {"jobId": "not-a-uuid"}, {"jobId": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"},
    {"agentId": "luna"}, {"profileId": "shell"}, {"projectRoot": []},
    {"brief": "must not go with a root"}, {"model": "arbitrary"}, {"command": "whoami"},
    {"timeoutSeconds": 1}, {"tools": ["Bash"]}, {"environment": {}},
])
def test_invalid_inputs_never_spawn(project, change):
    value = request(project) | change
    children = []
    code, frames = exchange(bridge.encode(value), capture=children)
    assert_stream(code, frames, "failed")
    assert code == 2 and len(frames) == 1 and not children
    assert "whoami" not in json.dumps(frames)


@pytest.mark.parametrize("raw", [
    b"[]\n", b"null\n", b"{}\n", b"\xef\xbb\xbf{}\n", b"\xff\n",
    b'{"protocolVersion":1,"protocolVersion":1}\n', b'{"number":NaN}\n',
    b'{"number":Infinity}\n', b'{"text":"\\ud800"}\n', b"x" * 16384 + b"\n",
    b"[" * 2000 + b"]" * 2000 + b"\n",
])
def test_strict_json_and_byte_bounds(raw):
    code, frames = exchange(raw)
    assert_stream(code, frames, "failed")
    assert code == 2 and len(frames) == 1


@pytest.mark.parametrize("brief", ["", " \n ", "a" * 4001, "\x1bsecret", "\x00"])
def test_bad_research_brief(brief):
    code, frames = exchange(bridge.encode(research_request() | {"brief": brief}))
    assert_stream(code, frames, "failed")
    assert code == 2


def test_request_profile_combinations():
    for value in [research_request() | {"projectRoot": "/srv/projects/example"},
                  research_request() | {"agentId": "automation-engineer"}]:
        with pytest.raises(bridge.BridgeError):
            bridge.validate_request(value)


def test_research_disabled_has_no_started_or_model_process():
    children = []
    code, frames = exchange(bridge.encode(research_request()), capture=children)
    assert_stream(code, frames, "failed")
    assert code == 3 and not children and len(frames) == 1


def test_catalog_uses_actual_union_and_no_credentials_or_model():
    with patch.object(bridge, "_offline_help", side_effect=AssertionError("must stay offline")), \
         _patch_telemetry_forbidden():
        result = bridge.catalog()
    assert len(result["agents"]) == 14
    loid = next(row for row in result["agents"] if row["agentId"] == "loid")
    assert loid["pluginDefined"] and not loid["discordRegistered"]
    assert [(p["profileId"], p["agentId"]) for p in result["profiles"]] == [
        ("project-check", "automation-engineer"), ("public-research", "research"),
    ]
    assert result["profiles"][0]["available"]
    assert not result["profiles"][1]["available"]
    assert len(bridge.encode(result)) <= bridge.CATALOG_BYTES
    assert "keychain" not in json.dumps(result) and "client_id" not in json.dumps(result)


def _patch_telemetry_forbidden():
    return patch.object(claude_invoke.agent_telemetry, "Invocation",
                        side_effect=AssertionError("no telemetry writes"))


def test_catalog_rejects_registry_instead_of_truncating():
    with patch.object(bridge, "discover_registry", return_value=[{"id": "a" * 65}] * 129):
        with pytest.raises(bridge.BridgeError, match="registryUnavailable"):
            bridge.catalog()


def test_missing_registry_profile_does_not_grant_another_role():
    assert bridge.profile_reason("project-check", [{"agentId": "luna", "pluginDefined": True,
                                                   "discordRegistered": True}]) == "agentUnavailable"


def test_capability_gate_remains_unverified_even_if_help_lists_all_flags(monkeypatch):
    monkeypatch.setenv("NB_MOONBASE_PUBLIC_RESEARCH", "1")
    help_text = "\n".join(f"  {flag} <value>  description" for flag in claude_invoke.PUBLIC_RESEARCH_FLAGS)
    with patch.object(bridge.shutil, "which", return_value="/synthetic/claude"), \
         patch.object(bridge, "_offline_help", return_value=help_text):
        assert bridge.research_capability() == (None, "cliPolicyUnverified")


def test_missing_cli_and_direct_auth_are_not_retried(monkeypatch):
    monkeypatch.setenv("NB_MOONBASE_PUBLIC_RESEARCH", "1")
    with patch.object(bridge.shutil, "which", return_value=None):
        assert bridge.research_capability() == (None, "claudeMissing")
    monkeypatch.setenv("NB_CLAUDE_DIRECT", "1")
    with patch.object(bridge, "_offline_help", side_effect=AssertionError("no auth probe")):
        assert bridge.research_capability() == (None, "directAuthUnsupported")


def test_offline_help_is_bounded_and_isolated_without_model_or_auth(tmp_path):
    process = Mock()
    process.stdout = Mock()
    process.stdout.fileno.return_value = 99
    process.wait.return_value = 0
    with patch.object(bridge.subprocess, "Popen", return_value=process) as spawn, \
         patch.object(bridge.select, "select", return_value=([process.stdout], [], [])), \
         patch.object(bridge.os, "read", side_effect=[b"x" * 4096] * 17), \
         patch.object(bridge, "stop_owned", return_value=True) as stop:
        assert bridge._offline_help("/synthetic/claude") is None
    assert spawn.call_args.args[0] == ["/synthetic/claude", "--help"]
    assert spawn.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert spawn.call_args.kwargs["env"]["HOME"] != str(Path.home())
    assert "ANTHROPIC_API_KEY" not in spawn.call_args.kwargs["env"]
    stop.assert_called_once_with(process, group=True)


@pytest.mark.parametrize("root", ["/", "/Users", "/tmp", ".", "../project", str(Path.home()),
                                "/srv/projects/../example", "/srv//projects/example"])
def test_unsafe_roots(root):
    with pytest.raises(check.CheckError, match="invalidRoot"):
        check.open_project_root(root)


def test_invalid_root_is_rejected_before_started_by_timed_worker():
    children = []
    code, frames = exchange(bridge.encode(request(Path("/"))), capture=children)
    assert_stream(code, frames, "failed")
    assert code == 2 and frames[-1]["payload"]["reasonCode"] == "invalidRoot"
    assert len(frames) == 1 and len(children) == 1
    assert children[0].returncode == 2 and bridge._group_gone(children[0].pid)


def test_symlinked_root_and_ancestor_are_rejected(project, tmp_path):
    link = tmp_path.resolve() / "linked"
    link.symlink_to(project, target_is_directory=True)
    for root in [str(link), str(link / "child")]:
        with pytest.raises(check.CheckError, match="invalidRoot"):
            check.open_project_root(root)


def scan(root):
    fd = check.open_project_root(str(root))
    progress = []
    try:
        return check.scan_project(fd, checkpoint=lambda: None, progress=progress.append), progress
    finally:
        os.close(fd)


def test_hidden_links_dependencies_special_files_are_not_read(project, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("synthetic-private-contents")
    (project / "leak.md").symlink_to(secret)
    (project / "outside").symlink_to(tmp_path, target_is_directory=True)
    (project / ".env").write_text("synthetic-environment-secret")
    (project / ".git").mkdir()
    (project / "node_modules").mkdir()
    (project / "node_modules" / "evil.py").write_text("raise AssertionError\n")
    os.link(secret, project / "hardlink.txt")
    os.mkfifo(project / "pipe.txt")
    report, progress = scan(project)
    assert "Selected files: 3" in report
    assert "Skipped entries: 7" in report
    assert progress == [10]
    assert "private" not in report and str(tmp_path) not in report


@pytest.mark.parametrize("text", ['{"n":NaN}', '{"n":Infinity}', '{"n":-Infinity}', "{oops", "[]"])
def test_bad_package_metadata_fails_with_fixed_code(project, text):
    (project / "package.json").write_text(text)
    with pytest.raises(check.CheckError, match="checkFailed"):
        scan(project)


def test_invalid_toml_and_metadata_byte_limit(project):
    (project / "pyproject.toml").write_text("invalid = [")
    with pytest.raises(check.CheckError, match="checkFailed"):
        scan(project)
    (project / "pyproject.toml").unlink()
    (project / "package.json").write_bytes(b"x" * (check.MAX_METADATA + 1))
    with pytest.raises(check.CheckError, match="checkLimit"):
        scan(project)


def test_metadata_exact_byte_limit_is_accepted(project):
    text = '{"name":"synthetic"}'
    (project / "package.json").write_text(text + " " * (check.MAX_METADATA - len(text)))
    assert "Validated metadata files: 1" in scan(project)[0]


@pytest.mark.parametrize("limit,value", [("MAX_ENTRIES", 2), ("MAX_FILES", 2), ("MAX_BYTES", 1)])
def test_actual_scan_limits(project, monkeypatch, limit, value):
    monkeypatch.setattr(check, limit, value)
    with pytest.raises(check.CheckError, match="checkLimit"):
        scan(project)


def test_depth_limit(project, monkeypatch):
    (project / "a").mkdir()
    (project / "a" / "b").mkdir()
    monkeypatch.setattr(check, "MAX_DEPTH", 1)
    with pytest.raises(check.CheckError, match="checkLimit"):
        scan(project)


def test_missing_readme_and_metadata_are_findings_not_command_failures(tmp_path):
    root = tmp_path.resolve() / "empty"
    root.mkdir()
    report, progress = scan(root)
    assert "Findings: missingReadme, missingMetadata" in report
    assert "Selected files: 0" in report and progress == []


def test_event_limits_reserve_terminal_and_chunks_are_utf8_bounded():
    rows = []
    with patch.object(bridge, "write_bytes", side_effect=lambda _fd, data: rows.append(bridge.decode(data))):
        events = bridge.Events(-1)
        events.job_id = JOB
        for count in range(127):
            events.emit("progress", {"stage": "scan", "unit": "entries", "completed": count})
        with pytest.raises(bridge.BridgeError, match="outputLimit"):
            events.emit("progress", {"stage": "scan", "unit": "entries", "completed": 127})
        events.terminal("outputLimit")
    assert_stream(4, rows, "failed")
    rows = []
    with patch.object(bridge, "write_bytes", side_effect=lambda _fd, data: rows.append(bridge.decode(data))):
        events = bridge.Events(-1)
        text = "\N{SNOWMAN}" * 10000
        events.report(text)
        events.terminal("completed")
    assert_stream(0, rows, "succeeded")
    assert "".join(r["payload"]["text"] for r in rows[:-1]) == text


def test_request_exact_byte_limit_and_split_line():
    input_r, input_w = os.pipe()
    reader = bridge.Input(input_r)
    body = bridge.encode({"text": ""})
    line = bridge.encode({"text": "x" * (bridge.LIMITS["requestBytes"] - len(body))})
    assert len(line) == bridge.LIMITS["requestBytes"]
    try:
        # Never fill a pipe synchronously beyond its capacity.
        for offset in range(0, len(line), 4096):
            os.write(input_w, line[offset:offset + 4096])
            rows = reader.read()
            if offset + 4096 < len(line):
                assert not rows
        assert rows == [json.loads(line)] and not reader.buffer
    finally:
        os.close(input_r)
        os.close(input_w)


def test_worker_eof_without_report_is_interrupted(project, monkeypatch):
    input_r, input_w = os.pipe()
    output = []
    code = (
        "import json,sys; print(json.dumps({'type':'ready'}),flush=True); "
        "sys.stdin.buffer.readline()"
    )
    process = subprocess.Popen([sys.executable, "-I", "-u", "-c", code],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        events = bridge.Events(-1)
        with patch.object(bridge, "write_bytes", side_effect=lambda fd, data: output.append(data)):
            with pytest.raises(bridge.BridgeError, match="processInterrupted"):
                bridge._monitor(process, bridge.Input(input_r), events, request(project), [False])
    finally:
        assert bridge.stop_owned(process, group=True)
        process.stdin.close()
        process.stdout.close()
        os.close(input_r)
        os.close(input_w)


def test_output_closed_still_reaps_owned_job(project):
    children = []
    actual = bridge.subprocess.Popen
    input_r, input_w = os.pipe()
    output_r, output_w = os.pipe()
    os.write(input_w, bridge.encode(request(project)))
    os.close(output_r)

    def spawn(*args, **kwargs):
        p = actual(*args, **kwargs)
        children.append(p)
        return p

    try:
        with patch.object(bridge.subprocess, "Popen", side_effect=spawn):
            assert bridge.run(input_r, output_w) == 7
        assert children and children[0].returncode is not None
        assert bridge._group_gone(children[0].pid)
    finally:
        for fd in (input_r, input_w, output_w):
            os.close(fd)


@pytest.mark.parametrize("cancel", [False, True])
def test_mocked_research_owns_and_reaps_actual_standin_subprocess(cancel):
    input_r, input_w = os.pipe()
    children = []
    stopped = [False]
    output = []
    result = {"type": "result", "subtype": "success", "is_error": False,
              "num_turns": 1, "result": "Synthetic report. Source: https://example.org/one\nUnverified."}
    code = "import sys; sys.stdin.buffer.read(); print(" + repr(json.dumps(result)) + ",flush=True)"

    def start(**kwargs):
        process = subprocess.Popen([sys.executable, "-I", "-u", "-c", code],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        children.append(process)
        assert kwargs["model"] == "claude-opus-5"
        assert kwargs["effort"] == "high"
        assert "You are the Research agent for Neural Bridge." in kwargs["system_prompt"]
        return process

    def ready(value):
        output.append(value)
        if cancel:
            stopped[0] = True

    try:
        with os.fdopen(input_r, "rb") as owner, \
             patch.object(bridge.sys, "stdin", owner), \
             patch.object(bridge, "research_capability", return_value=("/synthetic/claude", None)), \
             patch.object(claude_invoke, "start_public_research", side_effect=start), \
             patch.object(bridge, "_worker_send", side_effect=ready), \
             patch.object(bridge, "_worker_go"), \
             _patch_telemetry_forbidden():
            if cancel:
                with pytest.raises(bridge.BridgeError, match="cancelled"):
                    bridge._research_worker(research_request(), stopped)
            else:
                assert bridge._research_worker(research_request(), stopped) == result["result"]
        assert output == [{"type": "ready"}]
        assert children[0].returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
    finally:
        os.close(input_w)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_total_byte_limit_reserves_terminal(monkeypatch):
    rows = []
    monkeypatch.setitem(bridge.LIMITS, "totalEventBytes", 1500)
    with patch.object(bridge, "write_bytes", side_effect=lambda _fd, data: rows.append(bridge.decode(data))):
        events = bridge.Events(-1)
        with pytest.raises(bridge.BridgeError, match="outputLimit"):
            events.report("x" * 1000)
        events.terminal("outputLimit")
    assert len(rows) == 1 and len(bridge.encode(rows[0])) <= 1500


@pytest.mark.parametrize("text", [
    "x" * 32769, " \n", "\x1b[31mraw", "Read /Users/person/private/file.txt",
    "File C:\\Users\\person\\secret.txt", "file:///tmp/local", "HOME=/home/person/file",
    "api_key=synthetic-credential", "Authorization: bearer synthetic",
    "sk-synthetic-credential-value", "https://example.org/?token=synthetic",
    "https://user:password@example.org/path", "https://localhost/path",
    "https://[fd00::1]/private", "https://127.0.0.1/private",
    "https://[bad-address]/path",
])
def test_unsafe_result_is_refused_not_echoed(text):
    with pytest.raises(bridge.BridgeError, match="resultRejected"):
        bridge._safe_report(text)


def test_prompt_echo_rejected_but_plain_public_citations_allowed():
    brief = research_request()["brief"]
    with pytest.raises(bridge.BridgeError, match="resultRejected"):
        bridge._safe_report("The input was: " + brief, brief)
    text = "A public source: https://example.org/standards/one\nUnverified synthesis."
    assert bridge._safe_report(text, brief) == text


def test_restricted_low_level_argv_and_environment_do_not_change_native_defaults(tmp_path):
    cwd = tmp_path.resolve()
    routed = {"ANTHROPIC_BASE_URL": "http://localhost:4142", "ANTHROPIC_API_KEY": "copilot-proxy",
              "NB_DISCORD_WEBHOOK": "must-not-leak", "SECRET": "must-not-leak"}
    with patch.object(claude_invoke, "_subprocess_env", return_value=routed) as env, \
         patch.object(claude_invoke.subprocess, "Popen", return_value=Mock()) as spawn, \
         _patch_telemetry_forbidden():
        claude_invoke.start_public_research(
            executable="/synthetic/claude", cwd=cwd, system_prompt="RELEASED CHARTER",
            model="claude-opus-5", effort="high", session_id=MODEL_JOB,
        )
    args = spawn.call_args.args[0]
    assert args == [
        "/synthetic/claude", "-p", "--output-format", "json", "--model", "claude-opus-5",
        "--effort", "high", "--bare", "--safe-mode", "--restricted",
        "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
        "--disable-slash-commands", "--no-session-persistence", "--no-chrome",
        "--setting-sources", "", "--max-turns", "6", "--system-prompt", "RELEASED CHARTER",
        "--session-id", MODEL_JOB, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    ]
    kwargs = spawn.call_args.kwargs
    assert kwargs["env"]["HOME"] == str(cwd)
    assert kwargs["cwd"] == cwd and kwargs["stdin"] == subprocess.PIPE
    assert "must-not-leak" not in repr(kwargs)
    assert not any("bypass" in arg.lower() or "resume" in arg for arg in args)
    env.assert_called_once_with(agent_id="research")


def test_native_runner_argv_and_telemetry_are_unchanged():
    result = Mock(returncode=0, stdout="native result", stderr="")
    with patch.object(claude_invoke, "_subprocess_env", return_value={}), \
         patch.object(claude_invoke.subprocess, "run", return_value=result) as invoke:
        assert claude_invoke.call_claude_sync(
            "native prompt", allowed_tools="Read", effort="medium", agent_id="docs-editor",
            add_dirs=["/synthetic/project"], session_id=JOB, resume=True,
        ) == (True, "native result", "")
    assert invoke.call_args.args[0] == [
        "claude", "-p", "native prompt", "--output-format", "text", "--model", claude_invoke.DEFAULT_MODEL,
        "--effort", "medium", "--allowedTools", "Read", "--strict-mcp-config", "--mcp-config",
        '{"mcpServers":{}}', "--add-dir", "/synthetic/project", "--resume", JOB,
    ]


def test_shared_fixtures_match_pinned_vocab_and_limits():
    fixture = json.loads((bridge.ROOT / "docs/examples/moonbase-work-v1.json").read_text())
    assert fixture["reasonExitCodes"] == bridge.REASONS
    assert set(fixture["unavailableReasons"]) == bridge.UNAVAILABLE
    assert fixture["catalog"]["limits"] == bridge.LIMITS
    assert fixture["catalogBounds"]["bytes"] == bridge.CATALOG_BYTES
    for case in fixture["cases"]:
        if case["name"] == "eofEvenWithZeroExit":
            assert not any(row["type"] == "terminal" for row in case["events"])
            assert case["expectedOutcome"] == "interrupted"
        else:
            assert_stream(case["processExitCode"], case["events"], case["expectedOutcome"])
