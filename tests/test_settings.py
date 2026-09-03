"""Tests for LLM settings: storage, precedence, and the dashboard page."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mserver.agent import llm  # noqa: E402
from mserver.agent import settings as settingsmod  # noqa: E402
from mserver.agent.core import Agent  # noqa: E402
from mserver.agent.ui import UI  # noqa: E402
from mserver.vos.kernel import VOS  # noqa: E402
from mserver.vos.shell import Shell  # noqa: E402
from mserver.web.server import Dashboard  # noqa: E402

ENV_VARS = ("MOPENAI_API_KEY", "OPENAI_API_KEY", "MOPENAI_BASE_URL",
            "MOPENAI_MODEL", "MOPENAI_TIMEOUT", "MOPENAI_RETRIES")


class EnvClean(unittest.TestCase):
    """Config reads the environment, so tests must control it."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in ENV_VARS}
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestStorage(EnvClean):
    def test_missing_file_is_empty(self):
        self.assertEqual(settingsmod.load(self.dir), {})

    def test_save_then_load(self):
        settingsmod.save(self.dir, {"api_key": "sk-abc12345", "model": "m"})
        got = settingsmod.load(self.dir)
        self.assertEqual(got["api_key"], "sk-abc12345")
        self.assertEqual(got["model"], "m")

    def test_file_is_chmod_600(self):
        settingsmod.save(self.dir, {"api_key": "sk-abc12345"})
        mode = settingsmod.config_path(self.dir).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_save_merges(self):
        settingsmod.save(self.dir, {"api_key": "sk-abc12345"})
        settingsmod.save(self.dir, {"model": "m2"})
        got = settingsmod.load(self.dir)
        self.assertEqual(got["api_key"], "sk-abc12345")
        self.assertEqual(got["model"], "m2")

    def test_none_removes_field(self):
        settingsmod.save(self.dir, {"api_key": "sk-abc12345"})
        settingsmod.save(self.dir, {"api_key": None})
        self.assertNotIn("api_key", settingsmod.load(self.dir))

    def test_unknown_fields_ignored(self):
        settingsmod.save(self.dir, {"api_key": "sk-abc12345", "evil": "x"})
        self.assertNotIn("evil", settingsmod.load(self.dir))

    def test_corrupt_file_gives_defaults(self):
        settingsmod.config_path(self.dir).write_text("{not json")
        self.assertEqual(settingsmod.load(self.dir), {})

    def test_non_dict_file_gives_defaults(self):
        settingsmod.config_path(self.dir).write_text("[1,2,3]")
        self.assertEqual(settingsmod.load(self.dir), {})


class TestMasking(unittest.TestCase):
    def test_masks_middle(self):
        m = settingsmod.mask("sk-proj-abcdefghijkl")
        self.assertTrue(m.startswith("sk-p"))
        self.assertTrue(m.endswith("ijkl"))
        self.assertNotIn("abcdefgh", m)

    def test_short_key_fully_masked(self):
        self.assertNotIn("a", settingsmod.mask("aaaa"))

    def test_empty(self):
        self.assertEqual(settingsmod.mask(""), "")


class TestValidation(unittest.TestCase):
    def test_accepts_plausible_key(self):
        self.assertEqual(settingsmod.validate({"api_key": "sk-abcdef123"})["api_key"],
                         "sk-abcdef123")

    def test_rejects_short_key(self):
        with self.assertRaises(settingsmod.SettingsError):
            settingsmod.validate({"api_key": "abc"})

    def test_rejects_key_with_space(self):
        with self.assertRaises(settingsmod.SettingsError):
            settingsmod.validate({"api_key": "sk-abc def12345"})

    def test_empty_key_clears(self):
        self.assertIsNone(settingsmod.validate({"api_key": ""})["api_key"])

    def test_accepts_https(self):
        self.assertEqual(
            settingsmod.validate({"base_url": "https://api.x.com/v1/"})["base_url"],
            "https://api.x.com/v1")

    def test_rejects_bare_host(self):
        with self.assertRaises(settingsmod.SettingsError):
            settingsmod.validate({"base_url": "api.openai.com"})

    def test_rejects_plain_http_remote(self):
        """Sending an API key over plain HTTP to a remote host leaks it."""
        with self.assertRaises(settingsmod.SettingsError):
            settingsmod.validate({"base_url": "http://evil.example.com/v1"})

    def test_allows_plain_http_localhost(self):
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:8080/v1"):
            self.assertEqual(settingsmod.validate({"base_url": url})["base_url"], url)

    def test_timeout_bounds(self):
        self.assertEqual(settingsmod.validate({"timeout": "30"})["timeout"], 30.0)
        for bad in ("0", "9999", "abc"):
            with self.assertRaises(settingsmod.SettingsError):
                settingsmod.validate({"timeout": bad})

    def test_retries_bounds(self):
        self.assertEqual(settingsmod.validate({"retries": "2"})["retries"], 2)
        for bad in ("-1", "99", "x"):
            with self.assertRaises(settingsmod.SettingsError):
                settingsmod.validate({"retries": bad})


