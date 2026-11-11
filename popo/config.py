"""POPO configuration — one immutable-ish value object, built once at startup.

Environment is read **only** when :meth:`Config.from_env` runs, never at import
time, so tests and multiple in-process instances stay isolated.

Environment aliases from 1.x (``POPO_*`` and ``ARMOR_*``) are honoured through
``ENV_ALIASES``; adding a setting means adding one entry there and one field
here, which keeps the compatibility surface explicit instead of scattered.
"""

from __future__ import annotations
import os
import socket

# name -> (env aliases in priority order, default)
ENV_ALIASES: dict[str, tuple[tuple[str, ...], object]] = {
    "bind_host": (("POPO_BIND", "ARMOR_BIND"), "127.0.0.1"),
    "bind_port": (("POPO_PORT", "ARMOR_PORT"), 8412),
    "upstream_host": (("POPO_UPSTREAM_HOST", "ARMOR_UPSTREAM_HOST"), ""),
    "upstream_key": (("POPO_UPSTREAM_KEY", "ARMOR_UPSTREAM_KEY"), ""),
    "upstream_timeout": (("POPO_UPSTREAM_TIMEOUT", "ARMOR_UPSTREAM_TIMEOUT"), 90),
    "upstream_connect_timeout": (("POPO_CONNECT_TIMEOUT",), 15),
    "upstream_ua": (("POPO_UPSTREAM_UA", "ARMOR_UPSTREAM_UA"), ""),
    "armor_enabled": (("POPO_ARMOR", "ARMOR_ENABLED"), True),
    # Additional retries after the first upstream call. 8 means at most 9 calls.
    "retry_max": (("POPO_RETRY_MAX",), 8),
    "retry_on_refusal": (("POPO_RETRY_ON_REFUSAL", "ARMOR_RETRY_REFUSAL"), True),
    "retry_budget_seconds": (("POPO_RETRY_BUDGET_S",), 150),
    "scrub_enabled": (("POPO_SCRUB",), True),
    "ephemeral": (("POPO_EPHEMERAL", "ARMOR_EPHEMERAL"), False),
    "data_dir": (("POPO_DATA_DIR", "ARMOR_DATA_DIR"), "data"),
    "require_client_key": (("POPO_REQUIRE_KEY", "ARMOR_REQUIRE_CLIENT_KEY"), True),
    "loopback_trusted": (("POPO_LOOPBACK_TRUSTED",), True),
    "max_body_bytes": (("POPO_MAX_BODY_BYTES", "ARMOR_MAX_BODY_BYTES"), 8 * 1024 * 1024),
    "console_title": (("POPO_CONSOLE_TITLE",), "POPO"),
    "adult_enabled": (("POPO_ADULT_ENABLED",), False),
    "public_base": (("POPO_PUBLIC_BASE", "ARMOR_PUBLIC_BASE"), ""),
    "allow_private_upstream": (("POPO_ALLOW_PRIVATE_UPSTREAM",), False),
    "max_concurrency": (("POPO_MAX_CONCURRENT", "ARMOR_MAX_CONCURRENT"), 12),
    "concurrency_wait_seconds": (("POPO_CONCURRENCY_WAIT_S",), 10),
    # V1-compatible context controls. Compression is loss-averse and enabled;
    # lossy history folding is opt-in.
    "compress_enabled": (("POPO_COMPRESS", "ARMOR_COMPRESS"), True),
    "fold_enabled": (("POPO_FOLD", "ARMOR_FOLD"), False),
    "compress_at_chars": (("POPO_COMPRESS_AT", "ARMOR_COMPRESS_AT"), 55000),
    "compress_target_chars": (("POPO_COMPRESS_TARGET", "ARMOR_COMPRESS_TARGET"), 30000),
    "safe_trim": (("POPO_SAFE_TRIM", "ARMOR_SAFE_TRIM"), 900),
    "fold_at_messages": (("POPO_FOLD_AT", "ARMOR_FOLD_AT"), 14),
    "fold_keep_turns": (("POPO_FOLD_KEEP", "ARMOR_FOLD_KEEP"), 8),
    "advertised_context": (("POPO_ADVERTISED_CONTEXT", "ARMOR_ADVERTISED_CONTEXT"), 200000),
    "state_ledger": (("POPO_STATE_LEDGER", "ARMOR_STATE"), True),
    "archive_enabled": (("POPO_ARCHIVE", "ARMOR_ARCHIVE"), False),
    "artifact_enabled": (("POPO_ARTIFACT", "ARMOR_ARTIFACT"), True),
    "capture_responses": (("POPO_CAPTURE_RESPONSES", "ARMOR_CAPTURE_RESP"), False),
    "capture_full": (("POPO_CAPTURE_FULL", "ARMOR_CAPTURE_FULL"), False),
    "scrub_prose": (("POPO_SCRUB_PROSE", "ARMOR_SCRUB_PROSE"), False),
    # Kept for old deployments, but disabled by default because the old
    # pair-summary compressor drops details that V1's deterministic trim keeps.
    "stateq_enabled": (("POPO_STATEQ",), False),
    "stateq_max_messages": (("POPO_STATEQ_MAX_MESSAGES",), 20),
    "stateq_compress_threshold": (("POPO_STATEQ_THRESHOLD",), 1600),
    "context_handoff_at": (("POPO_CONTEXT_HANDOFF_AT",), 0),
    "context_rotate_at": (("POPO_CONTEXT_ROTATE_AT",), 0),
    "local_admin": (("POPO_LOCAL_ADMIN",), True),
    "login_max_attempts": (("POPO_LOGIN_MAX_ATTEMPTS",), 10),
    "login_window_seconds": (("POPO_LOGIN_WINDOW_S",), 300),
}

