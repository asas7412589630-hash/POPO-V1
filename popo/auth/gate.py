"""Identity resolution.

Levels, weakest to strongest:

* ``none`` — no evidence at all.
* ``loopback`` — the TCP peer is a loopback address. Local tools send no
  ``Origin`` and no ``Sec-Fetch-*``; a browser always sends at least one. That
  distinction matters: a page on the same device can reach ``127.0.0.1``, so
  loopback alone must not be treated as "the operator".
* ``client-key`` — a valid ``triad-…`` token.
* ``operator`` — the admin credential or a valid owner session cookie.

Caller-written ``Host`` / ``Origin`` / ``X-Forwarded-*`` never raise the level.
"""

from __future__ import annotations
import ipaddress
import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from popo.auth.accounts import Account, AccountStore
from popo.auth.keys import KeyStore
from popo.config import Config
from popo.server.cors import CorsPolicy

BROWSER_MARKERS = ("origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "referer")


@dataclass(frozen=True)
class Identity:
    level: str
    client_key_id: str = ""
    user_id: str = ""
    via: str = ""
    allow_public: bool = False
    external_request: bool = False

    @property
    def is_operator(self) -> bool:
        return self.level == "operator"


class Gate:
    """Resolve an identity from request facts."""

    def __init__(self, config: Config, headers: dict[str, str],
                 client_address: tuple[str, int] | None, *,
                 keystore: KeyStore | None = None,
                 accounts: AccountStore | None = None,
                 admin_hashes: set[str] | None = None,
                 cors_policy: CorsPolicy | None = None):
        self.config = config
        # HTTP field names are case-insensitive. Reverse proxies commonly
        # normalize them to lowercase, so retain one canonical lookup form.
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.client_address = client_address or ("", 0)
        self.keystore = keystore
        self.accounts = accounts
        self.admin_hashes = admin_hashes or set()
        self.cors_policy = cors_policy

    # ---- public API ---------------------------------------------------
    def resolve(self) -> Identity:
        external = self.is_external_request()
        operator = self._operator(external_request=external)
        if operator:
            return Identity(level=operator.level, client_key_id=operator.client_key_id,
                            user_id=operator.user_id, via=operator.via,
                            allow_public=operator.allow_public,
                            external_request=external)
        if self.is_local_admin():
            return Identity(level="operator", via="local",
                            external_request=external)
        client = self._client_key(external_request=external)
        if client:
            return client
        if self.is_local_tool():
            return Identity(level="loopback", via="loopback",
                            external_request=external)
        if self.has_browser_markers():
            return Identity(level="browser", via="browser",
                            external_request=external)
        return Identity(level="none", external_request=external)

    def is_loopback_peer(self) -> bool:
        """Whether the TCP peer is this machine.

        Note what this does **not** prove: a tunnel client (cloudflared, ngrok,
        srv.us) connects to 127.0.0.1 on behalf of a remote stranger, so the peer
        address is loopback while the caller is on the internet. Trusting loopback
        for key-free access while the proxy is published would therefore hand
        every internet user the operator's quota.
        """
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def is_external_request(self) -> bool:
        """Identify Internet ingress without trusting caller-written proxy headers.

        The local API gateway is the only trusted public-tunnel hop and adds its
        fixed marker. Direct requests are external only when their peer address
        is not loopback or a private/link-local network address.
        """
        if (self.is_loopback_peer()
                and self.headers.get("x-forwarded-for", "").strip().lower()
                == "public-tunnel"):
            return True
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return True
        return not (address.is_private or address.is_loopback
                    or address.is_link_local or address.is_reserved)

    def is_local_tool(self) -> bool:
        """Loopback **and** no forwarding markers: a program on this device.

        A tunnel adds forwarding headers (``X-Forwarded-For``, ``CF-Connecting-IP``,
        ``X-Real-IP``) or a browser ``Origin``. Requiring their absence keeps the
        convenience of ``curl localhost:8412`` without extending it to the
        internet.
        """
        if not self.is_loopback_peer():
            return False
        forwarding = ("x-forwarded-for", "x-forwarded-host", "x-real-ip",
                      "cf-connecting-ip", "cf-ray", "true-client-ip")
        if any(marker in self.headers for marker in forwarding):
            return False
        return not self.has_browser_markers()

    def is_local_admin(self) -> bool:
        """Passwordless operator access for a same-origin, loopback-only install.

        A public/tunnel/LAN listener never gets this shortcut. Browser requests
        must prove they target a loopback host from the same origin; tunnel
        forwarding headers invalidate the proof.
        """
        if not getattr(self.config, "local_admin", True):
            return False
        if self.config.bind_host not in ("127.0.0.1", "localhost", "::1"):
            return False
        if not self.is_loopback_peer():
            return False
        forwarding = ("x-forwarded-for", "x-forwarded-host", "x-real-ip",
                      "cf-connecting-ip", "cf-ray", "true-client-ip")
        if any(marker in self.headers for marker in forwarding):
            return False
        if not self.has_browser_markers():
            return False

        host = (self.headers.get("host") or "").strip().lower()
        if not host:
            return False
        try:
            host_name = urlsplit("//" + host).hostname
        except ValueError:
            return False
        if host_name not in ("localhost", "127.0.0.1", "::1"):
            return False
        origin = (self.headers.get("origin") or "").strip()
        if origin:
            try:
                parsed = urlsplit(origin)
                return (parsed.scheme in ("http", "https")
                        and parsed.netloc.lower() == host)
            except ValueError:
                return False
        return self.headers.get("sec-fetch-site", "").lower() == "same-origin"

    def has_browser_markers(self) -> bool:
        return any(marker in self.headers for marker in BROWSER_MARKERS)

    # ---- levels -------------------------------------------------------
    def _operator(self, *, external_request: bool = False) -> Optional[Identity]:
        token = self.presented_token()
        if token and self.admin_hashes:
            digest = hashlib.sha256(token.encode()).hexdigest()
            for known in self.admin_hashes:
                if hmac.compare_digest(known, digest):
                    return Identity(level="operator", via="admin-key")

        # The key issued by first-run setup is the operator's own credential: on a
        # single-operator install the person who configured it is the person who
        # administers it.
        #
        # Scoped deliberately narrow — the label must be ``quick-setup`` AND the
        # install must have no owner account. Once an owner account exists, that
        # account is the only operator path, so a client key handed out earlier
        # cannot be used to administer the proxy. Without the second condition
        # this would be a privilege escalation for every key setup ever issued.
        if (not external_request and token and self.keystore is not None
                and self.accounts is not None):
            has_owner = any(account.role in ("owner", "admin") and not account.disabled
                            for account in self.accounts.list_all())
            if not has_owner:
                record = self.keystore.verify(token)
                if record is not None and getattr(record, "label", "") == "quick-setup":
                    return Identity(level="operator", via="setup-key",
                                    allow_public=record.allow_public)
        sid = self.cookie("triad_sid")
        if sid and self.accounts:
            account = self.accounts.verify_session(sid)
            if account and account.role in ("owner", "admin") and not account.disabled:
                return Identity(level="operator", user_id=account.uid, via="session")
        return None

    def _client_key(self, *, external_request: bool = False) -> Optional[Identity]:
        token = self.bearer_token() or self.x_api_key()
        if not token or not self.keystore:
            return None
        record = self.keystore.verify(token)
        if not record:
            return None
        try:
            self.keystore.touch(record)
        except OSError:
            pass
        return Identity(level="client-key", client_key_id=record.key_id,
                        user_id=record.owner_id, via="client-key",
                        allow_public=record.allow_public,
                        external_request=external_request)

    # ---- credential extraction ----------------------------------------
    def bearer_token(self) -> str:
        auth = self.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def x_api_key(self) -> str:
        """Anthropic clients send ``x-api-key`` instead of a bearer token."""
        return (self.headers.get("x-api-key") or "").strip()

    def presented_token(self) -> str:
        return self.bearer_token() or self.x_api_key()

    def cookie(self, name: str) -> str:
        raw = self.headers.get("cookie", "")
        for part in (raw or "").split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key.strip() == name:
                return value.strip()
        return ""
