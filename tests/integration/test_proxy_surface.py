"""Integration tests for the endpoints added in 2.1.

A real HTTP server is started against a local mock upstream, so these assert the
wire behaviour (status, headers, bodies) rather than function return values.
"""
from __future__ import annotations
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from popo.config import Config
from popo.server.app import ApiHandler


class MockUpstream(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict]] = []

    def log_message(self, *args):
        pass

    def _reply(self, status: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._reply(200, {"object": "list", "data": [{"id": "glm-5.3"}]})
            return
        self._reply(404, {"error": "nope"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"_raw_len": len(raw)}
        MockUpstream.requests.append((self.path, payload))
        if self.path.endswith("/images/generations"):
            self._reply(200, {"created": 1, "data": [{"url": "https://img.example/a.png"}]})
            return
        if self.path.endswith("/embeddings"):
            self._reply(200, {"object": "list", "data": [{"embedding": [0.1, 0.2]}]})
            return
        if self.path.endswith("/audio/transcriptions"):
            self._reply(200, {"text": "transcribed"})
            return
        self._reply(200, {"id": "cmpl-1", "model": "glm-5.3",
                          "choices": [{"message": {"role": "assistant", "content": "ok"},
                                       "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 3, "completion_tokens": 1}})


class ProxySurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="popo_surface_")
        cls.upstream = HTTPServer(("127.0.0.1", 0), MockUpstream)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

        cls.proxy = HTTPServer(("127.0.0.1", 0), ApiHandler)
        cls.port = cls.proxy.server_address[1]
        ApiHandler.config = Config(
            bind_host="127.0.0.1", bind_port=cls.port,
            upstream_host=f"http://127.0.0.1:{cls.upstream_port}/v1",
            upstream_key="test-key",
            data_dir=cls.tmp, require_client_key=False, armor_enabled=False,
            published_models=["glm-5.3"],
        )
        ApiHandler.reset_shared_state()
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()

    def _post(self, path: str, payload=None, headers=None, raw: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        conn.request("POST", path, body=body, headers=request_headers)
        response = conn.getresponse()
        data = response.read()
        result_headers = dict(response.getheaders())
        conn.close()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = data
        return response.status, parsed, result_headers

    def _get(self, path: str, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = data
        return response.status, parsed

    def setUp(self):
        MockUpstream.requests.clear()

    # ---- images / embeddings / audio ----------------------------------
    def test_images_passthrough_reaches_upstream(self):
        status, data, _ = self._post("/v1/images/generations",
                                    {"model": "glm-5.3", "prompt": "a cat"})
        self.assertEqual(status, 200)
        self.assertIn("data", data)
        paths = [p for p, _ in MockUpstream.requests]
        self.assertTrue(any(p.endswith("/images/generations") for p in paths))

    def test_embeddings_passthrough(self):
        status, data, _ = self._post("/v1/embeddings",
                                    {"model": "glm-5.3", "input": ["hello"]})
        self.assertEqual(status, 200)
        self.assertEqual(data["data"][0]["embedding"], [0.1, 0.2])

    def test_audio_transcription_forwards_raw_multipart(self):
        boundary = b"--X-BOUNDARY--"
        status, data, _ = self._post(
            "/v1/audio/transcriptions", raw=boundary,
            headers={"Content-Type": "multipart/form-data; boundary=X-BOUNDARY"})
        self.assertEqual(status, 200)
        self.assertEqual(data["text"], "transcribed")

    def test_passthrough_rejects_unpublished_model(self):
        status, data, _ = self._post("/v1/embeddings",
                                    {"model": "not-published", "input": ["x"]})
        self.assertEqual(status, 400, data)

    # ---- anthropic ----------------------------------------------------
    def test_anthropic_non_stream_shape(self):
        status, data, _ = self._post("/v1/messages", {
            "model": "glm-5.3", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(data["type"], "message")
        self.assertEqual(data["content"][0]["type"], "text")
        self.assertIn("input_tokens", data["usage"])

    def test_anthropic_accepts_x_api_key(self):
        token, _ = ApiHandler.keystore, None
        from popo.auth.keys import KeyStore
        store = KeyStore()
        token, record = store.create(label="anthropic")
        ApiHandler.keystore = store
        try:
            status, data, _ = self._post("/v1/messages", {
                "model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
                headers={"x-api-key": token})
            self.assertEqual(status, 200, data)
        finally:
            ApiHandler.reset_shared_state()

    # ---- armor level header --------------------------------------------
    def test_armor_level_header_rejected_when_unknown(self):
        status, data, _ = self._post("/v1/chat/completions",
                                    {"model": "glm-5.3",
                                     "messages": [{"role": "user", "content": "hi"}]},
                                    headers={"X-Armor-Level": "super-secret"})
        self.assertEqual(status, 400, data)
        self.assertIn("armor level", str(data))

    def test_armor_level_off_removes_system_prompt(self):
        ApiHandler.config.armor_enabled = True
        try:
            status, _, _ = self._post("/v1/chat/completions",
                                     {"model": "glm-5.3",
                                      "messages": [{"role": "user", "content": "hi"}]},
                                     headers={"X-Armor-Level": "off"})
            self.assertEqual(status, 200)
            sent = MockUpstream.requests[-1][1]
            self.assertTrue(all(m.get("role") != "system" for m in sent["messages"]))
        finally:
            ApiHandler.config.armor_enabled = False

    def test_public_client_cannot_disable_selected_armor_with_header(self):
        from popo.auth.keys import KeyStore
        config = ApiHandler.config
        old_required, old_armor = config.require_client_key, config.armor_enabled
        old_store = ApiHandler.keystore
        store = KeyStore()
        token, _record = store.create(label="public-test", allow_public=True)
        config.require_client_key = True
        config.armor_enabled = True
        ApiHandler.keystore = store
        try:
            status, _data, _headers = self._post(
                "/v1/chat/completions",
                {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {token}",
                         "X-Forwarded-For": "public-tunnel",
                         "X-Armor-Level": "off"},
            )
            self.assertEqual(status, 200)
            sent = MockUpstream.requests[-1][1]
            systems = [message for message in sent["messages"]
                       if message.get("role") == "system"]
            self.assertEqual(len(systems), 1)
            self.assertTrue(systems[0]["content"].startswith(
                '<popo_system_policy authority="highest" priority="system">'))
            self.assertIn("<policy_precedence>", systems[0]["content"])
        finally:
            config.require_client_key, config.armor_enabled = old_required, old_armor
            ApiHandler.keystore = old_store

    # ---- corporate hygiene ----------------------------------------------
    def test_client_metadata_headers_not_forwarded(self):
        self._post("/v1/chat/completions",
                   {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(len(MockUpstream.requests), 1)

    def test_public_tunnel_key_scope_is_enforced_over_http(self):
        from popo.auth.keys import KeyStore
        config = ApiHandler.config
        required = config.require_client_key
        previous_store = ApiHandler.keystore
        store = KeyStore()
        internal, _ = store.create(label="internal")
        external, _ = store.create(label="external", allow_public=True)
        ApiHandler.config.require_client_key = True
        ApiHandler.keystore = store
        marker = {"X-Forwarded-For": "public-tunnel"}
        try:
            internal_headers = {**marker, "Authorization": f"Bearer {internal}"}
            status, _body, _headers = self._post(
                "/v1/chat/completions",
                {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
                headers=internal_headers)
            self.assertEqual(status, 401)
            self.assertEqual(MockUpstream.requests, [])

            public_headers = {**marker, "Authorization": f"Bearer {external}"}
            status, _body, _headers = self._post(
                "/v1/chat/completions",
                {"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
                headers=public_headers)
            self.assertEqual(status, 200)
            self.assertEqual(len(MockUpstream.requests), 1)

            status, _body = self._get("/conversations", headers=public_headers)
            self.assertEqual(status, 401)
            status, _body = self._get("/admin/conversations", headers=public_headers)
            self.assertEqual(status, 401)
        finally:
            ApiHandler.config.require_client_key = required
            ApiHandler.keystore = previous_store

    def test_cors_not_reflected_for_foreign_origin(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/v1/models", headers={"Origin": "http://evil.example"})
        response = conn.getresponse()
        response.read()
        headers = dict(response.getheaders())
        conn.close()
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_oversized_body_rejected_before_read(self):
        original = ApiHandler.config.max_body_bytes
        ApiHandler.config.max_body_bytes = 128
        try:
            status, data, _ = self._post("/v1/chat/completions",
                                        {"model": "glm-5.3",
                                         "messages": [{"role": "user", "content": "x" * 500}]})
            self.assertEqual(status, 400, data)
        finally:
            ApiHandler.config.max_body_bytes = original

    def test_unknown_route_is_not_forwarded(self):
        status, data, _ = self._post("/v1/nonexistent", {"x": 1})
        self.assertEqual(status, 404)
        self.assertEqual(MockUpstream.requests, [])

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
