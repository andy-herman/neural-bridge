"""The model gateway: the Claude 5 request fix, streaming, and recovery from a
stale proxy token. Runs against a stand-in upstream on a local port; the
proxy restart is stubbed, so nothing touches launchd or the real proxy."""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts import model_gateway as mg  # noqa: E402

CLAUDE_5_BODY = {
    "model": "claude-sonnet-5",
    "system": [{"type": "text", "text": "base system"}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "system", "content": "a reminder Claude Code appends"},
    ],
    "max_tokens": 8,
}


class TestFold(unittest.TestCase):
    def test_trailing_system_message_moves_into_the_system_prompt(self):
        body = json.loads(json.dumps(CLAUDE_5_BODY))
        self.assertEqual(mg.fold_system_messages(body), 1)
        self.assertEqual([m["role"] for m in body["messages"]], ["user"])
        self.assertEqual([b["text"] for b in body["system"]], ["base system", "a reminder Claude Code appends"])

    def test_string_system_and_block_content_are_both_handled(self):
        body = {"system": "base", "messages": [
            {"role": "user", "content": "q"},
            {"role": "system", "content": [{"type": "text", "text": "one"}, {"type": "image", "source": {}}]},
            {"role": "assistant", "content": "a"},
            {"role": "system", "content": "two"},
            {"role": "user", "content": "q2"}]}
        self.assertEqual(mg.fold_system_messages(body), 2)
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "assistant", "user"])
        self.assertEqual([b["text"] for b in body["system"]], ["base", "one", "two"])

    def test_no_system_messages_changes_nothing(self):
        body = {"system": "base", "messages": [{"role": "user", "content": "q"}]}
        before = json.dumps(body)
        self.assertEqual(mg.fold_system_messages(body), 0)
        self.assertEqual(json.dumps(body), before)

    def test_missing_system_prompt_is_created(self):
        body = {"messages": [{"role": "user", "content": "q"}, {"role": "system", "content": "r"}]}
        mg.fold_system_messages(body)
        self.assertEqual(body["system"], [{"type": "text", "text": "r"}])

    def test_rewrite_only_touches_messages_posts(self):
        raw = json.dumps(CLAUDE_5_BODY).encode()
        self.assertEqual(mg.rewrite("GET", "/v1/messages", raw), (raw, {}))
        self.assertEqual(mg.rewrite("POST", "/v1/models", raw), (raw, {}))
        self.assertEqual(mg.rewrite("POST", "/v1/messages", b"not json"), (b"not json", {}))
        out, facts = mg.rewrite("POST", "/v1/messages/count_tokens", raw)
        self.assertEqual(facts, {"model": "claude-sonnet-5", "folded": 1})
        self.assertNotIn('"role": "system"', out.decode())


class Upstream:
    """A stand-in copilot-api. `script` is a list of responses served in order;
    the last one repeats. A response is (status, body) or ("stream", [chunks])."""

    def __init__(self):
        self.script: list = [(200, b'{"ok": true}')]
        self.requests: list[tuple[str, str, bytes]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _serve(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append((self.command, self.path, body))
                step = outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]
                if step[0] == "stream":
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for chunk in step[1]:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    return
                status, payload = step
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_HEAD = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GatewayCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"NB_REVIEW_QUEUE_DB": str(Path(self._tmp.name) / "q.db")})
        self._env.start()
        self.upstream = Upstream()
        self.restarts = 0
        self.now = [1000.0]

        def restart():
            self.restarts += 1
            return True

        self.recovery = mg.Recovery(restart=restart, upstream_up=lambda: True, wait=1,
                                    clock=lambda: self.now[0], sleep=lambda s: None)
        self.gw = mg.Gateway(f"http://127.0.0.1:{self.upstream.port}", self.recovery)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mg.make_handler(self.gw))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()
        self._env.stop()
        self._tmp.cleanup()

    def call(self, method="POST", path="/v1/messages", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body if body is not None else CLAUDE_5_BODY).encode() if method == "POST" else None
        conn.request(method, path, body=data, headers={"content-type": "application/json",
                                                       "x-api-key": "copilot-proxy"})
        resp = conn.getresponse()
        out = (resp.status, dict(resp.getheaders()), resp.read())
        conn.close()
        return out

    def open_alerts(self):
        from scripts.review_queue import store as qs
        return [i for i in qs.Store().items(states=qs.WAITING) if i.source == mg.ALERT_SOURCE]


