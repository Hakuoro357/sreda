"""F7 (#343, audit #336 MAJOR) — LLM reliability guard: bulkhead + circuit
breaker + explicit finite httpx timeouts on the chat-LLM invoke path.

Failure mode (validated `plans/current-main-critical-audit-validation-r1.md`
§2 F7): each LLM call runs on a per-call ``ThreadPoolExecutor``; the
wall-clock timeout frees the *turn* but cannot kill the worker thread. A
provider that accepts the connection and hangs leaves the thread + socket
alive; a run of hangs (or a fallback duplicating the call over a still-hung
primary) accumulates threads/sockets until the process is exhausted. Under
the wide-launch vector (alpha 200-300) the "acceptable at 10 users" comment
(`llm.py:55-58`) is void.

Direction (§2 F7 / §5 test-matrix): finite-limit bulkhead + per-provider
circuit breaker + explicit connect/read/write/pool httpx timeouts; verify the
fallback does not double the uncontrolled load. NOT in this slice: replacing
the ThreadPoolExecutor with cancellable async HTTP.

Test names for the machine-checkable acceptance items are kept verbatim from
the checklist (ПРАВИЛО #7).
"""

from __future__ import annotations

import math
import threading
import time

import httpx
import pytest

from sreda.config.settings import get_settings
from sreda.services import llm_bulkhead
from sreda.services.llm import (
    LLMBulkheadFull,
    LLMCallTimeout,
    LLMCircuitOpen,
    _build_request_timeout,
    _build_chat_llm,
    ainvoke_with_streaming_timeout,
    invoke_with_per_call_timeout,
)
from langchain_core.messages import AIMessageChunk


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FastRunnable:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return "fast-result"


