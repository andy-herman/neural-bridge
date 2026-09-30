#!/usr/bin/env python3
"""Neural Bridge's model gateway: one local relay between `claude -p` and the
Copilot proxy, owned by this repo.

    python -m scripts.model_gateway          (launchd: com.andyherman.neural-bridge.model-gateway)

Every agent turn and the memory pipeline reach models through
claude_env.proxy_env(), which points Claude Code here (localhost:4142). The
gateway relays to copilot-api (localhost:4141) and fixes two things on the way.

1. Claude 5 requests. Claude Code sends Claude 5 models a conversation that
   ends with a `role: "system"` message (the API allows mid-conversation
   system messages). copilot-api passes it through, and Copilot rejects the
   request: "This model does not support assistant message prefill. The
   conversation must end with a user message." Every Claude 5 call failed
   that way from at least 2026-09-25. The gateway folds those messages into
   the top-level system prompt. With that, Sonnet 5, Opus 5 and Opus 5.5
   answer, tool calls included (verified 2026-09-30, Claude Code 2.1.286).

2. A stale upstream token. copilot-api can keep a dead Copilot token and
   answer every model 403 "forbidden" while logging nothing about it; on
   2026-09-30 that took Luna down until the proxy was restarted by hand. On a
   403 the gateway restarts copilot-api (at most once per RESTART_INTERVAL),
   waits for it to answer, and retries the request once. If the retry is
   refused too, restarting is not the fix, so it raises a review-queue alert
   and clears it on the next success.

It is also the one place a provider change would go, so moving off the
Copilot seat is a change here rather than across the fleet.

Nothing it logs carries request text, keys or headers: method, path, model,
status, duration, and how many system messages it folded.

Config (environment):
    NB_MODEL_GATEWAY_PORT       listen port on 127.0.0.1 (default 4142)
    NB_MODEL_GATEWAY_UPSTREAM   upstream base URL (default http://localhost:4141)
    NB_MODEL_GATEWAY_RESTART    launchd label restarted on a 403 (default com.andyherman.copilot-api)
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_PORT = 4142
DEFAULT_UPSTREAM = "http://localhost:4141"
DEFAULT_RESTART_LABEL = "com.andyherman.copilot-api"
RESTART_INTERVAL = 300      # seconds; never restart the proxy more often than this
UPSTREAM_WAIT = 60          # seconds to wait for the proxy to answer after a restart
UPSTREAM_TIMEOUT = 900      # socket timeout; long agent turns stream for minutes
ALERT_SOURCE, ALERT_KEY = "model_gateway", "upstream_forbidden"

# Headers that describe one hop, not the message.
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-connection", "transfer-encoding",
                        "te", "trailer", "upgrade", "content-length", "host"})

_log = logging.getLogger("nb_model_gateway")


# ---------- the request fix (pure) ----------

def fold_system_messages(body: dict) -> int:
    """Move every `role: "system"` message into the top-level system prompt.

    Mutates `body`; returns how many messages were folded. Order is kept: the
    base system prompt first, then the folded messages in conversation order.
    Their text is not dropped, only moved to where every model accepts it.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    moved = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    if not moved:
        return 0
    body["messages"] = [m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]
    system = body.get("system")
    if system is None:
        blocks: list = []
    elif isinstance(system, str):
        blocks = [{"type": "text", "text": system}] if system else []
    else:
        blocks = list(system)
    for m in moved:
        content = m.get("content")
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            blocks.extend(b for b in content if isinstance(b, dict) and b.get("type") == "text")
    body["system"] = blocks
    return len(moved)


def rewrite(method: str, path: str, raw: bytes) -> tuple[bytes, dict]:
    """The request as it goes upstream, plus facts for the log line."""
    facts: dict = {}
    if method != "POST" or not path.split("?", 1)[0].startswith("/v1/messages") or not raw:
        return raw, facts
    try:
        body = json.loads(raw)
    except ValueError:
        return raw, facts
    if not isinstance(body, dict):
        return raw, facts
    facts["model"] = body.get("model")
    folded = fold_system_messages(body)
    if folded:
        facts["folded"] = folded
        return json.dumps(body).encode("utf-8"), facts
    return raw, facts


# ---------- recovering from a stale proxy token ----------

@dataclass
class Recovery:
    """Restart the proxy on a 403, at most once per `interval` seconds."""
    restart: Callable[[], bool]
    upstream_up: Callable[[], bool]
    interval: float = RESTART_INTERVAL
    wait: float = UPSTREAM_WAIT
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    restarts: int = 0
    last_restart: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def recover(self) -> bool:
        """True when a restart was issued (by this call or one moments ago) and
        the proxy answers again, so a retry is worth making."""
        with self._lock:
            now = self.clock()
            recent = self.last_restart is not None and now - self.last_restart < self.interval
            if not recent:
                ok = self.restart()
                self.last_restart = now
                if ok:
                    self.restarts += 1
                _log.warning("upstream 403: restarted the proxy (%s)", "ok" if ok else "restart command failed")
            deadline = self.clock() + self.wait
            while self.clock() < deadline:
                if self.upstream_up():
                    return True
                self.sleep(1.0)
            return False


def launchctl_restart(label: str) -> Callable[[], bool]:
    def _restart() -> bool:
        try:
            r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                               capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return _restart


# ---------- review-queue alert (only when a restart did not help) ----------