class TestPrecedence(EnvClean):
    def test_defaults_when_nothing_set(self):
        cfg = llm.Config.from_env(self.dir)
        self.assertFalse(cfg.has_key)
        self.assertEqual(cfg.base_url, llm.DEFAULT_BASE_URL)

    def test_stored_file_is_used(self):
        settingsmod.save(self.dir, {"api_key": "sk-stored123", "model": "stored"})
        cfg = llm.Config.from_env(self.dir)
        self.assertEqual(cfg.api_key, "sk-stored123")
        self.assertEqual(cfg.model, "stored")

    def test_env_beats_stored_file(self):
        """An existing deployment's env vars must keep winning."""
        settingsmod.save(self.dir, {"api_key": "sk-stored123", "model": "stored"})
        os.environ["MOPENAI_API_KEY"] = "sk-fromenv123"
        os.environ["MOPENAI_MODEL"] = "envmodel"
        cfg = llm.Config.from_env(self.dir)
        self.assertEqual(cfg.api_key, "sk-fromenv123")
        self.assertEqual(cfg.model, "envmodel")

    def test_openai_api_key_fallback(self):
        os.environ["OPENAI_API_KEY"] = "sk-fallback123"
        self.assertEqual(llm.Config.from_env(self.dir).api_key, "sk-fallback123")

    def test_numeric_env_parsed(self):
        os.environ["MOPENAI_TIMEOUT"] = "42"
        os.environ["MOPENAI_RETRIES"] = "1"
        cfg = llm.Config.from_env(self.dir)
        self.assertEqual(cfg.timeout, 42.0)
        self.assertEqual(cfg.retries, 1)

    def test_numeric_stored_parsed(self):
        settingsmod.save(self.dir, {"timeout": 25, "retries": 5})
        cfg = llm.Config.from_env(self.dir)
        self.assertEqual(cfg.timeout, 25.0)
        self.assertEqual(cfg.retries, 5)

    def test_no_data_dir_still_works(self):
        os.environ["MOPENAI_API_KEY"] = "sk-envonly123"
        self.assertEqual(llm.Config.from_env().api_key, "sk-envonly123")

    def test_env_overrides_reported(self):
        os.environ["MOPENAI_MODEL"] = "m"
        self.assertEqual(settingsmod.env_overrides().get("model"), "MOPENAI_MODEL")


