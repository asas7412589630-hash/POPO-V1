"""Compatibility routes for the original console.

The 2.0.1 console talks to bare paths (``/keys``, ``/panel/config``, ``/logs``…)
while this codebase grew ``/admin/*`` equivalents. Rewriting the console would
lose the screens it already has; duplicating the handlers would let the two drift.

So: aliases. Each entry maps a console path onto the handler that already exists,
and the auth level is *at least* as strict as the target's — an alias must never
be a way around a check.
"""

from __future__ import annotations
from dataclasses import dataclass

OPERATOR = "operator"
CLIENT = "client"
PUBLIC = "public"


@dataclass(frozen=True)
class Alias:
    """A console path plus how it maps onto an existing handler."""

    method: str
    path: str
    target: str          # the ``_route_*`` suffix of the handler to run
    auth: str
    match: str = "exact"


# Every alias targets an existing handler; none introduces new behaviour.
ALIASES: tuple[Alias, ...] = (
    # --- reads the console performs on load -------------------------------
    Alias("GET", "/panel/config", "admin_tuning_get", OPERATOR),
    Alias("POST", "/panel/config", "admin_tuning_set", OPERATOR),
    Alias("GET", "/keys", "admin_keys_list", OPERATOR),
    Alias("POST", "/keys", "admin_keys_action", OPERATOR),
    Alias("DELETE", "/keys/", "admin_keys_action", OPERATOR, match="prefix"),
    Alias("GET", "/logs", "logs", OPERATOR),
    Alias("GET", "/audit", "admin_audit", OPERATOR),
    Alias("GET", "/armor/info", "armor_registry", PUBLIC),
    Alias("POST", "/compare", "admin_compare", OPERATOR),
    Alias("POST", "/replay", "admin_replay", OPERATOR),
    Alias("POST", "/models/publish", "setup_publish", OPERATOR),
    Alias("GET", "/access", "access", PUBLIC),
    Alias("GET", "/sources", "admin_sources_list", OPERATOR),
    Alias("POST", "/sources/", "sources_reset", OPERATOR, match="prefix"),
)


def match_alias(method: str, path: str) -> Alias | None:
    for alias in ALIASES:
        if alias.method != method:
            continue
        if alias.match == "exact" and alias.path == path:
            return alias
        if alias.match == "prefix" and path.startswith(alias.path):
            # ``/sources/<name>/reset`` carries the name in the path; the handler
            # reads it from the request, so no rewriting is needed here.
            return alias
    return None
