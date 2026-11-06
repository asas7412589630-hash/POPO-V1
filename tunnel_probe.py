#!/usr/bin/env python3
"""tunnel_probe — is the published quick-tunnel URL still the live one?

WHY this exists: a quick tunnel is ephemeral. cloudflared gets a new hostname on
every start, but `tunnel_url.txt` keeps the old one until something rewrites it.
The console then shows a URL that no longer resolves, the operator copies it into
a client, and the failure ("Service temporarily unavailable") looks like a proxy
bug rather than an expired hostname.

So the URL is verified, not trusted: DNS + /healthz, cached briefly so a page
refresh does not hammer the tunnel.
"""
import os
import re
import socket
import threading
import time
import urllib.request

URL_FILE_NAME = "tunnel_url.txt"
CACHE_TTL = float(os.environ.get("ARMOR_TUNNEL_PROBE_TTL", "60"))
TIMEOUT = float(os.environ.get("ARMOR_TUNNEL_PROBE_TIMEOUT", "5"))

_cache = {"key": "", "ts": 0.0, "ok": False, "note": ""}
_lock = threading.Lock()

_PAT = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$")


def published_url(base: str) -> str:
    """The URL recorded by the tunnel launcher, or "" when absent/invalid."""
    try:
        with open(os.path.join(base, URL_FILE_NAME), "r", encoding="utf-8") as f:
            cand = f.read().strip().rstrip("/")
    except OSError:
        return ""
    return cand if _PAT.match(cand) else ""


def _dns_ok(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except Exception:
        return False


def probe(url: str) -> tuple:
    """(ok, note). Never raises: a probe failure must not break /access."""
    if not url:
        return False, "no url published"
    host = url.split("://", 1)[-1].split("/", 1)[0]
    if not _dns_ok(host):
        return False, "hostname does not resolve (tunnel likely restarted)"
    try:
        req = urllib.request.Request(url + "/healthz", headers={"User-Agent": "armor-probe"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(200)
            if r.status == 200 and b'"ok"' in body:
                return True, "verified"
            return False, "unexpected reply from tunnel"
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def status(base: str) -> dict:
    """Cached view of the published URL and whether it still answers."""
    url = published_url(base)
    now = time.time()
    with _lock:
        if url and _cache["key"] == url and now - _cache["ts"] < CACHE_TTL:
            return {"url": url, "ok": _cache["ok"], "note": _cache["note"],
                    "checked_ago_s": round(now - _cache["ts"], 1)}
    ok, note = probe(url)
    with _lock:
        _cache.update({"key": url, "ts": now, "ok": ok, "note": note})
    return {"url": url, "ok": ok, "note": note, "checked_ago_s": 0.0}


def invalidate() -> None:
    with _lock:
        _cache.update({"key": "", "ts": 0.0, "ok": False, "note": ""})
