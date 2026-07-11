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


class _BreakerAttempt:
    """Opaque token returned by ``_CircuitBreaker.allow`` and passed back to the
    ``record_*`` outcome methods.

    Ties an outcome to the exact call the breaker admitted (Codex R1 MAJOR).
    Without it, a call admitted while the breaker was closed could later report
    ``success`` and wrongly close an already-open breaker, or a stale error
    could release another call's half-open probe slot. ``token`` identifies the
    single admitted half-open probe; only its owner may close / re-open / release
    that probe slot.
    """

    __slots__ = ("is_probe", "token")

    def __init__(self, *, is_probe: bool, token: object | None) -> None:
        self.is_probe = is_probe
        self.token = token


class _CircuitBreaker:
    """Three-state breaker (closed → open → half-open), attempt-token guarded.

    - ``closed``: ``allow`` admits every call (non-probe attempt); consecutive
      wall-clock timeouts accumulate; a success resets the streak.
    - ``open``: ``allow`` returns ``None`` (fast-fail) until ``cooldown`` elapses.
    - ``half-open``: after cooldown, ``allow`` admits exactly ONE probe (a unique
      ``token``); further calls are refused while it is outstanding. Only the
      probe owner may close (on success), re-open (on timeout), or release the
      slot (on a fast error). A non-probe outcome NEVER closes an open breaker.

    ``open_after`` / ``cooldown`` are passed per call so a config change takes
    effect immediately. ``clock`` is injectable for deterministic tests.

    Semantics note (Codex R1 MINOR): ``_consecutive_timeouts`` counts timeouts
    **since the last success**, not strictly back-to-back outcomes — a fast
    (non-timeout) error is neutral (the provider responded and the worker
    finished; nothing hung to bound), so it neither counts nor resets the streak.
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
        # Identity of the single outstanding half-open probe (None = none).
        self._probe_token: object | None = None

    def allow(self, *, cooldown: float) -> _BreakerAttempt | None:
        """Admit a call. Returns an attempt token, or ``None`` to fast-fail."""
        with self._lock:
            if self._opened_at is None:
                return _BreakerAttempt(is_probe=False, token=None)  # closed
            if self._clock() - self._opened_at < cooldown:
                return None  # still open → fast-fail
            if self._probe_token is not None:
                return None  # a probe is already outstanding
            # Cooldown elapsed → half-open: admit exactly one probe.
            token = object()
            self._probe_token = token
            return _BreakerAttempt(is_probe=True, token=token)

    def _owns_probe(self, attempt: _BreakerAttempt) -> bool:
        return (
            attempt.is_probe
            and attempt.token is not None
            and attempt.token is self._probe_token
        )

    def record_success(self, attempt: _BreakerAttempt) -> None:
        with self._lock:
            if self._owns_probe(attempt):
                # Probe succeeded → close the breaker.
                self._opened_at = None
                self._probe_token = None
                self._consecutive_timeouts = 0
            elif not attempt.is_probe and self._opened_at is None:
                # Healthy response while closed → reset the timeout streak. A
                # non-probe success NEVER closes an open breaker (stale-success
                # guard).
                self._consecutive_timeouts = 0
            # else: stale / non-owning outcome → ignore.

    def record_timeout(self, attempt: _BreakerAttempt, *, open_after: int) -> None:
        with self._lock:
            if self._owns_probe(attempt):
                # Probe hung → re-open with a fresh cooldown window.
                self._opened_at = self._clock()
                self._probe_token = None
                self._consecutive_timeouts += 1
            elif not attempt.is_probe and self._opened_at is None:
                self._consecutive_timeouts += 1
                if self._consecutive_timeouts >= open_after:
                    self._opened_at = self._clock()
            # else: stale, or a non-probe timeout while already open → ignore.

    def record_other_error(self, attempt: _BreakerAttempt) -> None:
        """A fast (non-timeout) error or a cancellation. Neutral to the breaker
        (nothing hung to bound). Only the probe owner acts: it releases the
        half-open slot WITHOUT closing (the probe did not prove the provider
        healthy) so the next call may probe again after cooldown."""
        with self._lock:
            if self._owns_probe(attempt):
                self._probe_token = None
            # non-probe fast error: neutral (no state change).

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
