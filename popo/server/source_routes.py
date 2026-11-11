"""HTTP handlers for upstream source and model-route administration."""

from __future__ import annotations
import json

from popo.armor.blocks import load_registry
from popo.core.upstream import UpstreamSource
from popo.errors import ErrorKind, ProxyError
from popo.server import egress
from popo.server.sources import CatalogStore, Source, SourceCatalog


class SourceRoutes:
    """Operator routes for managing source credentials and model routes."""

    def _route_admin_sources_list(self, body, parsed, identity) -> None:
        store = CatalogStore(self.cfg.data_dir, ephemeral=self.cfg.ephemeral)
        catalog = store.load()
        secrets = store.load_secrets()
        self._writer().json(200, {
            "sources": [{"id": source.id, "name": source.name,
                         "base_url": source.base_url,
                         "configured": bool(secrets.get(source.id)),
                         "published_models": catalog.models_of(source.id)}
                        for source in sorted(catalog.sources.values(), key=lambda row: row.id)],
            "published": catalog.published_models(),
            "routes": {model: {"source_id": route.source_id,
                               "model_id": route.model_id,
                               "profile": route.profile,
                               "namespaced": route.namespaced}
                       for model, route in sorted(catalog.routes.items())},
        })

    def _route_admin_sources_action(self, body, parsed, identity) -> None:
        payload = json.loads(body.decode("utf-8") or "{}")
        action = payload.get("action")
        store = CatalogStore(self.cfg.data_dir, ephemeral=self.cfg.ephemeral)
        catalog = store.load()
        if action == "add":
            # Validate the destination before changing the catalog or secrets.
            source = Source.build(payload.get("id", ""), payload.get("name", ""),
                                  payload.get("base_url", ""))
            try:
                safe_base = egress.validate(source.base_url,
                                            allow_private=self.cfg.allow_private_upstream)
            except egress.EgressError as exc:
                raise ProxyError(ErrorKind.BAD_REQUEST, str(exc)) from exc
            source = Source.build(source.id, source.name, safe_base)
            updated = catalog.add_source(source)
            keys = [str(key) for key in (payload.get("keys") or []) if str(key).strip()]
            if keys:
                store.save_secret(source.id, keys)
            store.save(updated)
            for removed_id in set(catalog.sources) - set(updated.sources):
                store.delete_secret(removed_id)
            self._writer().json(200, {"ok": True, "source": {"id": source.id}})
            return
        if action == "remove":
            source_id = str(payload.get("id", ""))
            if source_id not in catalog.sources:
                raise ProxyError(ErrorKind.NOT_FOUND, "source not found")
            remaining = SourceCatalog(
                sources={key: value for key, value in catalog.sources.items()
                         if key != source_id},
                routes={key: value for key, value in catalog.routes.items()
                        if value.source_id != source_id})
            store.save(remaining)
            store.delete_secret(source_id)
            self._writer().json(200, {"ok": True,
                                      "removed_routes": len(catalog.routes) - len(remaining.routes)})
            return
        raise ProxyError(ErrorKind.BAD_REQUEST, "action must be add/remove")

    def _route_admin_sources_discover(self, body, parsed, identity) -> None:
        payload = json.loads(body.decode("utf-8") or "{}")
        source_id = str(payload.get("source_id", ""))
        source, client = self._admin_source_client(source_id)
        try:
            status, data = client.request_json("GET", "/v1/models")
        finally:
            client.close()
        models = [str(item.get("id")) for item in data.get("data", [])
                  if isinstance(item, dict) and item.get("id")] if isinstance(data, dict) else []
        if status != 200 or not models:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"source '{source_id}' returned HTTP {status} or no models")
        self._writer().json(200, {"source_id": source.id, "models": models})

    def _route_admin_sources_test(self, body, parsed, identity) -> None:
        payload = json.loads(body.decode("utf-8") or "{}")
        source_id = str(payload.get("source_id", ""))
        requested = str(payload.get("model", "")).strip()
        source, client = self._admin_source_client(source_id)
        failures = []
        try:
            status, listing = client.request_json("GET", "/v1/models")
            entries = listing.get("data", []) if isinstance(listing, dict) else []
            models = [str(row.get("id")) for row in entries
                      if isinstance(row, dict) and row.get("id")]
            if status != 200 or not models:
                raise ProxyError(ErrorKind.BAD_REQUEST,
                                 f"source '{source_id}' returned HTTP {status} or no models")
            ordered = ([requested] if requested in models else [])
            ordered.extend(model for model in models if model not in ordered)
            for model in ordered:
                try:
                    status, result = client.request_json("POST", "/v1/chat/completions", {
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 5,
                        "stream": False,
                    })
                    if status == 200 and result.get("choices"):
                        self._writer().json(200, {"ok": True, "source_id": source.id,
                                                  "model": model})
                        return
                    failures.append(f"{model}: HTTP {status}")
                except ProxyError as exc:
                    failures.append(f"{model}: {exc.message[:80]}")
        finally:
            client.close()
        details = "; ".join(failures[:3])
        raise ProxyError(ErrorKind.BAD_REQUEST,
                         f"source '{source_id}' failed its chat probe ({details})")

    def _admin_source_client(self, source_id: str):
        store = CatalogStore(self.cfg.data_dir, ephemeral=self.cfg.ephemeral)
        source = store.load().source(source_id)
        if source is None:
            raise ProxyError(ErrorKind.NOT_FOUND, "source not found")
        keys = store.load_secrets().get(source_id) or []
        if not keys:
            raise ProxyError(ErrorKind.PROXY_CONFIG,
                             f"source '{source_id}' has no saved API key")
        try:
            safe_base = egress.validate(source.base_url,
                                        allow_private=self.cfg.allow_private_upstream)
        except egress.EgressError as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST, str(exc)) from exc
        return source, UpstreamSource(
            safe_base, keys, timeout=self.cfg.upstream_timeout,
            connect_timeout=self.cfg.upstream_connect_timeout,
            user_agent=self.cfg.upstream_ua)

    def _route_admin_sources_publish(self, body, parsed, identity) -> None:
        payload = json.loads(body.decode("utf-8") or "{}")
        source_id = str(payload.get("source_id", ""))
        models = payload.get("models")
        if not isinstance(models, list):
            raise ProxyError(ErrorKind.BAD_REQUEST, "models must be a list")
        profiles = payload.get("profiles") or {}
        if not isinstance(profiles, dict):
            raise ProxyError(ErrorKind.BAD_REQUEST, "profiles must be an object")
        model_ids = {str(model) for model in models}
        invalid_models = sorted(set(profiles) - model_ids)
        known_profiles = load_registry().get("profiles", {})
        invalid_profiles = sorted({str(profile) for profile in profiles.values()
                                   if profile and profile not in known_profiles})
        if invalid_models:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"profiles include unpublished models: {invalid_models}")
        if invalid_profiles:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"unknown armor profiles: {invalid_profiles}")
        store = CatalogStore(self.cfg.data_dir, ephemeral=self.cfg.ephemeral)
        catalog = store.load().publish(source_id, [str(model) for model in models], profiles)
        store.save(catalog)
        self._invalidate_source_picker()
        self.cfg.published_models = catalog.published_models()
        self._writer().json(200, {"ok": True, "published": catalog.published_models(),
                                  "routes": len(catalog.routes)})

    def _invalidate_source_picker(self) -> None:
        # The catalog changed; close pooled sockets before rebuilding on demand.
        old = getattr(type(self), "_source_factory", None)
        if old is not None and hasattr(old, "close"):
            try:
                old.close()
            except Exception:  # noqa: BLE001 - cleanup must not fail publication
                pass
        type(self)._source_factory = None

    def _route_admin_model_routes(self, body, parsed, identity) -> None:
        catalog = CatalogStore(self.cfg.data_dir, ephemeral=self.cfg.ephemeral).load()
        self._writer().json(200, {"routes": [
            {"model": model, "source_id": route.source_id,
             "model_id": route.model_id, "profile": route.profile,
             "namespaced": route.namespaced}
            for model, route in sorted(catalog.routes.items())]})
