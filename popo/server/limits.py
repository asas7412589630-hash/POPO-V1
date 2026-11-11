"""Bounded concurrency for upstream inference.

``ThreadingHTTPServer`` spawns a thread per connection. Without a cap, a burst
(or a hostile client) creates unbounded threads and unbounded upstream requests;
the proxy dies of the thing it was supposed to protect you from.

Properties:

* **Bounded**, with a wait deadline: a request that cannot get a slot within
  ``wait_seconds`` is refused with 429 rather than queued forever. Waiting
  forever looks like a hang and gives the client no signal.
* **Always released.** The slot is released in ``finally`` on every path,
  including exceptions and client cancellation — a leaked slot is a slow outage.
* **Observable.** Counters are exposed so the operator can see saturation
  instead of guessing from latency.
"""

from __future__ import annotations
import threading
import time
from contextlib import contextmanager

from popo.errors import ErrorKind, ProxyError


class ConcurrencyLimit:
    """A counting semaphore with a bounded wait and simple metrics."""

    def __init__(self, limit: int = 12, wait_seconds: float = 10.0):
        self.limit = max(1, int(limit))
        self.wait_seconds = max(0.0, float(wait_seconds))
        self._slots = threading.BoundedSemaphore(self.limit)
        self._lock = threading.Lock()
        self._in_flight = 0
        self._peak = 0
        self._rejected = 0

    @contextmanager
    def slot(self):
        """Acquire a slot or raise ``RATE_LIMITED``."""
        if self.wait_seconds:
            acquired = self._slots.acquire(timeout=self.wait_seconds)
        else:
            acquired = self._slots.acquire(blocking=False)
        if not acquired:
            with self._lock:
                self._rejected += 1
            raise ProxyError(
                ErrorKind.RATE_LIMITED,
                f"too many concurrent requests (limit {self.limit}); retry shortly")
        with self._lock:
            self._in_flight += 1
            self._peak = max(self._peak, self._in_flight)
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
            self._slots.release()

    def stats(self) -> dict:
        with self._lock:
            return {"limit": self.limit, "in_flight": self._in_flight,
                    "peak": self._peak, "rejected": self._rejected,
                    # Not `_value`: reading a semaphore's private counter is
                    # fine for diagnostics but must not be the value callers
                    # make decisions on.
                    "available": max(0, self.limit - self._in_flight)}