class TestRelay(GatewayCase):
    def test_claude_5_request_reaches_upstream_folded(self):
        status, _, body = self.call()
        self.assertEqual((status, body), (200, b'{"ok": true}'))
        sent = json.loads(self.upstream.requests[-1][2])
        self.assertEqual([m["role"] for m in sent["messages"]], ["user"])
        self.assertIn("a reminder Claude Code appends", [b["text"] for b in sent["system"]])
        self.assertEqual(self.gw.stats["folded"], 1)

    def test_streamed_responses_arrive_whole(self):
        chunks = [b"event: message_start\ndata: {}\n\n", b"event: content_block_delta\ndata: {}\n\n",
                  b"event: message_stop\ndata: {}\n\n"]
        self.upstream.script = [("stream", chunks)]
        status, headers, body = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "text/event-stream")
        self.assertEqual(body, b"".join(chunks))

    def test_other_requests_pass_through(self):
        self.upstream.script = [(200, b'{"data": []}')]
        status, _, body = self.call("GET", "/v1/models")
        self.assertEqual((status, body), (200, b'{"data": []}'))
        self.assertEqual(self.upstream.requests[-1][:2], ("GET", "/v1/models"))

    def test_health_reports_counts(self):
        self.call()
        status, _, body = self.call("GET", "/_gateway/health")
        health = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual((health["requests"], health["folded"], health["restarts"]), (1, 1, 0))

    def test_unreachable_upstream_is_a_clear_502(self):
        self.upstream.close()
        status, _, body = self.call()
        self.assertEqual(status, 502)
        self.assertIn("proxy is unreachable", json.loads(body)["error"]["message"])


class TestStaleToken(GatewayCase):
    def test_a_403_restarts_the_proxy_and_the_retry_succeeds(self):
        # 2026-09-30: copilot-api held a dead token and answered every model 403.
        self.upstream.script = [(403, b'{"error":{"message":"forbidden\\n"}}'), (200, b'{"ok": true}')]
        status, _, body = self.call()
        self.assertEqual((status, body), (200, b'{"ok": true}'))
        self.assertEqual(self.restarts, 1)
        self.assertEqual(self.gw.stats["retried"], 1)
        self.assertEqual(self.open_alerts(), [])

    def test_a_restart_that_does_not_help_raises_one_alert_then_success_clears_it(self):
        self.upstream.script = [(403, b'{"error":{"message":"forbidden\\n"}}')]
        status, _, _ = self.call()
        self.assertEqual(status, 403)
        self.assertEqual(self.restarts, 1)
        self.assertEqual(len(self.open_alerts()), 1)
        self.call()  # still refused, still one alert, no second restart inside the interval
        self.assertEqual(self.restarts, 1)
        self.assertEqual(len(self.open_alerts()), 1)
        self.upstream.script = [(200, b'{"ok": true}')]
        self.assertEqual(self.call()[0], 200)
        self.assertEqual(self.open_alerts(), [])

    def test_restarts_are_rate_limited(self):
        self.upstream.script = [(403, b"x"), (200, b"ok"), (403, b"x"), (200, b"ok")]
        self.call()
        self.now[0] += 60
        self.call()
        self.assertEqual(self.restarts, 1, "a second 403 within the interval retries without restarting")
        self.now[0] += mg.RESTART_INTERVAL
        self.upstream.script = [(403, b"x"), (200, b"ok")]
        self.call()
        self.assertEqual(self.restarts, 2)

    def test_non_messages_403_is_passed_through_untouched(self):
        self.upstream.script = [(403, b"nope")]
        status, _, body = self.call("GET", "/v1/models")
        self.assertEqual((status, body, self.restarts), (403, b"nope", 0))


if __name__ == "__main__":
    unittest.main()
