"""
POPO-V2 error taxonomy.

Every failure is one of these. Each error carries a machine-readable kind
and a human-readable message. The server layer maps kinds to HTTP status.
"""

from __future__ import annotations
from enum import Enum


class ErrorKind(str, Enum):
    # client
    UNAUTHORIZED = "client.unauthorized"
    FORBIDDEN = "client.forbidden"
    BAD_REQUEST = "client.bad_request"
    NOT_FOUND = "client.not_found"
    METHOD_NOT_ALLOWED = "client.method_not_allowed"
    RATE_LIMITED = "client.rate_limited"
    INVALID_MODEL = "client.invalid_model"

    # upstream
    UPSTREAM_AUTH = "upstream.auth"
    UPSTREAM_EDGE_BLOCKED = "upstream.edge_blocked"
    UPSTREAM_CAPACITY = "upstream.capacity"
    # Retry-After present: the provider told us when to come back.
    UPSTREAM_RATE_LIMITED = "upstream.rate_limited"
    # Quota/credit exhausted: waiting does not help, the operator must act.
    UPSTREAM_QUOTA_EXHAUSTED = "upstream.quota_exhausted"
    # 5xx: the provider is unwell; retrying soon usually helps.
    UPSTREAM_OVERLOADED = "upstream.overloaded"
    UPSTREAM_TIMEOUT = "upstream.timeout"
    UPSTREAM_MODEL_NOT_FOUND = "upstream.model_not_found"
    UPSTREAM_INVALID_RESPONSE = "upstream.invalid_response"
    UPSTREAM_NETWORK = "upstream.network"

    # proxy
    PROXY_INTERNAL = "proxy.internal"
    PROXY_TIMEOUT = "proxy.timeout"
    PROXY_NOT_READY = "proxy.not_ready"
    PROXY_NO_ROUTE = "proxy.no_route"
    PROXY_CONFIG = "proxy.config"
    PROXY_NOT_AVAILABLE = "proxy.not_available"
    PROXY_NOT_IMPLEMENTED = "proxy.not_implemented"


class ProxyError(Exception):
    """Base for all proxy-originated errors."""

    def __init__(self, kind: ErrorKind, message: str, *, status: int | None = None, uri: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.uri = uri

    def envelope(self) -> dict:
        body = {"error": {"kind": self.kind.value, "message": self.message, "detail": self.message}}
        if self.uri:
            body["error"]["uri"] = self.uri
        return body

    def __repr__(self) -> str:
        return f"ProxyError({self.kind.value}, {self.message!r})"


class ClientError(ProxyError):
    """Caller-side error."""


class UpstreamError(ProxyError):
    """Upstream provider error."""


class ProxyRuntimeError(ProxyError):
    """Internal proxy problem."""


def classify_status(error: ProxyError) -> tuple[int, bool]:
    """Map an error to (http_status, retryable)."""

    mapping = {
        ErrorKind.UNAUTHORIZED: (401, False),
        ErrorKind.FORBIDDEN: (403, False),
        ErrorKind.BAD_REQUEST: (400, False),
        ErrorKind.NOT_FOUND: (404, False),
        ErrorKind.METHOD_NOT_ALLOWED: (405, False),
        ErrorKind.RATE_LIMITED: (429, True),
        ErrorKind.INVALID_MODEL: (400, False),

        ErrorKind.UPSTREAM_AUTH: (502, False),
        ErrorKind.UPSTREAM_EDGE_BLOCKED: (502, False),
        ErrorKind.UPSTREAM_CAPACITY: (503, True),
        ErrorKind.UPSTREAM_RATE_LIMITED: (429, True),
        ErrorKind.UPSTREAM_QUOTA_EXHAUSTED: (402, False),
        ErrorKind.UPSTREAM_OVERLOADED: (503, True),
        ErrorKind.UPSTREAM_TIMEOUT: (504, True),
        ErrorKind.UPSTREAM_MODEL_NOT_FOUND: (502, False),
        ErrorKind.UPSTREAM_INVALID_RESPONSE: (502, False),
        ErrorKind.UPSTREAM_NETWORK: (503, True),

        ErrorKind.PROXY_INTERNAL: (500, False),
        ErrorKind.PROXY_TIMEOUT: (504, True),
        ErrorKind.PROXY_NOT_READY: (503, False),
        ErrorKind.PROXY_NO_ROUTE: (404, False),
        ErrorKind.PROXY_CONFIG: (500, False),
        ErrorKind.PROXY_NOT_AVAILABLE: (503, False),
        ErrorKind.PROXY_NOT_IMPLEMENTED: (501, False),
    }
    return mapping.get(error.kind, (500, False))
