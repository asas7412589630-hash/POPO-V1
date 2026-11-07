#!/usr/bin/env python3
"""POPO-V1 Connection Pool & Dispatcher (armor_pool).

Decoupled upstream HTTP/HTTPS connection pooling and transport dispatcher.
Maintains persistent keep-alive sockets to upstream LLM providers, performs
zero-cost socket liveness probing before checkout, and handles narrow write-time
retries for stale pooled sockets.

Standard library only. No third-party dependencies.
"""

import http.client
import os
import select
import ssl
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

_CONN_POOL: Dict[Tuple, list] = {}
_CONN_POOL_LOCK = threading.Lock()
UPSTREAM_POOL_SIZE = int(os.environ.get("ARMOR_POOL_SIZE", "6"))

# Hit/miss/stale counters for healthz reporting
_POOL_STATS: Dict[str, int] = {"hit": 0, "miss": 0, "stale": 0}
_POOL_STATS_LOCK = threading.Lock()

# Process-wide default client TLS context (built once, reused thereafter)
_TLS_CONTEXT: Optional[ssl.SSLContext] = None
_TLS_CONTEXT_LOCK = threading.Lock()

# Optional logging callback
_LOGGER: Optional[Callable[[str], None]] = None


def set_pool_logger(fn: Optional[Callable[[str], None]]) -> None:
    """Set custom logger function for pool events."""
    global _LOGGER
    _LOGGER = fn


def _log(msg: str) -> None:
    if _LOGGER is not None:
        try:
            _LOGGER(msg)
            return
        except Exception:
            pass
    # Fallback to sys.stderr
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"{ts} [pool] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _conn_key(cls: Any, host: str, port: int, tls: bool) -> Tuple:
    """Canonical cache key for a connection pool target."""
    return (cls, str(host), int(port), bool(tls))


def _tls_context() -> ssl.SSLContext:
    """Process-wide default client TLS context (built once, reused thereafter)."""
    global _TLS_CONTEXT
    if _TLS_CONTEXT is not None:
        return _TLS_CONTEXT
    with _TLS_CONTEXT_LOCK:
        if _TLS_CONTEXT is None:
            _TLS_CONTEXT = ssl.create_default_context()
    return _TLS_CONTEXT


def _pool_take(key: Tuple) -> Optional[http.client.HTTPConnection]:
    """Pop a live connection for `key`, or None. Never returns a dead socket."""
    with _CONN_POOL_LOCK:
        idle = _CONN_POOL.get(key)
        if not idle:
            return None
        conn = idle.pop()
    try:
        sock = getattr(conn, "sock", None)
        if sock is None:
            return None

        # Probe if socket is readable (EOF or stray pipelined byte means unusable)
        try:
            ready, _, _ = select.select([sock], [], [], 0)
        except Exception:
            ready = [sock]

        if ready:
            try:
                sock.settimeout(0.2)
                junk = sock.recv(1)
            except Exception:
                junk = b""
            if junk == b"":
                with _POOL_STATS_LOCK:
                    _POOL_STATS["stale"] += 1
                try:
                    conn.close()
                except Exception:
                    pass
                return None
            # Unexpected bytes on an idle socket: do not trust it.
            try:
                conn.close()
            except Exception:
                pass
            return None
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None


def _pool_put(key: Tuple, conn: Optional[http.client.HTTPConnection]) -> None:
    """Return a connection for reuse. Caller has already consumed the response."""
    if conn is None:
        return
    try:
        if getattr(conn, "sock", None) is None:
            return
    except Exception:
        return
    with _CONN_POOL_LOCK:
        idle = _CONN_POOL.setdefault(key, [])
        if len(idle) >= UPSTREAM_POOL_SIZE:
            try:
                conn.close()
            except Exception:
                pass
            return
        idle.append(conn)


def _pool_drop(key: Tuple, conn: Optional[http.client.HTTPConnection]) -> None:
    """Close a connection that must not be reused (error / unknown state)."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _pool_clear() -> None:
    """Close and drop all idle connections in the pool."""
    with _CONN_POOL_LOCK:
        for idle in _CONN_POOL.values():
            for conn in idle:
                try:
                    conn.close()
                except Exception:
                    pass
        _CONN_POOL.clear()


def pool_stats() -> Dict[str, int]:
    """Return a copy of the current connection pool stats."""
    with _POOL_STATS_LOCK:
        return dict(_POOL_STATS)


def _route_request(route_cls: Any, host: str, port: int, tls: bool, kwargs: dict,
                   method: str, path: str, body: Any = None, headers: Any = None) -> Tuple[Any, Tuple, Any]:
    """Send one request, transparently replacing a stale pooled connection.

    Returns (conn, pool_key, resp).

    A pooled socket can die between the health check and the write (NAT idle
    timeout, upstream restart, a proxy in between dropping the flow). That is a
    transport problem, not an upstream failure, so it is retried exactly once on
    a fresh socket before the caller ever sees an error.
    """
    key = _conn_key(route_cls, host, port, tls)
    conn = _pool_take(key)
    with _POOL_STATS_LOCK:
        if conn is not None:
            _POOL_STATS["hit"] += 1
        else:
            _POOL_STATS["miss"] += 1

    reused = conn is not None
    if not reused:
        conn = route_cls(host, port, **kwargs)

    try:
        conn.request(method, path, body=body, headers=headers)
    except Exception as exc:
        _pool_drop(key, conn)
        if not reused:
            raise
        # A pooled socket died on write: nothing reached upstream as a complete
        # request, so one retry on a fresh connection is safe.
        _log("[pool] stale connection on write (%s) — retrying once" % type(exc).__name__)
        with _POOL_STATS_LOCK:
            _POOL_STATS["stale"] += 1
        fresh = route_cls(host, port, **kwargs)
        try:
            fresh.request(method, path, body=body, headers=headers)
            return fresh, key, fresh.getresponse()
        except Exception:
            _pool_drop(key, fresh)
            raise exc

    try:
        resp = conn.getresponse()
    except Exception:
        _pool_drop(key, conn)
        raise
    return conn, key, resp
