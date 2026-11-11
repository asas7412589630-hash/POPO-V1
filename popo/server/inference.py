"""Chat and inference endpoints.

Everything here is domain logic: pick a model, run the pipeline, shape the
response. No sockets, no header parsing, no status codes — the caller converts
what this returns into HTTP. That separation is what makes an endpoint testable
without starting a server.
"""

from __future__ import annotations
import json
import threading

from popo.armor.blocks import build_system_prompt, load_registry, resolve_profile
from popo.armor.render import render_authoritative_policy
from popo.config import Config
from popo.core.pipeline import PipelineResult, run_pipeline, run_pipeline_stream
from popo.core.request import ChatRequest
from popo.core.upstream import UpstreamSource
from popo.errors import ErrorKind, ProxyError
from popo.storage.records import RecordStore

# Passthrough endpoints: no armor, no retry, byte-faithful forwarding.
PASSTHROUGH_ROUTES = {
    "/v1/images/generations": "images",
    "/v1/embeddings": "embeddings",
}


class SourceFactory:
    """Build (and reuse) an upstream source for a model.

    One instance is kept per (base, keys, timeout) tuple. Creating a fresh
    ``UpstreamSource`` per request would make the connection pool pointless:
    every request would open a new TCP/TLS connection and the pool would be
    garbage-collected before it could ever be reused.
    """

    def __init__(self, config: Config):
        self.config = config
        self._sources: dict[tuple, UpstreamSource] = {}
        self._lock = threading.Lock()

    def _signature(self) -> tuple:
        return (self.config.upstream_host, tuple(self.config.upstream_key or []),
                self.config.upstream_timeout, self.config.upstream_connect_timeout,
                self.config.upstream_ua)

    def __call__(self, model: str = "") -> UpstreamSource:
        if not self.config.upstream_host:
            raise ProxyError(ErrorKind.PROXY_CONFIG, "no upstream host is configured")
        keys = self.config.upstream_key or []
        if not keys:
            raise ProxyError(ErrorKind.PROXY_CONFIG, "no upstream key is configured")
        signature = self._signature()
        with self._lock:
            source = self._sources.get(signature)
            if source is None:
                source = UpstreamSource(
                    self.config.upstream_host, keys,
                    timeout=self.config.upstream_timeout,
                    connect_timeout=self.config.upstream_connect_timeout,
                    user_agent=self.config.upstream_ua)
                # A config change (new host/key/timeout) invalidates the old
                # entry; keeping both would leak sockets.
                for stale in [key for key in self._sources if key != signature]:
                    self._sources.pop(stale).close()
                self._sources[signature] = source
            return source

    def profile_for(self, model: str) -> str:
        return str(getattr(self.config, "model_profiles", {}).get(model, ""))

    def close(self) -> None:
        with self._lock:
            for source in self._sources.values():
                source.close()
            self._sources.clear()


