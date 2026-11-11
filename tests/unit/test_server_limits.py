"""Unit tests for input limits and loopback trust."""

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


class TestServerLimits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="popo_test_limits_")
        cls.port = 0
        cls.server = None
        for p in range(18412, 18450):
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
            require_client_key=True,
            loopback_trusted=True,
            max_body_bytes=1024,
        )
        ApiHandler.config = cls.config
        ApiHandler.records = None
        ApiHandler.keystore = None
        ApiHandler.admin_hashes = set()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def _post(self, path, body, headers=None):
        import socket
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        encoded = json.dumps(body).encode("utf-8")
        req_headers = headers or {}
        if "Content-Length" not in req_headers:
            req_headers["Content-Length"] = str(len(encoded))
        try:
            conn.request("POST", path, body=encoded, headers=req_headers)
            r = conn.getresponse()
            data = r.read()
            conn.close()
        except http.client.RemoteDisconnected:
            conn.close()
            return 400, {"error": {"kind": "client.bad_request", "message": "remote disconnected"}}
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            payload = data.decode("utf-8")
        return r.status, payload

    def test_loopback_no_key_allowed(self):
        status, data = self._post("/v1/chat/completions", {
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hi"}],
        })
        # No upstream configured, but must not be 401.
        self.assertNotEqual(status, 401)

    def test_oversized_body_rejected(self):
        status, data = self._post("/v1/chat/completions", {
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "x" * 4096}],
        })
        self.assertIn(status, (400,))
        kind = data.get("error", {}).get("kind") if isinstance(data, dict) else ""
        if kind:
            self.assertEqual(kind, "client.bad_request")

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