class _SpyRunnable:
    """Records whether ``invoke`` was ever entered (to prove fast-fail spawns
    no worker thread)."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return "spy-result"


class _BlockingRunnable:
    """``invoke`` blocks on a shared Event → simulates a hung provider whose
    worker thread stays alive after the wall-clock timeout."""

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        # Safety cap so a mis-set test never wedges the suite.
        self._release.wait(timeout=30.0)
        return "unblocked"


class _BlockingStreamingRunnable:
    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def stream(self, _messages):
        self._release.wait(timeout=30.0)
        yield AIMessageChunk(content="late")

    def invoke(self, _messages):  # pragma: no cover - streaming path only
        raise AssertionError("streaming path should not call invoke")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_guard():
    llm_bulkhead.reset_guard_state()
    yield
    llm_bulkhead.reset_guard_state()


@pytest.fixture
def blocker():
    ev = threading.Event()
    yield ev
    ev.set()  # always release any hung worker thread on teardown


def _use_guard_config(monkeypatch, *, enabled, max_concurrent, open_after, cooldown):
    cfg = llm_bulkhead._GuardConfig(
        enabled=enabled,
        max_concurrent=max_concurrent,
        breaker_open_after=open_after,
        breaker_cooldown=cooldown,
    )
    monkeypatch.setattr(llm_bulkhead, "read_guard_config", lambda settings=None: cfg)


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Bulkhead primitive
# ---------------------------------------------------------------------------


def test_bulkhead_rejects_when_full():
    b = llm_bulkhead._Bulkhead()
    assert b.try_acquire(2) is True
    assert b.try_acquire(2) is True
    assert b.in_flight == 2
    assert b.try_acquire(2) is False  # full → fast-fail, no growth
    assert b.in_flight == 2


def test_bulkhead_release_frees_slot():
    b = llm_bulkhead._Bulkhead()
    assert b.try_acquire(1) is True
    assert b.try_acquire(1) is False
    b.release()
    assert b.in_flight == 0
    assert b.try_acquire(1) is True


# ---------------------------------------------------------------------------
# Circuit breaker primitive (injectable clock)
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_breaker_opens_after_consecutive_timeouts():
    clock = _FakeClock()
    br = llm_bulkhead._CircuitBreaker(provider="mimo", clock=clock)
    assert br.allow(open_after=3, cooldown=30.0) is True
    br.record_timeout(open_after=3)
    br.record_timeout(open_after=3)
    assert br.allow(open_after=3, cooldown=30.0) is True  # not yet at threshold
    br.record_timeout(open_after=3)
    assert br.is_open is True
    assert br.allow(open_after=3, cooldown=30.0) is False  # open → fast-fail


def test_breaker_half_open_after_cooldown_then_closes_on_success():
    clock = _FakeClock()
    br = llm_bulkhead._CircuitBreaker(provider="mimo", clock=clock)
    for _ in range(2):
        br.record_timeout(open_after=2)
    assert br.allow(open_after=2, cooldown=30.0) is False  # open
    clock.advance(31.0)
    assert br.allow(open_after=2, cooldown=30.0) is True  # half-open: admit ONE probe
    assert br.allow(open_after=2, cooldown=30.0) is False  # second probe blocked
    br.record_success()
    assert br.is_open is False
    assert br.allow(open_after=2, cooldown=30.0) is True  # closed again


def test_breaker_reopens_when_half_open_probe_times_out():
    clock = _FakeClock()
    br = llm_bulkhead._CircuitBreaker(provider="mimo", clock=clock)
    for _ in range(2):
        br.record_timeout(open_after=2)
    clock.advance(31.0)
    assert br.allow(open_after=2, cooldown=30.0) is True  # half-open probe
    br.record_timeout(open_after=2)  # probe hung too
    assert br.allow(open_after=2, cooldown=30.0) is False  # re-opened immediately
    clock.advance(31.0)
    assert br.allow(open_after=2, cooldown=30.0) is True  # cooldown again


def test_breaker_success_resets_timeout_streak():
    clock = _FakeClock()
    br = llm_bulkhead._CircuitBreaker(provider="mimo", clock=clock)
    br.record_timeout(open_after=3)
    br.record_timeout(open_after=3)
    br.record_success()  # resets streak
    br.record_timeout(open_after=3)
    br.record_timeout(open_after=3)
    assert br.is_open is False  # streak restarted → not open at 2


def test_breaker_other_error_is_neutral():
    """Fast provider errors (5xx/429) return promptly — no hung thread — so
    they must NOT trip the breaker (only wall-clock timeouts do)."""
    clock = _FakeClock()
    br = llm_bulkhead._CircuitBreaker(provider="mimo", clock=clock)
    for _ in range(10):
        br.record_other_error()
    assert br.is_open is False
    assert br.allow(open_after=2, cooldown=30.0) is True


# ---------------------------------------------------------------------------
# Integration through the invoke wrappers (acceptance-checklist names)
# ---------------------------------------------------------------------------


def test_hung_llm_call_bounded_by_semaphore(monkeypatch, blocker):
    """N hung calls beyond the limit → new calls fast-fail (LLMBulkheadFull)
    WITHOUT spawning a new worker thread; in-flight count stays bounded."""
    _use_guard_config(monkeypatch, enabled=True, max_concurrent=1, open_after=1000, cooldown=999.0)

    hung = _BlockingRunnable(blocker)
    first_result: dict = {}

    def _first():
        try:
            invoke_with_per_call_timeout(hung, [], timeout_seconds=0.3, provider="mimo")
        except BaseException as exc:  # noqa: BLE001
            first_result["exc"] = exc

    t = threading.Thread(target=_first, daemon=True)
    t.start()
    # The permit is taken synchronously before submit → in_flight becomes 1.
    assert _wait_until(lambda: llm_bulkhead.get_bulkhead().in_flight == 1)

    # Second call: bulkhead full → fast-fail, no worker thread created.
    spy = _SpyRunnable()
    with pytest.raises(LLMBulkheadFull):
        invoke_with_per_call_timeout(spy, [], timeout_seconds=1.0, provider="mimo")
    assert spy.calls == 0
    assert llm_bulkhead.get_bulkhead().in_flight == 1  # still just the hung one

    blocker.set()
    t.join(5.0)
    # After the hung thread completes, the permit is released.
    assert _wait_until(lambda: llm_bulkhead.get_bulkhead().in_flight == 0)


def test_circuit_breaker_opens_on_repeated_timeouts(monkeypatch, blocker):
    """A series of wall-clock timeouts opens the breaker; subsequent calls to
    that provider fast-fail (LLMCircuitOpen) without invoking the model."""
    _use_guard_config(monkeypatch, enabled=True, max_concurrent=100, open_after=2, cooldown=999.0)

    for _ in range(2):
        with pytest.raises(LLMCallTimeout):
            invoke_with_per_call_timeout(
                _BlockingRunnable(blocker), [], timeout_seconds=0.2, provider="mimo"
            )

    spy = _SpyRunnable()
    with pytest.raises(LLMCircuitOpen):
        invoke_with_per_call_timeout(spy, [], timeout_seconds=1.0, provider="mimo")
    assert spy.calls == 0  # provider never touched while breaker is open

    # A different provider is unaffected (per-provider breaker).
    fast = _FastRunnable()
    assert invoke_with_per_call_timeout(fast, [], timeout_seconds=1.0, provider="osa") == "fast-result"

    blocker.set()


def test_timeout_still_frees_current_turn(monkeypatch, blocker):
    """Regression: with the guard enabled, a hung provider still yields the
    turn promptly via the wall-clock timeout (does not block on the thread)."""
    _use_guard_config(monkeypatch, enabled=True, max_concurrent=100, open_after=1000, cooldown=999.0)

    start = time.monotonic()
    with pytest.raises(LLMCallTimeout):
        invoke_with_per_call_timeout(
            _BlockingRunnable(blocker), [], timeout_seconds=0.3, provider="mimo"
        )
    assert time.monotonic() - start < 2.0
    blocker.set()


def test_fallback_does_not_double_load(monkeypatch, blocker):
    """When the primary is hung (holding a permit), a fallback invoke shares
    the same finite bulkhead: at max_concurrent=1 it fast-fails rather than
    spawning a SECOND uncontrolled hung thread. Mirrors LlmCaller's
    primary→fallback path (runtime/llm_caller.py)."""
    _use_guard_config(monkeypatch, enabled=True, max_concurrent=1, open_after=1000, cooldown=999.0)

    hung_primary = _BlockingRunnable(blocker)
    hold: dict = {}

    def _primary():
        try:
            invoke_with_per_call_timeout(hung_primary, [], timeout_seconds=0.3, provider="mimo")
        except BaseException as exc:  # noqa: BLE001
            hold["exc"] = exc

    t = threading.Thread(target=_primary, daemon=True)
    t.start()
    assert _wait_until(lambda: llm_bulkhead.get_bulkhead().in_flight == 1)

    fallback = _SpyRunnable()
    with pytest.raises(LLMBulkheadFull):
        invoke_with_per_call_timeout(fallback, [], timeout_seconds=1.0, provider="osa")
    assert fallback.calls == 0
    assert llm_bulkhead.get_bulkhead().in_flight == 1  # not 2

    blocker.set()
    t.join(5.0)


def test_streaming_path_enforces_bulkhead(monkeypatch, blocker):
    """The async streaming wrapper (used by the ReAct hot path via LlmCaller)
    also enforces the bulkhead: a hung streaming primary holds the permit and
    a second streaming call fast-fails."""
    _use_guard_config(monkeypatch, enabled=True, max_concurrent=1, open_after=1000, cooldown=999.0)

    import asyncio

    async def _run_hung():
        with pytest.raises(LLMCallTimeout):
            await ainvoke_with_streaming_timeout(
                _BlockingStreamingRunnable(blocker),
                [],
                timeout_seconds=0.3,
                on_text_update=lambda _t: None,
                provider="mimo",
            )

    asyncio.run(_run_hung())
    # The streaming worker is still hung on the event → permit held.
    assert llm_bulkhead.get_bulkhead().in_flight == 1

    async def _run_second():
        with pytest.raises(LLMBulkheadFull):
            await ainvoke_with_streaming_timeout(
                _BlockingStreamingRunnable(blocker),
                [],
                timeout_seconds=1.0,
                on_text_update=lambda _t: None,
                provider="mimo",
            )

    asyncio.run(_run_second())
    blocker.set()


def test_guard_disabled_is_legacy_passthrough(monkeypatch, blocker):
    """Master kill-switch off → no bulkhead, no breaker (legacy behaviour)."""
    _use_guard_config(monkeypatch, enabled=False, max_concurrent=1, open_after=1, cooldown=999.0)

    fast = _FastRunnable()
    assert invoke_with_per_call_timeout(fast, [], timeout_seconds=1.0) == "fast-result"
    assert llm_bulkhead.get_bulkhead().in_flight == 0  # guard not tracking

    for _ in range(3):
        with pytest.raises(LLMCallTimeout):
            invoke_with_per_call_timeout(
                _BlockingRunnable(blocker), [], timeout_seconds=0.2, provider="mimo"
            )
    # Breaker never opened while disabled → a fast call still runs.
    assert invoke_with_per_call_timeout(
        _FastRunnable(), [], timeout_seconds=1.0, provider="mimo"
    ) == "fast-result"
    blocker.set()


# ---------------------------------------------------------------------------
# Explicit finite httpx timeouts
# ---------------------------------------------------------------------------


def test_explicit_httpx_timeouts_are_finite():
    """connect/read/write/pool are all set to finite values (not the
    unbounded default) — §5 F7 acceptance."""
    t = _build_request_timeout(get_settings())
    assert isinstance(t, httpx.Timeout)
    for field in (t.connect, t.read, t.write, t.pool):
        assert field is not None
        assert 0 < float(field) < math.inf


def test_build_chat_llm_passes_explicit_httpx_timeout():
    """The constructed ChatOpenAI carries the explicit httpx.Timeout, not a
    bare float, so per-phase timeouts actually reach httpx."""
    s = get_settings().model_copy(update={"mimo_api_key": "test-key"})
    llm = _build_chat_llm("mimo", s, model="mimo-v2-pro", temperature=0.3)
    assert llm is not None
    assert isinstance(llm.request_timeout, httpx.Timeout)
    t = llm.request_timeout
    for field in (t.connect, t.read, t.write, t.pool):
        assert field is not None
        assert 0 < float(field) < math.inf


def test_guard_config_defaults_are_finite():
    """Default guard config from settings is finite on every limit — a
    misconfigured prod cannot silently run unbounded."""
    cfg = llm_bulkhead.read_guard_config(get_settings())
    assert cfg.max_concurrent >= 1 and cfg.max_concurrent < 100000
    assert cfg.breaker_open_after >= 1
    assert 0 < cfg.breaker_cooldown < math.inf
