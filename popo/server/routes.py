"""Route table: every endpoint declares its own auth level.

The dispatcher enforces the level before the endpoint runs, so a new route
cannot be born unauthenticated. Auth is a property of the route, not of the
handler body — that is the difference between "we remembered to check" and
"it is impossible to forget".
"""

from __future__ import annotations
from dataclasses import dataclass

from popo.auth.gate import Identity
from popo.config import Config
from popo.errors import ErrorKind, ProxyError

PUBLIC = "public"
CLIENT = "client"
OPERATOR = "operator"

# A public client key authorizes inference only. Keep consumer conversation
# history and diagnostic previews local/operator-only even though they also
# carry the CLIENT auth level for trusted internal clients.
PUBLIC_CLIENT_ROUTES = frozenset({
    "chat", "anthropic_messages", "images", "embeddings",
    "audio_transcriptions", "adult_chat", "adult_image",
})


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    auth: str
    name: str
    match: str = "exact"  # exact | prefix


ROUTES: tuple[Route, ...] = (
    # ---- liveness / discovery (no secret leaves through these) ----
    Route("GET", "/healthz", PUBLIC, "health"),
    Route("GET", "/v1/models", PUBLIC, "models"),
    Route("GET", "/armor/registry", PUBLIC, "armor_registry"),
    Route("GET", "/armor", CLIENT, "armor_preview"),
    Route("GET", "/v1/whoami", PUBLIC, "whoami"),

    # ---- first-run setup ----
    Route("GET", "/setup/status", PUBLIC, "setup_status"),
    Route("GET", "/setup/models", PUBLIC, "setup_models"),
    Route("POST", "/setup/test", PUBLIC, "setup_test"),
    Route("POST", "/setup/list-models", PUBLIC, "setup_list_models"),
    Route("POST", "/setup/verify-key", PUBLIC, "setup_verify_key"),
    Route("GET", "/setup/token", PUBLIC, "setup_token"),
    Route("POST", "/setup/quick", PUBLIC, "setup_quick"),
    Route("POST", "/setup/models", OPERATOR, "setup_publish"),

    # ---- inference ----
    Route("POST", "/v1/chat/completions", CLIENT, "chat"),
    Route("POST", "/v1/messages", CLIENT, "anthropic_messages"),
    Route("POST", "/v1/images/generations", CLIENT, "images"),
    Route("POST", "/v1/embeddings", CLIENT, "embeddings"),
    Route("POST", "/v1/audio/transcriptions", CLIENT, "audio_transcriptions"),
    Route("POST", "/v1/adult/chat", CLIENT, "adult_chat"),
    Route("POST", "/v1/adult/image", CLIENT, "adult_image"),

    # ---- account / session ----
    Route("POST", "/v1/login", PUBLIC, "login"),
    Route("POST", "/v1/logout", PUBLIC, "logout"),

    # ---- management ----
    Route("GET", "/admin/keys", OPERATOR, "admin_keys_list"),
    Route("POST", "/admin/keys", OPERATOR, "admin_keys_action"),
    Route("GET", "/admin/conversations", OPERATOR, "admin_conversations"),
    Route("GET", "/admin/conversations/", OPERATOR, "admin_conversation_detail", match="prefix"),
    Route("GET", "/admin/profile", OPERATOR, "admin_profile"),
    Route("GET", "/admin/usage", OPERATOR, "admin_usage"),
    Route("GET", "/admin/accounts", OPERATOR, "admin_accounts"),
    Route("POST", "/admin/accounts", OPERATOR, "admin_accounts_action"),
    Route("GET", "/admin/audit", OPERATOR, "admin_audit"),
    Route("POST", "/admin/replay", OPERATOR, "admin_replay"),
    Route("POST", "/admin/compare", OPERATOR, "admin_compare"),
    Route("POST", "/admin/retention", OPERATOR, "admin_retention"),
    Route("GET", "/admin/trace/clients", OPERATOR, "admin_trace_clients"),
    Route("GET", "/admin/trace/conversations", OPERATOR, "admin_trace_conversations"),
    Route("GET", "/admin/trace/turns", OPERATOR, "admin_trace_turns"),
    Route("GET", "/admin/trace/turns/", OPERATOR, "admin_trace_turn", match="prefix"),
    Route("GET", "/admin/request-logs", OPERATOR, "admin_request_logs"),
    Route("GET", "/admin/sources", OPERATOR, "admin_sources_list"),
    Route("POST", "/admin/sources/discover", OPERATOR, "admin_sources_discover"),
    Route("POST", "/admin/sources/test", OPERATOR, "admin_sources_test"),
    Route("POST", "/admin/sources", OPERATOR, "admin_sources_action"),
    Route("POST", "/admin/sources/publish", OPERATOR, "admin_sources_publish"),
    Route("GET", "/admin/model-routes", OPERATOR, "admin_model_routes"),
    Route("GET", "/admin/tuning", OPERATOR, "admin_tuning_get"),
    Route("POST", "/admin/tuning", OPERATOR, "admin_tuning_set"),
    Route("GET", "/routes", OPERATOR, "routes"),

    # ---- consumer-facing records ----
    Route("GET", "/conversations", CLIENT, "conversations"),
    Route("GET", "/conversations/", CLIENT, "conversation_detail", match="prefix"),
)


def match_route(method: str, path: str) -> Route | None:
    for route in ROUTES:
        if route.method != method:
            continue
        if route.match == "exact" and route.path == path:
            return route
        if route.match == "prefix" and path.startswith(route.path):
            return route
    return None


def authorize(route: Route, identity: Identity, config: Config) -> None:
    """Raise unless the identity satisfies the route's declared level."""
    if route.auth == PUBLIC:
        return
    if route.auth == OPERATOR:
        if identity.level == "operator":
            return
        raise ProxyError(ErrorKind.UNAUTHORIZED, "operator credential required")
    # CLIENT
    if identity.external_request:
        if (route.name in PUBLIC_CLIENT_ROUTES
                and identity.level == "client-key"
                and identity.allow_public):
            return
        raise ProxyError(ErrorKind.UNAUTHORIZED,
                         "an API key enabled for public access is required")
    if not config.require_client_key:
        return
    if identity.level in ("operator", "client-key"):
        return
    # "loopback" now means "a local tool", not merely "a loopback peer": a tunnel
    # client also arrives from 127.0.0.1, and treating it as local would let the
    # whole internet use this proxy without a key.
    if identity.level == "loopback" and config.loopback_trusted:
        return
    raise ProxyError(ErrorKind.UNAUTHORIZED, "client key required")
