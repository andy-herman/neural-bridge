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
        self.routes: dict[str, tuple[int, bytes]] = {}
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
                if self.path in outer.routes:
                    step = outer.routes[self.path]
                else:
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


class TestGatewayProbes(GatewayCase):
    def test_token_expiry_is_read_without_keeping_the_token(self):
        self.upstream.routes["/token"] = (200, json.dumps({"token": "tid=abc;exp=1790000000;sku=x"}).encode())
        self.assertEqual(self.gw.token_expiry(), 1790000000.0)
        self.assertFalse(any("tid=abc" in str(v) for v in vars(self.gw).values()))

    def test_token_expiry_is_none_when_the_proxy_does_not_say(self):
        self.upstream.routes["/token"] = (404, b"nope")
        self.assertIsNone(self.gw.token_expiry())

    def test_probe_is_one_tiny_base_model_call(self):
        self.upstream.script = [(403, b"forbidden")]
        self.assertEqual(self.gw.probe(), 403)
        method, path, body = self.upstream.requests[-1]
        sent = json.loads(body)
        self.assertEqual((method, path, sent["model"], sent["max_tokens"]),
                         ("POST", "/v1/chat/completions", mg.PROBE_MODEL, 1))

    def test_in_flight_counts_messages_requests_while_they_run(self):
        seen = []
        real_open = self.gw.open_upstream

        def spy(*args, **kwargs):
            seen.append(self.gw.in_flight)
            return real_open(*args, **kwargs)
        self.gw.open_upstream = spy
        self.call()
        self.call("GET", "/v1/models")
        self.assertEqual(seen, [1, 0], "counted while relaying /v1/messages only")
        self.assertEqual(self.gw.in_flight, 0, "and released when done")

    def test_health_carries_the_watchdog(self):
        self.gw.watchdog = mg.Watchdog(self.gw, log_path=Path(self._tmp.name) / "none.log")
        self.gw.watchdog.last_result = "ok"
        _, _, body = self.call("GET", "/_gateway/health")
        self.assertEqual(json.loads(body)["watchdog"]["last_result"], "ok")


class FakeGw:
    """What the watchdog needs from a gateway, scripted."""

    def __init__(self, test):
        self.up = True
        self.exp: float | None = 10_000.0
        self.probes: list[int | None] = []
        self.probed = 0
        self.recovered = 0
        self.alerts = mg.Alerts()
        test_self = self

        class R:
            def recover(self_inner):
                test_self.recovered += 1
                return True
        self.recovery = R()
        self.in_flight = 0

    def upstream_up(self):
        return self.up

    def token_expiry(self):
        return self.exp

    def probe(self):
        self.probed += 1
        return self.probes.pop(0) if self.probes else 200


