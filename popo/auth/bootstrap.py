"""First-run bootstrap token.

The problem this solves: a fresh install has no credential, so it is either wide
open or unusable. Neither is acceptable, especially when a tunnel is already
pointing at the port.

Properties that matter:

* the token is random and short-lived, not a constant;
* it is stored as a hash, and the plaintext is readable only by the operator on
  the device (a file with mode 0600, or a line printed once when nothing can be
  written);
* it dies the moment a real client key exists — a bootstrap credential that
  outlives bootstrap is just a backdoor.
"""

from __future__ import annotations
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time


class BootstrapToken:
    """One-shot token that authorises creating the first operator credential."""

    FILE_NAME = "setup_token.json"
    DEFAULT_TTL = 30 * 60

    def __init__(self, data_dir: str, ephemeral: bool = False, ttl: int = DEFAULT_TTL):
        self.data_dir = data_dir
        self.ephemeral = ephemeral
        self.ttl = ttl
        self._digest = ""
        self._expires = 0.0
        self._loaded = False

    @property
    def path(self) -> str:
        return os.path.join(self.data_dir, self.FILE_NAME)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.ephemeral or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            self._digest = str(state.get("digest", ""))
            self._expires = float(state.get("expires", 0))
        except (OSError, ValueError, TypeError):
            self._digest = ""
            self._expires = 0.0

    def _persist(self) -> None:
        if self.ephemeral:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".token-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"digest": self._digest, "expires": self._expires}, handle)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def issue(self) -> str:
        """Create a new token, replacing any previous one. Returns plaintext once."""
        token = "setup-" + secrets.token_urlsafe(32)
        self._digest = hashlib.sha256(token.encode()).hexdigest()
        self._expires = time.time() + self.ttl
        self._loaded = True
        self._persist()
        return token

    def verify(self, token: str) -> bool:
        self._load()
        if not token or not self._digest:
            return False
        if time.time() > self._expires:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        return hmac.compare_digest(self._digest, digest)

    def consume(self, token: str) -> bool:
        """Verify and immediately invalidate: a bootstrap token works exactly once."""
        if not self.verify(token):
            return False
        self.revoke()
        return True

    def revoke(self) -> None:
        self._digest = ""
        self._expires = 0.0
        self._loaded = True
        if not self.ephemeral:
            try:
                os.unlink(self.path)
            except OSError:
                pass

    @property
    def active(self) -> bool:
        self._load()
        return bool(self._digest) and time.time() <= self._expires
