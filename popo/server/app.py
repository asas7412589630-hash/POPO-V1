"""HTTP handler: transport and dispatch only.

Request → route → auth → service → response. There is no domain rule here: no
retry, no model policy, no record shaping. Those live in ``endpoints.py``
(per-area handlers), ``inference.py`` (request policy) and ``pipeline`` (domain).
"""

from __future__ import annotations
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from popo.config import Config
from popo.errors import ErrorKind, ProxyError, classify_status
from popo.server import deps
from popo.server.console_routes import ConsoleRoutes
from popo.server.endpoints import (AdminRoutes, DiscoveryRoutes, InferenceRoutes,
                                   RecordRoutes, SessionRoutes, SetupRoutes)
from popo.server.source_routes import SourceRoutes
from popo.server.trace_routes import TraceRoutes
from popo.server.inference import InferenceService
from popo.server.responses import ResponseWriter
from popo.server.console_compat import match_alias
from popo.server.routes import authorize, match_route
from popo.server.setup import SetupService, SetupStore
from popo.server.static import content_type, is_console_available, resolve_path


class ApiHandler(DiscoveryRoutes, SetupRoutes, InferenceRoutes, SessionRoutes,
                 RecordRoutes, AdminRoutes, SourceRoutes, TraceRoutes, ConsoleRoutes,
                 BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    wbufsize = 8192

    # Public seam: assign a Config, then derived stores rebuild lazily.
    config: Config | None = None
    records = None
    keystore = None
    accounts = None
    admin_hashes: set[str] = set()
    source_picker = None

    @classmethod
    def reset_shared_state(cls) -> None:
        # Reconfiguration and test teardown must not orphan keep-alive sockets.
        # SourceRouter and SourceFactory both expose close(); custom pickers may
        # not, so keep this lifecycle hook optional.
        old_sources = getattr(cls, "_source_factory", None)
        close_sources = getattr(old_sources, "close", None)
        if callable(close_sources):
            close_sources()
        cls.records = None
        cls.keystore = None
        cls.accounts = None
        cls.admin_hashes = set()
        cls._cors_policy = None
        cls._cors_key = None
        cls._source_factory = None
        cls._concurrency = None
        cls._bootstrap_token = None

    def log_message(self, fmt, *args) -> None:  # noqa: A003 - stdlib signature
        pass

    # ---- dependencies -------------------------------------------------
    @property
    def cfg(self) -> Config:
        if type(self).config is None:
            type(self).config = Config.from_env()
        return type(self).config

    def _writer(self) -> ResponseWriter:
        return ResponseWriter(self, deps.cors_of(self))

    def _gate(self):
        return deps.gate_of(self)

    def _records(self):
        return deps.records_of(self)

    def _inference(self) -> InferenceService:
        return InferenceService(self.cfg, self._records(),
                                self.source_picker or deps.sources_of(self))

    def _setup_service(self) -> SetupService:
        # Install configuration is operator-authored and must survive a restart;
        # request data is what the record store gates.
        service = SetupService(self.cfg, SetupStore(self.cfg.data_dir, persist=True),
                               deps.keystore_of(self), deps.bootstrap_of(self))
        # Tell the service whether the caller is a program on this device. A
        # tunnel arrives from loopback too, so `is_local_tool()` (no forwarding
        # headers) is what separates the operator from the internet.
        service.caller_is_local = self._gate().resolve().level in ("operator", "loopback")
        return service

    # ---- request body ---------------------------------------------------
    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST, "invalid Content-Length") from exc
        if length < 0:
            raise ProxyError(ErrorKind.BAD_REQUEST, "invalid Content-Length")
        # Refuse before allocating: a hostile Content-Length is not a memory
        # exhaustion vector.
        if length > self.cfg.max_body_bytes:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"request body exceeds {self.cfg.max_body_bytes} bytes")
        return b"{}" if length == 0 else self.rfile.read(length)

    def _send_error(self, exc: ProxyError) -> None:
        status, _retryable = classify_status(exc)
        if exc.status and 400 <= exc.status < 600:
            status = exc.status
        try:
            if urlparse(self.path).path == "/v1/messages":
                error_type = {
                    400: "invalid_request_error",
                    401: "authentication_error",
                    403: "permission_error",
                    404: "not_found_error",
                    429: "rate_limit_error",
                }.get(status, "api_error")
                self._writer().json(status, {
                    "type": "error",
                    "error": {"type": error_type, "message": exc.message},
                })
            else:
                self._writer().json(status, exc.envelope())
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # ---- verbs ----------------------------------------------------------
    def do_OPTIONS(self) -> None:
        self._writer().empty(204, {"Allow": "GET, POST, OPTIONS"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._method_not_allowed("PUT")

    def do_DELETE(self) -> None:
        self._method_not_allowed("DELETE")

    def do_PATCH(self) -> None:
        self._method_not_allowed("PATCH")

    def _method_not_allowed(self, method: str) -> None:
        self._writer().json(405, {"error": {"kind": ErrorKind.METHOD_NOT_ALLOWED.value,
                                            "message": f"{method} is not supported"}})

    # ---- dispatch --------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and (parsed.path == "/app" or parsed.path.startswith("/app/")):
                self._serve_console(parsed.path)
                return
            # A tunnel publishes a bare hostname. Without this, the most likely
            # thing a person does — open the URL — lands on a JSON 404 that looks
            # like a broken deployment.
            if method == "GET" and parsed.path in ("/", "/index.html"):
                self._writer().empty(302, {"Location": "/app/"})
                return
            route = match_route(method, parsed.path)
            if route is None:
                # The original console speaks bare paths; map them onto the same
                # handlers rather than maintaining a second implementation.
                alias = match_alias(method, parsed.path)
                if alias is not None:
                    from popo.server.routes import Route, authorize as check
                    route = Route(method, parsed.path, alias.auth, alias.target)
                    identity = self._gate().resolve()
                    check(route, identity, self.cfg)
                    handler = getattr(self, f"_route_{alias.target}", None)
                    body = self._read_body() if method in ("POST", "DELETE") else b""
                    if handler is None:
                        raise ProxyError(ErrorKind.PROXY_NOT_IMPLEMENTED,
                                         f"alias target '{alias.target}' is missing")
                    handler(body, parsed, identity)
                    return
            if route is None:
                raise ProxyError(ErrorKind.PROXY_NO_ROUTE,
                                 f"no route for {method} {parsed.path}")
            identity = self._gate().resolve()
            authorize(route, identity, self.cfg)
            handler = getattr(self, f"_route_{route.name}", None)
            if handler is None:
                raise ProxyError(ErrorKind.PROXY_NOT_IMPLEMENTED,
                                 f"route '{route.name}' is declared but not implemented")
            body = self._read_body() if method == "POST" else b""
            handler(body, parsed, identity)
        except ProxyError as exc:
            self._send_error(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # never leak a traceback to a client
            print(f"[POPO] unhandled error on {method} {parsed.path}:\n{traceback.format_exc()}")
            self._send_error(ProxyError(ErrorKind.PROXY_INTERNAL,
                                        "internal error; see server log"))

    # ---- console ---------------------------------------------------------
    def _serve_console(self, path: str) -> None:
        if not is_console_available():
            raise ProxyError(ErrorKind.NOT_FOUND, "console is not installed")
        file_path, exists = resolve_path(path)
        if not exists:
            file_path, exists = resolve_path("/app/index.html")
        if not exists:
            raise ProxyError(ErrorKind.NOT_FOUND, "console file not found")
        with open(file_path, "rb") as handle:
            payload = handle.read()
        cache = "no-cache" if file_path.endswith((".html", ".js")) else "max-age=3600"
        self._writer().bytes(200, payload, content_type(file_path),
                             {"Cache-Control": cache})


def serve(config: Config | None = None) -> None:
    config = config or Config.from_env()
    from popo.server import tuning
    applied = tuning.load(config, config.data_dir)
    if applied:
        print(f"[POPO] loaded tuning: {', '.join(sorted(applied))}")
    ApiHandler.config = config
    ApiHandler.reset_shared_state()
    ApiHandler._started_at = time.time()
    # Issue the first-run token before accepting connections, so the operator can
    # read it from the startup log instead of being asked for a token that has
    # never existed.
    deps.bootstrap_of(ApiHandler)
    server = ThreadingHTTPServer((config.bind_host, config.bind_port), ApiHandler)
    server.daemon_threads = True
    print(f"POPO listening on http://{config.bind_host}:{config.bind_port}")
    if not config.upstream_host:
        print("[POPO] no upstream configured yet; open /app to run setup")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
