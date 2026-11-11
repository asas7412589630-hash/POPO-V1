"""CORS policy derived only from server-side facts.

A caller can write ``Origin``, ``Host`` and ``X-Forwarded-Host``; none of them
proves anything. The allow-list therefore comes from configuration the operator
set: the bind address, an explicitly configured public base (verified by the
operator/tunnel probe), and an optional explicit origin list.

``Origin: null`` is refused: it is sent by ``file://`` pages, sandboxed iframes
and ``data:`` URLs, and none of those should be able to read this proxy.
"""

from __future__ import annotations
from urllib.parse import urlparse


def _origin_of(base: str) -> str:
    """Return ``scheme://host[:port]`` for a configured base URL."""
    base = (base or "").strip().rstrip("/")
    if not base:
        return ""
    if "://" not in base:
        base = "http://" + base
    parsed = urlparse(base)
    if not parsed.hostname:
        return ""
    port = parsed.port
    default = 443 if parsed.scheme == "https" else 80
    host = parsed.hostname
    if ":" in host:  # IPv6
        host = f"[{host}]"
    if port and port != default:
        return f"{parsed.scheme}://{host}:{port}"
    return f"{parsed.scheme}://{host}"


class CorsPolicy:
    """Decide whether an origin may read a response."""

    def __init__(self, bind_host: str = "127.0.0.1", bind_port: int = 8412,
                 public_base: str = "", extra_origins: list[str] | None = None,
                 allow_null: bool = False):
        self.allowed: set[str] = set()
        host = bind_host or "127.0.0.1"
        if host in ("0.0.0.0", "::", ""):
            # A wildcard bind has no single reachable origin; loopback names are
            # still valid when the console is opened locally.
            for name in ("127.0.0.1", "localhost", "[::1]"):
                self.allowed.add(f"http://{name}:{bind_port}")
        else:
            self.allowed.add(_origin_of(f"http://{host}:{bind_port}"))
        base_origin = _origin_of(public_base)
        if base_origin:
            self.allowed.add(base_origin)
        for origin in extra_origins or []:
            cleaned = _origin_of(origin)
            if cleaned:
                self.allowed.add(cleaned)
        self.allow_null = allow_null

    def allows(self, origin: str) -> bool:
        origin = (origin or "").strip()
        if not origin:
            return False
        if origin == "null":
            return self.allow_null
        return origin.rstrip("/") in self.allowed

    def headers_for(self, origin: str) -> dict[str, str]:
        """Return CORS response headers. Empty dict means no CORS grant."""
        if not self.allows(origin):
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": (
                "Authorization, Content-Type, X-Conversation-Id, X-Armor-Level, "
                "x-api-key, anthropic-version"
            ),
        }
