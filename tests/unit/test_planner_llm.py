"""Unit tests for runtime/planner/llm.py — Sub-A12 Phase B.2."""

from __future__ import annotations

from typing import Any

import pytest

from sreda.config.settings import Settings
from sreda.runtime.planner.llm import (
    PlannerCallResult,
    PlannerProviderUnavailable,
    PlannerTimeoutError,
    call_planner,
)
from sreda.services.llm import LLMCallTimeout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings(**overrides: Any) -> Settings:
    """Build a Settings instance with planner-relevant overrides.

    Settings fields use ``validation_alias=AliasChoices('SREDA_FOO',
    'sreda_foo')``; pydantic-settings won't accept the field name
    directly (no populate_by_name). Map kwargs to lowercase aliases."""
    alias_map = {
        "planner_provider": "sreda_planner_provider",
        "planner_timeout_sec": "sreda_planner_timeout_sec",
    }
    payload = {
        "sreda_planner_provider": "mimo-v2.5",
        "sreda_planner_timeout_sec": 60.0,
    }
    for k, v in overrides.items():
        payload[alias_map.get(k, k)] = v
    return Settings(**payload)


class _FakeAIMessage:
    """Stand-in for langchain_core AIMessage.content access."""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChatLLM:
    """Stand-in for the LangChain Runnable returned by get_chat_llm."""

    def __init__(self, model_name: str = "mimo-v2.5-pro") -> None:
        self.model_name = model_name


# ---------------------------------------------------------------------------
# Provider availability
# ---------------------------------------------------------------------------