_BOOL_FIELDS = {"armor_enabled", "scrub_enabled", "ephemeral", "require_client_key",
                "loopback_trusted", "adult_enabled", "stateq_enabled", "local_admin",
                "allow_private_upstream", "retry_on_refusal", "compress_enabled",
                "fold_enabled", "state_ledger", "archive_enabled", "artifact_enabled",
                "capture_responses", "capture_full", "scrub_prose"}
_INT_FIELDS = {"bind_port", "retry_max", "max_body_bytes", "max_concurrency",
               "stateq_max_messages",
               "stateq_compress_threshold", "login_max_attempts", "login_window_seconds",
               "compress_at_chars", "compress_target_chars", "safe_trim",
               "fold_at_messages", "fold_keep_turns", "advertised_context"}
_INT_FIELDS.update({"context_handoff_at", "context_rotate_at"})
_FLOAT_FIELDS = {"upstream_timeout", "upstream_connect_timeout", "retry_budget_seconds",
                 "concurrency_wait_seconds"}


def _env_bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _read_env(aliases: tuple[str, ...], default: object, field: str) -> object:
    raw = ""
    for name in aliases:
        value = os.environ.get(name, "")
        if value != "":
            raw = value
            break
    if raw == "":
        return default
    if field in _BOOL_FIELDS:
        return _env_bool(raw, bool(default))
    if field in _INT_FIELDS:
        try:
            return int(raw)
        except ValueError:
            return default
    if field in _FLOAT_FIELDS:
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


# Named settings whose 1.x variable means the *opposite* of the value. Keeping
# them in one table makes the inversion explicit instead of hiding a `not` in
# the reader.
INVERTED_ALIASES: dict[str, tuple[str, ...]] = {
    # ARMOR_OFF=1 means armor is off.
    "armor_enabled": ("POPO_ARMOR_OFF", "ARMOR_OFF"),
}


