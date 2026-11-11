"""Unit tests for quick setup endpoints."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler

from popo.config import Config
from popo.server.app import ApiHandler
from popo.server.setup import _normalize_base


class TestNormalizeBase(unittest.TestCase):
    def test_does_not_duplicate_existing_prefix(self):
        self.assertEqual(_normalize_base("https://api.example.com/v1", "v1"),
                         "https://api.example.com/v1")
        self.assertEqual(_normalize_base("https://api.example.com", "v1"),
                         "https://api.example.com/v1")


class _MockUpstream(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "object": "list",
                "data": [
                    {"id": "glm-5.3", "object": "model"},
                    {"id": "glm-5.3-flash", "object": "model"},
                    {"id": "deepseek-v4.1-flash-1017", "object": "model"},
                ],
            }).encode())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _MockUpstream.requests.append((self.path, body))
        if self.path == "/v1/chat/completions":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "id": "upstream-1",
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
            }).encode())
            return
        self.send_response(404)
        self.end_headers()


class TestSetupEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="popo_test_setup_")
        # Start a fresh mock upstream.
        cls.upstream_port = 0
        cls.upstream = None
        for p in range(18212, 18250):
            try:
                cls.upstream = HTTPServer(("127.0.0.1", p), _MockUpstream)
                cls.upstream_port = p
                break
            except OSError:
                continue
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        time.sleep(0.1)
        cls.upstream_url = f"http://127.0.0.1:{cls.upstream_port}"

        cls.port = 0
        cls.server = None
        for p in range(18312, 18350):
            try:
                cls.server = HTTPServer(("127.0.0.1", p), ApiHandler)
                cls.port = p
                break
            except OSError:
                continue
        cls.config = Config(
            bind_host="127.0.0.1",
            bind_port=cls.port,
            upstream_host="",
            upstream_key="",
            data_dir=cls.tmp,
            require_client_key=False,
            ephemeral=True,
            # The mock upstream runs on 127.0.0.1, which the egress policy
            # blocks by default (correctly). A local mock must opt in.
            allow_private_upstream=True,
        )
        ApiHandler.config = cls.config
        ApiHandler.records = None
        ApiHandler.keystore = None
        ApiHandler.admin_hashes = set()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        req_headers = {"Accept": "application/json"}
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req_headers.update(headers or {})
        conn.request(method, path, encoded, req_headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            parsed = data
        return r.status, parsed

    def test_status_not_configured(self):
        # Remove any leftover state from previous test runs inside this temp dir.
        for fn in ["popo.json", "admin_key.txt"]:
            p = os.path.join(self.tmp, fn)
            if os.path.exists(p):
                os.remove(p)
        ApiHandler.config = Config(
            bind_host="127.0.0.1",
            bind_port=self.port,
            upstream_host="",
            upstream_key="",
            data_dir=self.tmp,
            require_client_key=False,
            ephemeral=True,
            allow_private_upstream=True,
        )
        status, body = self._request("GET", "/setup/status")
        self.assertEqual(status, 200)
        self.assertFalse(body["configured"])

    def test_test_upstream_succeeds(self):
        status, body = self._request("POST", "/setup/test", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("glm-5.3", body["models"])

    def test_test_upstream_rejects_unreachable(self):
        status, body = self._request("POST", "/setup/test", {
            "upstream_host": "http://127.0.0.1:1",
            "upstream_key": "mock-key",
        })
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_quick_setup(self):
        status, body = self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3"],
        })
        self.assertEqual(status, 200)
        self.assertIn("key", body)
        self.assertIn("glm-5.3", body["published"])
        self.assertEqual(ApiHandler.config.upstream_host, self.upstream_url)
        self.assertIn("glm-5.3", ApiHandler.config.published_models)

    def test_quick_setup_can_issue_internal_key(self):
        status, body = self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3"],
            "key_scope": "internal",
        })
        self.assertEqual(status, 200, body)
        self.assertFalse(body["allow_public"])
        record = ApiHandler.keystore.verify(body["key"])
        self.assertIsNotNone(record)
        self.assertFalse(record.allow_public)

    def test_quick_setup_rejects_unknown_key_scope(self):
        status, body = self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3"],
            "key_scope": "world",
        })
        self.assertEqual(status, 400, body)
        self.assertIn("key_scope", body["error"]["message"])

    def test_quick_setup_persists(self):
        status, body = self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3"],
        })
        self.assertEqual(status, 200)
        path = os.path.join(self.tmp, "popo.json")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["upstream_key"], "mock-key")
        self.assertIn("glm-5.3", state["published_models"])

    def test_quick_setup_persists_per_model_profiles(self):
        status, body = self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3"],
            "profiles": {"glm-5.3": "off"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["profiles"], {"glm-5.3": "off"})
        self.assertEqual(ApiHandler.config.model_profiles, {"glm-5.3": "off"})
        with open(os.path.join(self.tmp, "popo.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["model_profiles"], {"glm-5.3": "off"})

    def test_console_profile_change_updates_the_live_injected_prompt(self):
        _MockUpstream.requests.clear()
        status, setup = self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3"],
            "profiles": {"glm-5.3": "native_glm"},
        })
        self.assertEqual(status, 200, setup)

        status, response = self._request("POST", "/v1/chat/completions", {
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hello"}],
        })
        self.assertEqual(status, 200, response)
        sent = json.loads(_MockUpstream.requests[-1][1])
        system = next(message["content"] for message in sent["messages"]
                      if message["role"] == "system")
        # The prompt is proprietary and opaque. Verify the selected family is
        # active structurally without inspecting or printing its text.
        self.assertTrue(system)
        self.assertEqual(ApiHandler.config.model_profiles["glm-5.3"], "native_glm")

        import hashlib
        quick_key = setup["key"]
        quick_hash = hashlib.sha256(quick_key.encode()).hexdigest()
        ApiHandler.admin_hashes.add(quick_hash)
        status, updated = self._request("POST", "/setup/models", {
            "models": ["glm-5.3"], "profiles": {"glm-5.3": "off"},
        }, headers={"Authorization": f"Bearer {quick_key}"})
        self.assertEqual(status, 200, updated)
        self.assertEqual(ApiHandler.config.model_profiles["glm-5.3"], "off")

        status, response = self._request("POST", "/v1/chat/completions", {
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hello"}],
        })
        self.assertEqual(status, 200, response)
        sent = json.loads(_MockUpstream.requests[-1][1])
        self.assertFalse(any(message["role"] == "system" for message in sent["messages"]))

        status, invalid = self._request("POST", "/setup/models", {
            "models": ["glm-5.3"], "profiles": {"glm-5.3": "unknown-profile"},
        }, headers={"Authorization": f"Bearer {quick_key}"})
        self.assertEqual(status, 400, invalid)
        self.assertEqual(ApiHandler.config.model_profiles["glm-5.3"], "off")

        status, reset = self._request("POST", "/setup/models", {
            "models": ["glm-5.3"], "profiles": {},
        }, headers={"Authorization": f"Bearer {quick_key}"})
        self.assertEqual(status, 200, reset)
        self.assertEqual(reset["profiles"], {})
        self.assertEqual(ApiHandler.config.model_profiles, {})
        status, response = self._request("POST", "/v1/chat/completions", {
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hello"}],
        })
        self.assertEqual(status, 200, response)
        sent = json.loads(_MockUpstream.requests[-1][1])
        system = next(message["content"] for message in sent["messages"]
                      if message["role"] == "system")
        self.assertTrue(system)
        self.assertEqual(ApiHandler.config.model_profiles, {})
        ApiHandler.admin_hashes.discard(quick_hash)

    def test_quick_setup_rejects_unknown_profile(self):
        status, body = self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3"],
            "profiles": {"glm-5.3": "not-a-profile"},
        })
        self.assertEqual(status, 400)
        self.assertIn("unknown armor profiles", body["error"]["message"])

    def test_models_filtered_after_publish(self):
        self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3"],
        })
        status, body = self._request("GET", "/v1/models")
        self.assertEqual(status, 200)
        ids = [m["id"] for m in body["data"]]
        self.assertEqual(ids, ["glm-5.3"])
        status, body = self._request("POST", "/v1/chat/completions", {
            "model": "glm-5.3-flash",
            "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 400)
        detail = body["error"].get("detail") or body["error"].get("message", "")
        self.assertIn("not published", detail)

    def test_publish_models_endpoint(self):
        self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3", "glm-5.3-flash"],
        })
        # Add the quick key as admin for subsequent publish request.
        import hashlib
        quick = self._quick_key()
        ApiHandler.admin_hashes.add(hashlib.sha256(quick.encode()).hexdigest())
        status, body = self._request("POST", "/setup/models", {
            "models": ["deepseek-v4.1-flash-1017"],
        }, headers={"Authorization": f"Bearer {quick}"})
        self.assertEqual(status, 200)
        self.assertIn("deepseek-v4.1-flash-1017", body["published"])

    def _quick_key(self):
        # Extract the key returned in the last quick setup.
        status, body = self._request("POST", "/setup/quick", {
            "upstream_host": self.upstream_url,
            "upstream_key": "mock-key",
            "models": ["glm-5.3", "glm-5.3-flash"],
        })
        return body["key"]

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        if cls.upstream:
            cls.upstream.shutdown()
            cls.upstream.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
