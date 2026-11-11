"""Route implementations, grouped by area.

Each mixin assumes the host class provides ``cfg``, ``_writer()``, ``_inference()``,
``_setup_service()``, ``_records()`` and ``_gate()``. Keeping them out of the
handler means the handler file stays a dispatcher, and each area can be reviewed
(or replaced) without reading the socket code.
"""

from __future__ import annotations
import hashlib
import json
import os
import time
from http import cookies
from urllib.parse import parse_qs

from popo.errors import ErrorKind, ProxyError
from popo.server.anthropic_sse import iter_anthropic_events
from popo.server import admin as admin_views
from popo.server.console_routes import _degraded
from popo.server import deps
from popo.server import replay
from popo.server import tuning
from popo.server.responses import ResponseWriter  # noqa: F401  (type reference)
from popo.storage import audit, retention


class DiscoveryRoutes:
    """Liveness, model list, armor preview, identity echo."""

    def _route_health(self, body, parsed, identity) -> None:
        from popo.server.routes import ROUTES
        started = getattr(type(self), "_started_at", None)
        # Every field here is something the console renders. A field it asks for
        # and we omit becomes a dash that looks like a broken install, so the
        # shape is kept in step with console/app.js rather than guessed.
        self._writer().json(200, {
            "ok": True,
            "version": self.cfg.version,
            "armor": self.cfg.armor_enabled,
            "scrub": self.cfg.scrub_enabled,
            "ephemeral": self.cfg.ephemeral,
            "records": not self.cfg.ephemeral,
            "require_client_key": self.cfg.require_client_key,
            "models": len(self.cfg.published_models),
            "routes": len(ROUTES),
            "uptime_s": int(time.time() - started) if started else 0,
            "degraded": _degraded(self.cfg),
        })

    def _route_models(self, body, parsed, identity) -> None:
        self._writer().json(200, self._inference().models())

    def _route_armor_registry(self, body, parsed, identity) -> None:
        self._writer().json(200, self._inference().armor_registry())

    def _route_armor_preview(self, body, parsed, identity) -> None:
        query = parse_qs(parsed.query)
        model = (query.get("model") or ["glm-5.3"])[0]
        level = (query.get("level") or [None])[0]
        # Without this the preview always showed automatic routing, so an
        # operator who picked a family saw a different prompt than the one their
        # next request would get.
        profile = (query.get("profile") or [""])[0] or None
        try:
            payload = self._inference().armor_preview(model, level, profile)
        except (OSError, ValueError) as exc:
            raise ProxyError(ErrorKind.PROXY_NOT_AVAILABLE,
                             f"armor blocks unavailable: {exc}") from exc
        self._writer().json(200, payload)

    def _route_whoami(self, body, parsed, identity) -> None:
        self._writer().json(200, {"identity": identity.level, "via": identity.via,
                                  "user_id": identity.user_id})




    def _route_routes(self, body, parsed, identity) -> None:
        from popo.server.routes import ROUTES
        self._writer().json(200, {"routes": [
            {"method": route.method, "path": route.path,
             "auth": route.auth, "name": route.name} for route in ROUTES]})


class SetupRoutes:
    """First-run configuration."""

    def _route_setup_status(self, body, parsed, identity) -> None:
        status, payload = self._setup_service().status()
        self._writer().json(status, payload)

    def _route_setup_models(self, body, parsed, identity) -> None:
        status, payload = self._setup_service().list_published()
        self._writer().json(status, payload)

    def _route_setup_test(self, body, parsed, identity) -> None:
        status, payload = self._setup_service().test(body)
        self._writer().json(status, payload)

    def _route_setup_list_models(self, body, parsed, identity) -> None:
        status, payload = self._setup_service().list_upstream_models(body)
        self._writer().json(status, payload)

    def _route_setup_token(self, body, parsed, identity) -> None:
        """Hand the first-run token to the machine's own operator.

        Deliberately narrow: loopback peer, no browser markers, no existing key.
        That combination is "a terminal on this device", not "a page that reached
        127.0.0.1". If any part is missing the answer is 401 with the reason,
        never a token.
        """
        gate = self._gate()
        # NOT is_loopback_peer(): a tunnel client arrives from 127.0.0.1 while the
        # caller is remote, so that test would hand the first-run token to anyone
        # on the internet who can reach the published URL.
        if not gate.is_local_tool():
            raise ProxyError(ErrorKind.UNAUTHORIZED,
                             "setup token is readable only by a program on this device")
        plaintext = deps.ensure_bootstrap(self)
        if not plaintext:
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             "this install is already configured; no setup token needed")
        self._writer().json(200, {"setup_token": plaintext,
                                  "note": "single use, expires in 30 minutes"})

    def _route_setup_quick(self, body, parsed, identity) -> None:
        status, payload = self._setup_service().quick(body)
        self._writer().json(status, payload)

    def _route_setup_publish(self, body, parsed, identity) -> None:
        status, payload = self._setup_service().publish(body, identity)
        self._writer().json(status, payload)


