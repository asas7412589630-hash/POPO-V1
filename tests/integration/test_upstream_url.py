"""Unit-like integration test for UpstreamSource URL construction."""

import http.client
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from popo.core.upstream import UpstreamSource


class EchoHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []

    def log_message(self, *a): pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        EchoHandler.requests.append((self.path, raw))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8"))

    def do_GET(self):
        EchoHandler.requests.append((self.path, ""))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))


class TestUpstreamUrl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EchoHandler.requests.clear()
        cls.server = HTTPServer(("127.0.0.1", 0), EchoHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_v1_base_url(self):
        src = UpstreamSource(f"http://127.0.0.1:{self.port}/v1", "k")
        src.send({"model": "x", "messages": []})
        self.assertEqual(EchoHandler.requests[-1][0], "/v1/chat/completions")

    def test_root_base_url(self):
        src = UpstreamSource(f"http://127.0.0.1:{self.port}", "k")
        src.send({"model": "x", "messages": []})
        self.assertEqual(EchoHandler.requests[-1][0], "/v1/chat/completions")

    def test_prefixed_base_url(self):
        src = UpstreamSource(f"http://127.0.0.1:{self.port}/api", "k")
        src.send({"model": "x", "messages": []})
        self.assertEqual(EchoHandler.requests[-1][0], "/api/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
