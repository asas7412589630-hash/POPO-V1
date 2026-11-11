"""Multiple upstream sources, with per-model routing.

Why this exists: one proxy often fronts more than one provider — a fast model
here, a long-context model there, a local Ollama for drafts. V1 supported that
with a mutable JSON registry read through a cache with request-scoped
invalidation. That worked, but the invalidation logic was where its bugs lived:
publishing had to remember to clear a memo or the wizard saw stale state.

This module keeps the capability and drops the invalidation problem by making
the state explicit and immutable: a :class:`SourceCatalog` is a value; publishing
returns a new one and the caller swaps it. Nothing to invalidate.

Two rules carried over from V1 because they were learned the hard way:

* **Secrets never live in the registry.** The source list is safe to show; keys
  live in a separate file with 0600 permissions.
* **Publishing never deletes a bare alias.** V1 shipped a publish that removed
  every alias pointing at a source, which silently broke clients using
  ``model="glm-flash"``. Names a client already sends are data, not a derived
  view — this module only manages the namespaced alias it created.
"""

from __future__ import annotations
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

from popo.core.upstream import UpstreamSource
from popo.errors import ErrorKind, ProxyError

SOURCE_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


@dataclass(frozen=True)
class Source:
    """One upstream provider."""

    id: str
    name: str
    base_url: str

    @staticmethod
    def build(source_id: str, name: str, base_url: str) -> "Source":
        source_id = (source_id or "").strip().lower()
        name = (name or "").strip()
        base = (base_url or "").strip().rstrip("/")
        if not SOURCE_ID_RE.match(source_id):
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             "source id must be 1-40 chars of a-z, 0-9, _ or -")
        if not name:
            raise ProxyError(ErrorKind.BAD_REQUEST, "source name is required")
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             "source base_url must be a clean http(s) URL")
        if parsed.username or parsed.password:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             "base_url must not embed credentials; use the key field")
        if parsed.query or parsed.fragment:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             "base_url must not carry a query or fragment")
        return Source(id=source_id, name=name, base_url=base)


@dataclass(frozen=True)
class Route:
    """Where a model name goes, and which armour profile it uses."""

    source_id: str
    model_id: str
    base_url: str
    profile: str = ""
    namespaced: bool = False


@dataclass(frozen=True)
class SourceCatalog:
    """Sources plus the model→route table. Immutable; publishing returns a new one."""

    sources: dict[str, Source] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)

    # ---- lookup ------------------------------------------------------
    def route_for(self, model: str) -> Route | None:
        return self.routes.get(model)

    def source(self, source_id: str) -> Source | None:
        return self.sources.get(source_id)

    def models_of(self, source_id: str) -> list[str]:
        return sorted({route.model_id for route in self.routes.values()
                       if route.source_id == source_id and route.namespaced})

    def published_models(self) -> list[str]:
        return sorted(self.routes)

    # ---- mutation (returns a new catalog) -----------------------------
    def add_source(self, source: Source) -> "SourceCatalog":
        """Add or replace a source.

        Replacing by *base_url* rather than only by id prevents the duplicate
        that V1 measured: typing ``https://x`` and ``https://x/v1`` produced two
        ids and two independent route sets, so every model appeared twice in the
        client's picker.
        """
        normalised = source.base_url.rstrip("/").lower()
        kept = {sid: existing for sid, existing in self.sources.items()
                if sid != source.id
                and existing.base_url.rstrip("/").lower() != normalised}
        kept[source.id] = source
        # Drop routes that pointed at a source which no longer exists.
        routes = {model: route for model, route in self.routes.items()
                  if route.source_id in kept}
        return SourceCatalog(sources=kept, routes=routes)

    def publish(self, source_id: str, model_ids: list[str],
                profiles: dict[str, str] | None = None) -> "SourceCatalog":
        """Enable models from one source under namespaced (and free bare) aliases."""
        source = self.sources.get(source_id)
        if source is None:
            raise ProxyError(ErrorKind.BAD_REQUEST, f"unknown source '{source_id}'")
        profiles = profiles or {}
        routes = dict(self.routes)
        prefix = f"{source_id}/"

        # Remove only the aliases this function owns: the namespaced ones.
        for model in [name for name, route in routes.items()
                      if route.namespaced and route.source_id == source_id]:
            routes.pop(model, None)

        for model_id in sorted(set(str(m) for m in model_ids if m)):
            route = Route(source_id=source_id, model_id=model_id,
                          base_url=source.base_url,
                          profile=profiles.get(model_id, ""), namespaced=True)
            routes[prefix + model_id] = route
            # A bare name lets a client send model="glm-5.3". Only when free:
            # shadowing another source's model would silently reroute traffic.
            if model_id not in routes:
                routes[model_id] = Route(source_id=source_id, model_id=model_id,
                                         base_url=source.base_url,
                                         profile=profiles.get(model_id, ""),
                                         namespaced=False)
        return SourceCatalog(sources=dict(self.sources), routes=routes)