class InferenceRoutes:
    """Chat, Anthropic translation, passthrough endpoints."""

    def _inference_slot(self):
        """Acquire a global concurrency slot for the whole request.

        Held for the duration of the upstream work, not merely inspected first:
        a check-then-act sequence lets two requests both pass the check and then
        both exceed the limit. The context manager releases in ``finally`` on
        every path, including client disconnects.
        """
        return deps.concurrency_of(self).slot()

    def _armor_level(self) -> str | None:
        return self._inference().armor_level(self.headers.get("X-Armor-Level", ""), self.cfg)

    def _conversation_hint(self, body: bytes) -> str:
        explicit = self.headers.get("X-Conversation-Id", "").strip()
        if explicit:
            return explicit
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "c_none"
        for message in payload.get("messages", []) or []:
            if isinstance(message, dict) and message.get("role") == "user":
                digest = hashlib.sha256(str(message.get("content", "")).encode()).hexdigest()
                return "c_" + digest[:16]
        return "c_none"

    def _route_chat(self, body, parsed, identity) -> None:
        service = self._inference()
        level = self._armor_level()
        if _wants_stream(body):
            with self._inference_slot():
                _, result = service.chat_stream(
                    body, conversation_id=self._conversation_hint(body),
                    client_key_id=identity.client_key_id, armor_level=level)
                self._deliver_stream(result, anthropic=False)
            return
        with self._inference_slot():
            result = service.chat(body, conversation_id=self._conversation_hint(body),
                                  client_key_id=identity.client_key_id, armor_level=level)
            self._deliver_result(result, anthropic=False)

    def _route_anthropic_messages(self, body, parsed, identity) -> None:
        service = self._inference()
        level = self._armor_level()
        if _wants_stream(body):
            with self._inference_slot():
                request, result = service.anthropic_stream(
                    body, conversation_id=self._conversation_hint(body),
                    client_key_id=identity.client_key_id, armor_level=level)
                self._deliver_stream(result, anthropic=True, request=request)
            return
        with self._inference_slot():
            request, result = service.anthropic_chat(
                body, conversation_id=self._conversation_hint(body),
                client_key_id=identity.client_key_id, armor_level=level)
            self._deliver_result(result, anthropic=True, request=request)

    def _route_images(self, body, parsed, identity) -> None:
        with self._inference_slot():
            status, payload = self._inference().passthrough("/v1/images/generations", body)
        self._writer().json(status, payload)

    def _route_embeddings(self, body, parsed, identity) -> None:
        with self._inference_slot():
            status, payload = self._inference().passthrough("/v1/embeddings", body)
        self._writer().json(status, payload)

    def _route_audio_transcriptions(self, body, parsed, identity) -> None:
        with self._inference_slot():
            status, payload = self._inference().audio_transcription(
                body, self.headers.get("Content-Type", ""))
        self._writer().json(status, payload)

    def _route_adult_chat(self, body, parsed, identity) -> None:
        self._adult("/v1/adult/chat", body)

    def _route_adult_image(self, body, parsed, identity) -> None:
        self._adult("/v1/adult/image", body)

    def _adult(self, path: str, body: bytes) -> None:
        from popo.adult.router import AdultRouter
        if not self.cfg.adult_enabled:
            raise ProxyError(ErrorKind.PROXY_NOT_AVAILABLE, "adult routes are disabled")
        with self._inference_slot():
            picker = self.source_picker or deps.sources_of(self)
            result = AdultRouter(self.cfg, picker).dispatch(path, body)
        self._writer().json(200, result)

    # ---- response shaping ---------------------------------------------
    def _deliver_result(self, result, *, anthropic: bool, request=None) -> None:
        headers = {
            "X-Armor-Verdict": result.verdict,
            # V1 callers read these names; keep them so existing clients do not
            # have to change just because the implementation did.
            "X-Armor-Request-Model": getattr(request, "model", "") or _model_of(result.response),
            "X-Armor-Upstream-Model": _model_of(result.response),
            "X-Armor-Retries": str(result.attempts),
            "X-Armor-Attempts": str(result.attempts + 1),
            "X-Armor-Policy-Len": str(result.policy_len),
            "X-Armor-Blocks": ",".join(result.armor_blocks),
            "X-Armor-Score": f"{result.score:.2f}",
            "X-Armor-Classification": result.classification,
        }
        body = to_anthropic_message(result.response, request) if anthropic else result.response
        self._writer().json(200, body, headers=headers)

    def _deliver_stream(self, result, *, anthropic: bool, request=None) -> None:
        report = result.report_fn()
        headers = {
            "X-Armor-Verdict": report.get("verdict", "stream"),
            "X-Armor-Retries": str(report.get("attempts", 0)),
            "X-Armor-Attempts": str(report.get("attempts", 0) + 1),
            "X-Armor-Policy-Len": str(report.get("policy_len", 0)),
        }
        writer = self._writer()
        writer.sse_start(headers=headers)
        sent_done = False
        try:
            chunks = (iter_anthropic_events(
                result.iterator, fallback_model=getattr(request, "model", "unknown"))
                if anthropic else result.iterator)
            for chunk in chunks:
                if not anthropic and b"[DONE]" in chunk:
                    sent_done = True
                writer.sse_chunk(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The client hung up mid-stream. Closing our generator lets the
            # pipeline abort the upstream socket, which is what stops the spend.
            _close_iterator(result.iterator)
            return
        except ProxyError as exc:
            # The status line is already out; an in-band error event is the only
            # honest signal left.
            if anthropic:
                payload = json.dumps({
                    "type": "error",
                    "error": {"type": exc.kind.value, "message": exc.message},
                }, ensure_ascii=False).encode("utf-8")
                writer.sse_chunk(b"event: error\ndata: " + payload + b"\n\n")
            else:
                payload = json.dumps(
                    {"error": {"kind": exc.kind.value, "message": exc.message}},
                    ensure_ascii=False).encode("utf-8")
                writer.sse_chunk(b"event: error\ndata: " + payload + b"\n\n")
        finally:
            _close_iterator(result.iterator)
            # Exactly one terminator. Upstream usually sends its own; adding a
            # second unconditionally made every completion end with two, which a
            # client that counts them reports as a broken stream. Only supply one
            # when the upstream did not.
            if not anthropic and not sent_done:
                writer.sse_chunk(b"data: [DONE]\n\n")
            writer.sse_end()


class SessionRoutes:
    """Login and logout."""

    SESSION_COOKIE = "triad_sid"

    def _login_limiter(self):
        limiter = getattr(self, "_attempt_limiter", None)
        if limiter is None:
            limiter = deps.login_limiter_of(self)
            type(self)._attempt_limiter = limiter
        return limiter

    def _route_login(self, body, parsed, identity) -> None:
        payload = json.loads(body.decode("utf-8") or "{}")
        caller = self.client_address[0] if self.client_address else "unknown"
        limiter = self._login_limiter()
        exempt = self._gate().is_loopback_peer()
        if not exempt and not limiter.allowed(caller):
            raise ProxyError(ErrorKind.RATE_LIMITED,
                             f"too many failed logins; retry in {limiter.retry_after(caller)}s")
        accounts = deps.accounts_of(self)
        account = accounts.auth(str(payload.get("username", "")).strip(),
                                str(payload.get("password", "")))
        if not account:
            if not exempt:
                limiter.record_failure(caller)
            raise ProxyError(ErrorKind.UNAUTHORIZED, "invalid credentials")
        limiter.reset(caller)
        sid = accounts.create_session(account.uid)
        cookie = cookies.SimpleCookie()
        cookie[self.SESSION_COOKIE] = sid
        cookie[self.SESSION_COOKIE]["Path"] = "/"
        cookie[self.SESSION_COOKIE]["HttpOnly"] = True
        cookie[self.SESSION_COOKIE]["SameSite"] = "Lax"
        if not self._gate().is_loopback_peer():
            cookie[self.SESSION_COOKIE]["Secure"] = True
        self._writer().json(200, {"ok": True, "user_id": account.uid, "role": account.role},
                            headers={"Set-Cookie": cookie.output(header="").strip()})

    def _route_logout(self, body, parsed, identity) -> None:
        sid = self._gate().cookie(self.SESSION_COOKIE)
        if sid:
            deps.accounts_of(self).revoke_session(sid)
        cookie = cookies.SimpleCookie()
        cookie[self.SESSION_COOKIE] = ""
        cookie[self.SESSION_COOKIE]["Path"] = "/"
        cookie[self.SESSION_COOKIE]["Max-Age"] = 0
        self._writer().json(200, {"ok": True},
                            headers={"Set-Cookie": cookie.output(header="").strip()})


class RecordRoutes:
    """Consumer-facing conversation history."""

    def _route_conversations(self, body, parsed, identity) -> None:
        self._writer().json(200, {"conversations": self._records().list_conversations()})

    def _route_conversation_detail(self, body, parsed, identity) -> None:
        conversation_id = parsed.path[len("/conversations/"):]
        if not conversation_id:
            raise ProxyError(ErrorKind.NOT_FOUND, "conversation id required")
        rows = self._records().read_conversation(conversation_id)
        if not rows:
            raise ProxyError(ErrorKind.NOT_FOUND, "conversation not found")
        self._writer().json(200, {"conversation_id": conversation_id, "snapshots": rows})


class AdminRoutes:
    """Operator-only management."""

    def _route_admin_keys_list(self, body, parsed, identity) -> None:
        self._writer().json(200, admin_views.list_keys(deps.keystore_of(self)))

    def _route_admin_keys_action(self, body, parsed, identity) -> None:
        status, payload = admin_views.keys_action(deps.keystore_of(self), body, identity)
        self._writer().json(status, payload)

    def _route_admin_conversations(self, body, parsed, identity) -> None:
        self._writer().json(200, {"conversations": self._records().list_conversations()})

    def _route_admin_conversation_detail(self, body, parsed, identity) -> None:
        conversation_id = parsed.path[len("/admin/conversations/"):]
        if not conversation_id:
            raise ProxyError(ErrorKind.NOT_FOUND, "conversation id required")
        rows = self._records().read_conversation(conversation_id)
        if not rows:
            raise ProxyError(ErrorKind.NOT_FOUND, "conversation not found")
        self._writer().json(200, {"conversation_id": conversation_id, "snapshots": rows})

    def _route_admin_profile(self, body, parsed, identity) -> None:
        query = parse_qs(parsed.query)
        model = (query.get("model") or ["glm-5.3"])[0]
        level = (query.get("level") or [None])[0]
        self._writer().json(200, self._inference().profile(model, level))

    def _route_admin_usage(self, body, parsed, identity) -> None:
        query = parse_qs(parsed.query)
        try:
            days = int((query.get("days") or ["7"])[0])
        except ValueError as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST, "days must be an integer") from exc
        self._writer().json(200, admin_views.usage_summary(self._records(), days))

    def _route_admin_accounts(self, body, parsed, identity) -> None:
        self._writer().json(200, admin_views.list_accounts(deps.accounts_of(self)))

    def _route_admin_accounts_action(self, body, parsed, identity) -> None:
        status, payload = admin_views.accounts_action(deps.accounts_of(self), body, identity)
        self._writer().json(status, payload)

    def _route_admin_audit(self, body, parsed, identity) -> None:
        query = parse_qs(parsed.query)
        days = _int_param(query, "days", 7, low=1, high=365)
        findings = audit.audit(self.cfg.data_dir, days=days)
        findings.extend(audit.audit_conversations(self._records(), days=days))
        self._writer().json(200, audit.summary(findings))

    def _route_admin_replay(self, body, parsed, identity) -> None:
        payload = json.loads(body.decode("utf-8") or "{}")
        conversation_id = str(payload.get("conversation_id", "")).strip()
        if not conversation_id:
            raise ProxyError(ErrorKind.BAD_REQUEST, "conversation_id is required")
        try:
            outcome = replay.replay(
                self.cfg, self._records(), conversation_id,
                prompt=payload.get("prompt"),
                repeats=int(payload.get("repeats") or 1),
                source_picker=self.source_picker,
                model=str(payload.get("model", "")))
        except replay.ReplayError as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST, str(exc)) from exc
        self._writer().json(200, outcome.as_dict())

    def _route_admin_compare(self, body, parsed, identity) -> None:
        payload = json.loads(body.decode("utf-8") or "{}")
        models = payload.get("models") or self.cfg.published_models
        try:
            result = replay.compare(
                self.cfg, str(payload.get("prompt", "")), list(models or []),
                repeats=int(payload.get("repeats") or 1),
                source_picker=self.source_picker)
        except ValueError as exc:
            raise ProxyError(ErrorKind.BAD_REQUEST, str(exc)) from exc
        self._writer().json(200, result)

    def _route_admin_tuning_get(self, body, parsed, identity) -> None:
        self._writer().json(200, {"current": tuning.current(self.cfg), "fields": tuning.describe(),
                                  "config": self.cfg.as_dict()})

    def _route_admin_tuning_set(self, body, parsed, identity) -> None:
        payload = json.loads(body.decode("utf-8") or "{}")
        result = tuning.apply(self.cfg, payload)
        # Persist so a restart does not silently revert the operator's tuning.
        tuning.save(self.cfg.data_dir, result["applied"])
        self._writer().json(200, result)

    def _route_admin_retention(self, body, parsed, identity) -> None:
        payload = json.loads(body.decode("utf-8") or "{}")
        days = int(payload.get("days") or 0)
        if days < 1:
            raise ProxyError(ErrorKind.BAD_REQUEST, "days must be a positive integer")
        if not payload.get("confirm"):
            # Deleting history is destructive; require an explicit
            # acknowledgement rather than inferring intent from a number.
            raise ProxyError(ErrorKind.BAD_REQUEST,
                             "confirm=true is required to delete records")
        report = retention.prune_records(self.cfg.data_dir, days)
        self._writer().json(200, report.as_dict())