class TestWatchdog(unittest.TestCase):
    NOW = 5_000.0

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"NB_REVIEW_QUEUE_DB": str(Path(self._tmp.name) / "q.db")})
        self._env.start()
        self.log = Path(self._tmp.name) / "copilot-api.log"
        self.log.write_text("--> POST /v1/messages \x1b[33m403\x1b[0m 5ms\n")  # history, ignored
        self.gw = FakeGw(self)
        self.now = [self.NOW]
        self.wd = mg.Watchdog(self.gw, log_path=self.log, clock=lambda: self.now[0])
        self.wd.new_refusals()  # as serve() does: start at the log's current end

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def refuse(self, n=1):
        with self.log.open("a") as f:
            for _ in range(n):
                f.write("--> POST /v1/messages \x1b[33m403\x1b[0m 106ms\n")

    def open_alerts(self):
        from scripts.review_queue import store as qs
        return sorted(i.key for i in qs.Store().items(states=qs.WAITING) if i.source == mg.ALERT_SOURCE)

    def test_healthy_proxy_is_left_alone(self):
        self.assertEqual(self.wd.check(), "ok")
        self.assertEqual((self.gw.recovered, self.gw.probed), (0, 0))
        self.assertEqual(self.wd.token_expires_in, 5000)

    def test_a_token_about_to_expire_is_restarted_before_anything_fails(self):
        self.gw.exp = self.NOW + 120
        result = self.wd.check()
        self.assertTrue(result.startswith("restarted: its token expires in 2 min"), result)
        self.assertEqual(self.gw.recovered, 1)

    def test_an_expiring_token_waits_for_turns_in_progress(self):
        # A restart kills requests mid-stream; while the token is still valid
        # the watchdog lets them finish and tries again at the next check.
        self.gw.exp = self.NOW + 240
        self.gw.in_flight = 2
        self.assertIn("waiting for 2 request(s) in flight", self.wd.check())
        self.assertEqual(self.gw.recovered, 0)
        self.gw.in_flight = 0
        self.wd.check()
        self.assertEqual(self.gw.recovered, 1)

    def test_an_expired_token_restarts_even_with_turns_in_progress(self):
        self.gw.exp = self.NOW - 5
        self.gw.in_flight = 3
        self.wd.check()
        self.assertEqual(self.gw.recovered, 1)

    def test_an_expired_token_is_restarted(self):
        self.gw.exp = self.NOW - 600
        self.assertIn("expired 10 min ago", self.wd.check())
        self.assertEqual(self.gw.recovered, 1)

    def test_logged_refusals_confirmed_by_a_probe_restart_the_proxy(self):
        # Synapse and Gauntlet call the proxy directly; the log is how the
        # watchdog sees their 403s.
        self.refuse(3)
        self.gw.probes = [403, 200]
        result = self.wd.check()
        self.assertTrue(result.startswith("restarted: 3 request(s) refused 403"), result)
        self.assertEqual((self.gw.recovered, self.gw.probed), (1, 2))

    def test_logged_refusals_the_probe_does_not_confirm_change_nothing(self):
        self.refuse()
        self.gw.probes = [200]
        self.assertIn("not stale", self.wd.check())
        self.assertEqual(self.gw.recovered, 0)

    def test_old_refusals_in_the_log_are_history(self):
        self.assertEqual(self.wd.check(), "ok")
        self.assertEqual(self.gw.probed, 0)

    def test_a_restart_that_does_not_help_raises_the_alert(self):
        self.gw.exp = self.NOW - 1
        self.gw.probes = [403]
        self.assertIn("restart did not help", self.wd.check())
        self.assertEqual(self.open_alerts(), [mg.ALERT_KEY])
        self.gw.exp = self.NOW + 10_000
        self.refuse()
        self.gw.probes = [200]
        self.wd.check()
        self.gw.exp = self.NOW - 1
        self.gw.probes = [200]
        self.wd.check()
        self.assertEqual(self.open_alerts(), [], "a restart that works clears it")

    def test_a_proxy_down_past_the_grace_period_raises_then_clears(self):
        self.gw.up = False
        self.wd.check()
        self.assertEqual(self.open_alerts(), [], "launchd gets its chance first")
        self.now[0] += mg.DOWN_ALERT_AFTER
        self.wd.check()
        self.assertEqual(self.open_alerts(), [mg.DOWN_KEY])
        self.gw.up = True
        self.wd.check()
        self.assertEqual(self.open_alerts(), [])

    def test_a_rotated_log_is_read_from_the_start(self):
        self.log.unlink()  # rotated: a new file, even one of the same size
        self.log.write_text("--> POST /v1/messages \x1b[33m403\x1b[0m 1ms\n")
        self.assertEqual(self.wd.new_refusals(), 1)

    def test_a_replaced_log_with_a_reused_inode_is_still_caught(self):
        # CI on Linux, 2026-09-30: a file deleted and recreated at once got the
        # same inode number back, so an inode-only check read nothing.
        new_text = "--> POST /v1/messages \x1b[33m403\x1b[0m 1ms\n"
        with mock.patch.object(mg.Path, "stat") as fake_stat:
            fake_stat.return_value = mock.Mock(st_size=len(new_text), st_ino=self.wd.inode)
            self.log.write_text(new_text)
            self.assertEqual(self.wd.new_refusals(), 1)

    def test_appended_lines_are_not_mistaken_for_a_new_file(self):
        self.refuse(2)
        self.assertEqual(self.wd.new_refusals(), 2)
        self.refuse(1)
        self.assertEqual(self.wd.new_refusals(), 1)

    def test_a_truncated_log_is_read_from_the_start(self):
        # Same file, now shorter than where the last read stopped.
        self.log.write_text("--> GET / \x1b[33m403\x1b[0m 1ms\n")
        self.assertEqual(self.wd.new_refusals(), 1)

    def test_startup_clears_what_an_earlier_process_raised(self):
        mg.Alerts().raise_("left over", key=mg.DOWN_KEY)
        self.assertEqual(self.open_alerts(), [mg.DOWN_KEY])
        mg.Alerts().clear_all()
        self.assertEqual(self.open_alerts(), [])


if __name__ == "__main__":
    unittest.main()
