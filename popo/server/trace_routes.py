"""Operator-only V1-style trace browsing endpoints."""
from __future__ import annotations

from urllib.parse import parse_qs

from popo.errors import ErrorKind, ProxyError


def _int_query(query: dict, name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int((query.get(name) or [default])[0])))
    except (TypeError, ValueError) as exc:
        raise ProxyError(ErrorKind.BAD_REQUEST, f"{name} must be an integer") from exc


class TraceRoutes:
    """Admin-gated client → conversation → turn views, with lazy detail reads."""

    def _route_admin_trace_clients(self, body, parsed, identity) -> None:
        query = parse_qs(parsed.query)
        days = _int_query(query, "days", 7, 1, 60)
        self._writer().json(200, {"clients": self._records().list_trace_clients(days), "days": days})

    def _route_admin_trace_conversations(self, body, parsed, identity) -> None:
        query = parse_qs(parsed.query)
        days = _int_query(query, "days", 7, 1, 60)
        client = (query.get("client") or [""])[0]
        self._writer().json(200, {"conversations": self._records().list_trace_conversations(client, days),
                                  "client": client, "days": days})

    def _route_admin_trace_turns(self, body, parsed, identity) -> None:
        query = parse_qs(parsed.query)
        days = _int_query(query, "days", 7, 1, 60)
        limit = _int_query(query, "limit", 60, 1, 200)
        client = (query.get("client") or [""])[0]
        conv = (query.get("conv") or [""])[0]
        self._writer().json(200, {"records": self._records().list_turns(client, conv, days, limit),
                                  "client": client, "conv": conv, "days": days})

    def _route_admin_trace_turn(self, body, parsed, identity) -> None:
        record_id = parsed.path.rsplit("/", 1)[-1]
        record = self._records().get_turn(record_id)
        if not record:
            raise ProxyError(ErrorKind.NOT_FOUND, "turn record not found")
        self._writer().json(200, {"ok": True, "record": record})

    def _route_admin_request_logs(self, body, parsed, identity) -> None:
        query = parse_qs(parsed.query)
        limit = _int_query(query, "limit", 200, 1, 200)
        self._writer().json(200, {"entries": self._records().recent_requests(limit), "limit": limit})