# ---- protocol translation ------------------------------------------------

_ANTHROPIC_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "stop_sequence",
}


def to_anthropic_message(response: dict, request=None) -> dict:
    """Convert an OpenAI chat completion into an Anthropic message object."""
    choice = (response.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    usage = response.get("usage") or {}
    content = []
    response_content = message.get("content")
    if isinstance(response_content, str) and response_content:
        content.append({"type": "text", "text": response_content})
    elif isinstance(response_content, list):
        for block in response_content:
            if isinstance(block, dict) and block.get("type") == "text":
                content.append({"type": "text", "text": str(block.get("text", ""))})
    for index, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        raw_input = function.get("arguments", "{}")
        try:
            tool_input = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
        except (ValueError, TypeError):
            tool_input = {"_raw": str(raw_input)}
        if not isinstance(tool_input, dict):
            tool_input = {"value": tool_input}
        content.append({
            "type": "tool_use",
            "id": str(call.get("id") or f"toolu_popo_{index}"),
            "name": str(function.get("name") or ""),
            "input": tool_input,
        })
    return {
        "id": response.get("id") or "msg_popo",
        "type": "message",
        "role": "assistant",
        "model": response.get("model") or getattr(request, "model", "unknown"),
        "content": content,
        "stop_reason": _ANTHROPIC_STOP.get(
            choice.get("finish_reason"),
            "tool_use" if message.get("tool_calls") else "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": int(usage.get("prompt_tokens") or 0),
                  "output_tokens": int(usage.get("completion_tokens") or 0)},
    }

def _model_of(response: dict) -> str:
    """The model the upstream actually reported, not what was requested."""
    return str((response or {}).get("model") or "")


def _close_iterator(iterator) -> None:
    """Tell a generator we are done with it, without swallowing real errors."""
    close = getattr(iterator, "close", None)
    if close is None:
        return
    try:
        close()
    except (BrokenPipeError, ConnectionResetError, StopIteration):
        pass


def _wants_stream(body: bytes) -> bool:
    try:
        return bool(json.loads(body.decode("utf-8") or "{}").get("stream"))
    except (ValueError, UnicodeDecodeError):
        return False


def _int_param(query: dict, name: str, default: int, *, low: int, high: int) -> int:
    raw = (query.get(name) or [str(default)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ProxyError(ErrorKind.BAD_REQUEST, f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise ProxyError(ErrorKind.BAD_REQUEST, f"{name} must be between {low} and {high}")
    return value
