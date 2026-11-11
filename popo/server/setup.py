"""Setup service: validate an upstream, publish models, persist install state.

The service returns ``(status, body)`` and never touches the HTTP socket, so it
can be unit-tested without a server. The handler owns transport concerns only.

Install state is written atomically at ``<data_dir>/popo.json`` (mode 0600).

**Ephemeral mode is about request data, not operator configuration.** The
upstream address and key the operator typed must survive a restart, or the proxy
cannot boot without re-entering them. What ephemeral forbids — request logs,
traces, usage ledgers, conversations — lives in the record store, not here.
Set ``persist_install=False`` explicitly if the key must not touch disk; the
install then has to be re-entered on every start.
"""

from __future__ import annotations
import json
import os
import tempfile
from urllib.parse import urlparse

from popo.armor.blocks import load_registry
from popo.auth.gate import Identity
from popo.auth.keys import KeyStore
from popo.config import Config
from popo.core.upstream import UpstreamSource
from popo.errors import ErrorKind, ProxyError
from popo.server import egress


class SetupStore:
    """Install state, persisted only when the operator asked for it."""

    def __init__(self, data_dir: str, persist: bool = True):
        self.data_dir = data_dir
        self.persist = persist
        self._state: dict = {}
        self._loaded = False

    @property
    def path(self) -> str:
        return os.path.join(self.data_dir, "popo.json")

    def load(self) -> dict:
        if self._loaded:
            return self._state
        self._loaded = True
        if not self.persist or not os.path.exists(self.path):
            return self._state
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self._state = loaded
        except (OSError, ValueError) as exc:
            print(f"[POPO] install state unreadable ({exc}); starting unconfigured")
        return self._state

    def save(self) -> None:
        if not self.persist:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".popo-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key: str, default=None):
        return self.load().get(key, default)

    def update(self, **values) -> None:
        self.load().update(values)
        self.save()


def _bad_request(message: str) -> ProxyError:
    return ProxyError(ErrorKind.BAD_REQUEST, message)


def _normalize_base(host: str, prefix: str = "") -> str:
    base = (host or "").strip().rstrip("/")
    if not base:
        raise _bad_request("upstream_host is required")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    prefix = (prefix or "").strip().strip("/")
    if prefix:
        existing_path = urlparse(base).path.rstrip("/").lower()
        normal_prefix = prefix.lower()
        if existing_path != f"/{normal_prefix}" and not existing_path.endswith(f"/{normal_prefix}"):
            base = f"{base.rstrip('/')}/{prefix}"
    return base.rstrip("/")


