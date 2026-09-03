"""Tests for the live dashboard: SSE event bus, web terminal, status JSON."""
from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mserver.vos.kernel import VOS  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402
from mserver.web.events import (  # noqa: E402
    MAX_SUBSCRIBERS,
    EventBus,
    LiveEvents,
    sse_format,
)
from mserver.web.server import Dashboard  # noqa: E402


class TestEventBus(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_subscribe_returns_queue(self):
        self.assertIsInstance(self.bus.subscribe(), queue.Queue)

    def test_published_event_reaches_subscriber(self):
        q = self.bus.subscribe()
        self.bus.publish("tool", {"name": "vos_run"})
        ev = q.get_nowait()
        self.assertEqual(ev["kind"], "tool")
        self.assertEqual(ev["data"]["name"], "vos_run")

    def test_all_subscribers_receive(self):
        qs = [self.bus.subscribe() for _ in range(3)]
        self.bus.publish("shell", {"command": "ls"})
        for q in qs:
            self.assertEqual(q.get_nowait()["data"]["command"], "ls")

    def test_sequence_increments(self):
        q = self.bus.subscribe()
        self.bus.publish("a")
        self.bus.publish("b")
        self.assertEqual(q.get_nowait()["seq"] + 1, q.get_nowait()["seq"])

    def test_unsubscribe_stops_delivery(self):
        q = self.bus.subscribe()
        self.bus.unsubscribe(q)
        self.bus.publish("tool")
        self.assertTrue(q.empty())

    def test_subscriber_cap(self):
        granted = [self.bus.subscribe() for _ in range(MAX_SUBSCRIBERS + 3)]
        self.assertEqual(sum(1 for g in granted if g is not None),
                         MAX_SUBSCRIBERS)
        self.assertIsNone(granted[-1])

    def test_slow_subscriber_drops_oldest_not_newest(self):
        """A throttled phone tab must not grow the queue without bound."""
        q = self.bus.subscribe()
        for i in range(500):
            self.bus.publish("tool", {"n": i})
        self.assertLessEqual(q.qsize(), 200)
        latest = None
        while not q.empty():
            latest = q.get_nowait()
        self.assertEqual(latest["data"]["n"], 499)

    def test_publish_with_no_subscribers_is_safe(self):
        self.assertEqual(self.bus.publish("tool")["kind"], "tool")

    def test_count(self):
        self.assertEqual(self.bus.subscriber_count(), 0)
        self.bus.subscribe()
        self.assertEqual(self.bus.subscriber_count(), 1)

    def test_thread_safe_publish(self):
        q = self.bus.subscribe()
        def spam():
            for _ in range(50):
                self.bus.publish("tool", {})
        ts = [threading.Thread(target=spam) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertGreater(q.qsize(), 0)


class TestSseFormat(unittest.TestCase):
    def test_has_event_and_data(self):
        out = sse_format({"seq": 3, "kind": "tool", "data": {"a": 1}}).decode()
        self.assertIn("id: 3\n", out)
        self.assertIn("event: tool\n", out)
        self.assertIn('data: {"a": 1}\n', out)

    def test_ends_with_blank_line(self):
        self.assertTrue(sse_format({"kind": "x", "data": {}}).endswith(b"\n\n"))

    def test_handles_unserialisable(self):
        self.assertIn(b"data:", sse_format({"kind": "x", "data": {"o": object()}}))


class TestLiveEvents(unittest.TestCase):
    def test_behaves_like_a_list(self):
        ev = LiveEvents(EventBus())
        ev.append({"name": "a"})
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["name"], "a")

    def test_append_publishes(self):
        bus = EventBus()
        q = bus.subscribe()
        LiveEvents(bus).append({"name": "vos_run"})
        self.assertEqual(q.get_nowait()["data"]["name"], "vos_run")

    def test_publish_failure_does_not_break_append(self):
        class Broken(EventBus):
            def publish(self, *a, **k):
                raise RuntimeError("boom")
        ev = LiveEvents(Broken())
        ev.append({"name": "x"})
        self.assertEqual(len(ev), 1)


def new_dash():
    d = tempfile.mkdtemp()
    vos = VOS(os.path.join(d, ".mserver"))
    shell = Shell(vos)
    arts = Path(d) / "artifacts"
    arts.mkdir()
    dash = Dashboard(vos, shell, arts, port=0)
    dash.start()
    return dash, vos, shell


class HttpCase(unittest.TestCase):
    def setUp(self):
        self.dash, self.vos, self.shell = new_dash()
        self.base = f"http://127.0.0.1:{self.dash.actual_port}"

    def tearDown(self):
        self.dash.stop()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post(self, path, obj):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestRoutes(HttpCase):
    def test_status_page(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("MServerOS", body)

    def test_status_page_has_no_meta_refresh(self):
        """Replaced by SSE; a full reload threw away scroll position."""
        self.assertNotIn("http-equiv=\"refresh\"", self.get("/")[1])

    def test_terminal_page(self):
        code, body = self.get("/term")
        self.assertEqual(code, 200)
        self.assertIn("web terminal", body)

    def test_chat_page_still_works(self):
        self.assertEqual(self.get("/chat")[0], 200)

    def test_unknown_route_404(self):
        self.assertEqual(self.get("/nope")[0], 404)

    def test_api_status_shape(self):
        code, body = self.get("/api/status")
        self.assertEqual(code, 200)
        j = json.loads(body)
        for key in ("os", "hostname", "uptime", "user", "disk",
                    "processes", "services", "packages", "recent"):
            self.assertIn(key, j)

    def test_api_status_reflects_user(self):
        self.shell.run("su agent")
        self.assertEqual(json.loads(self.get("/api/status")[1])["user"], "agent")

    def test_api_status_reflects_services(self):
        self.shell.run("pkg install nginx")
        self.shell.run("service nginx start")
        j = json.loads(self.get("/api/status")[1])
        self.assertEqual(j["services"].get("nginx"), "running")
        self.assertTrue(any(l["port"] == 80 for l in j["listeners"]))


class TestWebTerminal(HttpCase):
    def token(self):
        return self.dash.token

    def test_requires_token(self):
        code, body = self.post("/term", {"command": "whoami"})
        self.assertEqual(code, 401)
        self.assertFalse(body["ok"])

    def test_rejects_wrong_token(self):
        self.assertEqual(self.post("/term", {"command": "whoami",
                                             "token": "wrong"})[0], 401)

    def test_runs_command(self):
        code, body = self.post("/term", {"command": "whoami",
                                         "token": self.token()})
        self.assertEqual(code, 200)
        self.assertEqual(body["out"].strip(), "root")

    def test_reports_exit_code(self):
        body = self.post("/term", {"command": "nosuchcommand",
                                   "token": self.token()})[1]
        self.assertNotEqual(body["code"], 0)
        self.assertIn("not found", body["err"])

    def test_empty_command_rejected(self):
        self.assertEqual(self.post("/term", {"command": "  ",
                                             "token": self.token()})[0], 400)

    def test_bad_json_rejected(self):
        req = urllib.request.Request(
            self.base + "/term", data=b"{not json",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected an error")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_cwd_persists_between_commands(self):
        self.post("/term", {"command": "cd /etc", "token": self.token()})
        body = self.post("/term", {"command": "pwd", "token": self.token()})[1]
        self.assertEqual(body["cwd"], "/etc")
        self.assertIn("/etc", body["out"])

    def test_shares_session_with_repl_shell(self):
        self.post("/term", {"command": "export FOO=bar", "token": self.token()})
        self.assertEqual(self.shell.env.get("FOO"), "bar")

    def test_sandbox_still_enforced(self):
        body = self.post("/term", {"command": "cat ../../../../etc/passwd",
                                   "token": self.token()})[1]
        self.assertIn("escapes the virtual OS", body["err"])
        self.assertNotIn("root:x:", body["out"])

    def test_permissions_still_enforced(self):
        self.post("/term", {"command": "su agent", "token": self.token()})
        body = self.post("/term", {"command": "touch /etc/evil",
                                   "token": self.token()})[1]
        self.assertIn("Permission denied", body["err"])

    def test_reports_current_user(self):
        self.post("/term", {"command": "su agent", "token": self.token()})
        body = self.post("/term", {"command": "whoami",
                                   "token": self.token()})[1]
        self.assertEqual(body["user"], "agent")

    def test_commands_are_logged(self):
        self.post("/term", {"command": "echo audited", "token": self.token()})
        self.assertIn("echo audited", self.vos.read("/var/log/syslog"))

    def test_failed_auth_is_logged(self):
        self.post("/term", {"command": "whoami", "token": "bad"})
        self.assertIn("failed terminal auth", self.vos.read("/var/log/auth.log"))

    def test_rate_limited_eventually(self):
        tok = self.token()
        codes = [self.post("/term", {"command": "true", "token": tok})[0]
                 for _ in range(70)]
        self.assertIn(429, codes)


class TestStreaming(HttpCase):
    def read_events(self, n=4, timeout=8):
        out = []
        try:
            with urllib.request.urlopen(self.base + "/events", timeout=timeout) as r:
                for _ in range(n):
                    line = r.readline()
                    if not line:
                        break
                    out.append(line.decode().strip())
        except Exception:
            pass
        return out

    def test_stream_sends_hello(self):
        lines = self.read_events(3)
        self.assertTrue(any("hello" in x for x in lines))

    def test_shell_command_is_streamed(self):
        got = []
        t = threading.Thread(target=lambda: got.extend(self.read_events(8)))
        t.start()
        time.sleep(0.5)
        self.post("/term", {"command": "echo streamed", "token": self.dash.token})
        t.join(timeout=8)
        self.assertTrue(any("shell" in x for x in got), got)

    def test_subscriber_released_after_disconnect(self):
        t = threading.Thread(target=lambda: self.read_events(1, timeout=2))
        t.start()
        t.join(timeout=6)
        for i in range(3):
            self.post("/term", {"command": f"echo {i}", "token": self.dash.token})
            time.sleep(0.2)
        self.assertEqual(self.dash.bus.subscriber_count(), 0)

    def test_run_command_publishes(self):
        q = self.dash.bus.subscribe()
        self.dash.run_command("echo hi", "test")
        kinds = []
        while not q.empty():
            kinds.append(q.get_nowait()["kind"])
        self.assertIn("shell", kinds)
        self.assertIn("shell-result", kinds)


class TestNoHostExecution(unittest.TestCase):
    """The web terminal must never be a shell on the real machine."""

    def test_no_subprocess_import_in_web_package(self):
        import mserver.web.server as srv
        src = Path(srv.__file__).read_text()
        for bad in ("subprocess", "os.system", "os.popen", "eval(", "exec("):
            self.assertNotIn(bad, src, f"{bad} must not appear in the web server")

    def test_run_command_uses_the_vos_shell(self):
        dash, vos, shell = new_dash()
        try:
            out, _, _ = dash.run_command("pwd", "test")
            self.assertIn("/", out)
            self.assertFalse(Path("/etc/passwd").read_text() in out)
        finally:
            dash.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
