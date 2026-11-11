"""Real-upstream E2E test.

Requires an upstream API reachable from localhost. In the Hark sandbox this uses
``hark-model-use`` as an OpenAI-compatible upstream.

**Opt-in only.** This test needs an external executable that is not part of the
project, so running it in the default suite turns a missing tool into a red
build. Set ``POPO_ONLINE=1`` (or run ``./run-tests.sh --online``) to include it.
"""

from __future__ import annotations
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from popo.config import Config
from popo.server.app import ApiHandler

ONLINE = os.environ.get("POPO_ONLINE") == "1"


def _find_free_port() -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HarkModelUseUpstream:
    """Minimal upstream that proxies to hark-model-use run."""

    def __init__(self, model: str = "z-ai/glm-5.3-flash"):
        self.model = model
        self.port = _find_free_port()
        self.server = HTTPServer(("127.0.0.1", self.port), HarkHandler)
        self.server.model = model

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


class HarkHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            body = json.loads(raw)
        except Exception:
            body = {}

        if parsed.path.rstrip("/") == "/v1/chat/completions":
            self._chat(body)
        else:
            self._send_json(404, {"error": f"not found: {parsed.path}"})

    def _chat(self, body: dict):
        model = getattr(self.server, "model", "z-ai/glm-5.3-flash")
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        payload = {
            "messages": body.get("messages", []),
            "temperature": body.get("temperature", 0.7),
            "max_tokens": body.get("max_tokens", 256),
        }
        tmp.write(json.dumps(payload, ensure_ascii=False))
        tmp.close()
        try:
            proc = subprocess.run(
                ["hark-model-use", "run", "--model", model, "--input", tmp.name],
                capture_output=True, text=True, timeout=120,
            )
            text = proc.stdout or "{}"
            # hark-model-use may emit proot info lines before JSON.
            if "{" in text:
                text = text[text.find("{"):]
            try:
                upstream = json.loads(text)
            except Exception as exc:
                self._send_json(502, {"error": f"invalid upstream response: {exc}", "raw": proc.stdout[:500]})
                return
            content = ""
            if isinstance(upstream, dict):
                choices = upstream.get("choices")
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                else:
                    content = upstream.get("text", "") or upstream.get("content", "") or str(upstream)[:500]
            resp = {
                "id": "popo-real",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": str(content)}, "finish_reason": "stop"}],
            }
            self._send_json(200, resp)
        finally:
            os.unlink(tmp.name)

    def _send_json(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(ONLINE, "set POPO_ONLINE=1 to run tests that need a real upstream")
class TestRealUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = HarkModelUseUpstream(model="z-ai/glm-5.3-flash")
        cls.upstream.start()
        cls.proxy_port = _find_free_port()
        cls.config = Config(
            bind_host="127.0.0.1",
            bind_port=cls.proxy_port,
            upstream_host=cls.upstream.url(),
            upstream_key="dummy",
            upstream_timeout=120,
            armor_enabled=False,
            data_dir=tempfile.mkdtemp(prefix="popo_real_"),
            require_client_key=False,
        )
        cls.server = HTTPServer(("127.0.0.1", cls.proxy_port), ApiHandler)
        ApiHandler.config = cls.config
        ApiHandler.records = None
        ApiHandler.keystore = None
        ApiHandler.admin_hashes = set()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.upstream.stop()

    def test_real_upstream_chat(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=120)
        body = {
            "model": "glm-5.3",
            "messages": [{"role": "user", "content": "hello, reply with one word"}],
            "max_tokens": 32,
        }
        conn.request("POST", "/v1/chat/completions", body=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 200, msg=data)
        self.assertTrue(data["choices"][0]["message"]["content"])


if __name__ == "__main__":
    unittest.main()
