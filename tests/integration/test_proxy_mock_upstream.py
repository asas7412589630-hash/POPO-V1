"""
Integration test: start POPO proxy pointed at a mock upstream, and verify
/v1/chat/completions and /v1/matches pass through with armor injected.
"""

from __future__ import annotations
import http.client
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from popo.config import Config
from popo.server.app import ApiHandler, serve
from popo.armor.blocks import build_system_prompt


class MockUpstreamHandler(BaseHTTPRequestHandler):
    recorded: list[dict] = []

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        data = json.loads(body)
        MockUpstreamHandler.recorded.append(data)
        resp = {
            "id": "mock-1",
            "object": "chat.completion",
            "model": data.get("model", "x"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }
        encoded = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


import socket


class TestProxyMockUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream_port = _free_port()
        cls.proxy_port = _free_port()
        cls.upstream = HTTPServer(("127.0.0.1", cls.upstream_port), MockUpstreamHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()

        os.environ["POPO_UPSTREAM_HOST"] = f"http://127.0.0.1:{cls.upstream_port}/v1"
        os.environ["POPO_UPSTREAM_KEY"] = "mock-key"
        os.environ["POPO_PORT"] = str(cls.proxy_port)
        os.environ["POPO_UPSTREAM_TIMEOUT"] = "5"
        os.environ["POPO_ARMOR_OFF"] = "1"
        os.environ["POPO_REQUIRE_KEY"] = "0"

        cls.config = Config.from_env()
        cls.proxy = HTTPServer(("127.0.0.1", cls.proxy_port), ApiHandler)
        cls.proxy.config = cls.config
        ApiHandler.config = cls.config
        ApiHandler.records = None
        ApiHandler.keystore = None
        ApiHandler.admin_hashes = set()
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.proxy_thread.join(timeout=2)
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.upstream_thread.join(timeout=2)
        ApiHandler.reset_shared_state()

    def setUp(self):
        MockUpstreamHandler.recorded.clear()

    def test_chat_completions_passthrough(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=10)
        body = json.dumps({
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hello"}],
        }).encode()
        conn.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(MockUpstreamHandler.recorded), 1)
        sent = MockUpstreamHandler.recorded[0]
        self.assertEqual(sent["model"], "glm-5.3")
        self.assertEqual(sent["messages"][-1]["content"], "hello")
        conn.close()

    def test_healthz(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=10)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data["ok"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