class SourceRouter:
    """Callable source picker backed by a catalog.

    Implements the protocol the pipeline already uses
    (``picker(model) -> UpstreamSource``), so nothing in the request path had to
    change to gain multi-source support.

    **Stickiness.** When a bare alias exists on several sources, the choice must
    not flip between requests of the same conversation: source A does not know
    what source B was told, so switching mid-conversation hands the model an
    unrelated history. The first source chosen for a session is remembered until
    the session ends or that source is marked unhealthy.
    """

    def __init__(self, catalog: SourceCatalog, secrets: dict[str, list[str]],
                 *, timeout: float = 90.0, connect_timeout: float = 15.0,
                 user_agent: str = "", default_keys: list[str] | None = None,
                 default_profiles: dict[str, str] | None = None,
                 sticky_capacity: int = 4096):
        self.catalog = catalog
        self.secrets = secrets
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.user_agent = user_agent
        self.default_keys = list(default_keys or [])
        self.default_profiles = dict(default_profiles or {})
        self._cache: dict[tuple, UpstreamSource] = {}
        self._sticky: dict[str, str] = {}
        self._sticky_capacity = max(16, sticky_capacity)
        self._lock = threading.Lock()
        # Consecutive failures per source, and a cooldown deadline. Without this
        # every request re-probes a source that is already known to be down.
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self.failure_threshold = 3
        self.cooldown_seconds = 30.0

    def bind_session(self, session: str, model: str) -> None:
        """Record which source served a session, so later turns stay there."""
        route = self.catalog.route_for(model)
        if route is None or not session:
            return
        with self._lock:
            if len(self._sticky) >= self._sticky_capacity:
                # Bounded memory: drop the oldest quarter rather than one entry,
                # which would make every later insert walk the whole dict.
                for key in list(self._sticky)[: max(1, len(self._sticky) // 4)]:
                    self._sticky.pop(key, None)
            self._sticky[session] = route.source_id

    def sticky_source(self, session: str) -> str:
        with self._lock:
            return self._sticky.get(session, "")

    def profile_for(self, model: str) -> str:
        route = self.catalog.route_for(model)
        return (route.profile if route is not None else "") or self.default_profiles.get(model, "")

    def mark_failure(self, source_id: str) -> None:
        """Record a failed call; trip the breaker after a few in a row."""
        with self._lock:
            count = self._failures.get(source_id, 0) + 1
            self._failures[source_id] = count
            if count >= self.failure_threshold:
                self._cooldown_until[source_id] = time.time() + self.cooldown_seconds

    def mark_success(self, source_id: str) -> None:
        with self._lock:
            self._failures.pop(source_id, None)
            self._cooldown_until.pop(source_id, None)

    def is_cooling(self, source_id: str) -> bool:
        with self._lock:
            until = self._cooldown_until.get(source_id, 0.0)
            if until and time.time() >= until:
                # Cooldown elapsed: allow one probe through, but keep the record
                # so a single success is what clears it.
                return False
            return bool(until)

    def __call__(self, model: str = "") -> UpstreamSource:
        route = self.catalog.route_for(model)
        if route is None:
            raise ProxyError(
                ErrorKind.INVALID_MODEL,
                f"model '{model}' is not routed to any source; "
                f"published: {', '.join(self.catalog.published_models()) or '(none)'}")
        keys = self.secrets.get(route.source_id) or self.default_keys
        if not keys:
            raise ProxyError(ErrorKind.PROXY_CONFIG,
                             f"source '{route.source_id}' has no credential configured")
        signature = (route.base_url, tuple(keys), self.timeout)
        source = self._cache.get(signature)
        if source is None:
            source = UpstreamSource(route.base_url, keys, timeout=self.timeout,
                                    connect_timeout=self.connect_timeout,
                                    user_agent=self.user_agent)
            self._cache[signature] = source
        return source

    def close(self) -> None:
        for source in self._cache.values():
            source.close()
        self._cache.clear()


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

class CatalogStore:
    """Two files: a readable registry and a private secrets file.

    Splitting them is deliberate: the source list can be shown in a UI or pasted
    into a bug report, while the keys cannot.
    """

    REGISTRY = "sources.json"
    SECRETS = "source_secrets.json"

    def __init__(self, data_dir: str, ephemeral: bool = False):
        self.data_dir = data_dir
        self.ephemeral = ephemeral

    @property
    def registry_path(self) -> str:
        return os.path.join(self.data_dir, self.REGISTRY)

    @property
    def secrets_path(self) -> str:
        return os.path.join(self.data_dir, self.SECRETS)

    def _read(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"[POPO] ignoring unreadable {os.path.basename(path)}: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, path: str, payload, mode: int) -> None:
        if self.ephemeral:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".catalog-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except OSError as exc:
            print(f"[POPO] could not persist {os.path.basename(path)}: {exc}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def load(self) -> SourceCatalog:
        raw = self._read(self.registry_path)
        sources: dict[str, Source] = {}
        for row in raw.get("sources", []) if isinstance(raw, dict) else []:
            try:
                source = Source.build(row["id"], row.get("name", ""), row["base_url"])
            except (KeyError, TypeError, ProxyError):
                continue
            sources[source.id] = source
        routes: dict[str, Route] = {}
        for model, row in (raw.get("routes") or {}).items():
            if not isinstance(row, dict) or "source_id" not in row:
                continue
            if row["source_id"] not in sources:
                continue
            routes[model] = Route(
                source_id=row["source_id"],
                model_id=str(row.get("model_id") or model),
                base_url=sources[row["source_id"]].base_url,
                profile=str(row.get("profile") or ""),
                namespaced=bool(row.get("namespaced", "/" in model)),
            )
        return SourceCatalog(sources=sources, routes=routes)

    def save(self, catalog: SourceCatalog) -> None:
        self._write(self.registry_path, {
            "schema": 1,
            "sources": [{"id": s.id, "name": s.name, "base_url": s.base_url}
                        for s in sorted(catalog.sources.values(), key=lambda s: s.id)],
            "routes": {model: {"source_id": route.source_id,
                               "model_id": route.model_id,
                               "profile": route.profile,
                               "namespaced": route.namespaced}
                       for model, route in sorted(catalog.routes.items())},
        }, 0o644)

    def load_secrets(self) -> dict[str, list[str]]:
        raw = self._read(self.secrets_path)
        return {str(sid): [str(k) for k in keys if k]
                for sid, keys in raw.items() if isinstance(keys, list)}

    def save_secret(self, source_id: str, keys: list[str]) -> None:
        secrets = self.load_secrets()
        secrets[source_id] = [k for k in keys if k]
        self._write(self.secrets_path, secrets, 0o600)

    def delete_secret(self, source_id: str) -> None:
        secrets = self.load_secrets()
        secrets.pop(source_id, None)
        self._write(self.secrets_path, secrets, 0o600)


def build_router(config, data_dir: str) -> SourceRouter | None:
    """Return a catalog-backed router, or None when no catalog is configured.

    Returning ``None`` (rather than an empty router) keeps the single-upstream
    path working unchanged for installs that never touch multi-source.
    """
    store = CatalogStore(data_dir, ephemeral=getattr(config, "ephemeral", False))
    catalog = store.load()
    if not catalog.sources:
        return None
    return SourceRouter(catalog, store.load_secrets(),
                        timeout=config.upstream_timeout,
                        connect_timeout=config.upstream_connect_timeout,
                        user_agent=config.upstream_ua,
                        default_keys=list(config.upstream_key or []),
                        default_profiles=dict(getattr(config, "model_profiles", {})))
