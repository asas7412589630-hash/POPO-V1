"""Small cross-conversation fact ledger, scoped to one client key.

Only stable identifiers are carried forward. We do not copy prompts, prose, or
raw shell commands into future system messages.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from typing import Any

_ADDR = re.compile(r"\b0x[0-9a-fA-F]{4,12}\b")
_SYMBOL = re.compile(r"\b(?:[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*|Java_[A-Za-z0-9_]{3,})\b")
_TARGET = re.compile(r"(?<![\w.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:apk|so|dex|smali|aab|elf)\b", re.I)
_PROGRAMS = {"adb", "python", "python3", "git", "grep", "rg", "nm", "readelf",
             "objdump", "file", "apktool", "jadx", "zip", "unzip", "pip",
             "npm", "cargo", "go", "java", "strings"}


class StateLedgerStore:
    def __init__(self, data_dir: str, gate):
        self.path = os.path.join(data_dir, "state", "ledgers.json") if data_dir else ""
        self.gate = gate
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, list[str]]] = {}
        self._load()

    @staticmethod
    def _key(client_id: str) -> str:
        return hashlib.sha256((client_id or "anonymous").encode("utf-8", "replace")).hexdigest()

    def _load(self) -> None:
        if self.gate.is_enabled() or not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                self._items = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError) as exc:
            print(f"[POPO] state ledger ignored ({exc})")

    def get(self, client_id: str) -> dict[str, list[str]]:
        with self._lock:
            return {name: list(values) for name, values in
                    (self._items.get(self._key(client_id)) or {}).items()}

    def update(self, client_id: str, messages: list[dict[str, Any]]) -> None:
        found = {"targets": [], "symbols": [], "addresses": [], "programs": []}
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            if isinstance(content, str):
                if role == "user":
                    found["targets"].extend(_TARGET.findall(content)[:6])
                if role in ("tool", "assistant") or "0x" in content.lower():
                    found["addresses"].extend(_ADDR.findall(content)[:8])
                    found["symbols"].extend(_SYMBOL.findall(content)[:8])
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = (call or {}).get("function") or {}
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except (TypeError, ValueError):
                        args = {}
                    command = args.get("cmd") or args.get("command") or ""
                    if isinstance(command, str) and command.strip():
                        program = command.strip().split()[0].rsplit("/", 1)[-1]
                        if program in _PROGRAMS:
                            found["programs"].append(program)
        key = self._key(client_id)
        with self._lock:
            merged = self._items.setdefault(key, {})
            for name, values in found.items():
                prior = list(merged.get(name) or [])
                for value in values:
                    value = str(value)[:120]
                    if value in prior:
                        prior.remove(value)
                    prior.append(value)
                merged[name] = prior[-8:]
            self._save()

    def _save(self) -> None:
        if self.gate.is_enabled() or not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                          prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._items, handle, ensure_ascii=False, indent=2)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            print(f"[POPO] state ledger save failed: {exc}")

    def prompt_block(self, client_id: str) -> str:
        item = self.get(client_id)
        labels = (("targets", "TARGETS"), ("symbols", "SYMBOLS"),
                  ("addresses", "ADDRESSES"), ("programs", "TOOLS USED"))
        lines = [f"{label}: {' | '.join(item[name])}" for name, label in labels
                 if item.get(name)]
        return "[POPO STATE LEDGER - established identifiers only]\n" + "\n".join(lines) if lines else ""

