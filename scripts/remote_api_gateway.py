"""Expose only the POPO client API through a public TCP tunnel.

The local console and operator endpoints remain private on port 18422. This
gateway forwards inference routes to POPO and adds a forwarding marker so a
loopback tunnel cannot inherit passwordless local access.
"""
from __future__ import annotations

import http.client
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("POPO_PORT", "8412"))
BIND_HOST = "127.0.0.1"
BIND_PORT = int(os.environ.get("POPO_GATEWAY_PORT", "18423"))
MAX_BODY_BYTES = 32 * 1024 * 1024

ALLOWED = {
    "GET": {"/healthz", "/v1/models"},
    "POST": {
        "/v1/chat/completions",
        "/v1/messages",
        "/v1/images/generations",
        "/v1/embeddings",
        "/v1/audio/transcriptions",
        "/v1/adult/chat",
        "/v1/adult/image",
    },
}
REQUEST_HEADERS = {
    "authorization", "x-api-key", "content-type", "accept", "accept-encoding",
    "user-agent", "anthropic-version", "anthropic-beta", "openai-organization",
    "openai-project", "x-request-id",
}
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "set-cookie",
}


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        # Avoid logging authorization headers, prompts, or model responses.
        pass

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._error(405, "method not allowed")

    def do_HEAD(self) -> None:
        self._error(405, "method not allowed")

    def _error(self, status: int, message: str) -> None:
        payload = json.dumps({"error": {"message": message, "type": "gateway_error"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _forward(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path not in ALLOWED.get(self.command, set()):
            self._error(404, "route not available through the public API link")
            return

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._error(400, "invalid content length")
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._error(413, "request body too large")
            return
        body = self.rfile.read(length) if length else None

        headers = {name: value for name, value in self.headers.items()
                   if name.lower() in REQUEST_HEADERS}
        headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        # Gate treats this as a forwarded request, never as a local operator.
        headers["X-Forwarded-For"] = "public-tunnel"
        headers["Connection"] = "close"

        target = parsed.path + (("?" + parsed.query) if parsed.query else "")
        connection = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=300)
        try:
            connection.request(self.command, target, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)

            response_headers = response.getheaders()
            content_length = next((value for name, value in response_headers
                                   if name.lower() == "content-length"), None)
            for name, value in response_headers:
                lower = name.lower()
                if lower in HOP_HEADERS or lower == "content-length":
                    continue
                self.send_header(name, value)
            if content_length is not None:
                self.send_header("Content-Length", content_length)
            else:
                self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()

            while True:
                chunk = response.read1(8192)
                if not chunk:
                    break
                if content_length is None:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if content_length is None:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            # A client disconnect should close this connection without logging
            # request data or creating a second response on a broken socket.
            self.close_connection = True
        finally:
            connection.close()


if __name__ == "__main__":
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), GatewayHandler)
    server.daemon_threads = True
    print(f"PUBLIC_API_GATEWAY=http://{BIND_HOST}:{BIND_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
