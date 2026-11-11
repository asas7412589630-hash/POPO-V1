"""Deployment helpers: systemd unit generation and tunnel URL discovery.

Both exist because the failure they prevent is silent:

*A unit that embeds the API key* leaks it into `/etc/systemd/system`, which is
world-readable on many systems, and into shell history when someone copies the
generated text around. The unit therefore references an ``EnvironmentFile`` and
refuses to inline a credential.

*A dead tunnel URL pasted into a client* looks exactly like a broken proxy.
Discovery therefore validates each candidate and returns an explicit "none"
rather than guessing.
"""

from __future__ import annotations
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

# Only these suffixes are treated as tunnel URLs. A typo in a log file must not
# become a published address.
TRUSTED_SUFFIXES = ("srv.us", "trycloudflare.com", "ngrok-free.app",
                    "ngrok.io", "loca.lt", "bore.pub")
URL_PATTERN = re.compile(r"https://[A-Za-z0-9.\-]+(?::\d+)?")


@dataclass
class TunnelCandidate:
    url: str
    source: str
    trusted: bool

    def as_dict(self) -> dict:
        return {"url": self.url, "source": self.source, "trusted": self.trusted}


def discover_tunnel(candidates: list[tuple[str, str]]) -> TunnelCandidate | None:
    """Pick the first trusted candidate.

    ``candidates`` is ``(text, source)`` pairs from whatever the operator
    configured: a log file, a URL file written by the tunnel script, an env var.
    The order is the caller's priority order.
    """
    for text, source in candidates:
        found = _first_trusted_url(text or "")
        if found:
            return TunnelCandidate(url=found, source=source, trusted=True)
    return None


def _first_trusted_url(text: str) -> str:
    for match in URL_PATTERN.findall(text):
        host = (urlparse(match).hostname or "").lower()
        if any(host == suffix or host.endswith("." + suffix) for suffix in TRUSTED_SUFFIXES):
            return match.rstrip("/")
    return ""


def is_reachable(url: str, timeout: float = 5.0) -> bool:
    """Best-effort liveness check.

    A 401/403/404 still proves something is answering, which is what the operator
    needs to know; only a connection failure means "do not publish this".
    """
    import http.client
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    connection_class = (http.client.HTTPSConnection if parsed.scheme == "https"
                        else http.client.HTTPConnection)
    connection = connection_class(parsed.hostname,
                                  parsed.port or (443 if parsed.scheme == "https" else 80),
                                  timeout=timeout)
    try:
        connection.request("GET", "/healthz")
        connection.getresponse().read()
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        try:
            connection.close()
        except OSError:
            pass


def systemd_unit(*, workdir: str, python: str = "/usr/bin/env python3",
                 user: str = "", port: int = 8412,
                 env_file: str = "/etc/popo/popo.env",
                 bind: str = "127.0.0.1") -> str:
    """Generate a hardened systemd unit.

    The unit never contains a credential: the key lives in ``env_file`` which the
    operator creates with mode 0600. Everything else here is about limiting the
    damage a bug in the proxy can do.
    """
    escaped = workdir.rstrip("/")
    lines = [
        "[Unit]",
        "Description=POPO armor proxy",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={escaped}",
        f"EnvironmentFile={env_file}",
        f"Environment=POPO_BIND={bind}",
        f"Environment=POPO_PORT={port}",
        f"ExecStart={python} -m popo",
        "Restart=on-failure",
        "RestartSec=5s",
        # A proxy that cannot be killed cleanly is a proxy that loses in-flight
        # streamed answers on every restart.
        "KillSignal=SIGTERM",
        "TimeoutStopSec=20s",
        "",
        "# Hardening: the proxy needs its own data dir and nothing else.",
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        f"ReadWritePaths={escaped}/data {escaped}/logs",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "MemoryDenyWriteExecute=yes",
    ]
    if user:
        lines.insert(lines.index("Restart=on-failure"), f"User={user}")
    lines += ["", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines)


def env_file_template() -> str:
    """Content for the ``EnvironmentFile``, with placeholders only."""
    return (
        "# POPO environment. chmod 600 this file; it holds the upstream key.\n"
        "POPO_UPSTREAM_HOST=https://api.example.invalid/v1\n"
        "POPO_UPSTREAM_KEY=sk-replace-me\n"
        "POPO_BIND=127.0.0.1\n"
        "POPO_PORT=8412\n"
        "# POPO_EPHEMERAL=1\n"
    )


def unit_contains_secret(unit_text: str) -> bool:
    """Guard for tests and for the generator itself: no inline credential."""
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        if re.search(r"(KEY|TOKEN|SECRET|PASSWORD)=", stripped, re.I):
            return True
    return False
