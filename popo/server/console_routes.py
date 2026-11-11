"""Console compatibility endpoints.

These three routes answer the original console's questions — what addresses this
install has, what the last requests were, and how to clear a tripped breaker.
They live apart from ``endpoints.py`` so that module stays within the size
budget, and because they exist for one client's shape rather than the product's.
"""

from __future__ import annotations
import ipaddress
import os
import socket
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import parse_qs, urlsplit

from popo.errors import ErrorKind, ProxyError


class ConsoleRoutes:
    """Mixed into the handler next to the other route groups."""

    def _route_logs(self, body, parsed, identity) -> None:
        """Recent request log rows for the console's 紀錄 card."""
        from urllib.parse import parse_qs
        query = parse_qs(parsed.query)
        try:
            limit = max(1, min(int((query.get("limit") or ["40"])[0]), 200))
        except ValueError:
            limit = 40
        rows = self._records().recent_requests(limit)
        self._writer().json(200, {"entries": rows, "limit": limit})

    def _route_access(self, body, parsed, identity) -> None:
        """Every address this install can be reached by, with liveness.

        The console renders this list before handing out a URL, because a dead
        address pasted into a client looks exactly like a broken proxy. An address
        is only marked reachable when something actually answered on it.
        """
        port = self.cfg.bind_port
        addresses = [{
            "kind": "loopback",
            "label": "本機",
            "base_url": f"http://127.0.0.1:{port}/v1",
            "reachable": True,
            "note": "只有這台裝置能用",
        }]

        # LAN address, only when the server is actually bound beyond loopback.
        if self.cfg.bind_host not in ("127.0.0.1", "localhost"):
            lan_ip = _primary_ip()
            if lan_ip:
                addresses.append({
                    "kind": "lan", "label": "區網",
                    "base_url": f"http://{lan_ip}:{port}/v1",
                    "reachable": True,
                    "note": "同一個 Wi-Fi 下的裝置能用",
                })

        # A tunnel URL counts only if it was configured AND answered a moment ago.
        configured_public = (self.cfg.public_base or "").strip().rstrip("/")
        tunnel_file = os.path.join(self.cfg.data_dir, "tunnel_url.txt")
        if not configured_public and os.path.exists(tunnel_file):
            try:
                with open(tunnel_file, "r", encoding="utf-8") as handle:
                    configured_public = handle.read().strip().rstrip("/")
            except OSError:
                configured_public = ""
        # A locally reachable URL is not necessarily public. Reject loopback,
        # LAN, link-local and split-DNS names before treating it as an external
        # client address; otherwise a public-scoped key can be handed out with
        # a URL that only works on this machine or network.
        public = configured_public if _is_public_base(configured_public) else ""
        public_live = _probe_public_base(public) if public else False
        if public:
            addresses.append({
                "kind": "public", "label": "公開",
                "base_url": f"{public}/v1",
                "reachable": public_live,
                "note": ("已驗證，任何網路的裝置都能連線"
                         if public_live else "網址已設定，但目前無法連線"),
            })

        self._writer().json(200, {
            "bind": f"{self.cfg.bind_host}:{self.cfg.bind_port}",
            "addresses": addresses,
            "public_base": public if public_live else "",
            "configured_public_base": public,
            "public_configured": bool(configured_public),
            "tunnel_ok": public_live if public else False,
            "published": public_live,
            "require_client_key": self.cfg.require_client_key,
            "local_trust": "local tools only (loopback without forwarding headers)",
            "reachable": bool(addresses),
        })
    def _route_sources_reset(self, body, parsed, identity) -> None:
        """Clear a source's failure count after the operator fixed it."""
        name = parsed.path.rsplit("/", 2)[-2] if "/reset" in parsed.path else ""
        picker = deps.sources_of(self)
        mark_success = getattr(picker, "mark_success", None)
        if mark_success is None:
            raise ProxyError(ErrorKind.PROXY_NOT_AVAILABLE,
                             "this install uses a single upstream; no breaker to reset")
        if not name:
            raise ProxyError(ErrorKind.BAD_REQUEST, "source name is required")
        mark_success(name)
        self._writer().json(200, {"ok": True, "source": name})


    def _route_setup_verify_key(self, body, parsed, identity) -> None:
        """Check the key and list models without publishing anything.

        The 2.5.4 console asks this before the operator commits, so it must not
        write install state — verifying a key is not configuring an install.
        """
        service = self._setup_service()
        service.caller_is_local = self._gate().resolve().level in ("operator", "loopback")
        status, payload = service.test(body)
        self._writer().json(status, payload)


def _probe_public_base(base: str) -> bool:
    """Only offer a public client URL after its health endpoint answers."""
    if not base.startswith(("https://", "http://")):
        return False


def _is_public_base(base: str) -> bool:
    """Reject internal or malformed URLs before publishing them to clients."""
    try:
        parsed = urlsplit(base)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            return False
        host = parsed.hostname.rstrip(".").lower()
        if (host in ("localhost",) or host.endswith((".localhost", ".local",
                                                       ".internal", ".lan", ".home"))):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(row[4][0].split("%", 1)[0])
                         for row in rows}
            return bool(addresses) and all(item.is_global for item in addresses)
        return address.is_global
    except (OSError, TypeError, ValueError):
        return False
    try:
        with urlopen(base.rstrip("/") + "/healthz", timeout=2.5) as response:
            return response.status == 200
    except (OSError, URLError, ValueError):
        return False


def _degraded(config) -> list[str]:
    """Capabilities this install cannot use, named rather than silently absent."""
    missing: list[str] = []
    if not config.upstream_host:
        missing.append("未設定上游")
    if not config.upstream_key:
        missing.append("未設定上游金鑰")
    if config.ephemeral:
        missing.append("零落地模式（紀錄停用）")
    return missing

def _primary_ip() -> str:
    """The address other machines on this network would use.

    Determined by asking the kernel which interface it would route through; no
    packet is sent. Returns "" when there is no route, which is the honest answer
    for a device with no network.
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()
