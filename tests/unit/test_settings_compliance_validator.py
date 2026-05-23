"""R7 (M-R6-3): Settings.model_validator catches misconfigured trace flags.

Plan: plans/mellow-discovering-conway-final.md — Section 7.
Issue: #68.

Compliance-strict mode requires `SREDA_LLM_TRACE_REQUIRE_PERSIST=true` to
imply `SREDA_LLM_TRACE_LOGGING_ENABLED=true`. Otherwise misconfig would
silently fail-open. Validator must raise ValueError at Settings construction
→ fail loud in FastAPI startup.
"""
from __future__ import annotations

import pytest

from sreda.config.settings import Settings


def test_require_persist_without_logging_raises(monkeypatch):
    """Misconfig: require_persist=True + logging_enabled=False → ValueError."""
    monkeypatch.setenv("SREDA_LLM_TRACE_LOGGING_ENABLED", "false")
    monkeypatch.setenv("SREDA_LLM_TRACE_REQUIRE_PERSIST", "true")
    with pytest.raises(ValueError, match="SREDA_LLM_TRACE_REQUIRE_PERSIST"):
        Settings()


def test_require_persist_with_logging_ok(monkeypatch):
    """require_persist=True + logging_enabled=True → valid config."""
    monkeypatch.setenv("SREDA_LLM_TRACE_LOGGING_ENABLED", "true")
    monkeypatch.setenv("SREDA_LLM_TRACE_REQUIRE_PERSIST", "true")
    s = Settings()
    assert s.llm_trace_logging_enabled is True
    assert s.llm_trace_require_persist is True


def test_logging_only_ok(monkeypatch):
    """logging_enabled=True + require_persist=False → valid (default fail-open)."""
    monkeypatch.setenv("SREDA_LLM_TRACE_LOGGING_ENABLED", "true")
    monkeypatch.setenv("SREDA_LLM_TRACE_REQUIRE_PERSIST", "false")
    s = Settings()
    assert s.llm_trace_logging_enabled is True
    assert s.llm_trace_require_persist is False


def test_both_disabled_ok(monkeypatch):
    """Both False — feature off entirely, no validation issue."""
    monkeypatch.setenv("SREDA_LLM_TRACE_LOGGING_ENABLED", "false")
    monkeypatch.setenv("SREDA_LLM_TRACE_REQUIRE_PERSIST", "false")
    s = Settings()
    assert s.llm_trace_logging_enabled is False
    assert s.llm_trace_require_persist is False


def test_defaults_safe(monkeypatch):
    """Settings() без flags — feature off (default), no validation error.

    Strip the trace env vars explicitly so dev shell с настроенными trace
    флагами не делает тест order-dependent.
    """
    monkeypatch.delenv("SREDA_LLM_TRACE_LOGGING_ENABLED", raising=False)
    monkeypatch.delenv("SREDA_LLM_TRACE_REQUIRE_PERSIST", raising=False)
    s = Settings()
    assert s.llm_trace_logging_enabled is False
    assert s.llm_trace_require_persist is False