def test_call_planner_raises_when_provider_unavailable() -> None:
    """``get_chat_llm`` may return None (no key, unknown provider).
    call_planner MUST surface that as PlannerProviderUnavailable so
    orchestrator can map to failure_kind='provider_error'."""

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return None

    with pytest.raises(PlannerProviderUnavailable):
        call_planner(
            "hello",
            settings_factory=lambda: _make_settings(),
            chat_llm_factory=fake_factory,
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_call_planner_returns_typed_result() -> None:
    """End-to-end through mocked LLM — typed PlannerCallResult emitted
    with raw_text, latency, model, provider, attempt_no fields."""

    fake_runnable = _FakeChatLLM(model_name="mimo-v2.5-pro")
    fake_response = _FakeAIMessage(content='{"clarity": "clear"}')

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return fake_runnable

    def fake_invoke(runnable: Any, messages: list, *, timeout_seconds: float):
        assert runnable is fake_runnable
        assert len(messages) == 1
        return fake_response

    # Monotonic clock — return 100 then 350 (250ms elapsed)
    ticks = iter([100, 350])

    result = call_planner(
        "build me a plan",
        provider="mimo-v2.5",
        timeout_seconds=30.0,
        attempt_no=1,
        settings_factory=lambda: _make_settings(),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: next(ticks),
    )

    assert isinstance(result, PlannerCallResult)
    assert result.raw_text == '{"clarity": "clear"}'
    assert result.latency_ms == 250
    assert result.provider == "mimo-v2.5"
    assert result.model == "mimo-v2.5-pro"
    assert result.attempt_no == 1
    assert result.parsed_plan is None  # parsing in orchestrator


def test_call_planner_uses_settings_provider_when_unspecified() -> None:
    """When provider arg is None, defaults to settings.planner_provider."""

    captured: dict[str, Any] = {}

    def fake_factory(*, settings: Any, provider: str | None = None, **kw: Any) -> Any:
        captured["provider"] = provider
        return _FakeChatLLM()

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        return _FakeAIMessage("ok")

    call_planner(
        "x",
        settings_factory=lambda: _make_settings(planner_provider="mimo-flash"),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    assert captured["provider"] == "mimo-flash"


def test_call_planner_uses_explicit_provider_override() -> None:
    captured: dict[str, Any] = {}

    def fake_factory(*, settings: Any, provider: str | None = None, **kw: Any) -> Any:
        captured["provider"] = provider
        return _FakeChatLLM()

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        return _FakeAIMessage("ok")

    call_planner(
        "x",
        provider="mimo-v2.5",
        settings_factory=lambda: _make_settings(planner_provider="mimo-flash"),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    assert captured["provider"] == "mimo-v2.5"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_call_planner_wraps_llm_timeout_as_planner_timeout() -> None:
    """LLMCallTimeout from the underlying invoke helper is re-raised
    as PlannerTimeoutError (subclass — preserves backwards-compat
    catch-paths)."""

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return _FakeChatLLM()

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        raise LLMCallTimeout("simulated wall-clock timeout")

    with pytest.raises(PlannerTimeoutError) as exc_info:
        call_planner(
            "x",
            settings_factory=lambda: _make_settings(planner_timeout_sec=5.0),
            chat_llm_factory=fake_factory,
            invoke=fake_invoke,
            now_ms=lambda: 0,
        )
    # Subclass relationship preserved — old code catching LLMCallTimeout
    # still matches.
    assert isinstance(exc_info.value, LLMCallTimeout)


def test_call_planner_passes_timeout_to_invoke() -> None:
    """settings.planner_timeout_sec is forwarded to invoke wrapper."""
    captured: dict[str, Any] = {}

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return _FakeChatLLM()

    def fake_invoke(runnable: Any, messages: list, *, timeout_seconds: float):
        captured["timeout_seconds"] = timeout_seconds
        return _FakeAIMessage("ok")

    call_planner(
        "x",
        settings_factory=lambda: _make_settings(planner_timeout_sec=12.5),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    assert captured["timeout_seconds"] == 12.5


def test_call_planner_explicit_timeout_overrides_settings() -> None:
    captured: dict[str, Any] = {}

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return _FakeChatLLM()

    def fake_invoke(runnable: Any, messages: list, *, timeout_seconds: float):
        captured["timeout_seconds"] = timeout_seconds
        return _FakeAIMessage("ok")

    call_planner(
        "x",
        timeout_seconds=3.0,
        settings_factory=lambda: _make_settings(planner_timeout_sec=60.0),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    assert captured["timeout_seconds"] == 3.0


# ---------------------------------------------------------------------------
# Non-timeout exceptions propagate
# ---------------------------------------------------------------------------


def test_call_planner_propagates_provider_errors() -> None:
    """Non-timeout exceptions from the LLM provider bubble up unchanged
    (orchestrator decides whether they're retryable)."""

    class ProviderRateLimit(Exception):
        pass

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return _FakeChatLLM()

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        raise ProviderRateLimit("429 from mimo")

    with pytest.raises(ProviderRateLimit, match="429"):
        call_planner(
            "x",
            settings_factory=lambda: _make_settings(),
            chat_llm_factory=fake_factory,
            invoke=fake_invoke,
            now_ms=lambda: 0,
        )


# ---------------------------------------------------------------------------
# Text extraction (multimodal-safe)
# ---------------------------------------------------------------------------


def test_call_planner_concatenates_list_content() -> None:
    """LangChain may return content as list[str | dict] for multimodal
    responses. call_planner concatenates text parts."""

    fake_response = _FakeAIMessage(content="should not see this")
    fake_response.content = ["hello ", {"type": "text", "text": "world"}, "!"]

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return _FakeChatLLM()

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        return fake_response

    result = call_planner(
        "x",
        settings_factory=lambda: _make_settings(),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    assert result.raw_text == "hello world!"


def test_call_planner_falls_back_to_str_on_unknown_response_shape() -> None:
    """Test stubs (dicts, plain objects) → ``str(response)`` so call
    never raises on unexpected response types."""

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return _FakeChatLLM()

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        return {"plain": "dict"}  # no .content

    result = call_planner(
        "x",
        settings_factory=lambda: _make_settings(),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    assert "plain" in result.raw_text


# ---------------------------------------------------------------------------
# Model name resolution
# ---------------------------------------------------------------------------


def test_call_planner_resolves_model_from_runnable_attr() -> None:
    """When runnable exposes ``.model_name``, that's the recorded model."""

    fake = _FakeChatLLM(model_name="custom-model-x")

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return fake

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        return _FakeAIMessage("ok")

    result = call_planner(
        "x",
        settings_factory=lambda: _make_settings(),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    assert result.model == "custom-model-x"


def test_call_planner_resolves_model_falls_back_to_provider() -> None:
    """When runnable has no model attribute AND provider isn't in the
    real registry maps, fall back to the provider key as last resort."""

    class OpaqueRunnable:
        pass  # no model attr

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return OpaqueRunnable()

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        return _FakeAIMessage("ok")

    result = call_planner(
        "x",
        provider="totally-unknown-provider",
        settings_factory=lambda: _make_settings(),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    # Not in either _MIMO_MODEL_BY_PROVIDER nor _OPENROUTER_MODEL_BY_PROVIDER
    # → returns provider key as final fallback.
    assert result.model == "totally-unknown-provider"


def test_call_planner_resolves_model_from_registry_for_opaque_runnable() -> None:
    """R2 fix verification: opaque runnable + known provider key
    ``"mimo-v2.5"`` resolves to the actual model literal
    ``"mimo-v2.5-pro"`` via ``_MIMO_MODEL_BY_PROVIDER``. Earlier code
    referenced non-existent ``_PROVIDER_TO_MODEL`` symbol so this path
    was dead and returned ``"mimo-v2.5"`` instead."""

    class OpaqueRunnable:
        pass  # no model attr

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return OpaqueRunnable()

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        return _FakeAIMessage("ok")

    result = call_planner(
        "x",
        provider="mimo-v2.5",
        settings_factory=lambda: _make_settings(),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    # _MIMO_MODEL_BY_PROVIDER["mimo-v2.5"] = "mimo-v2.5-pro"
    assert result.model == "mimo-v2.5-pro"


# ---------------------------------------------------------------------------
# Settings integration
# ---------------------------------------------------------------------------


def test_settings_has_planner_provider_default_mimo_v2_5() -> None:
    """Codex Sub-A12 R1 CRITICAL — default provider key must be
    ``mimo-v2.5`` (provider key, maps to model ``mimo-v2.5-pro``),
    not the model name itself. Verifies the R3 fix landed."""
    s = Settings()  # all defaults
    assert s.planner_provider == "mimo-v2.5"


def test_settings_has_planner_timeout_sec() -> None:
    """Field exists with reasonable default."""
    s = Settings()
    assert s.planner_timeout_sec == 60.0


def test_call_planner_attempt_no_echoed_in_result() -> None:
    """Orchestrator passes attempt_no=2 for retries — must round-trip."""

    def fake_factory(*a: Any, **kw: Any) -> Any:
        return _FakeChatLLM()

    def fake_invoke(*a: Any, **kw: Any) -> Any:
        return _FakeAIMessage("retry response")

    result = call_planner(
        "x",
        attempt_no=2,
        settings_factory=lambda: _make_settings(),
        chat_llm_factory=fake_factory,
        invoke=fake_invoke,
        now_ms=lambda: 0,
    )
    assert result.attempt_no == 2
