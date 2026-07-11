"""LLM reliability guard — bulkhead + per-provider circuit breaker (#343 / F7).

Why this exists
---------------
Every chat-LLM call runs on a per-call ``ThreadPoolExecutor`` (see
``services/llm.py``). The wall-clock timeout there frees the *turn* but cannot
kill the worker thread: a provider that accepts the connection and then hangs
(keepalive chunks reset httpx's per-chunk read timer — a 131s reply without a
``TimeoutError`` was observed in prod) leaves the thread **and** its HTTP
socket alive. A run of hangs — or a fallback that duplicates the call over a
still-hung primary — accumulates threads/sockets until the process is
exhausted. The old ``llm.py`` comment declared this acceptable "at 10
concurrent users"; the wide-launch vector (alpha 200-300) voids that.

Two finite-limit, process-global, env-tunable mechanisms:

1. **Bulkhead** — a counter-based semaphore bounding the number of LLM
   invocations *concurrently in flight* (hung threads included). The caller
   holds the permit for the **entire lifetime of the worker thread** (released
   via ``future.add_done_callback``), NOT on the wall-clock timeout — otherwise
   a provider that always hangs would let each call acquire→timeout→release→
   acquire… and spawn unbounded hung threads. Once ``max_concurrent`` permits
   are held, a new call fast-fails with :class:`LLMBulkheadFull` *before*
   creating a thread.

2. **Circuit breaker** (per provider) — after ``open_after`` consecutive
   wall-clock timeouts, the breaker OPENS: new calls to that provider fast-fail
   with :class:`LLMCircuitOpen` (no thread, no socket) for ``cooldown``
   seconds, then HALF-OPEN admits a single probe. A success closes it; a
   timeout re-opens it. This relieves bulkhead pressure (a dead provider does
   not permanently saturate the bulkhead from *new* calls) and self-heals.

The breaker counts **timeouts** (the hang signal), not fast provider errors
(5xx/429 return promptly, the thread completes, the permit releases — no
accumulation), so ordinary transient errors never trip it.

Kill switch: ``settings.llm_reliability_guard_enabled = False`` makes both
mechanisms no-ops (legacy byte-for-byte path) for one-flag rollback.

The exception classes live in ``services/llm`` (next to ``LLMCallTimeout``) so
callers import the whole failure taxonomy from one place; they are re-exported
here for convenience.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from sreda.config.settings import Settings, get_settings
from sreda.services.llm_guard_errors import LLMBulkheadFull, LLMCircuitOpen

__all__ = [
    "LLMBulkheadFull",
    "LLMCircuitOpen",
    "read_guard_config",
    "get_bulkhead",
    "get_breaker",
    "reset_guard_state",
]


# ---------------------------------------------------------------------------
# Config snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GuardConfig:
    enabled: bool
    max_concurrent: int
    breaker_open_after: int
    breaker_cooldown: float


def read_guard_config(settings: Settings | None = None) -> _GuardConfig:
    """Snapshot the guard limits from settings.

    Read fresh at each guard entry so an env change (after restart) or a test
    monkeypatch takes effect without rebuilding singletons. Tests typically
    monkeypatch this function to inject a tight config.
    """
    s = settings or get_settings()
    return _GuardConfig(
        enabled=s.llm_reliability_guard_enabled,
        max_concurrent=s.llm_bulkhead_max_concurrent,
        breaker_open_after=s.llm_breaker_open_after_timeouts,
        breaker_cooldown=s.llm_breaker_cooldown_seconds,
    )


# ---------------------------------------------------------------------------
# Bulkhead — bounded concurrency
# ---------------------------------------------------------------------------


class _Bulkhead:
    """Counter-based semaphore. The limit is passed per ``try_acquire`` so it
    can be re-read from config each call instead of baked into a fixed-size
    ``threading.Semaphore`` (simpler to reconfigure and to test)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0

    def try_acquire(self, limit: int) -> bool:
        with self._lock:
            if self._in_flight >= limit:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def _reset(self) -> None:
        with self._lock:
            self._in_flight = 0


# ---------------------------------------------------------------------------
# Circuit breaker — per provider
# ---------------------------------------------------------------------------


class _CircuitBreaker:
    """Three-state breaker (closed → open → half-open).

    - ``closed``: ``allow`` returns True; consecutive timeouts accumulate.
    - ``open``: ``allow`` returns False until ``cooldown`` elapses.
    - ``half-open``: after cooldown, ``allow`` admits exactly ONE probe; while
      that probe is outstanding further calls are refused. A success closes the
      breaker; a timeout re-opens it (fresh cooldown).

    ``open_after`` / ``cooldown`` are passed per call so a config change takes
    effect immediately. ``clock`` is injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        provider: str | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self._lock = threading.Lock()
        self._clock = clock
        self._consecutive_timeouts = 0
        self._opened_at: float | None = None
        self._half_open_probe_in_flight = False

    def allow(self, *, open_after: int, cooldown: float) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True  # closed
            if self._clock() - self._opened_at < cooldown:
                return False  # still open → fast-fail
            # Cooldown elapsed → half-open: admit exactly one probe.
            if self._half_open_probe_in_flight:
                return False
            self._half_open_probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_timeouts = 0
            self._opened_at = None
            self._half_open_probe_in_flight = False

    def record_timeout(self, *, open_after: int) -> None:
        with self._lock:
            self._consecutive_timeouts += 1
            was_half_open_probe = self._half_open_probe_in_flight
            self._half_open_probe_in_flight = False
            if self._opened_at is not None:
                # Already open. A half-open probe that timed out re-arms the
                # cooldown from now; a stray late timeout while open likewise
                # keeps it open with a fresh window.
                if was_half_open_probe:
                    self._opened_at = self._clock()
                return
            if self._consecutive_timeouts >= open_after:
                self._opened_at = self._clock()

    def record_other_error(self) -> None:
        """A fast (non-timeout) error: neutral. It does not open the breaker
        (no hung thread to bound) and does not reset the timeout streak; it
        only clears an outstanding half-open probe so the next call may probe
        again."""
        with self._lock:
            self._half_open_probe_in_flight = False

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None


# ---------------------------------------------------------------------------
# Process-global singletons
# ---------------------------------------------------------------------------

_BULKHEAD = _Bulkhead()
_BREAKERS: dict[str, _CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()

# Key used when a caller does not know / pass the provider name. All such calls
# share one breaker bucket; per-provider callers (LlmCaller) get their own.
_DEFAULT_PROVIDER_KEY = "__unknown__"


def get_bulkhead() -> _Bulkhead:
    return _BULKHEAD


def get_breaker(provider: str | None) -> _CircuitBreaker:
    key = provider or _DEFAULT_PROVIDER_KEY
    with _BREAKERS_LOCK:
        br = _BREAKERS.get(key)
        if br is None:
            br = _CircuitBreaker(provider=provider)
            _BREAKERS[key] = br
        return br


def reset_guard_state() -> None:
    """Reset bulkhead counter and clear all breakers. For tests and for a
    clean slate; not used on the hot path."""
    _BULKHEAD._reset()
    with _BREAKERS_LOCK:
        _BREAKERS.clear()