class InferenceService:
    """Runs one inference request and returns a transport-neutral result."""

    def __init__(self, config: Config, records: RecordStore,
                 sources: SourceFactory | None = None):
        self.config = config
        self.records = records
        self.sources = sources or SourceFactory(config)

    # ---- validation ---------------------------------------------------
    def ensure_published(self, model: str) -> None:
        published = self.config.published_models or []
        if published and model not in published:
            raise ProxyError(
                ErrorKind.INVALID_MODEL,
                f"model '{model}' is not published on this install; "
                f"available: {', '.join(published)}",
            )

    @staticmethod
    def armor_level(header_value: str, config: Config) -> str | None:
        """Per-request armor level, limited to what the operator allows."""
        allowed = {level.lower() for level in (config.armor_levels or [])}
        if not allowed:
            allowed = {"native", "lean", "off"}
        value = (header_value or "").strip().lower()
        if not value:
            return None
        if value not in allowed:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"unknown armor level '{value}'; allowed: {sorted(allowed)}")
        return value

    # ---- buffered chat ------------------------------------------------
    def chat(self, body: bytes, *, conversation_id: str, client_key_id: str = "",
             armor_level: str | None = None) -> PipelineResult:
        request = ChatRequest.from_openai(body)
        self.ensure_published(request.model)
        return run_pipeline(
            request, self.config, source_picker=self.sources,
            records=self.records, conversation_id=conversation_id,
            client_key_id=client_key_id, armor_level=armor_level,
        )

    def anthropic_chat(self, body: bytes, *, conversation_id: str,
                       client_key_id: str = "", armor_level: str | None = None):
        request = ChatRequest.from_anthropic(body)
        self.ensure_published(request.model)
        return request, run_pipeline(
            request, self.config, source_picker=self.sources,
            records=self.records, conversation_id=conversation_id,
            client_key_id=client_key_id, armor_level=armor_level,
        )

    def chat_stream(self, body: bytes, *, conversation_id: str,
                    client_key_id: str = "", armor_level: str | None = None):
        request = ChatRequest.from_openai(body)
        self.ensure_published(request.model)
        return request, run_pipeline_stream(
            request, self.config, source_picker=self.sources,
            records=self.records, conversation_id=conversation_id,
            client_key_id=client_key_id, armor_level=armor_level,
        )

    def anthropic_stream(self, body: bytes, *, conversation_id: str,
                         client_key_id: str = "", armor_level: str | None = None):
        request = ChatRequest.from_anthropic(body)
        self.ensure_published(request.model)
        return request, run_pipeline_stream(
            request, self.config, source_picker=self.sources,
            records=self.records, conversation_id=conversation_id,
            client_key_id=client_key_id, armor_level=armor_level,
        )

    # ---- passthrough ---------------------------------------------------
    def passthrough(self, path: str, body: bytes, model: str = "") -> tuple[int, dict]:
        """Forward a non-chat endpoint unchanged.

        The body is parsed only to reject a shape we cannot forward; the bytes
        sent upstream are the caller's bytes, not a re-serialisation. Re-encoding
        would silently drop fields this proxy does not know about.
        """
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST, f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProxyError(ErrorKind.BAD_REQUEST, "request body must be a JSON object")
        # The published-model list is a policy, not a chat-only convention: an
        # unpublishable model must not slip through a passthrough route.
        body_model = payload.get("model")
        requested = model or (body_model if isinstance(body_model, str) else "")
        if requested:
            self.ensure_published(requested)
        source = self.sources(requested)
        status, data = source.request_json("POST", path, payload)
        return status, data

    def audio_transcription(self, body: bytes, content_type: str) -> tuple[int, dict]:
        """Forward a multipart upload without decoding it."""
        source = self.sources()
        headers = {"Content-Type": content_type} if content_type else {}
        status, data = source.request_bytes("POST", "/v1/audio/transcriptions", body, headers)
        return status, data

    # ---- discovery -----------------------------------------------------
    def models(self) -> dict:
        published = self.config.published_models or list(DEFAULT_MODELS)
        return {"object": "list",
                "data": [{"id": model, "object": "model", "owned_by": "popo",
                          "context_window": self.config.advertised_context}
                         for model in published]}

    def armor_preview(self, model: str, level: str | None) -> dict:
        policy_text, used = build_system_prompt(model, level=level)
        text = render_authoritative_policy(policy_text)
        return {"model": model, "level": level, "policy_len": len(text),
                "blocks": used, "content": text}

    def armor_registry(self) -> dict:
        registry = load_registry()
        profiles = registry.get("profiles", {})
        families = registry.get("console_profiles") or [
            {"id": name, "label": item.get("label", name),
             "desc": item.get("desc", "")}
            for name, item in profiles.items() if item.get("selectable", True)
        ]
        return {"version": registry.get("version", 1),
                "blocks": sorted(registry.get("blocks", {})),
                "profiles": sorted(profiles),
                "families": families,
                "details": {name: {"label": item.get("label", name),
                                   "desc": item.get("desc", ""),
                                   "blocks": item.get("blocks", [])}
                            for name, item in profiles.items()},
                "levels": sorted(registry.get("levels", {}))}

    def profile(self, model: str, level: str | None) -> dict:
        registry = load_registry()
        policy_text, used = build_system_prompt(model, level=level)
        text = render_authoritative_policy(policy_text)
        return {"model": model, "level": level, "blocks": used,
                "policy_len": len(text), "content": text,
                "levels": sorted(registry.get("levels", {})),
                "profiles": {name: {"desc": profile.get("desc", ""),
                                    "blocks": profile.get("blocks", [])}
                             for name, profile in registry.get("profiles", {}).items()},
                "resolved": resolve_profile(model, registry, level=level)}


DEFAULT_MODELS = (
    "glm-5.3", "glm-5.3-flash", "deepseek-v4-pro-0813",
    "deepseek-v4.1-flash-1017", "gpt-5.2",
)
