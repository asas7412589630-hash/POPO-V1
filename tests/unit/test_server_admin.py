"""Unit tests for the HTTP server admin endpoints."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import HTTPServer

from popo.config import Config
from popo.server.app import ApiHandler
from popo.storage.ephemeral import EphemeralGate
from popo.storage.records import RecordStore


class TestServerAdmin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="popo_test_server_admin_")
        cls.admin_key = "triad-admin-test-000000000000000000000000000"
        os.environ["POPO_ADMIN_KEY"] = cls.admin_key
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
        )
        ApiHandler.config = cls.config
        # Reset class-level stores to exercise bootstrap
        ApiHandler.records = None
        ApiHandler.keystore = None
        ApiHandler.admin_hashes = set()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def _request(self, method, path, body=None, headers=None):
        import socket
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        req_headers = headers or {}
        req_body = json.dumps(body).encode("utf-8") if body is not None else b""
        if method == "GET":
            req_body = None
        if req_body is not None and "Content-Length" not in req_headers:
            req_headers["Content-Length"] = str(len(req_body))
        try:
            conn.request(method, path, body=req_body, headers=req_headers)
            r = conn.getresponse()
            data = r.read()
            conn.close()
        except http.client.RemoteDisconnected:
            conn.close()
            return 401, {"remote_disconnected": True}
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            parsed = data.decode("utf-8")
        return r.status, parsed

    def test_admin_keys_requires_auth(self):
        status, _ = self._request("GET", "/admin/keys")
        self.assertEqual(status, 401)

    def test_admin_keys_crud(self):
        auth = f"Bearer {self.admin_key}"
        status, data = self._request("POST", "/admin/keys", {"action": "create", "label": "x"}, {"Authorization": auth})
        self.assertEqual(status, 200)
        self.assertIn("key", data)
        self.assertTrue(data["key"]["token"].startswith("triad-"))
        self.assertEqual(data["key"]["scope"], "internal")
        self.assertFalse(data["key"]["allow_public"])
        kid = data["key"]["key_id"]

        status, public = self._request("POST", "/admin/keys", {
            "action": "create", "label": "external", "scope": "public",
        }, {"Authorization": auth})
        self.assertEqual(status, 200, public)
        self.assertEqual(public["key"]["scope"], "public")
        self.assertTrue(public["key"]["allow_public"])
        self.assertTrue(ApiHandler.keystore.verify(public["key"]["token"]).allow_public)

        status, invalid = self._request("POST", "/admin/keys", {
            "action": "create", "scope": "invalid",
        }, {"Authorization": auth})
        self.assertEqual(status, 400, invalid)

        status, data = self._request("GET", "/admin/keys", None, {"Authorization": auth})
        self.assertEqual(status, 200)
        self.assertTrue(any(k["key_id"] == kid for k in data["keys"]))

        status, _ = self._request("POST", "/admin/keys", {"action": "disable", "key_id": kid}, {"Authorization": auth})
        self.assertEqual(status, 200)

    def test_admin_conversations_empty(self):
        auth = f"Bearer {self.admin_key}"
        status, data = self._request("GET", "/admin/conversations", None, {"Authorization": auth})
        self.assertEqual(status, 200)
        self.assertEqual(data["conversations"], [])

    def test_v1_tuning_groups_apply_live_and_persist(self):
        auth = f"Bearer {self.admin_key}"
        status, data = self._request("GET", "/admin/tuning", None, {"Authorization": auth})
        self.assertEqual(status, 200)
        self.assertEqual({field["group"] for field in data["fields"]},
                         {"retry", "context", "memory", "capture"})
        status, applied = self._request("POST", "/admin/tuning", {
            "retry_on_refusal": False, "compress_at_chars": 42000,
        }, {"Authorization": auth})
        self.assertEqual(status, 200)
        self.assertFalse(self.config.retry_on_refusal)
        self.assertEqual(self.config.compress_at_chars, 42000)
        self.assertEqual(applied["applied"]["compress_at_chars"], 42000)

    def test_turn_trace_hierarchy_is_operator_only(self):
        ApiHandler.records = RecordStore(self.tmp, EphemeralGate(self.config))
        ApiHandler.records.append_turn({"client": "key-a", "conv": "conv-1",
            "model": "glm-5.3", "user": "hello", "final": "world",
            "verdict": "clean", "attempts": [{"n": 1, "kind": "ok", "text": "world"}]})
        status, _ = self._request("GET", "/admin/trace/clients")
        self.assertEqual(status, 401)
        auth = {"Authorization": f"Bearer {self.admin_key}"}
        status, clients = self._request("GET", "/admin/trace/clients", None, auth)
        self.assertEqual(status, 200)
        self.assertEqual(clients["clients"][0]["client"], "key-a")
        status, convs = self._request("GET", "/admin/trace/conversations?client=key-a", None, auth)
        self.assertEqual(status, 200)
        self.assertEqual(convs["conversations"][0]["conv"], "conv-1")
        status, turns = self._request("GET", "/admin/trace/turns?client=key-a&conv=conv-1", None, auth)
        self.assertEqual(status, 200)
        record_id = turns["records"][0]["id"]
        status, detail = self._request("GET", "/admin/trace/turns/" + record_id, None, auth)
        self.assertEqual(status, 200)
        self.assertEqual(detail["record"]["final"], "world")

    def test_armor_registry(self):
        status, data = self._request("GET", "/armor/registry")
        self.assertEqual(status, 200)
        self.assertIn("profiles", data)
        self.assertIn("levels", data)

    def test_healthz_reports_key_required(self):
        status, data = self._request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIn("require_client_key", data)
        self.assertFalse(data["require_client_key"])

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        os.environ.pop("POPO_ADMIN_KEY", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
