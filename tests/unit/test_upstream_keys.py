"""Unit tests for upstream key rotation."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from popo.core.upstream import UpstreamError, UpstreamSource
from popo.errors import ErrorKind


class RotatingHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        data = json.loads(body)
        auth = self.headers.get("Authorization", "")
        data["_authorization"] = auth
        RotatingHandler.calls.append(data)
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if token == "good-key":
            resp = {
                "id": "m1",
                "object": "chat.completion",
                "model": "glm-5.3",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            }
            status = 200
        elif token == "rate-key":
            status = 429
            resp = {"error": {"message": "rate limited"}}
        elif token == "auth-key":
            status = 401
            resp = {"error": {"message": "unauthorized"}}
        else:
            status = 401
            resp = {"error": {"message": "no such key"}}
        encoded = json.dumps(resp).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class TestUpstreamKeyRotation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = HTTPServer(("127.0.0.1", 0), RotatingHandler)
        cls.port = cls.upstream.server_address[1]
        cls.thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.thread.start()

    def setUp(self):
        RotatingHandler.calls.clear()

    def test_first_key_succeeds(self):
        src = UpstreamSource(f"http://127.0.0.1:{self.port}/v1", ["good-key", "backup"])
        resp = src.send({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(resp.data["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(RotatingHandler.calls), 1)

    def test_rotates_on_rate_limit(self):
        src = UpstreamSource(f"http://127.0.0.1:{self.port}/v1", ["rate-key", "good-key"])
        resp = src.send({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(resp.data["choices"][0]["message"]["content"], "ok")
        tokens = [c["_authorization"][7:] for c in RotatingHandler.calls]
        self.assertEqual(tokens, ["rate-key", "good-key"])

    def test_rotates_on_auth(self):
        src = UpstreamSource(f"http://127.0.0.1:{self.port}/v1", ["auth-key", "good-key"])
        resp = src.send({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(resp.data["choices"][0]["message"]["content"], "ok")
        tokens = [c["_authorization"][7:] for c in RotatingHandler.calls]
        self.assertEqual(tokens, ["auth-key", "good-key"])

    def test_exhausts_keys(self):
        src = UpstreamSource(f"http://127.0.0.1:{self.port}/v1", ["auth-key", "rate-key"])
        with self.assertRaises(UpstreamError) as ctx:
            src.send({"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
        # Both keys bad; should try auth-key then rate-key, then stop.
        self.assertEqual(len(RotatingHandler.calls), 2)
        tokens = [c["_authorization"][7:] for c in RotatingHandler.calls]
        self.assertEqual(tokens, ["auth-key", "rate-key"])
        # The last failure was a 429, which is now reported as a rate limit
        # rather than the old catch-all "capacity". The distinction is what lets
        # the caller decide to wait instead of giving up.
        self.assertEqual(ctx.exception.kind, ErrorKind.UPSTREAM_RATE_LIMITED)

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()


if __name__ == "__main__":
    unittest.main()