class Alerts:
    def __init__(self):
        self.open = False

    def raise_(self, detail: str) -> None:
        try:
            from scripts.review_queue import store as qs
            qs.Store().raise_item(source=ALERT_SOURCE, key=ALERT_KEY, kind=qs.ALERT,
                                  title="Copilot proxy refuses requests even after a restart",
                                  detail=detail[:200])
            self.open = True
        except Exception as exc:  # the alert path must never break the relay
            _log.error("could not raise the review-queue alert: %s", exc)

    def clear(self) -> None:
        if not self.open:
            return
        try:
            from scripts.review_queue import store as qs
            qs.Store().clear(source=ALERT_SOURCE, key=ALERT_KEY)
            self.open = False
        except Exception as exc:
            _log.error("could not clear the review-queue alert: %s", exc)


# ---------- the relay ----------

class Gateway:
    def __init__(self, upstream: str, recovery: Recovery, alerts: Alerts | None = None):
        parts = urlsplit(upstream)
        self.host = parts.hostname or "localhost"
        self.port = parts.port or 80
        self.recovery = recovery
        self.alerts = alerts or Alerts()
        self.stats = {"requests": 0, "folded": 0, "retried": 0, "forbidden": 0, "upstream_errors": 0}
        self._stats_lock = threading.Lock()

    def bump(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def upstream_up(self) -> bool:
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=3)
            conn.request("HEAD", "/")
            ok = conn.getresponse().status < 500
            conn.close()
            return ok
        except OSError:
            return False

    def open_upstream(self, method: str, path: str, headers: dict, body: bytes):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=UPSTREAM_TIMEOUT)
        conn.request(method, path, body=body if body else None, headers=headers)
        return conn, conn.getresponse()


def make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # our own log line, without request detail
            pass

        def _health(self) -> None:
            with gw._stats_lock:
                stats = dict(gw.stats)
            payload = {"ok": True, "upstream_up": gw.upstream_up(), "restarts": gw.recovery.restarts,
                       "alert_open": gw.alerts.open, **stats}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _relay(self) -> None:
            if self.path == "/_gateway/health":
                return self._health()
            started = time.monotonic()
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            body, facts = rewrite(self.command, self.path, raw)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            if body:
                headers["Content-Length"] = str(len(body))
            is_messages = self.command == "POST" and self.path.startswith("/v1/messages")
            gw.bump("requests")
            if facts.get("folded"):
                gw.bump("folded", facts["folded"])

            try:
                conn, resp = gw.open_upstream(self.command, self.path, headers, body)
            except OSError as exc:
                gw.bump("upstream_errors")
                return self._error(502, f"model gateway: the proxy is unreachable ({type(exc).__name__})",
                                   facts, started)

            retried = False
            if resp.status == 403 and is_messages:
                resp.read()
                conn.close()
                gw.bump("forbidden")
                if gw.recovery.recover():
                    retried = True
                    gw.bump("retried")
                    try:
                        conn, resp = gw.open_upstream(self.command, self.path, headers, body)
                    except OSError as exc:
                        gw.bump("upstream_errors")
                        return self._error(502, f"model gateway: the proxy is unreachable after a restart "
                                                f"({type(exc).__name__})", facts, started)
                else:
                    try:
                        conn, resp = gw.open_upstream(self.command, self.path, headers, body)
                    except OSError:
                        return self._error(502, "model gateway: the proxy did not come back after a restart",
                                           facts, started)
                if resp.status == 403:
                    gw.alerts.raise_("Every request was refused 403 before and after restarting the proxy; "
                                     "check the Copilot login and the proxy log.")

            if is_messages and resp.status < 400:
                gw.alerts.clear()
            try:
                self._stream(resp)
            finally:
                conn.close()
                self._log_line(resp.status, facts, started, retried)

        def _stream(self, resp) -> None:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            if self.command == "HEAD":
                self.send_header("content-length", "0")
                self.end_headers()
                return
            length = resp.getheader("content-length")
            chunked = length is None
            if chunked:
                self.send_header("transfer-encoding", "chunked")
            else:
                self.send_header("content-length", length)
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                    if not chunk:
                        break
                    if chunked:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the caller went away mid-stream (a cancelled turn)

        def _error(self, status: int, message: str, facts: dict, started: float) -> None:
            data = json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self._log_line(status, facts, started, False)

        def _log_line(self, status: int, facts: dict, started: float, retried: bool) -> None:
            if self.command == "HEAD":
                return
            ms = int((time.monotonic() - started) * 1000)
            extra = "".join([f" model={facts['model']}" if facts.get("model") else "",
                             f" folded={facts['folded']}" if facts.get("folded") else "",
                             " retried" if retried else ""])
            _log.info("%s %s %s %dms%s", self.command, self.path.split("?", 1)[0], status, ms, extra)

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _relay

    return Handler


def _configure_logging() -> None:
    log_dir = Path.home() / "Library" / "Logs" / "neural-bridge"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "model-gateway.log", maxBytes=5 * 1024 * 1024,
                                  backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)


def serve(port: int, upstream: str, restart_label: str) -> ThreadingHTTPServer:
    gw_holder: dict = {}

    def upstream_up() -> bool:
        return gw_holder["gw"].upstream_up()

    recovery = Recovery(restart=launchctl_restart(restart_label), upstream_up=upstream_up)
    gw = Gateway(upstream, recovery)
    gw_holder["gw"] = gw
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(gw))
    server.daemon_threads = True
    return server


def main() -> int:
    _configure_logging()
    port = int(os.environ.get("NB_MODEL_GATEWAY_PORT") or DEFAULT_PORT)
    upstream = os.environ.get("NB_MODEL_GATEWAY_UPSTREAM") or DEFAULT_UPSTREAM
    label = os.environ.get("NB_MODEL_GATEWAY_RESTART") or DEFAULT_RESTART_LABEL
    server = serve(port, upstream, label)
    # A restart after a crash resolves nothing it did not open itself.
    Alerts().clear()
    _log.info("model gateway listening on 127.0.0.1:%d -> %s", port, upstream)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
