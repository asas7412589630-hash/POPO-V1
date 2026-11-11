"""Upstream transport: connection reuse, header normalisation, error mapping.

This layer knows nothing about armor, retries or records. It forwards bytes and
translates *upstream* failures into the proxy's error taxonomy exactly once, so
callers never have to sniff HTML bodies to tell a WAF block from a bad key.

Two properties matter more than speed here:

* **Bounded resources.** Every connection is returned, closed, or explicitly
  abandoned; a socket that cannot be reused is never put back in the pool.
* **No client metadata leaks.** ``X-Forwarded-For``, ``CF-Connecting-IP`` and
  similar headers are dropped rather than forwarded: they carry the caller's
  address to a third party and can change WAF behaviour.
"""

from __future__ import annotations
import http.client
import json
import select
import socket
import ssl
from typing import Iterator
from urllib.parse import urlparse

from popo.errors import ErrorKind, ProxyError, UpstreamError

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_CLIENT_METADATA_HEADERS = (
    "x-forwarded-for", "x-real-ip", "cf-connecting-ip", "x-client-ip",
    "forwarded", "x-armor-admin", "x-armor-level", "true-client-ip",
)
_EDGE_MARKERS = (b"cloudflare", b"cf-error", b"attention required", b"just a moment")

# Phrases providers use when the account is out of credit rather than pacing us.
# "insufficient_quota" is OpenAI's spelling; the Chinese forms appear in domestic
# gateways. Checked because a 429 and an exhausted balance need opposite handling.
# Encoded as UTF-8 literals, not \u escapes: these are bytes, and a str escape
# would not match what the provider actually sent.
_QUOTA_MARKERS = (b"insufficient_quota", b"insufficient quota",
                  b"exceeded your current quota", b"quota exhausted",
                  b"credit balance is too low", b"no remaining",
                  "餘額不足".encode(), "餘額不够".encode(),
                  "額度不足".encode(), "余额不足".encode())


def _looks_like_quota_exhausted(raw: bytes) -> bool:
    head = (raw or b"")[:2048].lower()
    return any(marker in head for marker in _QUOTA_MARKERS)


class UpstreamResponse:
    """Buffered upstream response."""

    def __init__(self, status: int, data: dict, headers: dict | None = None):
        self.status = status
        self.data = data
        self.headers = headers or {}

    def __repr__(self) -> str:
        return f"UpstreamResponse(status={self.status})"


class UpstreamStream:
    """An in-flight streaming response that can be read or aborted.

    ``abort()`` closes the socket. That is the whole point: a provider stops
    generating when its peer disappears, so a client that hangs up stops the
    spend. ``close()`` is the normal path and is idempotent.
    """

    def __init__(self, conn, response):
        self._conn = conn
        self._response = response
        self.aborted = False
        self._closed = False

    def read(self) -> bytes:
        """Return the next chunk, or ``b""`` at end of stream.

        Deliberately not a generator: a ``for ... break`` over a generator runs
        its ``finally`` block, which would close the socket the moment the caller
        stopped inspecting — exactly the opposite of what the buffered-opening
        logic needs.
        """
        if self._closed:
            return b""
        try:
            return self._response.read1(8192)
        except (OSError, http.client.HTTPException) as exc:
            if self.aborted:
                return b""
            raise UpstreamError(ErrorKind.UPSTREAM_NETWORK,
                                f"stream broke: {exc}") from exc

    def __iter__(self):
        """Convenience iteration that closes the socket when exhausted."""
        try:
            while True:
                chunk = self.read()
                if not chunk:
                    return
                yield chunk
        finally:
            self.close()

    def abort(self) -> None:
        """Stop the upstream stream because the client is gone."""
        self.aborted = True
        # shutdown() wakes a blocked read on the other side; close() alone can
        # leave the peer generating until its next write fails.
        try:
            if self._conn.sock is not None:
                self._conn.sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except OSError:
            pass


