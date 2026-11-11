"""Unit tests for the HTTP server surface."""

import http.client
import json
import os
import threading
import unittest
from http.server import HTTPServer

from popo.config import Config
from popo.server.app import ApiHandler


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 0
        cls.server = None
        for p in range(18112, 18150):
            try:
                cls.server = HTTPServer(("127.0.0.1", p), ApiHandler)
                cls.port = p
                break
            except OSError:
                continue
        cls.server.config = Config(
            bind_host="127.0.0.1",
            bind_port=cls.port,
            upstream_host="",
            upstream_key="",
        )
        ApiHandler.config = cls.server.config
        ApiHandler.records = None
        ApiHandler.keystore = None
        ApiHandler.admin_hashes = set()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Accept": "application/json"})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def test_healthz(self):
        status, data = self._get("/healthz")
        self.assertEqual(status, 200)
        body = json.loads(data.decode("utf-8"))
        self.assertTrue(body["ok"])

    def test_models(self):
        status, data = self._get("/v1/models")
        self.assertEqual(status, 200)
        body = json.loads(data.decode("utf-8"))
        self.assertTrue(any(m["id"] == "glm-5.3" for m in body["data"]))

    def test_armor(self):
        status, data = self._get("/armor?model=glm-5.3")
        self.assertEqual(status, 200)
        body = json.loads(data.decode("utf-8"))
        self.assertIn("content", body)

    def test_app_serves_index(self):
        status, data = self._get("/app/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", data.lower())

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()


if __name__ == "__main__":
    unittest.main()
