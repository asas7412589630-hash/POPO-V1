"""Per-request dependencies.

The HTTP handler must not own configuration, stores or identity logic. This
module assembles them and caches instances on the handler class, because
``BaseHTTPRequestHandler`` is instantiated per request by ``HTTPServer``.

``ApiHandler.config`` / ``.records`` / ``.keystore`` remain the public seam used
by tests together with ``reset_shared_state()``.
"""

from __future__ import annotations
import hashlib
import os
import secrets

from popo.auth.accounts import AccountStore
from popo.auth.bootstrap import BootstrapToken
from popo.auth.gate import Gate
from popo.auth.keys import KeyStore
from popo.auth.limiter import AttemptLimiter
from popo.server.limits import ConcurrencyLimit
from popo.config import Config
from popo.server.cors import CorsPolicy
from popo.storage.ephemeral import EphemeralGate
from popo.storage.records import RecordStore


def _cls(handler):
    """The handler class, whether given an instance or the class itself.

    ``serve()`` needs to prepare state before any request exists, so every helper
    here must accept a class. Wrapping this in one place beats ``type(x) if not
    isinstance(x, type)`` scattered through the module.
    """
    return handler if isinstance(handler, type) else type(handler)


def config_of(handler) -> Config:
    return _cls(handler).config


def records_of(handler) -> RecordStore:
    cls = _cls(handler)
    if cls.records is None:
        cls.records = RecordStore(cls.config.data_dir, EphemeralGate(cls.config))
    return cls.records


def keystore_of(handler) -> KeyStore:
    cls = _cls(handler)
    if cls.keystore is None:
        cls.keystore = KeyStore(cls.config.data_dir, EphemeralGate(cls.config))
        bootstrap_admin_key(handler)
    return cls.keystore


def accounts_of(handler) -> AccountStore:
    cls = _cls(handler)
    if cls.accounts is None:
        cls.accounts = AccountStore(cls.config.data_dir, EphemeralGate(cls.config))
        # No automatic owner account. Its password went to the log where nobody
        # read it, and its existence blocked the setup-issued key from acting as
        # the operator — a dead end for a single-operator install. An account is
        # created deliberately from the console when someone wants one.
    return cls.accounts


def bootstrap_of(handler) -> BootstrapToken:
    """First-run token: issued once at startup, printed on the device, dies on use.

    Issuing it lazily (on first setup call) meant the operator opened the console,
    was asked for a token, and had no way to obtain it — the token appeared only
    after the request that needed it. It is now issued during startup, before any
    browser can connect.
    """
    cls = _cls(handler)
    token = getattr(cls, "_bootstrap_token", None)
    if token is None:
        token = BootstrapToken(cls.config.data_dir, ephemeral=cls.config.ephemeral)
        cls._bootstrap_token = token
        if not token.active and not _has_client_keys(handler):
            _announce_token(token, handler)
    return token


def _has_client_keys(handler) -> bool:
    """True when a real client key exists.

    The admin key is registered into the keystore at bootstrap, so "the keystore is
    non-empty" is always true and cannot answer this question. What matters is
    whether a *client-facing* key exists, because that is what makes the install
    usable by a client.
    """
    store = keystore_of(handler)
    return any(record.key_id != "admin" for record in store.list_all())


def may_read_bootstrap(handler) -> bool:
    """Whether this caller may be handed the first-run token.

    The gate is the *combination* — loopback peer, no browser markers, no client
    key yet. Each alone is insufficient: a page on the same device reaches
    loopback, and a remote terminal still should not receive a credential.
    """
    if _has_client_keys(handler):
        return False
    if handler.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return False
    browser_markers = ("Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Referer")
    return not any(marker in handler.headers for marker in browser_markers)


def ensure_bootstrap(handler) -> str:
    """Return a usable first-run token in plaintext, or "".

    Only callable from loopback without browser markers: it hands back a
    credential, so a hostile page must not be able to read it.

    The plaintext is kept in memory for the lifetime of the issued token because
    ``BootstrapToken`` stores only a hash — by design, so a leaked file is not a
    leaked credential. Re-issuing on every call would invalidate the token the
    operator just copied, so the in-memory copy is the one returned.
    """
    if not may_read_bootstrap(handler):
        return ""
    token = bootstrap_of(handler)
    plaintext = getattr(_cls(handler), "_bootstrap_plaintext", "")
    if token.active and plaintext:
        return plaintext
    issued = token.issue()
    _cls(handler)._bootstrap_plaintext = issued
    return issued


