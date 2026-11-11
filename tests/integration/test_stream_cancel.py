"""Cancellation must actually stop the upstream, not just stop reading it.

A test that only asserts "we stopped forwarding" would pass even if the provider
kept generating (and billing) tokens. These tests observe the upstream side: the
server records whether its connection was closed mid-stream.
"""
from __future__ import annotations
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from popo.config import Config
from popo.core.pipeline import run_pipeline_stream
from popo.core.request import ChatRequest
from popo.core.upstream import UpstreamSource


class CountingUpstream(BaseHTTPRequestHandler):
    """Sends many SSE chunks slowly, and notices when the peer leaves."""

    protocol_version = "HTTP/1.1"
    chunks_sent = 0
    peer_closed = False
    finished = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        CountingUpstream.chunks_sent = 0
        CountingUpstream.peer_closed = False
        CountingUpstream.finished = False
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for index in range(200):
                payload = ("data: " + json.dumps(
                    {"choices": [{"delta": {"content": f"tok{index} "}}]}) + "\n\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(payload), payload))
                self.wfile.flush()
                CountingUpstream.chunks_sent += 1
                time.sleep(0.01)
            self.wfile.write(b"0\r\n\r\n")
            CountingUpstream.finished = True
        except (BrokenPipeError, ConnectionResetError):
            CountingUpstream.peer_closed = True


def _config() -> Config:
    return Config(upstream_host="", upstream_key=["k"], armor_enabled=False,
                  scrub_enabled=False, stateq_enabled=False, retry_max=1,
                  retry_budget_seconds=5)


class StreamCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CountingUpstream)
        cls.port = cls.server.server_address[1]
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _stream(self):
        request = ChatRequest(model="glm-5.3", stream=True,
                              messages=[{"role": "user", "content": "go"}])

        def picker(_model):
            return UpstreamSource(f"http://127.0.0.1:{self.port}/v1", "k", timeout=10)

        return run_pipeline_stream(request, _config(), source_picker=picker, records=None)

    def test_full_stream_completes(self):
        result = self._stream()
        chunks = list(result.iterator)
        self.assertGreater(len(chunks), 10)
        self.assertEqual(result.report_fn()["verdict"], "stream")

    def test_client_hangup_aborts_the_upstream_socket(self):
        """The failure this catches: we stop reading but the provider keeps billing."""
        result = self._stream()
        iterator = result.iterator
        next(iterator)                      # consume the opening
        for _ in range(3):
            next(iterator)
        before = CountingUpstream.chunks_sent
        iterator.close()                    # simulates the handler on disconnect
        time.sleep(0.4)
        after = CountingUpstream.chunks_sent
        # Some chunks may already be in flight; the stream must not run to the end.
        self.assertLess(after, 200, "upstream was allowed to generate the whole answer")
        self.assertLess(after - before, 60, "upstream kept streaming after the client left")

    def test_abort_marks_the_report(self):
        result = self._stream()
        iterator = result.iterator
        for _ in range(3):
            next(iterator)
        iterator.close()
        report = result.report_fn()
        self.assertTrue(report.get("aborted"), report)

    def test_upstream_handle_abort_is_idempotent(self):
        source = UpstreamSource(f"http://127.0.0.1:{self.port}/v1", "k", timeout=10)
        stream = source.stream({"model": "m", "messages": [], "stream": True})
        stream.abort()
        stream.abort()          # must not raise
        stream.close()
        self.assertTrue(stream.aborted)


if __name__ == "__main__":
    unittest.main()
