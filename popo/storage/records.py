"""Disk-backed records with ephemeral gating.

Records are append-only JSON lines. Nothing in core calls this directly in
ephemeral mode. All writes go through _append_jsonl.
"""

from __future__ import annotations
import json
import hashlib
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from popo.storage.ephemeral import EphemeralGate, get_gate
from popo.storage.state_ledger import StateLedgerStore
from popo.storage.turn_traces import TurnTraceStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RecordStore:
    """Append-only records: request log, trace, usage, conversation snapshots."""

    def __init__(self, data_dir: str, gate: EphemeralGate | None = None):
        self.data_dir = data_dir
        self.gate = gate or get_gate()
        self._handoff_lock = threading.RLock()
        self._handoffs: dict[str, dict[str, Any]] = {}
        self.turn_traces = TurnTraceStore(data_dir, self.gate)
        self.state_ledger = StateLedgerStore(data_dir, self.gate)
        self._archive_counts: dict[str, int] = {}
        self._ensure_dirs()
        self._load_handoffs()

    def _ensure_dirs(self) -> None:
        if self.gate.is_enabled():
            return
        for sub in ("logs", "runtime/trace", "runtime/archive", "state", "artifacts", "conversations"):
            os.makedirs(os.path.join(self.data_dir, sub), exist_ok=True)

    @property
    def _handoffs_path(self) -> str:
        return os.path.join(self.data_dir, "state", "handoffs.json")

    def _load_handoffs(self) -> None:
        if self.gate.is_enabled() or not self.data_dir or not os.path.exists(self._handoffs_path):
            return
        try:
            with open(self._handoffs_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                self._handoffs = {str(key): value for key, value in raw.items()
                                  if isinstance(value, dict)}
        except (OSError, ValueError) as exc:
            print(f"[POPO] context handoff state ignored ({exc})")

    def get_handoff(self, conversation_id: str) -> dict[str, Any]:
        """Return the saved long-task handoff for a conversation, if any."""
        if not conversation_id:
            return {}
        with self._handoff_lock:
            return dict(self._handoffs.get(conversation_id) or {})

    def save_handoff(self, conversation_id: str, text: str, model: str = "") -> bool:
        """Save a usable handoff; ephemeral mode keeps it in memory only."""
        text = (text or "").strip()
        if not conversation_id or len(text) < 300:
            return False
        with self._handoff_lock:
            previous = self._handoffs.get(conversation_id) or {}
            self._handoffs[conversation_id] = {
                "text": text,
                "model": model,
                "created_at": time.time(),
                "rotations": int(previous.get("rotations") or 0),
            }
            # Bound persistent state and discard summaries older than 30 days.
            cutoff = time.time() - 30 * 86400
            self._handoffs = {key: value for key, value in self._handoffs.items()
                              if float(value.get("created_at") or 0) >= cutoff}
            if not self.gate.is_enabled() and self.data_dir:
                self._save_handoffs()
        return True

    def mark_handoff_rotation(self, conversation_id: str) -> None:
        if not conversation_id:
            return
        with self._handoff_lock:
            record = self._handoffs.get(conversation_id)
            if not record:
                return
            record["rotations"] = int(record.get("rotations") or 0) + 1
            record["last_rotate"] = time.time()
            if not self.gate.is_enabled() and self.data_dir:
                self._save_handoffs()

    def _save_handoffs(self) -> None:
        os.makedirs(os.path.dirname(self._handoffs_path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self._handoffs_path),
                                   prefix=".handoffs-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._handoffs, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._handoffs_path)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            print(f"[POPO] could not persist context handoff: {exc}")

    def _append_jsonl(self, path: str, record: dict[str, Any]) -> None:
        if not self.gate.check("append"):
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_request(
        self,
        *,
        conversation_id: str,
        model: str,
        armor_blocks: list[str],
        attempts: int,
        verdict: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        duration_ms: int = 0,
        client_key_id: str = "",
    ) -> None:
        if not self.gate.check("request log"):
            return
        path = os.path.join(self.data_dir, "logs", "requests.jsonl")
        self._append_jsonl(path, {
            "ts": _now_iso(),
            "conversation_id": conversation_id,
            "model": model,
            "armor_blocks": armor_blocks,
            "attempts": attempts,
            "verdict": verdict,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "duration_ms": duration_ms,
            "client_key_id": client_key_id,
        })

    def append_trace(
        self,
        conversation_id: str,
        attempt: int,
        direction: str,
        record: dict[str, Any],
    ) -> None:
        if not self.gate.check("trace"):
            return
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = os.path.join(self.data_dir, "runtime", "trace", f"{today}.jsonl")
        self._append_jsonl(path, {
            "ts": _now_iso(),
            "conversation_id": conversation_id,
            "attempt": attempt,
            "direction": direction,
            **record,
        })

    def append_turn(self, record: dict[str, Any]) -> None:
        """Persist one V1-compatible turn record; failures never affect inference."""
        if not self.gate.check("turn trace"):
            return
        try:
            self.turn_traces.append(record)
        except Exception as exc:
            print(f"[POPO] turn trace write failed: {exc}")

    def list_trace_clients(self, days: int = 7) -> list[dict[str, Any]]:
        return self.turn_traces.clients(days)

    def list_trace_conversations(self, client: str = "", days: int = 7) -> list[dict[str, Any]]:
        return self.turn_traces.conversations(client, days)

    def list_turns(self, client: str = "", conv: str = "", days: int = 7,
                   limit: int = 60) -> list[dict[str, Any]]:
        return self.turn_traces.turns(client, conv, days, limit)

    def get_turn(self, record_id: str) -> dict[str, Any] | None:
        return self.turn_traces.get(record_id)

    def get_ledger(self, client_id: str) -> dict[str, list[str]]:
        return self.state_ledger.get(client_id)

    def update_ledger(self, client_id: str, messages: list[dict[str, Any]]) -> None:
        self.state_ledger.update(client_id, messages)

    def ledger_prompt(self, client_id: str) -> str:
        return self.state_ledger.prompt_block(client_id)

    def capture_response(self, record: dict[str, Any]) -> None:
        """Optional independent response log, matching V1's capture switch."""
        if not self.gate.check("response capture"):
            return
        path = os.path.join(self.data_dir, "logs", "responses.jsonl")
        self._append_jsonl(path, record)

    def write_artifacts(self, text: str, conversation_id: str = "") -> list[str]:
        """Save fenced code/patch blocks as inert Markdown artifacts."""
        if self.gate.is_enabled() or not text:
            return []
        blocks = re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S)
        paths = []
        for block in blocks[:20]:
            content = block[:1_000_000]
            if not content.strip():
                continue
            path = os.path.join(self.data_dir, "artifacts", f"{uuid.uuid4().hex}.md")
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(content)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                paths.append(path)
            except OSError as exc:
                print(f"[POPO] artifact write failed: {exc}")
        return paths

    def archive_snapshot(self, conversation_id: str, messages: list[dict[str, Any]]) -> bool:
        """Write every tenth turn as a periodic conversation snapshot."""
        if self.gate.is_enabled() or not conversation_id:
            return False
        count = self._archive_counts.get(conversation_id, 0) + 1
        self._archive_counts[conversation_id] = count
        if count % 10:
            return False
        key = hashlib.sha256(conversation_id.encode("utf-8", "replace")).hexdigest()[:24]
        path = os.path.join(self.data_dir, "runtime", "archive", f"{key}.jsonl")
        self._append_jsonl(path, {"ts": _now_iso(), "turn": count,
                                  "messages": messages})
        return True

    def write_conversation(
        self,
        conversation_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        if not self.gate.check("conversation"):
            return
        path = os.path.join(self.data_dir, "conversations", f"{conversation_id}.jsonl")
        # Keep only the latest snapshot per conversation per turn.
        self._append_jsonl(path, {
            "ts": _now_iso(),
            "messages": messages,
        })

    def write_usage(
        self,
        *,
        key_id: str,
        conversation_id: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
    ) -> None:
        if not self.gate.check("usage"):
            return
        path = os.path.join(self.data_dir, "state", "usage.jsonl")
        self._append_jsonl(path, {
            "ts": _now_iso(),
            "key_id": key_id,
            "conversation_id": conversation_id,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        })

    def read_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        if self.gate.is_enabled():
            return []
        path = os.path.join(self.data_dir, "conversations", f"{conversation_id}.jsonl")
        if not os.path.exists(path):
            return []
        out: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def recent_requests(self, limit: int = 40) -> list[dict[str, Any]]:
        """The last ``limit`` request-log rows, newest first."""
        if self.gate.is_enabled():
            return []
        path = os.path.join(self.data_dir, "logs", "requests.jsonl")
        if not os.path.exists(path):
            return []
        rows: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return list(reversed(rows[-max(1, limit):]))

    def read_usage(self, days: int = 7) -> list[dict[str, Any]]:
        """Return usage rows from the last ``days`` days (bounded file read)."""
        if self.gate.is_enabled():
            return []
        path = os.path.join(self.data_dir, "state", "usage.jsonl")
        if not os.path.exists(path):
            return []
        cutoff = datetime.now(timezone.utc).timestamp() - max(1, days) * 86400
        rows: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = row.get("ts", "")
                try:
                    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.timestamp() >= cutoff:
                    rows.append(row)
        return rows

    def list_conversations(self) -> list[dict[str, Any]]:
        if self.gate.is_enabled():
            return []
        conv_dir = os.path.join(self.data_dir, "conversations")
        if not os.path.isdir(conv_dir):
            return []
        entries = []
        for fn in os.listdir(conv_dir):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(conv_dir, fn)
            st = os.stat(path)
            entries.append({
                # ".jsonl" is six characters. Slicing seven silently truncated
                # the last character of every id, so detail lookups 404'd.
                "conversation_id": fn[: -len(".jsonl")],
                "updated_at": st.st_mtime,
                "size": st.st_size,
            })
        entries.sort(key=lambda x: x["updated_at"], reverse=True)
        return entries