def _announce_token(token: BootstrapToken, handler) -> str:
    plaintext = token.issue()
    # Remember the plaintext so a later /setup/token call hands back the same
    # token instead of issuing a new one that invalidates what was just printed.
    _cls(handler)._bootstrap_plaintext = plaintext
    # flush=True matters: redirected stdout is block-buffered, so under nohup the
    # token sat in the buffer and the log looked empty exactly when someone
    # needed to read it.
    import sys as _sys
    banner = [
        "=" * 58,
        "first-run setup token (paste it into the console):",
        f"  {plaintext}",
        "expires in 30 minutes, works once.",
        "=" * 58,
    ]
    for line in banner:
        print(f"[POPO] {line}", flush=True)


def sources_of(handler):
    """Return the model→source picker for this server.

    A configured multi-source catalog wins; otherwise the single-upstream factory
    is used unchanged. Keyed on config identity, not merely "first time": tests
    and embedded servers swap the config, and a picker still bound to the
    previous one would silently send requests to the old upstream.
    """
    from popo.server.inference import SourceFactory
    from popo.server.sources import build_router
    cls = _cls(handler)
    cached = getattr(cls, "_source_factory", None)
    if cached is not None and getattr(cached, "_popo_config", None) is cls.config:
        return cached
    router = build_router(cls.config, cls.config.data_dir)
    picker = router if router is not None else SourceFactory(cls.config)
    picker._popo_config = cls.config
    cls._source_factory = picker
    return picker


def concurrency_of(handler) -> ConcurrencyLimit:
    """One limiter per process: the thing being bounded is this listener."""
    cls = _cls(handler)
    limiter = getattr(cls, "_concurrency", None)
    if limiter is None or limiter.limit != cls.config.max_concurrency:
        limiter = ConcurrencyLimit(cls.config.max_concurrency,
                                   cls.config.concurrency_wait_seconds)
        cls._concurrency = limiter
    return limiter


def login_limiter_of(handler) -> AttemptLimiter:
    return AttemptLimiter(max_attempts=handler.config.login_max_attempts,
                          window_seconds=handler.config.login_window_seconds)


def gate_of(handler) -> Gate:
    return Gate(
        _cls(handler).config,
        dict(handler.headers.items()),
        handler.client_address,
        keystore=keystore_of(handler),
        accounts=accounts_of(handler),
        admin_hashes=_cls(handler).admin_hashes,
        cors_policy=cors_of(handler),
    )


def cors_of(handler) -> CorsPolicy:
    cls = _cls(handler)
    cfg = cls.config
    key = (cfg.bind_host, cfg.bind_port, cfg.public_base, tuple(cfg.cors_origins))
    policy = getattr(cls, "_cors_policy", None)
    if policy is None or getattr(cls, "_cors_key", None) != key:
        policy = CorsPolicy(bind_host=cfg.bind_host, bind_port=cfg.bind_port,
                            public_base=cfg.public_base,
                            extra_origins=list(cfg.cors_origins))
        cls._cors_policy = policy
        cls._cors_key = key
    return policy


def bootstrap_admin_key(handler) -> None:
    """Make the operator admin token usable as a client key too.

    One credential for one operator is the 1.x behaviour. Issuing a second,
    separate token would be a product decision — and a silent one.
    """
    cls = _cls(handler)
    if cls.admin_hashes:
        return
    token = os.environ.get("POPO_ADMIN_KEY", "").strip()
    if not token:
        token = _load_or_create_admin_key(cls.config)
    if not token:
        return
    cls.keystore.activate_admin(token)
    cls.admin_hashes.add(hashlib.sha256(token.encode()).hexdigest())


def _load_or_create_admin_key(config: Config) -> str:
    if config.ephemeral:
        print("[POPO] ephemeral mode: admin key kept in memory only")
        return "triad-admin-" + secrets.token_urlsafe(32)
    path = os.path.join(config.data_dir, "admin_key.txt")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                existing = handle.read().strip()
            if existing:
                return existing
    except OSError:
        pass
    token = "triad-admin-" + secrets.token_urlsafe(32)
    try:
        os.makedirs(config.data_dir, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        print(f"[POPO] admin key written to {path} (mode 0600)")
    except OSError as exc:
        print(f"[POPO] could not persist admin key: {exc}")
    return token
