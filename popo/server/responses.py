"""HTTP response plumbing.

Only transport lives here: status codes, headers, body encoding, SSE framing.
No domain decisions (no retry, no auth policy, no model resolution).
"""

from __future__ import annotations
import json

from popo.server.cors import CorsPolicy


def json_bytes(body) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class ResponseWriter:
    """Writes responses for one request. Nothing else."""

    def __init__(self, handler, cors: CorsPolicy):
        self.handler = handler
        self.cors = cors

    def _origin(self) -> str:
        return self.handler.headers.get("Origin", "") or ""

    def _apply_cors(self) -> None:
        for name, value in self.cors.headers_for(self._origin()).items():
            self.handler.send_header(name, value)

    def json(self, status: int, body, headers: dict | None = None) -> None:
        encoded = json_bytes(body)
        self.handler.send_response(status)
        self.handler.send_header("Content-Type", "application/json; charset=utf-8")
        self.handler.send_header("Content-Length", str(len(encoded)))
        self._apply_cors()
        for name, value in (headers or {}).items():
            self.handler.send_header(name, value)
        self.handler.end_headers()
        self._write(encoded)

    def empty(self, status: int, headers: dict | None = None) -> None:
        self.handler.send_response(status)
        # Content-Length: 0 is required, not cosmetic: with HTTP/1.1 keep-alive
        # and no body length, the client waits for bytes that never arrive and
        # the connection hangs until its own timeout.
        self.handler.send_header("Content-Length", "0")
        self._apply_cors()
        for name, value in (headers or {}).items():
            self.handler.send_header(name, value)
        self.handler.end_headers()

    def bytes(self, status: int, body: bytes, content_type: str,
              headers: dict | None = None) -> None:
        self.handler.send_response(status)
        self.handler.send_header("Content-Type", content_type)
        self.handler.send_header("Content-Length", str(len(body)))
        self._apply_cors()
        for name, value in (headers or {}).items():
            self.handler.send_header(name, value)
        self.handler.end_headers()
        self._write(body)

    def sse_start(self, headers: dict | None = None) -> None:
        self.handler.send_response(200)
        self.handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.handler.send_header("Cache-Control", "no-cache")
        # HTTP/1.1 with neither Content-Length nor Transfer-Encoding leaves the
        # client no way to know the body ended except closing the socket. A
        # client that reconnects on timeout then receives the same events twice
        # (measured 2026-11-11: two `data: [DONE]` markers for one completion).
        # Chunked framing states the end explicitly.
        self.handler.send_header("Transfer-Encoding", "chunked")
        self.handler.send_header("Connection", "keep-alive")
        self.handler.send_header("X-Accel-Buffering", "no")
        self._apply_cors()
        for name, value in (headers or {}).items():
            self.handler.send_header(name, value)
        self.handler.end_headers()
        self._chunked = True

    def sse_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        # chunked framing: <hex length>CRLF<data>CRLF
        self.handler.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        self.handler.wfile.flush()

    def sse_end(self) -> None:
        """Terminate a chunked body with the zero-length chunk."""
        if not getattr(self, "_chunked", False):
            return
        try:
            self.handler.wfile.write(b"0\r\n\r\n")
            self.handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self._chunked = False

    def _write(self, body: bytes) -> None:
        try:
            self.handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The caller went away; there is nobody left to tell.
            pass
