"""Per-turn records with client → conversation → turn indexes.

The list methods return headers only; full attempt and answer bodies are loaded
only after an operator opens one turn. Daily JSONL files make retention simple
and keep unrelated conversations out of each lookup.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

ATTEMPT_TEXT_CAP = 30000
_WRITE_LOCK = threading.Lock()


class TurnTraceStore:
    def __init__(self, data_dir: str, gate):
        self.data_dir = data_dir
        self.gate = gate
        self.path = os.path.join(data_dir, "runtime", "trace")

    def append(self, record: dict[str, Any]) -> None:
        if not self.data_dir or self.gate.is_enabled():
            return
        os.makedirs(self.path, exist_ok=True)
        row = dict(record)
        now = float(row.get("ts") or time.time())
        row.setdefault("id", f"{int(now * 1000)}-{uuid.uuid4().hex[:8]}")
        row["ts"] = now
        row.setdefault("when", datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        attempts = []
        for item in row.get("attempts") or []:
            attempt = dict(item)
            body = str(attempt.get("text") or "")
            attempt.setdefault("chars", len(body))
            attempt["text"] = body[:ATTEMPT_TEXT_CAP]
            attempts.append(attempt)
        row["attempts"] = attempts
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        with _WRITE_LOCK:
            with open(os.path.join(self.path, day + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _read(self, days: int = 7) -> list[dict[str, Any]]:
        if self.gate.is_enabled() or not os.path.isdir(self.path):
            return []
        count = max(1, min(60, int(days)))
        now = time.time()
        files = [os.path.join(self.path, datetime.fromtimestamp(
            now - i * 86400, timezone.utc).strftime("%Y-%m-%d") + ".jsonl")
            for i in range(count)]
        rows = []
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(record, dict):
                            rows.append(record)
            except OSError:
                continue
        rows.sort(key=lambda item: float(item.get("ts") or 0), reverse=True)
        return rows

    @staticmethod
    def _header(record: dict[str, Any]) -> dict[str, Any]:
        item = {key: value for key, value in record.items()
                if key not in ("attempts", "final", "request")}
        item["kinds"] = [attempt.get("kind", "") for attempt in record.get("attempts") or []]
        item["attempt_count"] = len(record.get("attempts") or [])
        item["user"] = str(record.get("user") or "")[:160]
        # Keep list payloads small and independent of the saved request body.
        return item

    def clients(self, days: int = 7) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in self._read(days):
            client = str(row.get("client") or "anonymous")
            item = grouped.setdefault(client, {"client": client, "turns": 0,
                "convs": set(), "last": 0.0, "models": set()})
            item["turns"] += 1
            item["convs"].add(str(row.get("conv") or "unknown"))
            item["models"].add(str(row.get("model") or "?"))
            item["last"] = max(item["last"], float(row.get("ts") or 0))
        result = []
        for item in grouped.values():
            item["convs"] = sorted(item["convs"])
            item["models"] = sorted(item["models"])
            item["last_when"] = datetime.fromtimestamp(item["last"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            result.append(item)
        return sorted(result, key=lambda item: item["last"], reverse=True)

    def conversations(self, client: str = "", days: int = 7) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in self._read(days):
            owner = str(row.get("client") or "anonymous")
            if client and owner != client:
                continue
            conv = str(row.get("conv") or "unknown")
            item = grouped.setdefault((owner, conv), {"conv": conv, "client": owner,
                "turns": 0, "last": 0.0, "models": [], "retries": 0,
                "errors": 0, "first_user": "", "_first_user_ts": float("inf")})
            item["turns"] += 1
            row_ts = float(row.get("ts") or 0)
            item["last"] = max(item["last"], row_ts)
            item["retries"] += max(0, len(row.get("attempts") or []) - 1)
            item["errors"] += int(bool(row.get("error")))
            model = str(row.get("model") or "?")
            if model not in item["models"]:
                item["models"].append(model)
            user = str(row.get("user") or "")
            if user and row_ts < item["_first_user_ts"]:
                item["_first_user_ts"] = row_ts
                item["first_user"] = user[:120]
        result = []
        for item in grouped.values():
            item.pop("_first_user_ts", None)
            item["last_when"] = datetime.fromtimestamp(item["last"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            item["mixed_models"] = len(item["models"]) > 1
            result.append(item)
        return sorted(result, key=lambda item: item["last"], reverse=True)

    def turns(self, client: str = "", conv: str = "", days: int = 7,
              limit: int = 60) -> list[dict[str, Any]]:
        rows = []
        for row in self._read(days):
            if client and row.get("client") != client:
                continue
            if conv and row.get("conv") != conv:
                continue
            rows.append(self._header(row))
            if len(rows) >= max(1, min(200, int(limit))):
                break
        return rows

    def get(self, record_id: str, days: int = 60) -> dict[str, Any] | None:
        if not record_id:
            return None
        return next((row for row in self._read(days) if row.get("id") == record_id), None)

