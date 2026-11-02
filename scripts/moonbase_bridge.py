"""One finite Moonbase job over stdio; see docs/MOONBASE_WORK_BRIDGE.md."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

# The private worker is executed with Python -I and this trusted absolute file.
ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.discord_bot.agent_telemetry import discover_registry
from scripts.moonbase_project_check import CheckError, open_project_root, scan_project

LIMITS = {
    "requestBytes": 16384, "eventBytes": 16384, "totalEventBytes": 262144,
    "eventCount": 128, "resultBytes": 32768,
}
CATALOG_BYTES = 65536
CHUNK_BYTES = 2048
RAW_BYTES = 131072
INPUT_SECONDS = 5
CLEANUP_SECONDS = 2
OUTPUT_SECONDS = 1
PROFILES = {
    "project-check": ("automation-engineer", "script", 30),
    "public-research": ("research", "model", 480),
}
REASONS = {
    "completed": 0,
    "invalidRequest": 2, "invalidControl": 2, "invalidRoot": 2,
    "profileUnavailable": 3, "registryUnavailable": 3, "unsupportedPlatform": 3,
    "checkFailed": 4, "checkLimit": 4, "researchFailed": 4,
    "resultRejected": 4, "outputLimit": 4, "workerProtocol": 4,
    "timedOut": 5, "cancelled": 6,
    "inputClosed": 7, "processInterrupted": 7, "cleanupUnknown": 7,
    "internalError": 7,
}
UNAVAILABLE = frozenset({
    "unsupportedPlatform", "registryUnavailable", "agentUnavailable",
    "researchDisabled", "claudeMissing", "cliPolicyUnverified", "directAuthUnsupported",
})
OUTCOMES = {
    0: "succeeded", 2: "failed", 3: "failed", 4: "failed",
    5: "timed_out", 6: "cancelled", 7: "interrupted",
}
UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


class BridgeError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BridgeError("invalidRequest")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise BridgeError("invalidRequest")


def decode(data: bytes) -> dict:
    try:
        text = data.decode("utf-8")
        text.encode("utf-8")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise BridgeError("invalidRequest")
        # JSON permits escaped lone surrogates; the transport does not.
        encode(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeError("invalidRequest") from None


def valid_uuid(value: object) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def validate_request(value: dict) -> dict:
    common = {"protocolVersion", "jobId", "profileId", "agentId"}
    profile = value.get("profileId")
    if (
        type(value.get("protocolVersion")) is not int or value["protocolVersion"] != 1
        or not valid_uuid(value.get("jobId")) or not isinstance(profile, str)
        or profile not in PROFILES or value.get("agentId") != PROFILES[profile][0]
    ):
        raise BridgeError("invalidRequest")
    field = "projectRoot" if profile == "project-check" else "brief"
    if set(value) != common | {field} or not isinstance(value[field], str):
        raise BridgeError("invalidRequest")
    if field == "brief" and (
        not value[field].strip() or len(value[field]) > 4000
        or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", value[field])
    ):
        raise BridgeError("invalidRequest")
    return value


def platform_supported() -> bool:
    return os.name == "posix" and sys.version_info >= (3, 12) and hasattr(os, "O_NOFOLLOW")


def _group_gone(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def stop_owned(process: subprocess.Popen, *, group: bool) -> bool:
    """Signal only a still-owned child, reap it, then check its process group.

    A dead/reaped leader is never used as a new kill target. A leftover group
    in that case is unknown, not a reason to risk signalling a reused PID.
    """
    if process.poll() is None:
        try:
            if group:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                if group:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=CLEANUP_SECONDS)
            except subprocess.TimeoutExpired:
                return False
    return process.returncode is not None and (not group or _group_gone(process.pid))


def _offline_help(executable: str) -> str | None:
    with tempfile.TemporaryDirectory(prefix="nb-moonbase-help-") as directory:
        env = {"PATH": os.defpath, "HOME": directory, "TMPDIR": directory,
               "CLAUDE_CONFIG_DIR": directory, "LANG": "C.UTF-8",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        try:
            process = subprocess.Popen(
                [executable, "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=directory, env=env, start_new_session=True,
            )
        except OSError:
            return None
        data = bytearray()
        deadline = time.monotonic() + 3
        try:
            assert process.stdout is not None
            while time.monotonic() < deadline:
                if select.select([process.stdout], [], [], 0.05)[0]:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        if process.wait(timeout=0.5) == 0 and _group_gone(process.pid):
                            return data.decode("utf-8")
                        return None
                    data.extend(chunk)
                    if len(data) > CATALOG_BYTES:
                        return None
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            return None
        finally:
            stop_owned(process, group=True)
            if process.stdout is not None:
                process.stdout.close()
    return None


def research_capability() -> tuple[str | None, str | None]:
    if os.environ.get("NB_MOONBASE_PUBLIC_RESEARCH") != "1":
        return None, "researchDisabled"
    if os.environ.get("NB_CLAUDE_DIRECT"):
        return None, "directAuthUnsupported"
    executable = shutil.which("claude")
    if not executable:
        return None, "claudeMissing"
    help_text = _offline_help(executable)
    from scripts.discord_bot.claude_invoke import PUBLIC_RESEARCH_FLAGS
    advertised = set(re.findall(r"(?m)^  (--[a-zA-Z-]+)(?=[\s,])", help_text or ""))
    if not set(PUBLIC_RESEARCH_FLAGS) <= advertised:
        return None, "cliPolicyUnverified"
    # Help is not an isolation certificate: managed-policy hooks may still
    # apply. V1 cannot prove their absence without inspecting private settings.
    # Do not let a CLI upgrade or operator opt-in silently open this boundary.
    return None, "cliPolicyUnverified"


def registry() -> list[dict]:
    try:
        rows = discover_registry(ROOT)
        if len(rows) > 128 or any(len(row["id"]) > 64 for row in rows):
            raise BridgeError("registryUnavailable")
        return [
            {"agentId": row["id"], "displayName": row["display_name"],
             "pluginDefined": row["plugin_defined"], "discordRegistered": row["discord_registered"]}
            for row in rows
        ]
    except (OSError, ValueError, KeyError, TypeError):
        raise BridgeError("registryUnavailable") from None


def profile_reason(profile: str, agents: list[dict]) -> str | None:
    agent = next((a for a in agents if a["agentId"] == PROFILES[profile][0]), None)
    if not platform_supported():
        return "unsupportedPlatform"
    if not agent or not agent["pluginDefined"] or not agent["discordRegistered"]:
        return "agentUnavailable"
    return research_capability()[1] if profile == "public-research" else None


def catalog() -> dict:
    if not platform_supported():
        raise BridgeError("unsupportedPlatform")
    agents = registry()
    profiles = []
    for profile, (agent, kind, timeout) in PROFILES.items():
        reason = profile_reason(profile, agents)
        profiles.append({
            "profileId": profile, "agentId": agent, "executionKind": kind,
            "available": reason is None, "unavailableReason": reason, "timeoutSeconds": timeout,
        })
    result = {"protocolVersion": 1, "type": "catalog", "agents": agents,
              "profiles": profiles, "limits": LIMITS}
    if len(encode(result)) > CATALOG_BYTES:
        raise BridgeError("registryUnavailable")
    return result


def write_bytes(fd: int, data: bytes) -> None:
    deadline = time.monotonic() + OUTPUT_SECONDS
    os.set_blocking(fd, False)
    while data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise BrokenPipeError
        try:
            written = os.write(fd, data)
        except BlockingIOError:
            continue
        if not written:
            raise BrokenPipeError
        data = data[written:]


class Events:
    def __init__(self, fd: int):
        self.fd = fd
        self.job_id: str | None = None
        self.sequence = 0
        self.bytes = 0
        self.terminal_sent = False

    def emit(self, kind: str, payload: dict) -> None:
        if self.terminal_sent:
            raise BridgeError("workerProtocol")
        frame = encode({"protocolVersion": 1, "jobId": self.job_id,
                        "sequence": self.sequence + 1, "type": kind, "payload": payload})
        reserve = 0 if kind == "terminal" else 1024
        if (
            len(frame) > LIMITS["eventBytes"]
            or self.bytes + len(frame) + reserve > LIMITS["totalEventBytes"]
            or self.sequence + 1 + bool(reserve) > LIMITS["eventCount"]
        ):
            raise BridgeError("outputLimit")
        write_bytes(self.fd, frame)
        self.bytes += len(frame)
        self.sequence += 1
        self.terminal_sent = kind == "terminal"

    def terminal(self, reason: str, cleanup: bool = True) -> int:
        if not cleanup:
            reason = "cleanupUnknown"
        code = REASONS[reason]
        self.emit("terminal", {"outcome": OUTCOMES[code], "reasonCode": reason,
                               "exitCode": code, "cleanupConfirmed": cleanup})
        return code

    def report(self, text: str) -> None:
        if not text.strip() or len(text.encode("utf-8")) > LIMITS["resultBytes"]:
            raise BridgeError("resultRejected")
        chunks: list[str] = []
        current: list[str] = []
        size = 0
        for char in text:
            width = len(char.encode("utf-8"))
            if size + width > CHUNK_BYTES:
                chunks.append("".join(current))
                current, size = [], 0
            current.append(char)
            size += width
        if current:
            chunks.append("".join(current))
        for index, chunk in enumerate(chunks):
            self.emit("result", {"format": "plainText", "chunkIndex": index, "text": chunk})


class Input:
    def __init__(self, fd: int):
        self.fd = fd
        self.buffer = bytearray()
        self.closed = False
        self.lines = 0

    def read(self) -> list[dict]:
        chunk = os.read(self.fd, 4096)
        if not chunk:
            self.closed = True
            if self.buffer:
                raise BridgeError("invalidRequest")
            return []
        self.buffer.extend(chunk)
        rows = []
        while b"\n" in self.buffer:
            end = self.buffer.index(b"\n") + 1
            if end > LIMITS["requestBytes"] or self.lines == 128:
                raise BridgeError("invalidRequest")
            line = bytes(self.buffer[:end])
            del self.buffer[:end]
            self.lines += 1
            rows.append(decode(line))
        if len(self.buffer) >= LIMITS["requestBytes"]:
            raise BridgeError("invalidRequest")
        return rows


@contextmanager
def cancellation():
    stopped = [False]

    def handle(_signal, _frame):
        stopped[0] = True

    previous = {sig: signal.signal(sig, handle) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield stopped
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _controls(rows: list[dict], job_id: str) -> bool:
    expected = {"protocolVersion": 1, "type": "cancel", "jobId": job_id}
    for row in rows:
        if row != expected or type(row.get("protocolVersion")) is not int:
            raise BridgeError("invalidControl")
    return bool(rows)


def _initial(reader: Input, stopped: list[bool]) -> tuple[dict, list[dict]]:
    deadline = time.monotonic() + INPUT_SECONDS
    while time.monotonic() < deadline:
        if stopped[0]:
            raise BridgeError("cancelled")
        if select.select([reader.fd], [], [], 0.05)[0]:
            rows = reader.read()
            if rows:
                return rows[0], rows[1:]
            if reader.closed:
                break
    raise BridgeError("invalidRequest")


def _safe_report(text: object, brief: str = "") -> str:
    if not isinstance(text, str) or not text.strip():
        raise BridgeError("resultRejected")
    if len(text.encode("utf-8")) > LIMITS["resultBytes"] or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
        raise BridgeError("resultRejected")
    if brief and " ".join(brief.split()).casefold() in " ".join(text.split()).casefold():
        raise BridgeError("resultRejected")
    remainder = text
    for match in re.finditer(r"https?://[^\s<>\"']+", text):
        url = match.group(0).rstrip(").,;]")
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
        except ValueError:
            raise BridgeError("resultRejected") from None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            literal_address = False
        else:
            literal_address = True
        if (
            parts.username or parts.password or parts.query or parts.fragment
            or not host or host == "localhost" or host.endswith((".local", ".internal"))
            or literal_address
        ):
            raise BridgeError("resultRejected")
        remainder = remainder.replace(url, "")
    if re.search(
        r"(?i)(?:[a-z]:[\\/]|file://|~/|(?<!\w)/(?:[\w .-]+/)+|"
        r"\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9_-]{8,}|"
        r"\b(?:api[_-]?key|token|password|secret|authorization)\s*[:=]|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
        remainder,
    ):
        raise BridgeError("resultRejected")
    return text


def _worker_send(value: dict) -> None:
    data = encode(value)
    if len(data) > RAW_BYTES:
        raise BridgeError("outputLimit")
    write_bytes(sys.stdout.fileno(), data)


def _worker_checkpoint(stopped: list[bool]) -> None:
    if stopped[0]:
        raise BridgeError("cancelled")
    if select.select([sys.stdin.fileno()], [], [], 0)[0]:
        if not os.read(sys.stdin.fileno(), 4096):
            raise BridgeError("inputClosed")
        raise BridgeError("invalidControl")


def _worker_go(stopped: list[bool]) -> None:
    deadline = time.monotonic() + INPUT_SECONDS
    while time.monotonic() < deadline:
        if stopped[0]:
            raise BridgeError("cancelled")
        if select.select([sys.stdin.fileno()], [], [], 0.05)[0]:
            if os.read(sys.stdin.fileno(), 3) != b"go\n":
                raise BridgeError("inputClosed")
            return
    raise BridgeError("inputClosed")


def _research_worker(request: dict, stopped: list[bool]) -> str:
    from scripts.discord_bot.claude_invoke import DEFAULT_MODEL, start_public_research, wrap_untrusted
    from scripts.discord_bot.mention import effort_for, load_agent_definition, model_for

    executable, reason = research_capability()
    if reason or not executable:
        raise BridgeError("profileUnavailable")
    charter = load_agent_definition("research")
    system = (
        charter + "\n\nFinite public-research profile overrides local-memory and filing rules above. "
        "Use only public web sources for the supplied brief. No local files, memories, other "
        "agents, private tools, or chat delivery. Return a concise cited report as text, not "
        "a tool transcript or prompt echo. State uncertainty; do not claim verified truth. "
        "Do not include credentials, local paths, or URL credentials/query strings/fragments."
    )
    prompt = wrap_untrusted(request["brief"], "public-brief").encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="nb-moonbase-research-") as directory:
        process = start_public_research(
            executable=executable, cwd=Path(directory), system_prompt=system,
            model=model_for("research") or DEFAULT_MODEL, effort=effort_for("research"),
            session_id=str(uuid.uuid4()),
        )
        try:
            if process.poll() is not None:
                raise BridgeError("researchFailed")
            _worker_send({"type": "ready"})
            _worker_go(stopped)
            assert process.stdin is not None and process.stdout is not None
            os.set_blocking(process.stdin.fileno(), False)
            raw = bytearray()
            deadline = time.monotonic() + PROFILES["public-research"][2]
            while time.monotonic() < deadline:
                _worker_checkpoint(stopped)
                reads, writes, _ = select.select(
                    [process.stdout, sys.stdin.fileno()],
                    [process.stdin] if prompt else [], [], 0.05,
                )
                if process.stdin in writes:
                    count = os.write(process.stdin.fileno(), prompt)
                    prompt = prompt[count:]
                    if not prompt:
                        process.stdin.close()
                if process.stdout in reads:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        if process.wait(timeout=0.5) != 0:
                            raise BridgeError("researchFailed")
                        break
                    raw.extend(chunk)
                    if len(raw) > RAW_BYTES:
                        raise BridgeError("outputLimit")
            else:
                raise BridgeError("timedOut")
            try:
                result = decode(bytes(raw))
            except BridgeError:
                raise BridgeError("researchFailed") from None
            if (
                result.get("type") != "result" or result.get("subtype") != "success"
                or result.get("is_error") is not False or type(result.get("num_turns")) is not int
                or not 1 <= result["num_turns"] <= 6
            ):
                raise BridgeError("researchFailed")
            return _safe_report(result.get("result"), request["brief"])
        finally:
            cleaned = stop_owned(process, group=False)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
            if not cleaned:
                raise BridgeError("cleanupUnknown")


def worker() -> int:
    root_fd = -1
    with cancellation() as stopped:
        try:
            line = sys.stdin.buffer.readline(LIMITS["requestBytes"] + 1)
            if not line.endswith(b"\n") or len(line) > LIMITS["requestBytes"]:
                raise BridgeError("invalidRequest")
            request = validate_request(decode(line))
            if request["profileId"] == "project-check":
                try:
                    root_fd = open_project_root(request["projectRoot"])
                except (CheckError, OSError):
                    raise BridgeError("invalidRoot") from None
                _worker_send({"type": "ready"})
                _worker_go(stopped)
                text = scan_project(
                    root_fd, checkpoint=lambda: _worker_checkpoint(stopped),
                    progress=lambda count: _worker_send({"type": "progress", "completed": count}),
                )
            else:
                text = _research_worker(request, stopped)
            _worker_send({"type": "report", "text": text})
            return 0
        except (BridgeError, CheckError) as exc:
            reason = exc.reason if isinstance(exc, BridgeError) else str(exc)
            _worker_send({"type": "error", "reasonCode": reason})
            return REASONS[reason]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            _worker_send({"type": "error", "reasonCode": "checkFailed" if root_fd >= 0 else "researchFailed"})
            return 4
        finally:
            if root_fd >= 0:
                os.close(root_fd)


def _monitor(process: subprocess.Popen, reader: Input, events: Events,
             request: dict, stopped: list[bool]) -> str:
    assert process.stdout is not None and process.stdin is not None
    deadline = time.monotonic() + PROFILES[request["profileId"]][2]
    pending = bytearray()
    raw_bytes = count = completed = 0
    started = False
    report: str | None = None
    error: str | None = None
    while time.monotonic() < deadline:
        if stopped[0]:
            raise BridgeError("cancelled")
        reads = select.select([reader.fd, process.stdout], [], [], 0.05)[0]
        # Cancellation/disconnect wins if both it and completion are observable.
        if reader.fd in reads:
            try:
                controls = reader.read()
            except BridgeError:
                raise BridgeError("invalidControl") from None
            if reader.closed:
                raise BridgeError("inputClosed")
            if _controls(controls, request["jobId"]):
                if reader.buffer:
                    raise BridgeError("invalidControl")
                raise BridgeError("cancelled")
        if process.stdout not in reads:
            continue
        chunk = os.read(process.stdout.fileno(), 4096)
        if not chunk:
            if pending:
                raise BridgeError("workerProtocol")
            try:
                exit_code = process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                raise BridgeError("processInterrupted") from None
            if error:
                raise BridgeError(error)
            if exit_code != 0 or not started or report is None:
                raise BridgeError("processInterrupted")
            return report
        raw_bytes += len(chunk)
        if raw_bytes > RAW_BYTES:
            raise BridgeError("outputLimit")
        pending.extend(chunk)
        while b"\n" in pending:
            end = pending.index(b"\n") + 1
            try:
                row = decode(bytes(pending[:end]))
            except BridgeError:
                raise BridgeError("workerProtocol") from None
            del pending[:end]
            count += 1
            if count > 80 or error or report is not None:
                raise BridgeError("workerProtocol")
            if row == {"type": "ready"} and not started:
                if process.poll() is not None:
                    raise BridgeError("processInterrupted")
                events.emit("started", {key: request[key] for key in ("profileId", "agentId")} |
                            {"executionKind": PROFILES[request["profileId"]][1]})
                started = True
                process.stdin.write(b"go\n")
                process.stdin.flush()
            elif (
                set(row) == {"type", "completed"} and row["type"] == "progress" and started
                and request["profileId"] == "project-check" and type(row["completed"]) is int
                and completed < row["completed"] <= 4096
            ):
                completed = row["completed"]
                events.emit("progress", {"stage": "scan", "unit": "entries", "completed": completed})
            elif set(row) == {"type", "text"} and row["type"] == "report" and started:
                report = _safe_report(row["text"], request.get("brief", ""))
            elif (
                set(row) == {"type", "reasonCode"} and row["type"] == "error"
                and isinstance(row["reasonCode"], str) and row["reasonCode"] in REASONS
                and row["reasonCode"] != "completed"
            ):
                error = row["reasonCode"]
            else:
                raise BridgeError("workerProtocol")
    raise BridgeError("timedOut")


def run(input_fd: int, output_fd: int) -> int:
    events = Events(output_fd)
    reader = Input(input_fd)
    process: subprocess.Popen | None = None
    reason = "internalError"
    cleaned = True
    with cancellation() as stopped:
        try:
            if not platform_supported():
                raise BridgeError("unsupportedPlatform")
            raw, controls = _initial(reader, stopped)
            if valid_uuid(raw.get("jobId")):
                events.job_id = raw["jobId"]
            request = validate_request(raw)
            if _controls(controls, request["jobId"]) or stopped[0]:
                raise BridgeError("cancelled")
            if profile_reason(request["profileId"], registry()):
                raise BridgeError("profileUnavailable")
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-u", str(Path(__file__).resolve()), "_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            assert process.stdin is not None
            process.stdin.write(encode(request))
            process.stdin.flush()
            report = _monitor(process, reader, events, request, stopped)
            cleaned = stop_owned(process, group=True)
            if not cleaned:
                raise BridgeError("cleanupUnknown")
            if reader.buffer:
                raise BridgeError("invalidControl")
            events.report(report)
            reason = "completed"
        except BridgeError as exc:
            reason = exc.reason
        except (OSError, ValueError):
            reason = "internalError"
        finally:
            if process is not None:
                cleaned = stop_owned(process, group=True) and cleaned
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
        # Closing stdin after this write is normal completion, not disconnect.
        try:
            if reason == "completed" and select.select([reader.fd], [], [], 0)[0]:
                try:
                    controls = reader.read()
                    if reader.closed:
                        reason = "inputClosed"
                    elif _controls(controls, request["jobId"]):
                        reason = "cancelled"
                except BridgeError:
                    reason = "invalidControl"
            if reason == "completed" and stopped[0]:
                reason = "cancelled"
            if reason in {"completed", "cancelled"} and reader.buffer:
                reason = "invalidControl"
            return events.terminal(reason, cleaned)
        except (BridgeError, OSError):
            return 7


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["run"]:
        return run(sys.stdin.fileno(), sys.stdout.fileno())
    if args == ["catalog"]:
        try:
            write_bytes(sys.stdout.fileno(), encode(catalog()))
            return 0
        except (BridgeError, OSError) as exc:
            reason = exc.reason if isinstance(exc, BridgeError) else "registryUnavailable"
            print(reason, file=sys.stderr)
            return 3
    if args == ["_worker"]:
        return worker()
    print("usage: python -m scripts.moonbase_bridge catalog|run", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
