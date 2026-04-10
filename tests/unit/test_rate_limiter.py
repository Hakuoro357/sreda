"""Unit tests for the in-memory rate limiter.

The limiter is a defence-in-depth layer on top of (future) ingress
rate-limiting: it protects single-process deployments and guarantees
a floor of safety even when no reverse-proxy is in front. It runs
entirely in-memory, so the tests control time via an injected clock
instead of ``time.sleep`` to keep them deterministic.
"""

from __future__ import annotations

from itertools import count

from sreda.services.rate_limiter import InMemoryRateLimiter


def test_rate_limiter_allows_requests_under_the_cap() -> None:
    ticks = iter(count(0))
    limiter = InMemoryRateLimiter(
        max_requests=3,
        window_seconds=60.0,
        clock=lambda: float(next(ticks)),
    )

    assert limiter.check("ip-1") is True
    assert limiter.check("ip-1") is True
    assert limiter.check("ip-1") is True


def test_rate_limiter_rejects_requests_over_the_cap() -> None:
    # Freeze the clock so the whole burst lands in the same window.
    limiter = InMemoryRateLimiter(
        max_requests=2,
        window_seconds=60.0,
        clock=lambda: 1000.0,
    )

    assert limiter.check("ip-1") is True
    assert limiter.check("ip-1") is True
    assert limiter.check("ip-1") is False


def test_rate_limiter_isolates_keys() -> None:
    limiter = InMemoryRateLimiter(
        max_requests=1,
        window_seconds=60.0,
        clock=lambda: 1000.0,
    )

    assert limiter.check("ip-1") is True
    assert limiter.check("ip-2") is True
    # Second call from ip-1 still blocked even though ip-2 is fine.
    assert limiter.check("ip-1") is False
    assert limiter.check("ip-2") is False


def test_rate_limiter_forgets_expired_entries() -> None:
    now = [1000.0]
    limiter = InMemoryRateLimiter(
        max_requests=2,
        window_seconds=10.0,
        clock=lambda: now[0],
    )

    assert limiter.check("ip-1") is True
    assert limiter.check("ip-1") is True
    assert limiter.check("ip-1") is False

    # Advance time past the window — the limiter must release the slot.
    now[0] += 11.0
    assert limiter.check("ip-1") is True
    assert limiter.check("ip-1") is True
    assert limiter.check("ip-1") is False


def test_rate_limiter_max_zero_blocks_everything() -> None:
    limiter = InMemoryRateLimiter(
        max_requests=0,
        window_seconds=60.0,
        clock=lambda: 1000.0,
    )

    # A ``max_requests=0`` config should deny every call — useful as a
    # kill-switch in config without removing the Depends wiring.
    assert limiter.check("ip-1") is False


def test_rate_limiter_is_thread_safe() -> None:
    # Smoke test: 50 threads hammer the same key concurrently. With a
    # single-request window the total number of allowed calls must be
    # exactly 1, regardless of interleaving.
    import threading

    limiter = InMemoryRateLimiter(
        max_requests=1,
        window_seconds=60.0,
        clock=lambda: 1000.0,
    )
    allowed: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(50)

    def worker() -> None:
        barrier.wait()
        result = limiter.check("shared")
        with lock:
            allowed.append(result)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for ok in allowed if ok) == 1
    assert sum(1 for ok in allowed if not ok) == 49
