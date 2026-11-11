"""The connection pool must be reused across requests, not rebuilt per request.

The audit's point was precise: a pool class that passes unit tests proves
nothing about the serving path if the service constructs a new pool for every
request. These tests count TCP connections on the *server* side, so "reused"
means the second request arrived on the same socket.
"""
from __future__ import annotations
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from popo.config import Config
from popo.core.upstream import UpstreamSource
from popo.server.inference import SourceFactory


class ConnectionCountingUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections = 0
    requests = 0

    def log_message(self, *args):
        pass

    def setup(self):
        ConnectionCountingUpstream.connections += 1
        super().setup()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        ConnectionCountingUpstream.requests += 1
        body = b'{"choices":[{"message":{"content":"ok"}}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _config(host: str) -> Config:
    return Config(upstream_host=host, upstream_key=["k"], upstream_timeout=10,
                  armor_enabled=False, scrub_enabled=False, stateq_enabled=False,
                  retry_max=1)


class PoolReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ConnectionCountingUpstream)
        cls.port = cls.server.server_address[1]
        cls.server.daemon_threads = True
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

    def setUp(self):
        ConnectionCountingUpstream.connections = 0
        ConnectionCountingUpstream.requests = 0

    def _base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def test_source_reuses_the_socket(self):
        source = UpstreamSource(self._base(), "k", timeout=10)
        self.addCleanup(source.close)
        for _ in range(3):
            source.send({"model": "m", "messages": []})
        self.assertEqual(ConnectionCountingUpstream.requests, 3)
        self.assertEqual(ConnectionCountingUpstream.connections, 1,
                         "three requests should share one connection")

    def test_factory_returns_the_same_source_for_the_same_config(self):
        config = _config(self._base())
        factory = SourceFactory(config)
        self.addCleanup(factory.close)
        first = factory("m")
        second = factory("m")
        self.assertIs(first, second, "a per-request pool defeats the purpose")

    def test_factory_rebuilds_when_config_changes(self):
        config = _config(self._base())
        factory = SourceFactory(config)
        self.addCleanup(factory.close)
        first = factory("m")
        config.upstream_key = ["another-key"]
        second = factory("m")
        self.assertIsNot(first, second, "changed credentials must not reuse the pool")

    def test_factory_reuse_produces_one_connection_across_requests(self):
        config = _config(self._base())
        factory = SourceFactory(config)
        self.addCleanup(factory.close)
        for _ in range(3):
            factory("m").send({"model": "m", "messages": []})
        self.assertEqual(ConnectionCountingUpstream.requests, 3)
        self.assertEqual(ConnectionCountingUpstream.connections, 1)

    def test_close_releases_every_pooled_connection(self):
        config = _config(self._base())
        factory = SourceFactory(config)
        factory("m").send({"model": "m", "messages": []})
        factory.close()
        self.assertEqual(len(factory._sources), 0)


if __name__ == "__main__":
    unittest.main()