class Config:
    """Runtime configuration for one proxy process."""

    version = "2.7.13"

    __slots__ = tuple(dict.fromkeys(
        list(ENV_ALIASES) + ["upstream_key", "upstream_keys", "published_models",
                             "cors_origins", "armor_levels", "model_profiles"]))

    def __init__(self, **overrides):
        defaults = {name: default for name, (_, default) in ENV_ALIASES.items()}
        values = {**defaults, **overrides}
        for name in ENV_ALIASES:
            setattr(self, name, values[name])
        self.upstream_key = self.parse_keys(values.get("upstream_key", ""))
        self.upstream_keys = list(self.upstream_key)
        published = values.get("published_models")
        self.published_models = [m for m in (published or []) if isinstance(m, str)]
        self.cors_origins = list(values.get("cors_origins") or [])
        self.armor_levels = list(values.get("armor_levels") or ["native", "lean", "off"])
        raw_profiles = values.get("model_profiles") or {}
        self.model_profiles = ({str(model): str(profile)
                                for model, profile in raw_profiles.items()
                                if model and profile}
                               if isinstance(raw_profiles, dict) else {})
        self._normalise()

    def _normalise(self) -> None:
        self.bind_port = int(self.bind_port)
        self.max_body_bytes = int(self.max_body_bytes)
        self.retry_max = max(0, int(self.retry_max))
        self.compress_at_chars = max(0, int(self.compress_at_chars))
        self.compress_target_chars = max(2000, int(self.compress_target_chars))
        self.safe_trim = max(0, int(self.safe_trim))
        self.fold_at_messages = max(4, int(self.fold_at_messages))
        self.fold_keep_turns = max(1, int(self.fold_keep_turns))
        self.upstream_timeout = float(self.upstream_timeout)
        self.upstream_connect_timeout = float(self.upstream_connect_timeout)
        self.retry_budget_seconds = float(self.retry_budget_seconds)
        self.max_concurrency = max(1, int(self.max_concurrency))
        self.concurrency_wait_seconds = max(0.0, float(self.concurrency_wait_seconds))
        if not self.upstream_ua:
            self.upstream_ua = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )

    @staticmethod
    def parse_keys(raw) -> list[str]:
        if isinstance(raw, (list, tuple)):
            return [str(k).strip() for k in raw if str(k).strip()]
        if not raw:
            return []
        return [k.strip() for k in str(raw).replace(";", ",").split(",") if k.strip()]

    @classmethod
    def from_env(cls) -> "Config":
        values = {name: _read_env(aliases, default, name)
                  for name, (aliases, default) in ENV_ALIASES.items()}
        for name, inverted_names in INVERTED_ALIASES.items():
            for env_name in inverted_names:
                raw = os.environ.get(env_name, "")
                if raw != "":
                    # ARMOR_OFF=1 disables armor; ARMOR_OFF=0 keeps the default.
                    values[name] = not _env_bool(raw, False)
                    break
        return cls(**values)

    # ---- persistence ------------------------------------------------
    def load_install_state(self) -> None:
        """Merge operator-supplied install state from ``<data_dir>/popo.json``.

        This reads *configuration*, not runtime state; it is separate from the
        record store so a missing or corrupt file only means "not configured".
        """
        import json
        path = os.path.join(self.data_dir, "popo.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"[POPO] install state ignored ({exc})")
            return
        if not isinstance(state, dict):
            return
        if state.get("upstream_host"):
            self.upstream_host = str(state["upstream_host"])
        if state.get("upstream_key"):
            self.upstream_key = self.parse_keys(state["upstream_key"])
            self.upstream_keys = list(self.upstream_key)
        if isinstance(state.get("published_models"), list):
            self.published_models = [m for m in state["published_models"] if isinstance(m, str)]
        if isinstance(state.get("model_profiles"), dict):
            self.model_profiles = {str(model): str(profile)
                                   for model, profile in state["model_profiles"].items()
                                   if isinstance(model, str) and isinstance(profile, str)}

    def as_dict(self) -> dict:
        """Redacted view, safe to log or return from a management endpoint."""
        return {
            "bind_host": self.bind_host,
            "bind_port": self.bind_port,
            "upstream_host": self.upstream_host,
            "upstream_key": "***" if self.upstream_key else "",
            "armor_enabled": self.armor_enabled,
            "retry_max": self.retry_max,
            "scrub_enabled": self.scrub_enabled,
            "ephemeral": self.ephemeral,
            "data_dir": self.data_dir,
            "require_client_key": self.require_client_key,
            "published_models": list(self.published_models),
            "model_profiles": dict(self.model_profiles),
            "adult_enabled": self.adult_enabled,
        }

    def __repr__(self) -> str:
        return (f"Config(host={self.bind_host!r}, port={self.bind_port}, "
                f"armor={self.armor_enabled}, ephemeral={self.ephemeral}, "
                f"key_required={self.require_client_key})")


def get_config() -> Config:
    config = Config.from_env()
    config.load_install_state()
    return config


def find_free_port(start: int = 19900, end: int = 19990) -> int:
    for port in range(start, end):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.02)
        try:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
        finally:
            sock.close()
    raise RuntimeError("no free loopback port")
