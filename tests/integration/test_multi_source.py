"""End-to-end: two providers, one proxy, model decides which is used.

The unit tests prove the catalog routes correctly. This proves the *request
path* does: a client asking for `beta/beta-only` must reach beta's server and not
alpha's. A routing layer that is correct in isolation but bypassed by the
pipeline would pass the unit tests and fail the user.
"""
from __future__ import annotations
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from popo.config import Config
from popo.server.app import ApiHandler
from popo.server.sources import CatalogStore, Source, SourceCatalog

ADMIN = "triad-admin-multisource-test"


class TaggedUpstream(BaseHTTPRequestHandler):
    """Answers with its own name so the test can tell who replied."""

    protocol_version = "HTTP/1.1"
    tag = "?"
    hits = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path not in ("/v1/models", "/models"):
            self.send_error(404)
            return
        body = json.dumps({"data": [{"id": f"{type(self).tag}-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        type(self).hits += 1
        body = json.dumps({
            "id": "x", "model": type(self).tag,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": f"answer from {type(self).tag}"}}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(tag: str):
    handler = type(f"Upstream{tag}", (TaggedUpstream,), {"tag": tag, "hits": 0})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], handler, thread


class MultiSourceEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.alpha_server, alpha_port, cls.alpha, cls.alpha_thread = _serve("alpha")
        cls.beta_server, beta_port, cls.beta, cls.beta_thread = _serve("beta")
        cls.tmp = tempfile.mkdtemp(prefix="popo_multisrc_")

        store = CatalogStore(cls.tmp)
        catalog = SourceCatalog()
        catalog = catalog.add_source(
            Source.build("alpha", "Alpha", f"http://127.0.0.1:{alpha_port}/v1"))
        catalog = catalog.add_source(
            Source.build("beta", "Beta", f"http://127.0.0.1:{beta_port}/v1"))
        catalog = catalog.publish("alpha", ["shared-name", "alpha-only"])
        catalog = catalog.publish("beta", ["shared-name", "beta-only"])
        store.save(catalog)
        store.save_secret("alpha", ["ka"])
        store.save_secret("beta", ["kb"])

        cls.proxy = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        cls.port = cls.proxy.server_address[1]
        cls.proxy.daemon_threads = True
        ApiHandler.config = Config(
            bind_host="127.0.0.1", bind_port=cls.port, data_dir=cls.tmp,
            require_client_key=False, armor_enabled=False, scrub_enabled=False,
            stateq_enabled=False, retry_max=1, allow_private_upstream=True)
        ApiHandler.reset_shared_state()
        ApiHandler.admin_hashes.add(hashlib.sha256(ADMIN.encode()).hexdigest())
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.proxy_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.proxy_thread.join(timeout=2)
        ApiHandler.reset_shared_state()
        cls.alpha_server.shutdown()
        cls.alpha_server.server_close()
        cls.alpha_thread.join(timeout=2)
        cls.beta_server.shutdown()
        cls.beta_server.server_close()
        cls.beta_thread.join(timeout=2)

    def setUp(self):
        self.alpha.hits = 0
        self.beta.hits = 0

    def _chat(self, model: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("POST", "/v1/chat/completions",
                     body=json.dumps({"model": model,
                                      "messages": [{"role": "user", "content": "hi"}]}).encode(),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, {"raw": raw.decode("utf-8", "replace")}

    def _admin_post(self, path: str, payload: dict):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("POST", path, body=json.dumps(payload).encode(),
                     headers={"Content-Type": "application/json",
                              "Authorization": f"Bearer {ADMIN}"})
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def _admin_get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("GET", path, headers={"Authorization": f"Bearer {ADMIN}"})
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def test_namespaced_model_reaches_its_own_source(self):
        status, data = self._chat("beta/beta-only")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["model"], "beta")
        self.assertEqual(self.beta.hits, 1)
        self.assertEqual(self.alpha.hits, 0)

    def test_other_source_is_not_contacted(self):
        self._chat("alpha/alpha-only")
        self.assertEqual(self.alpha.hits, 1)
        self.assertEqual(self.beta.hits, 0)

    def test_bare_alias_goes_to_the_first_publisher(self):
        status, data = self._chat("shared-name")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["model"], "alpha")
        self.assertEqual(self.beta.hits, 0)

    def test_unrouted_model_is_refused_and_contacts_nobody(self):
        status, data = self._chat("never-published")
        self.assertEqual(status, 400, data)
        self.assertEqual(self.alpha.hits + self.beta.hits, 0)

    def test_publishing_is_visible_without_restart(self):
        # Publish a new model at runtime and use it immediately: a publish that
        # only changes a file, with the live picker still holding the old
        # catalog, looks identical to a publish that silently failed.
        status, data = self._admin_post("/admin/sources/publish", {
            "source_id": "beta", "models": ["shared-name", "beta-only", "fresh"],
            "profiles": {"fresh": "off"},
        })
        self.assertEqual(status, 200, data)
        status, routes = self._admin_get("/admin/model-routes")
        self.assertEqual(status, 200, routes)
        route = next(item for item in routes["routes"] if item["model"] == "beta/fresh")
        self.assertEqual(route["profile"], "off")

        status, data = self._chat("beta/fresh")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["model"], "beta")

    def test_source_discovery_and_chat_probe(self):
        status, data = self._admin_post("/admin/sources/discover", {"source_id": "alpha"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["models"], ["alpha-model"])
        status, data = self._admin_post("/admin/sources/test", {
            "source_id": "alpha", "model": "alpha-model"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["model"], "alpha-model")
        self.assertEqual(self.alpha.hits, 1)

    def test_source_secret_is_not_returned_and_is_deleted_with_source(self):
        status, data = self._admin_post("/admin/sources", {
            "action": "add", "id": "gamma", "name": "Gamma",
            "base_url": "http://127.0.0.1:1/v1",
            "keys": ["gamma-secret"],
        })
        self.assertEqual(status, 200, data)
        self.assertNotIn("gamma-secret", json.dumps(data))
        status, data = self._admin_get("/admin/sources")
        self.assertEqual(status, 200, data)
        gamma = next(row for row in data["sources"] if row["id"] == "gamma")
        self.assertTrue(gamma["configured"])
        self.assertNotIn("gamma-secret", json.dumps(data))

        status, data = self._admin_post("/admin/sources", {"action": "remove", "id": "gamma"})
        self.assertEqual(status, 200, data)
        store = CatalogStore(self.tmp)
        self.assertNotIn("gamma", store.load_secrets())
        self.assertIsNone(store.load().source("gamma"))


if __name__ == "__main__":
    unittest.main()