class SetupService:
    """Implements the setup workflow without knowing about HTTP."""

    def __init__(self, config: Config, store: SetupStore, keystore: KeyStore,
                 bootstrap=None):
        self.config = config
        self.store = store
        self.keystore = keystore
        # A fresh install has no credential yet. Without a bootstrap gate, the
        # first stranger to reach the port configures it; with one, the person
        # holding the device does.
        self.bootstrap = bootstrap

    # ---- reads -------------------------------------------------------
    def status(self) -> tuple[int, dict]:
        state = self.store.load()
        configured = bool(state.get("upstream_host") and state.get("upstream_key"))
        return 200, {
            "configured": configured,
            "upstream_host": state.get("upstream_host", self.config.upstream_host),
            "published": state.get("published_models", []),
            "models": state.get("all_models") or list(self.config.published_models),
            "profiles": dict(state.get("model_profiles") or self.config.model_profiles),
        }

    def list_published(self) -> tuple[int, dict]:
        return 200, {"published": list(self.config.published_models)}

    # ---- upstream probing -------------------------------------------
    def list_upstream_models(self, body: bytes) -> tuple[int, dict]:
        data = json.loads(body.decode("utf-8") or "{}")
        self._require_bootstrap(data)
        base = self._checked_base(data)
        key = (data.get("upstream_key") or "").strip()
        if not key:
            raise _bad_request("upstream_key is required")
        models = self._probe_models(base, key)
        if not models:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"{base} did not return a model list (check the address "
                             f"and key; a bare host needs a /v1 suffix)")
        return 200, {"base": base, "models": models}

    def test(self, body: bytes) -> tuple[int, dict]:
        data = json.loads(body.decode("utf-8") or "{}")
        self._require_bootstrap(data)
        base = self._checked_base(data)
        key = (data.get("upstream_key") or "").strip()
        if not key:
            raise _bad_request("upstream_key is required")
        models = self._probe_models(base, key)
        if not models:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"{base} did not return a model list (check the address "
                             f"and key; a bare host needs a /v1 suffix)")
        chat_model = self._probe_first_chat_model(base, key, models, data.get("model"))
        return 200, {"ok": True, "models": models, "base": base, "model": chat_model}

    def quick(self, body: bytes) -> tuple[int, dict]:
        data = json.loads(body.decode("utf-8") or "{}")
        key_scope = data.get("key_scope", "public")
        if key_scope not in ("internal", "public"):
            raise _bad_request("key_scope must be internal or public")
        # Authorise and validate the destination BEFORE any socket work: an
        # unauthenticated caller must not be able to make this process connect
        # anywhere, even briefly.
        self._require_bootstrap(data)
        base = self._checked_base(data)
        key = (data.get("upstream_key") or "").strip()
        if not key:
            raise _bad_request("upstream_key is required")
        models = self._probe_models(base, key)
        if not models:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             f"{base} did not return a model list (check the address "
                             f"and key; a bare host needs a /v1 suffix)")
        chat_model = self._probe_first_chat_model(base, key, models, data.get("model"))
        published = self._resolve_published(data.get("models"), models)
        model_profiles = self._checked_profiles(data.get("profiles"), published)

        self.store.update(
            upstream_host=base,
            upstream_key=key,
            upstream_path_prefix=data.get("upstream_path_prefix", ""),
            published_models=published,
            all_models=models,
            model_profiles=model_profiles,
        )
        self.config.upstream_host = base
        self.config.upstream_key = [key]
        self.config.published_models = published
        self.config.model_profiles = model_profiles

        # A re-apply rotates the setup-issued client key so repeated setup does
        # not leave a trail of active credentials behind.
        for existing in self.keystore.list_all():
            if existing.label == "quick-setup" and existing.enabled:
                self.keystore.disable(existing.key_id)
        token, record = self.keystore.create(
            label="quick-setup", allow_public=key_scope == "public")
        base_url = f"http://127.0.0.1:{self.config.bind_port}/v1"
        self.bootstrap.revoke() if self.bootstrap is not None else None
        return 200, {
            # Two naming conventions on purpose: the 2.0.1 console reads
            # `base_url`/`api_key`, the newer one reads `url`/`key`. Returning
            # both keeps either client working without a version check.
            "url": base_url,
            "base_url": base_url,
            "key": token,
            "api_key": token,
            "key_created": bool(token),
            "key_id": record.key_id,
            "allow_public": record.allow_public,
            "models": models,
            "published": published,
            "model": published[0] if published else "",
            "profiles": model_profiles,
        }

    def publish(self, body: bytes, identity: Identity) -> tuple[int, dict]:
        if identity.level not in ("operator", "loopback"):
            raise ProxyError(ErrorKind.UNAUTHORIZED, "operator required")
        data = json.loads(body.decode("utf-8") or "{}")
        published = data.get("models", [])
        if not isinstance(published, list):
            raise _bad_request("models must be a list")
        known = self._available_models()
        invalid = [m for m in published if m not in known]
        if invalid:
            raise _bad_request(f"unknown models: {invalid}")
        current_profiles = dict(self.store.get("model_profiles") or self.config.model_profiles)
        if "profiles" in data:
            # The setup console sends the selected per-model profiles together
            # with the published model list. Treat an explicit empty mapping as
            # "return these models to automatic fingerprint routing"; only an
            # omitted field keeps the previous overrides.
            model_profiles = self._checked_profiles(data.get("profiles"), published)
        else:
            model_profiles = {model: profile for model, profile in current_profiles.items()
                              if model in published}
        self.store.update(published_models=published, model_profiles=model_profiles)
        self.config.published_models = published
        self.config.model_profiles = model_profiles
        return 200, {"published": published, "profiles": model_profiles}

    @staticmethod
    def _checked_profiles(raw, published: list[str]) -> dict[str, str]:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise _bad_request("profiles must be an object of model id to profile id")
        known = load_registry().get("profiles", {})
        invalid_models = sorted(set(raw) - set(published))
        invalid_profiles = sorted({str(value) for value in raw.values()
                                   if value and value not in known})
        if invalid_models:
            raise _bad_request(f"profiles include unpublished models: {invalid_models}")
        if invalid_profiles:
            raise _bad_request(f"unknown armor profiles: {invalid_profiles}")
        return {str(model): str(profile) for model, profile in raw.items()
                if profile and model in published}

    # ---- helpers -----------------------------------------------------
    def _checked_base(self, data: dict) -> str:
        """Normalise the upstream URL and enforce the outbound destination policy."""
        base = _normalize_base(data.get("upstream_host", ""),
                               data.get("upstream_path_prefix", ""))
        try:
            return egress.validate(base, allow_private=self.config.allow_private_upstream)
        except egress.EgressError as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST, str(exc)) from exc

    def _has_client_keys(self) -> bool:
        return any(record.key_id != "admin" for record in self.keystore.list_all())

    def _require_bootstrap(self, data: dict) -> None:
        """First configuration requires the one-shot token.

        Once a client key exists the install is no longer fresh, and the
        operator's existing credential is enough; requiring the token then would
        lock out a working installation.

        This runs *before* any probe: the order is the security property. A
        check that happens after the network call still lets an anonymous caller
        use the proxy as a scanner.
        """
        if self.bootstrap is None:
            return
        if self._has_client_keys():
            return
        presented = str(data.get("setup_token", "")).strip()
        if not presented:
            # A program on this device is the operator; that is the same trust
            # rule the rest of the server uses, not a second one.
            if getattr(self, "caller_is_local", False):
                return
            raise ProxyError(ErrorKind.UNAUTHORIZED,
                             "this install is unconfigured: pass setup_token "
                             "(printed on the device) to /setup/quick")
        if not self.bootstrap.consume(presented):
            raise ProxyError(ErrorKind.UNAUTHORIZED,
                             "setup_token is invalid, expired, or already used")
    def _available_models(self) -> list[str]:
        models = self.store.get("all_models")
        if models:
            return list(models)
        return list(self.config.published_models or [])

    # Endpoints a model id is known not to serve. Taken from the id itself: a
    # `/v1/models` list mixes chat, image, embedding and audio models, so
    # "models[0]" is a coin flip — measured against logfare.ai, the first entry
    # was `flux-2-dev`, an image model, and the setup probe failed with a helpful
    # looking but wrong message ("not a usable upstream").
    _NON_CHAT_HINTS = ("flux", "sdxl", "dall-e", "stable-diffusion", "whisper",
                       "nova-", "tts", "bge-", "embedding", "embed")

    def _pick_chat_model(self, models: list[str], requested: str = "") -> str:
        """Return the first model that actually answers a chat probe.

        Guessing from the name is not reliable: logfare.ai advertises image,
        embedding and audio models in the same list, and neither `models[0]` nor a
        keyword filter picked a working chat model. The only trustworthy test is
        to ask, so this walks the list in preference order and stops at the first
        model that answers. Probing is cheap (one tiny request per candidate) and
        happens only during setup.
        """
        requested = (requested or "").strip()
        candidates = ([requested] if requested and requested in models else []) + \
                     [m for m in models if m != requested]
        # Try the plausible chat models first, then the rest.
        preferred = [m for m in candidates
                     if not any(hint in m.lower() for hint in self._NON_CHAT_HINTS)]
        ordered = preferred + [m for m in candidates if m not in preferred]
        return ordered[0] if ordered else ""

    def _probe_first_chat_model(self, base: str, key: str, models: list[str],
                                requested: str = "") -> str:
        """Walk the model list until a chat request succeeds; return its name."""
        requested = (requested or "").strip()
        ordered = ([requested] if requested and requested in models else []) + \
                  [m for m in models if m != requested]
        preferred = [m for m in ordered
                     if not any(hint in m.lower() for hint in self._NON_CHAT_HINTS)]
        errors: list[str] = []
        for model in preferred + [m for m in ordered if m not in preferred]:
            if not model:
                continue
            try:
                self._probe_chat(base, key, model)
                return model
            except ProxyError as exc:
                errors.append(f"{model}: {exc.message[:60]}")
                continue
        detail = "; ".join(errors[:3]) if errors else "no models were listed"
        raise ProxyError(
            ErrorKind.BAD_REQUEST,
            f"none of the models offered by {base} accepted a chat request ({detail})")

    def _probe_models(self, base: str, key: str) -> list[str]:
        try:
            source = UpstreamSource(base, key, timeout=min(self.config.upstream_timeout, 15))
            status, data = source.request_json("GET", "/v1/models")
            if status == 200 and isinstance(data, dict):
                ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
                if ids:
                    return ids
        except (ProxyError, ValueError):
            pass
        return []

    def _probe_chat(self, base: str, key: str, model: str) -> None:
        try:
            source = UpstreamSource(base, key, timeout=min(self.config.upstream_timeout, 30))
            status, data = source.request_json("POST", "/v1/chat/completions", {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
                "stream": False,
            })
            if status != 200 or not isinstance(data, dict) or "choices" not in data:
                raise ProxyError(ErrorKind.UPSTREAM_INVALID_RESPONSE,
                                 f"{base} rejected a probe chat request (status {status})")
        except ProxyError as exc:
            raise ProxyError(
                ErrorKind.BAD_REQUEST,
                f"{base} rejected a chat probe with model '{model}': {exc.message}") from exc
        except Exception as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST, f"{base} unreachable: {exc}") from exc

    def _resolve_published(self, publish, models: list[str]) -> list[str]:
        """Which models to publish when the caller did not say.

        Never ``models[:1]``: a provider's list mixes image, embedding and audio
        models, so the first entry is arbitrary — measured against logfare.ai it
        was ``flux-2-dev``, an image model, and the install then advertised an
        image model as its chat API.
        """
        if isinstance(publish, list) and publish:
            chosen = [m for m in publish if m in models]
            if chosen:
                return chosen
        # Fall back to something that can actually answer a chat request.
        for model in models:
            if not any(hint in model.lower() for hint in self._NON_CHAT_HINTS):
                return [model]
        return models[:1]