class _FakeLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        b = json.dumps({"choices": [{"message": {"content": "ok",
                                                 "role": "assistant"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


class DashCase(EnvClean):
    def setUp(self):
        super().setUp()
        self.llm = HTTPServer(("127.0.0.1", 0), _FakeLLM)
        threading.Thread(target=self.llm.serve_forever, daemon=True).start()
        self.llm_url = f"http://127.0.0.1:{self.llm.server_port}/v1"
        data = self.dir / "data"
        arts = data / "presented"
        arts.mkdir(parents=True)
        self.vos = VOS(data / "vos")
        self.shell = Shell(self.vos)
        self.agent = Agent(self.vos, self.shell, UI(), arts, data_dir=data)
        self.dash = Dashboard(self.vos, self.shell, arts, port=0,
                              agent=self.agent, data_dir=data)
        self.dash.start()
        self.base = f"http://127.0.0.1:{self.dash.actual_port}"
        self.data = data

    def tearDown(self):
        self.dash.stop()
        self.llm.shutdown()
        super().tearDown()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=8) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def post(self, obj):
        req = urllib.request.Request(
            self.base + "/api/settings", data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def tok(self):
        return self.dash.token


class TestSettingsPage(DashCase):
    def test_page_served(self):
        code, body = self.get("/settings")
        self.assertEqual(code, 200)
        self.assertIn("API key", body)

    def test_status_page_links_to_settings(self):
        self.assertIn("/settings", self.get("/")[1])

    def test_api_requires_token(self):
        self.assertEqual(self.get("/api/settings")[0], 401)

    def test_api_returns_state(self):
        code, body = self.get(f"/api/settings?token={self.tok()}")
        self.assertEqual(code, 200)
        j = json.loads(body)
        self.assertIn("model", j)
        self.assertFalse(j["has_key"])

    def test_post_requires_token(self):
        self.assertEqual(self.post({"action": "save", "api_key": "sk-x1234567"})[0],
                         401)

    def test_key_never_returned_in_full(self):
        self.post({"token": self.tok(), "action": "save",
                   "api_key": "sk-supersecret999"})
        body = self.get(f"/api/settings?token={self.tok()}")[1]
        self.assertNotIn("sk-supersecret999", body)
        self.assertIn("sk-s", body)


class TestSaveApplies(DashCase):
    def test_agent_starts_offline(self):
        self.assertTrue(self.agent.local)

    def test_save_brings_agent_online_without_restart(self):
        """The whole point of a settings page."""
        code, body = self.post({"token": self.tok(), "action": "save",
                                "api_key": "sk-test-123456",
                                "base_url": self.llm_url, "model": "demo"})
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["online"])
        self.assertFalse(self.agent.local)
        self.assertEqual(self.agent.cfg.model, "demo")

    def test_persists_for_next_run(self):
        self.post({"token": self.tok(), "action": "save",
                   "api_key": "sk-test-123456"})
        self.assertEqual(llm.Config.from_env(self.data).api_key, "sk-test-123456")

    def test_test_connection(self):
        self.post({"token": self.tok(), "action": "save",
                   "api_key": "sk-test-123456", "base_url": self.llm_url})
        body = self.post({"token": self.tok(), "action": "test"})[1]
        self.assertTrue(body["ok"], body)

    def test_test_without_key_fails(self):
        self.assertFalse(self.post({"token": self.tok(), "action": "test"})[1]["ok"])

    def test_clear_takes_agent_offline(self):
        self.post({"token": self.tok(), "action": "save",
                   "api_key": "sk-test-123456", "base_url": self.llm_url})
        self.assertFalse(self.agent.local)
        self.post({"token": self.tok(), "action": "clear"})
        self.assertTrue(self.agent.local)

    def test_invalid_key_rejected(self):
        code, body = self.post({"token": self.tok(), "action": "save",
                                "api_key": "no"})
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])

    def test_unknown_action(self):
        self.assertEqual(self.post({"token": self.tok(), "action": "boom"})[0], 400)

    def test_mode_label_updates(self):
        self.post({"token": self.tok(), "action": "save",
                   "api_key": "sk-test-123456", "base_url": self.llm_url,
                   "model": "demo"})
        self.assertIn("demo", self.dash.mode_label)

    def test_change_is_logged_without_the_value(self):
        self.post({"token": self.tok(), "action": "save",
                   "api_key": "sk-verysecret-9999"})
        log = self.vos.read("/var/log/auth.log")
        self.assertIn("settings changed", log)
        self.assertNotIn("sk-verysecret-9999", log)

    def test_failed_auth_logged(self):
        self.post({"action": "save", "api_key": "sk-x1234567", "token": "bad"})
        self.assertIn("failed settings auth", self.vos.read("/var/log/auth.log"))


class TestKeyIsNotReachableByAgent(DashCase):
    """The agent must not be able to read its own credentials."""

    def test_config_is_outside_the_rootfs(self):
        self.post({"token": self.tok(), "action": "save",
                   "api_key": "sk-test-123456"})
        cfg = settingsmod.config_path(self.data)
        self.assertTrue(cfg.exists())
        self.assertNotIn(str(self.vos.root), str(cfg))

    def test_agent_cannot_cat_it(self):
        self.post({"token": self.tok(), "action": "save",
                   "api_key": "sk-test-123456"})
        for path in ("/config.json", "/../config.json",
                     "/../../config.json", "~/config.json"):
            out, err, _ = self.shell.run(f"cat {path}")
            self.assertNotIn("sk-test-123456", out, path)

    def test_key_not_in_vos_search(self):
        self.post({"token": self.tok(), "action": "save",
                   "api_key": "sk-test-123456"})
        hits = self.vos.search("sk-test-123456", "/")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
