"""
Client key management.

Only ``sha256(token)`` is stored. The plaintext token is returned exactly once by
:meth:`KeyStore.create` and never read back. Revocation takes effect on the next
request because verification always consults the store rather than a cache.

A key file that cannot be parsed is quarantined (renamed to ``.corrupt``) instead
of being treated as empty — silently forgetting every key is worse than a loud
failure, because the operator loses access without knowing why.
"""

from __future__ import annotations
import hashlib
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

from popo.storage.ephemeral import EphemeralGate


@dataclass
class KeyRecord:
    key_id: str
    hash: str
    label: str = ""
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_used: float = 0.0
    owner_id: str = ""
    allow_public: bool = False


class KeyStore:
    """Key store with atomic persistence and ephemeral gating."""

    FILE_NAME = "keys.json"

    def __init__(self, data_dir: str = "", gate: EphemeralGate | None = None):
        self.data_dir = data_dir
        self.gate = gate or EphemeralGate()
        self._keys: dict[str, KeyRecord] = {}
        self._load_error: str = ""
        self._load()

    @property
    def persistent(self) -> bool:
        """An empty data dir means an explicitly in-memory store (tests, ephemeral)."""
        return bool(self.data_dir) and not self.gate.is_enabled()

    @property
    def path(self) -> str:
        return os.path.join(self.data_dir, self.FILE_NAME)

    def _load(self) -> None:
        if not self.persistent:
            return
        path = self.path
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            records = raw.get("keys", raw) if isinstance(raw, dict) else raw
            for item in records if isinstance(records, list) else records.values():
                record = KeyRecord(
                    key_id=item["key_id"],
                    hash=item["hash"],
                    label=item.get("label", ""),
                    enabled=bool(item.get("enabled", True)),
                    created_at=item.get("created_at", 0.0),
                    last_used=item.get("last_used", 0.0),
                    owner_id=item.get("owner_id", ""),
                    # Keys created before per-key scoping retain their previous
                    # public API behavior. New keys default to internal-only.
                    allow_public=bool(item.get("allow_public", True)),
                )
                self._keys[record.key_id] = record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._load_error = str(exc)
            self._quarantine()

    def _quarantine(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = f"{self.path}.corrupt-{stamp}"
        try:
            os.replace(self.path, target)
            print(f"[POPO] unusable key file quarantined to {target}; "
                  f"all keys must be reissued")
        except OSError as exc:
            print(f"[POPO] could not quarantine unusable key file: {exc}")

    def _save(self) -> None:
        if not self.persistent:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        payload = {"schema": 1, "keys": [record.__dict__ for record in self._keys.values()]}
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".keys-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def create(self, label: str = "", owner_id: str = "", *,
               allow_public: bool = False) -> tuple[str, KeyRecord]:
        token = "triad-" + secrets.token_urlsafe(32)
        record = KeyRecord(
            key_id=secrets.token_hex(8),
            hash=hashlib.sha256(token.encode()).hexdigest(),
            label=label,
            owner_id=owner_id,
            allow_public=bool(allow_public),
        )
        self._keys[record.key_id] = record
        self._save()
        return token, record

    def verify(self, token: str) -> Optional[KeyRecord]:
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for record in self._keys.values():
            # Constant-time compare: a byte-by-byte early exit is observable.
            if secrets.compare_digest(record.hash, digest) and record.enabled:
                return record
        return None

    def touch(self, record: KeyRecord) -> None:
        record.last_used = time.time()
        self._save()

    def disable(self, key_id: str) -> bool:
        record = self._keys.get(key_id)
        if not record:
            return False
        record.enabled = False
        self._save()
        return True

    def delete(self, key_id: str) -> bool:
        if key_id not in self._keys:
            return False
        del self._keys[key_id]
        self._save()
        return True

    def list_all(self) -> list[KeyRecord]:
        return list(self._keys.values())

    def activate_admin(self, token: str) -> KeyRecord:
        """Register the operator admin token as a usable client key."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        record = KeyRecord(key_id="admin", hash=digest, label="admin",
                           allow_public=False)
        self._keys["admin"] = record
        self._save()
        return record