def _looks_like_edge_block(status: int, body: bytes, headers: dict) -> bool:
    """Distinguish a CDN/WAF rejection from a provider error.

    A WAF answers with HTML, a ``cf-mitigated`` header, or a Cloudflare error
    page; a provider answers with JSON and an error object. Reporting both as
    ``upstream.auth`` sends the operator to rotate a key that is fine.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    if "cf-mitigated" in lowered:
        return True
    head = body[:2048].lower()
    # A named edge marker is decisive: those strings only appear on WAF pages.
    if any(marker in head for marker in _EDGE_MARKERS) and status >= 400:
        return True
    # A bare HTML body is *not* enough. A plain 503 from a reverse proxy can be
    # HTML too, and calling it "edge blocked" tells the operator to chase a WAF
    # that is not there. Require both HTML and a status a WAF actually uses.
    stripped = body.lstrip()[:64].lower()
    return (stripped.startswith(b"<") and status in (403, 503)
            and b"<html" in head and (b"error" in head or b"blocked" in head))


class UpstreamSource:
    """A resolved upstream destination plus its credentials."""

    def __init__(self, base_url: str, api_keys: str | list[str] | None, *,
                 timeout: float = 90.0, connect_timeout: float = 15.0,
                 user_agent: str = "", pool_size: int = 6):
        parsed = urlparse(base_url or "")
        self.scheme = parsed.scheme or "https"
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if self.scheme == "https" else 80)
        self.path_prefix = parsed.path.rstrip("/")
        if isinstance(api_keys, str):
            self._keys = [api_keys] if api_keys.strip() else [""]
        else:
            keys = [k for k in (api_keys or []) if k]
            self._keys = keys or [""]
        self._key_index = 0
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.user_agent = user_agent or DEFAULT_UA
        self._pool: list = []
        self._pool_size = max(1, int(pool_size))
        self._tls: ssl.SSLContext | None = None

    # ---- key selection ----------------------------------------------
    @property
    def api_key(self) -> str:
        return self._keys[self._key_index]

    def rotate_key(self) -> bool:
        if len(self._keys) <= 1:
            return False
        self._key_index += 1
        return self._key_index < len(self._keys)

    def reset_keys(self) -> None:
        self._key_index = 0

    # ---- connection pool --------------------------------------------
    def _tls_context(self) -> ssl.SSLContext:
        if self._tls is None:
            self._tls = ssl.create_default_context()
        return self._tls

    def _new_connection(self):
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=self._tls_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    @staticmethod
    def _reusable(conn) -> bool:
        """A socket with unread bytes is not reusable — those bytes belong to
        the previous response and would corrupt the next one."""
        try:
            if conn.sock is None:
                return False
            ready, _, _ = select.select([conn.sock], [], [], 0)
            return not ready
        except (OSError, ValueError):
            return False

    def _acquire(self):
        while self._pool:
            conn = self._pool.pop()
            if self._reusable(conn):
                return conn
            try:
                conn.close()
            except OSError:
                pass
        return self._new_connection()

    def _release(self, conn) -> None:
        if len(self._pool) >= self._pool_size or not self._reusable(conn):
            try:
                conn.close()
            except OSError:
                pass
            return
        self._pool.append(conn)

    def close(self) -> None:
        while self._pool:
            try:
                self._pool.pop().close()
            except OSError:
                pass

    # ---- URL ---------------------------------------------------------
    def url_for(self, path: str) -> str:
        prefix = self.path_prefix.rstrip("/")
        if not prefix:
            return path
        if prefix.endswith("/v1") and path.startswith("/v1"):
            return prefix + path[len("/v1"):]
        return prefix + path

    # ---- headers -----------------------------------------------------
    def headers_for(self, content_length: int | None = None,
                    extra: dict | None = None,
                    content_type: str = "application/json") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": self.user_agent,
        }
        if content_length is not None:
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(content_length)
        for key, value in (extra or {}).items():
            if key.lower() in _CLIENT_METADATA_HEADERS:
                continue
            headers[key] = value
        return headers

    # ---- transport ---------------------------------------------------
    def _request(self, method: str, path: str, body: bytes | None,
                 extra: dict | None, content_type: str = "application/json"):
        conn = self._acquire()
        try:
            conn.request(
                method, self.url_for(path), body=body,
                headers=self.headers_for(
                    len(body) if body is not None else None,
                    dict(extra or {}), content_type=content_type))
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            try:
                conn.close()
            except OSError:
                pass
            raise UpstreamError(ErrorKind.UPSTREAM_NETWORK,
                                f"cannot reach upstream: {exc}") from exc

    def request_json(self, method: str, path: str, payload: dict | None = None,
                     extra_headers: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        conn, response = self._request(method, path, body, extra_headers)
        try:
            raw = response.read()
            headers = dict(response.getheaders())
        finally:
            self._release(conn)
        if response.status >= 400:
            raise self._error_for(response.status, raw, headers)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpstreamError(
                ErrorKind.UPSTREAM_INVALID_RESPONSE,
                f"upstream returned non-JSON body: {exc}",
            ) from exc
        if not isinstance(data, dict):
            raise UpstreamError(ErrorKind.UPSTREAM_INVALID_RESPONSE,
                             "upstream returned a non-object JSON body")
        return response.status, data

    def request_bytes(self, method: str, path: str, body: bytes,
                      headers: dict | None = None) -> tuple[int, dict]:
        """Send opaque bytes (e.g. a multipart upload) and parse the JSON reply.

        The body is not decoded or re-encoded: a multipart upload must reach the
        provider exactly as the caller framed it.
        """
        content_type = ""
        send_headers = dict(headers or {})
        for key in list(send_headers):
            if key.lower() == "content-type":
                content_type = send_headers.pop(key)
        conn, response = self._request(method, path, body, send_headers,
                                       content_type=content_type or "application/json")
        try:
            raw = response.read()
            response_headers = dict(response.getheaders())
        finally:
            self._release(conn)
        if response.status >= 400:
            raise self._error_for(response.status, raw, response_headers)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UpstreamError(ErrorKind.UPSTREAM_INVALID_RESPONSE,
                                f"upstream returned non-JSON body: {exc}") from exc
        return response.status, data if isinstance(data, dict) else {"data": data}

    def send(self, payload: dict, path: str = "/v1/chat/completions") -> UpstreamResponse:
        attempts = 0
        while True:
            try:
                status, data = self.request_json("POST", path, payload)
                return UpstreamResponse(status=status, data=data)
            except ProxyError as exc:
                attempts += 1
                if self._retryable(exc) and self.rotate_key() and attempts < len(self._keys):
                    continue
                raise

    def stream(self, payload: dict, path: str = "/v1/chat/completions") -> "UpstreamStream":
        """Start a streaming request.

        Returns a handle rather than a bare generator so the caller can *abort*
        it. Aborting matters for cost: if the client hangs up, closing our socket
        is the only thing that tells the provider to stop generating — otherwise
        it keeps producing (and billing) tokens nobody will read.
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn, response = self._request("POST", path, body, None)
        if response.status >= 400:
            try:
                raw = response.read()
                headers = dict(response.getheaders())
            finally:
                conn.close()
            raise self._error_for(response.status, raw, headers)
        return UpstreamStream(conn, response)

    # ---- error mapping ------------------------------------------------
    @staticmethod
    def _retryable(exc: ProxyError) -> bool:
        """Whether switching keys could plausibly help.

        ``quota_exhausted`` is deliberately absent: every key on the same account
        shares the balance, so rotating produces the same answer for more money.
        ``auth`` is included because rotating *does* change the credential.
        """
        return exc.kind in (ErrorKind.UPSTREAM_AUTH, ErrorKind.UPSTREAM_CAPACITY,
                            ErrorKind.UPSTREAM_RATE_LIMITED, ErrorKind.UPSTREAM_OVERLOADED,
                            ErrorKind.UPSTREAM_NETWORK)

    @staticmethod
    def _error_for(status: int, raw: bytes, headers: dict) -> UpstreamError:
        """Map an upstream failure to an actionable kind.

        ``upstream.capacity`` used to cover every 429 and every 5xx, which hid the
        one distinction an operator needs: *wait* or *top up*. A 429 with
        ``Retry-After`` is a pacing instruction (wait); a 429 whose body says the
        quota is gone is terminal (act). Treating both the same way means either
        retrying into a wall or giving up on a request that would have succeeded.
        """
        snippet = raw[:300].decode("utf-8", "replace")
        lowered_headers = {k.lower(): v for k, v in (headers or {}).items()}
        retry_after = lowered_headers.get("retry-after", "").strip()

        if _looks_like_edge_block(status, raw, headers):
            return UpstreamError(ErrorKind.UPSTREAM_EDGE_BLOCKED,
                                 f"upstream edge/WAF rejected the request ({status})",
                                 status=status)
        if status in (401, 403):
            return UpstreamError(ErrorKind.UPSTREAM_AUTH,
                                 "upstream rejected the credential", status=status)
        if status == 404:
            return UpstreamError(ErrorKind.UPSTREAM_MODEL_NOT_FOUND,
                                 f"upstream reports no such model: {snippet}", status=status)
        if status == 402 or _looks_like_quota_exhausted(raw):
            return UpstreamError(
                ErrorKind.UPSTREAM_QUOTA_EXHAUSTED,
                f"upstream quota or credit is exhausted; retrying will not help: {snippet}",
                status=status)
        if status == 429:
            if retry_after:
                return UpstreamError(
                    ErrorKind.UPSTREAM_RATE_LIMITED,
                    f"upstream asked us to wait {retry_after}s before retrying",
                    status=status)
            return UpstreamError(ErrorKind.UPSTREAM_RATE_LIMITED,
                                 f"upstream is rate limiting this client ({status})",
                                 status=status)
        if status >= 500:
            return UpstreamError(ErrorKind.UPSTREAM_OVERLOADED,
                                 f"upstream is unwell ({status}): {snippet}", status=status)
        if status in (408, 504):
            return UpstreamError(ErrorKind.UPSTREAM_TIMEOUT,
                                 "upstream timed out", status=status)
        return UpstreamError(ErrorKind.UPSTREAM_INVALID_RESPONSE,
                             f"upstream error {status}: {snippet}", status=status)
