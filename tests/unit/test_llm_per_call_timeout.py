"""Tests for invoke_with_per_call_timeout — внешний wall-clock timeout
на LLM invoke через ThreadPoolExecutor.

Контекст (2026-04-28 incident tg_634496616): MiMo отвечал через 131s
несмотря на `ChatOpenAI(timeout=60)`. Без явного timeout fallback
chain `.with_fallbacks([grok])` НЕ сработал — нет exception'а.
Helper кидает LLMCallTimeout (TimeoutError) → langchain ловит и
переключается на fallback.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from langchain_core.messages import AIMessageChunk

from sreda.services.llm import (
    LLMCallTimeout,
    ainvoke_with_streaming_timeout,
    invoke_with_per_call_timeout,
)


class _SlowRunnable:
    """Mock runnable который спит N секунд перед возвратом результата."""

    def __init__(self, sleep_s: float, result: object = "ok"):
        self.sleep_s = sleep_s
        self.result = result

    def invoke(self, _messages):  # langchain Runnable protocol
        time.sleep(self.sleep_s)
        return self.result


class _FastRunnable:
    def invoke(self, _messages):
        return "fast-result"


class _RaisingRunnable:
    def __init__(self, exc: Exception):
        self.exc = exc

    def invoke(self, _messages):
        raise self.exc


class _StreamingRunnable:
    def stream(self, _messages):
        yield AIMessageChunk(content="При")
        yield AIMessageChunk(content="вет")

    def invoke(self, _messages):
        raise AssertionError("streaming path should not call invoke")


class _SlowStreamingRunnable:
    def stream(self, _messages):
        yield AIMessageChunk(content="Поч")
        time.sleep(3.0)
        yield AIMessageChunk(content="ти готово")


def test_returns_immediately_on_fast_runnable():
    result = invoke_with_per_call_timeout(
        _FastRunnable(), [], timeout_seconds=5.0
    )
    assert result == "fast-result"


def test_raises_llm_call_timeout_on_slow_runnable():
    """Slow runnable (3s sleep) с timeout=0.5 → LLMCallTimeout."""
    runnable = _SlowRunnable(sleep_s=3.0)
    start = time.monotonic()
    with pytest.raises(LLMCallTimeout) as exc_info:
        invoke_with_per_call_timeout(runnable, [], timeout_seconds=0.5)
    elapsed = time.monotonic() - start
    # Timeout должен сработать ~0.5s, не ждать полные 3s
    assert elapsed < 2.0
    assert "0.5" in str(exc_info.value) or "exceeded" in str(exc_info.value)


def test_llm_call_timeout_is_subclass_of_timeout_error():
    """Critical: langchain RunnableWithFallbacks ловит TimeoutError
    и переключается на fallback. Иерархия должна быть правильной."""
    assert issubclass(LLMCallTimeout, TimeoutError)
    assert issubclass(LLMCallTimeout, Exception)


def test_propagates_non_timeout_exceptions():
    """Если runnable raises ValueError — пробрасываем как есть, не
    превращаем в LLMCallTimeout."""
    runnable = _RaisingRunnable(ValueError("upstream error"))
    with pytest.raises(ValueError, match="upstream error"):
        invoke_with_per_call_timeout(runnable, [], timeout_seconds=5.0)


def test_default_timeout_is_60s():
    """Sanity: значение по умолчанию = 60s (matches mimo_request_timeout_seconds)."""
    from sreda.services.llm import _PER_CALL_TIMEOUT_DEFAULT

    assert _PER_CALL_TIMEOUT_DEFAULT == 60.0


def test_with_messages_passed_through():
    """Helper должен пробрасывать messages в runnable.invoke без изменений."""
    runnable = MagicMock()
    runnable.invoke.return_value = "result"
    messages = [{"role": "system", "content": "hi"}]
    result = invoke_with_per_call_timeout(
        runnable, messages, timeout_seconds=5.0
    )
    assert result == "result"
    runnable.invoke.assert_called_once_with(messages)


def test_fallback_chain_simulation():
    """Имитируем cценарий с langchain `.with_fallbacks([fb])`:
    primary timeout → fallback runs successfully.

    Это не реальный langchain тест (он заплатает запросы), но
    проверяет что наш TimeoutError совместим с fallback ловлей.
    """
    primary = _SlowRunnable(sleep_s=3.0)
    fallback = _FastRunnable()

    # Симуляция RunnableWithFallbacks логики:
    try:
        result = invoke_with_per_call_timeout(
            primary, [], timeout_seconds=0.3
        )
    except TimeoutError:
        # Fallback берёт верх
        result = invoke_with_per_call_timeout(
            fallback, [], timeout_seconds=5.0
        )

    assert result == "fast-result"


def test_streaming_helper_emits_incremental_text_and_returns_final_message():
    updates: list[str] = []

    result = asyncio.run(
        ainvoke_with_streaming_timeout(
            _StreamingRunnable(),
            [],
            timeout_seconds=5.0,
            on_text_update=updates.append,
        )
    )

    assert result.content == "Привет"
    assert updates == ["При", "Привет"]


def test_streaming_helper_times_out_mid_stream():
    updates: list[str] = []
    start = time.monotonic()

    with pytest.raises(LLMCallTimeout):
        asyncio.run(
            ainvoke_with_streaming_timeout(
                _SlowStreamingRunnable(),
                [],
                timeout_seconds=0.3,
                on_text_update=updates.append,
            )
        )

    assert time.monotonic() - start < 2.0
    assert updates == ["Поч"]
