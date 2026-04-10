"""In-memory sliding-window rate limiter.

This is a **defence-in-depth** layer meant to run inside a
single-process FastAPI deployment. It does not share state across
workers — when the service is scaled to multiple processes the limits
will effectively multiply by the worker count, so for production a
reverse-proxy (nginx/traefik) should enforce the primary cap and this
limiter exists as a safety net.

Design:
  * Sliding window with per-key request log (``deque[float]``).
  * ``check()`` is O(N) in the size of the bucket but in practice N is
    bounded by ``max_requests`` because anything above is pruned on
    entry.
  * Thread-safe via a single ``threading.Lock`` — the granularity is
    coarse but the work inside the critical section is tiny (O(1) deque
    pops, O(1) dict access) and callers are HTTP request handlers that
    already have much larger costs elsewhere.
  * Clock is injected so tests can run deterministically without
    ``time.sleep``.
"""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
from time import monotonic
from typing import Callable


class InMemoryRateLimiter:
    def __init__(
        self,
        *,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_requests < 0:
            raise ValueError("max_requests must be >= 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max = max_requests
        self._window = window_seconds
        self._clock = clock or monotonic
        self._log: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> bool:
        """Return ``True`` if a request under ``key`` should be allowed.

        Records the current timestamp on success. On failure, nothing
        is recorded — an attacker hammering the endpoint does not get
        to shift their own window by making more attempts.
        """

        if self._max == 0:
            return False
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            bucket = self._log[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        """Drop all recorded state. Intended for tests."""

        with self._lock:
            self._log.clear()
