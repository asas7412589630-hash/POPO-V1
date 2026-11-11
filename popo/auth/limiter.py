"""Bounded rate limiting for credential attempts.

Two properties matter more than the algorithm:

* **Bounded memory.** An attacker choosing new identities must not be able to
  grow the table without limit; the oldest window is evicted.
* **No lockout by proxy.** A rate limit keyed on the *account* would let anyone
  lock the owner out by guessing. Keying is on the caller address, and the
  operator's own loopback traffic is exempt so a local mistake is recoverable.
"""

from __future__ import annotations
import time
from collections import deque


class AttemptLimiter:
    """Sliding-window attempt counter keyed by caller identity."""

    def __init__(self, max_attempts: int = 10, window_seconds: float = 300.0,
                 max_keys: int = 4096):
        self.max_attempts = max(1, int(max_attempts))
        self.window = float(window_seconds)
        self.max_keys = max(16, int(max_keys))
        self._hits: dict[str, deque] = {}

    def _prune(self, key: str, now: float) -> deque:
        hits = self._hits.get(key)
        if hits is None:
            hits = deque()
            self._hits[key] = hits
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def _evict_if_needed(self) -> None:
        if len(self._hits) <= self.max_keys:
            return
        # Drop the oldest quarter rather than one entry: evicting one per insert
        # turns every subsequent request into an eviction walk.
        stale = sorted(self._hits.items(), key=lambda item: item[1][-1] if item[1] else 0.0)
        for key, _ in stale[: max(1, len(stale) // 4)]:
            self._hits.pop(key, None)

    def allowed(self, key: str) -> bool:
        now = time.time()
        hits = self._prune(key, now)
        return len(hits) < self.max_attempts

    def record_failure(self, key: str) -> None:
        now = time.time()
        hits = self._prune(key, now)
        hits.append(now)
        self._evict_if_needed()

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def retry_after(self, key: str) -> int:
        hits = self._prune(key, time.time())
        if len(hits) < self.max_attempts:
            return 0
        return max(1, int(self.window - (time.time() - hits[0])))
