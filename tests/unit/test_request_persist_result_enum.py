"""R5 (M-R5-1): persist_request_envelope returns PersistResult enum.

Plan: plans/mellow-discovering-conway-final.md — Section 2.
Issue: #68.
"""
from __future__ import annotations

import asyncio

import pytest

from sreda.services.llm_trace import (
    PersistResult, persist_request_envelope, startup_writer, shutdown_drain,
)
from sreda.services import llm_trace as _llm_trace_module


@pytest.fixture(autouse=True)
def _reset_llm_trace_state():
    """Module-level state cleanup between tests (writer tasks/flags/dicts)."""
    _llm_trace_module._SHUTDOWN_FLAG = False
    _llm_trace_module._WRITER_TASK = None
    _llm_trace_module._WRITE_QUEUE = None
    _llm_trace_module._GC_TASK = None
    _llm_trace_module._MAIN_LOOP = None
    _llm_trace_module._TRACE_DATES.clear()
    _llm_trace_module._TRACE_SEQ.clear()
    _llm_trace_module._TRACE_LAST_USED.clear()
    _llm_trace_module._WRITER_READY.clear()
    yield
    _llm_trace_module._SHUTDOWN_FLAG = False


@pytest.fixture
def _enable_logging(monkeypatch):
    monkeypatch.setenv("SREDA_LLM_TRACE_LOGGING_ENABLED", "true")
    monkeypatch.delenv("SREDA_LLM_TRACE_REQUIRE_PERSIST", raising=False)
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _envelope() -> dict:
    return {
        "schema_version": 1, "phase": "request", "attempt": "primary",
        "trace_id": "trace_test", "run_id": "run_test", "iter": 0,
        "tenant_id": "t1", "user_id": "u1", "feature_key": "test",
        "request": {"messages": [], "tool_schemas": []},
    }


@pytest.mark.asyncio
async def test_written_when_logging_disabled_and_no_require(monkeypatch):
    """Feature off (logging=False, require=False) → no-op success WRITTEN."""
    monkeypatch.delenv("SREDA_LLM_TRACE_LOGGING_ENABLED", raising=False)
    monkeypatch.delenv("SREDA_LLM_TRACE_REQUIRE_PERSIST", raising=False)
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    result = await persist_request_envelope(_envelope())
    assert result == PersistResult.WRITTEN


@pytest.mark.asyncio
async def test_failed_when_misconfigured(monkeypatch):
    """R7 (M-R6-3): logging=False + require=True at runtime → FAILED.
    Defense-in-depth — validator catches at startup, runtime returns FAILED."""
    from sreda.config import settings as settings_module

    class _FakeSettings:
        llm_trace_logging_enabled = False
        llm_trace_require_persist = True

    # Patch get_settings IN llm_trace module (where it's actually called),
    # not just in settings_module — `from X import Y` creates local binding.
    monkeypatch.setattr(_llm_trace_module, "get_settings", lambda: _FakeSettings())
    result = await persist_request_envelope(_envelope())
    assert result == PersistResult.FAILED


@pytest.mark.asyncio
async def test_written_when_writer_ready(_enable_logging, tmp_path, monkeypatch):
    """Happy path: writer running → envelope persisted → WRITTEN."""
    monkeypatch.setattr(_llm_trace_module, "_TRACE_ROOT", tmp_path)
    await startup_writer()
    try:
        result = await persist_request_envelope(_envelope())
        assert result == PersistResult.WRITTEN
    finally:
        await shutdown_drain(timeout_seconds=2.0)


@pytest.mark.asyncio
async def test_dropped_when_shutdown(_enable_logging, tmp_path, monkeypatch):
    """SHUTDOWN_FLAG set → DROPPED."""
    monkeypatch.setattr(_llm_trace_module, "_TRACE_ROOT", tmp_path)
    monkeypatch.setattr(_llm_trace_module, "_SHUTDOWN_FLAG", True)
    result = await persist_request_envelope(_envelope())
    assert result == PersistResult.DROPPED
