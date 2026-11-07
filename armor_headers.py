#!/usr/bin/env python3
"""POPO-V1 Header Normalization & Edge Defense Utilities (armor_headers).

Provides header sanitization for upstream WAF bypass, edge block detection
(Cloudflare / Akamai challenges), client IP resolution through reverse proxies,
and request ID generation.

Standard library only. No third-party dependencies.
"""

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

_UPSTREAM_UA = (os.environ.get("ARMOR_UPSTREAM_UA") or "").strip()
_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_BOT_UA_RE = re.compile(
    r"^(python-?(urllib|requests|httpx)|curl/|wget/|java/|go-http-client)",
    re.I
)


def _normalise_upstream_headers(headers: dict) -> None:
    """Make the outbound request look like an ordinary client to the edge.

    1. User-Agent: If ARMOR_UPSTREAM_UA is set, it wins. Otherwise, common bot
       signatures (python-urllib, curl, Go) are rewritten to a standard browser UA.
    2. Accept-Language: Injected if absent to score favorably on WAF heuristics.
    3. Accept: Defaulted to JSON / SSE if missing.
    """
    try:
        cur = ""
        for k in list(headers.keys()):
            if k.lower() == "user-agent":
                cur = str(headers.pop(k) or "")
        if _UPSTREAM_UA:
            headers["User-Agent"] = _UPSTREAM_UA
        elif not cur or _BOT_UA_RE.match(cur.strip()):
            headers["User-Agent"] = _DEFAULT_UA
        else:
            headers["User-Agent"] = cur

        if not any(k.lower() == "accept-language" for k in headers):
            headers["Accept-Language"] = "en-US,en;q=0.9,zh-TW;q=0.8"
        if not any(k.lower() == "accept" for k in headers):
            headers["Accept"] = "application/json, text/event-stream, */*"
    except Exception:
        # Header normalization must never break request forwarding
        pass


def _looks_like_edge_block(status: int, hdrs: Any, body: bytes) -> bool:
    """Detect whether a response is an edge/WAF challenge rather than an API error.

    Cloudflare/WAF challenges typically arrive as 403, 429, or 503 containing
    HTML or cf- markers rather than model provider JSON errors.
    """
    if status not in (403, 429, 503):
        return False
    try:
        for k in ("cf-mitigated", "cf-ray", "x-served-by", "server"):
            v = (hdrs.get(k) or "") if hasattr(hdrs, "get") else ""
            if k == "cf-mitigated" and v:
                return True
        low = (body or b"")[:2000].lower()
        if b"<html" in low and any(t in low for t in (b"cloudflare", b"attention required",
                                                      b"access denied", b"captcha")):
            return True
        if b"cf-error" in low or b"ray id" in low:
            return True
    except Exception:
        return False
    return False


def _forwarded_client(hdrs: Any) -> str:
    """Extract real client IP when sitting behind a reverse proxy or tunnel."""
    if not hasattr(hdrs, "get"):
        return ""
    for h in ("CF-Connecting-IP", "cf-connecting-ip",
              "X-Real-IP", "x-real-ip",
              "X-Forwarded-For", "x-forwarded-for"):
        v = (hdrs.get(h) or "").strip()
        if v:
            return v.split(",")[0].strip()
    return ""


def _ip_from_headers(hdrs: Any) -> str:
    """Extract first IP from X-Forwarded-For header."""
    if not hasattr(hdrs, "get"):
        return ""
    xf = (hdrs.get("X-Forwarded-For") or hdrs.get("x-forwarded-for") or "").strip()
    if xf:
        return xf.split(",")[0].strip()
    return ""


def _is_loopback(addr: str) -> bool:
    """Check if an IP address string represents localhost/loopback."""
    a = (addr or "").strip()
    return a.startswith("127.") or a in ("::1", "localhost", "")


def _rid_now() -> str:
    """Generate monotonic millisecond Request ID string."""
    return str(int(time.time() * 1000))
