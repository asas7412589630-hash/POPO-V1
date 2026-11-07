#!/usr/bin/env python3
"""POPO-V1 Sliding-Window Rate Limiter (armor_limiter).

Tracks requests per identity across sliding minute, hour, and day windows.
Loads configuration dynamically from rate_limit.json. Includes proactive
memory eviction of expired sliding windows to prevent unbounded memory leaks
under long-running production uptime.

Standard library only. No third-party dependencies.
"""

import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple


class RateLimiter:
    """Sliding-window counters per identity, configured via rate_limit.json."""

    def __init__(self, path: str, prune_interval: float = 60.0):
        self.path = path
        self.cfg: dict = {}
        self.state: Dict[str, Dict[str, List[float]]] = {}
        self.lock = threading.Lock()
        self.prune_interval = prune_interval
        self._last_prune = time.time()

    def _reload(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.cfg = data
        except Exception:
            pass

    def enabled(self) -> bool:
        if not self.cfg:
            self._reload()
        return bool(self.cfg.get("enabled"))

    def prune(self, now: Optional[float] = None, force: bool = False) -> int:
        """Evict identities with no active timestamps in any sliding window.

        Returns the number of evicted identities.
        """
        if now is None:
            now = time.time()
        if not force and (now - self._last_prune < self.prune_interval) and len(self.state) < 1000:
            return 0

        self._last_prune = now
        to_del = []
        for identity, windows in list(self.state.items()):
            # Filter timestamps
            windows["minute"] = [t for t in windows.get("minute", []) if now - t < 60]
            windows["hour"] = [t for t in windows.get("hour", []) if now - t < 3600]
            windows["day"] = [t for t in windows.get("day", []) if now - t < 86400]
            if not windows["minute"] and not windows["hour"] and not windows["day"]:
                to_del.append(identity)

        for identity in to_del:
            self.state.pop(identity, None)

        return len(to_del)

    def check(self, identity: str, path: str) -> Tuple[bool, str]:
        """Check whether the request by `identity` to `path` is within quota.

        Returns (ok: bool, window_name: str).
        """
        if not self.enabled():
            return True, "-"

        paths_cfg = self.cfg.get("paths") or {}
        prefixes = paths_cfg.get("apply_prefixes") or []
        if prefixes and not any(path.startswith(p) for p in prefixes):
            return True, "-"
        if path in (paths_cfg.get("exclude_exact") or []):
            return True, "-"

        scope = (self.cfg.get("scopes") or {}).get("global") or {}
        per_min = float(scope.get("per_minute") or 0)
        per_hour = float(scope.get("per_hour") or 0)
        per_day = float(scope.get("per_day") or 0)
        now = time.time()

        with self.lock:
            # Periodic hygiene prune
            if (now - self._last_prune > self.prune_interval) or len(self.state) > 1000:
                self.prune(now=now, force=True)

            st = self.state.setdefault(identity, {"minute": [], "hour": [], "day": []})
            st["minute"] = [t for t in st["minute"] if now - t < 60]
            st["hour"] = [t for t in st["hour"] if now - t < 3600]
            st["day"] = [t for t in st["day"] if now - t < 86400]

            if per_min and len(st["minute"]) >= per_min:
                return False, "minute"
            if per_hour and len(st["hour"]) >= per_hour:
                return False, "hour"
            if per_day and len(st["day"]) >= per_day:
                return False, "day"

            st["minute"].append(now)
            st["hour"].append(now)
            st["day"].append(now)

        return True, "-"

    def reset(self) -> None:
        """Reset internal rate limiting state."""
        with self.lock:
            self.state.clear()
            self._last_prune = time.time()
