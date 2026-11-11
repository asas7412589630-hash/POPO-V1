"""Outbound destination policy for setup probes.

Why this exists: ``/setup/test`` takes a URL from the request and asks the proxy
to fetch it. Without a destination check, that is a server-side request forgery
primitive — the proxy becomes a way to reach the host's own loopback services,
the LAN, or a cloud metadata endpoint.

Three layers, in order:

1. **Scheme.** Only ``http``/``https``. ``file:``, ``gopher:`` and friends are
   not upstream APIs.
2. **Pre-resolve check.** Reject literal private/loopback/link-local/metadata
   hosts before any DNS traffic.
3. **Post-resolve check.** Resolve the name and reject if *any* returned address
   is private. Checking only the first address is a bypass: a name can return a
   public address first and a loopback address second.

An operator who genuinely runs an upstream on their own LAN can opt in with
``allow_private=True`` (``POPO_ALLOW_PRIVATE_UPSTREAM=1``). That is a deliberate,
recorded decision rather than a silent default.
"""

from __future__ import annotations
import ipaddress
import socket
from urllib.parse import urlparse

# Cloud metadata services and other things nobody should reach through a proxy.
BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "169.254.169.254",
}
BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local + metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

# RFC 2544 benchmarking space. Not an internal network and not routable on the
# public internet — but it IS the default fake-IP pool of Clash/mihomo TUN mode.
# On a machine whose DNS answers every name with a 198.18.x.x fake-IP, treating
# the range as "private" would refuse every legitimate upstream probe
# (measured 2026-11-12: api.example.invalid -> 198.18.0.104). The real egress is
# mediated by the local TUN client, so the guard's SSRF concern does not apply.
FAKEIP_NETWORKS = (
    ipaddress.ip_network("198.18.0.0/15"),
)


class EgressError(ValueError):
    """The requested destination is not allowed."""


def _is_blocked_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True  # unresolvable text is not an address we can vouch for
    if parsed.is_loopback or parsed.is_link_local:
        return True
    if any(parsed in network for network in FAKEIP_NETWORKS):
        return False
    if parsed.is_private:
        return True
    if parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified:
        return True
    return any(parsed in network for network in BLOCKED_NETWORKS)


def validate(url: str, *, allow_private: bool = False) -> str:
    """Return the normalised URL, or raise :class:`EgressError`.

    ``allow_private`` exists for self-hosted providers on a LAN. It must be an
    explicit operator choice, never a default, because the same flag that lets a
    proxy reach Ollama on 127.0.0.1 also lets it reach anything else on the host.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise EgressError(f"unsupported scheme '{parsed.scheme or '(none)'}'; use http or https")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise EgressError("upstream URL has no host")
    if allow_private:
        return url

    if host in BLOCKED_HOSTS:
        raise EgressError(f"'{host}' is a metadata/instance endpoint and is never allowed")
    # A literal IP can be checked without DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_address(str(literal)):
            raise EgressError(
                f"'{host}' is a private/loopback address; set "
                f"POPO_ALLOW_PRIVATE_UPSTREAM=1 if this is a deliberate self-hosted upstream")
        return url

    for address in _resolve(host):
        if _is_blocked_address(address):
            raise EgressError(
                f"'{host}' resolves to the private address {address}; refusing to "
                f"connect (set POPO_ALLOW_PRIVATE_UPSTREAM=1 to override)")
    return url


def _resolve(host: str) -> list[str]:
    """All addresses for a host. An empty list means 'could not resolve'."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return sorted({info[4][0] for info in infos})


def looks_private(host: str) -> bool:
    """Best-effort classification used for messaging, not for enforcement."""
    try:
        return _is_blocked_address(host)
    except (ValueError, TypeError):
        return False
