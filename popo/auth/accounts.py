"""
Account and session management.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-account salt and a
configurable iteration count; comparisons are constant time. Sessions live in a
signed cookie and also in the store, so a revocation is immediate.

Unreadable state is quarantined rather than being replaced by an empty dict —
losing every account silently would lock the operator out with no explanation.
"""

from __future__ import annotations
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from popo.storage.ephemeral import EphemeralGate

PBKDF2_ITERATIONS = 240_000
SESSION_TTL_SECONDS = 7 * 24 * 3600


@dataclass
class Account:
    uid: str
    username: str
    password_hash: str
    salt: str
    role: str = "owner"
    created_at: float = field(default_factory=time.time)
    disabled: bool = False
    iterations: int = PBKDF2_ITERATIONS
    algo: str = "pbkdf2_sha256"


def hash_password(password: str, salt: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt.encode("utf-8"), iterations).hex()


def verify_password(password: str, account: Account) -> bool:
    if account.algo == "pbkdf2_sha256":
        candidate = hash_password(password, account.salt, account.iterations)
    else:  # legacy 1.x/2.0.0 format: sha256(salt + password)
        candidate = hashlib.sha256((account.salt + password).encode()).hexdigest()
    return hmac.compare_digest(candidate, account.password_hash)


class AccountStore:
    """Account store with atomic persistence and ephemeral gating."""

    def __init__(self, data_dir: str = "", gate: EphemeralGate | None = None):
        self.data_dir = data_dir
        self.gate = gate or EphemeralGate()
        self._accounts: dict[str, Account] = {}
        self._sessions: dict[str, dict] = {}
        self._sessions_path = os.path.join(data_dir, "sessions.json") if data_dir else ""
        self._load()
        self._load_sessions()

    @property
    def persistent(self) -> bool:
        return bool(self.data_dir) and not self.gate.is_enabled()

    # ---- persistence -------------------------------------------------
    def _path(self) -> str:
        return os.path.join(self.data_dir, "accounts.json")

    def _load(self) -> None:
        if not self.persistent or not os.path.exists(self._path()):
            return
        try:
            with open(self._path(), "r", encoding="utf-8") as handle:
                data = json.load(handle)
            for uid, rec in data.items():
                self._accounts[uid] = Account(**rec)
        except (OSError, ValueError, TypeError) as exc:
            self._quarantine(self._path(), exc)

    def _quarantine(self, path: str, exc: Exception) -> None:
        target = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(path, target)
            print(f"[POPO] unusable state quarantined to {target}: {exc}")
        except OSError:
            print(f"[POPO] unusable state at {path}: {exc}")

    def _write_json(self, path: str, payload) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save(self) -> None:
        if not self.persistent:
            return
        self._write_json(self._path(), {uid: asdict(acc) for uid, acc in self._accounts.items()})

    def _load_sessions(self) -> None:
        if not self.persistent or not self._sessions_path or not os.path.exists(self._sessions_path):
            return
        try:
            with open(self._sessions_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            now = time.time()
            self._sessions = {sid: rec for sid, rec in raw.items()
                              if isinstance(rec, dict) and rec.get("expires", 0) > now}
        except (OSError, ValueError, AttributeError) as exc:
            self._quarantine(self._sessions_path, exc)

    def _save_sessions(self) -> None:
        if not self.persistent or not self._sessions_path:
            return
        self._write_json(self._sessions_path, self._sessions)

    # ---- accounts ----------------------------------------------------
    def list_all(self) -> list[Account]:
        return list(self._accounts.values())

    def count(self) -> int:
        return len(self._accounts)

    def find(self, username: str) -> Optional[Account]:
        for account in self._accounts.values():
            if account.username == username:
                return account
        return None

    def create(self, username: str, password: str, role: str = "owner") -> Account:
        username = (username or "").strip()
        if not username:
            raise ValueError("username is required")
        if not password:
            raise ValueError("password is required")
        if self.find(username):
            raise ValueError("username already exists")
        salt = secrets.token_hex(16)
        account = Account(
            uid="u_" + secrets.token_hex(6),
            username=username,
            password_hash=hash_password(password, salt),
            salt=salt,
            role=role,
        )
        self._accounts[account.uid] = account
        self._save()
        return account

    def update(self, account: Account) -> None:
        self._accounts[account.uid] = account
        self._save()

    def auth(self, username: str, password: str) -> Optional[Account]:
        account = self.find(username)
        if not account or account.disabled:
            return None
        if not verify_password(password or "", account):
            return None
        if account.algo != "pbkdf2_sha256":
            # Transparent upgrade of a legacy hash, once, on successful login.
            salt = secrets.token_hex(16)
            account.salt = salt
            account.algo = "pbkdf2_sha256"
            account.iterations = PBKDF2_ITERATIONS
            account.password_hash = hash_password(password, salt)
            self._save()
        return account

    def set_password(self, uid: str, password: str) -> bool:
        account = self._accounts.get(uid)
        if not account:
            return False
        salt = secrets.token_hex(16)
        account.salt = salt
        account.algo = "pbkdf2_sha256"
        account.iterations = PBKDF2_ITERATIONS
        account.password_hash = hash_password(password, salt)
        self._save()
        return True

    def disable(self, uid: str) -> bool:
        account = self._accounts.get(uid)
        if not account:
            return False
        account.disabled = True
        self._save()
        for sid, rec in list(self._sessions.items()):
            if rec.get("uid") == uid:
                del self._sessions[sid]
        self._save_sessions()
        return True

    # ---- sessions ----------------------------------------------------
    def create_session(self, uid: str, ttl: int = SESSION_TTL_SECONDS) -> str:
        sid = "s_" + secrets.token_urlsafe(32)
        self._sessions[sid] = {"uid": uid, "expires": time.time() + ttl}
        self._save_sessions()
        return sid

    def verify_session(self, sid: str) -> Optional[Account]:
        rec = self._sessions.get(sid or "")
        if not rec:
            return None
        if rec.get("expires", 0) <= time.time():
            self._sessions.pop(sid, None)
            self._save_sessions()
            return None
        account = self._accounts.get(rec.get("uid", ""))
        if not account or account.disabled:
            return None
        return account

    def revoke_session(self, sid: str) -> bool:
        if sid in self._sessions:
            del self._sessions[sid]
            self._save_sessions()
            return True
        return False

    def bootstrap(self) -> Optional[Account]:
        if self._accounts:
            return None
        password = secrets.token_urlsafe(24)
        account = self.create("owner", password)
        print("[POPO] bootstrap owner account created; password printed once:")
        print(f"[POPO] owner password: {password}")
        return account
