"""R7 (M-R6-3): defensive runtime check для misconfigured trace flags.

Plan: plans/mellow-discovering-conway-final.md — Section 2.
Issue: #68.

Settings model_validator catches `require_persist=True + logging=False`
at startup — но если settings были mutated post-construction (test patches,
hot-reload бэкдор), defensive runtime check в persist_request_envelope
returns FAILED, не WRITTEN.
"""
from __future__ import annotations

import pytest

from sreda.services.llm_trace import PersistResult, persist_request_envelope
from sreda.services import llm_trace as _llm_trace_module


@pytest.fixture(autouse=True)
def _reset_llm_trace_state():
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


def _envelope() -> dict:
    return {
        "schema_version": 1, "phase": "request", "attempt": "primary",
        "trace_id": "trace_misconfig", "run_id": "r", "iter": 0,
        "tenant_id": "t", "user_id": "u", "feature_key": "f",
        "request": {"messages": [], "tool_schemas": []},
    }


@pytest.mark.asyncio
async def test_runtime_failed_when_require_persist_without_logging(monkeypatch):
    """logging_enabled=False AND require_persist=True at runtime → FAILED."""
    class _FakeSettings:
        llm_trace_logging_enabled = False
        llm_trace_require_persist = True

    # Patch IN llm_trace module (where it's imported into local namespace)
    monkeypatch.setattr(_llm_trace_module, "get_settings", lambda: _FakeSettings())
    result = await persist_request_envelope(_envelope())
    assert result == PersistResult.FAILED


@pytest.mark.asyncio
async def test_runtime_written_when_disabled_without_require(monkeypatch):
    """logging_enabled=False + require_persist=False → WRITTEN (legitimate no-op)."""
    class _FakeSettings:
        llm_trace_logging_enabled = False
        llm_trace_require_persist = False

    monkeypatch.setattr(_llm_trace_module, "get_settings", lambda: _FakeSettings())
    result = await persist_request_envelope(_envelope())
    assert result == PersistResult.WRITTEN
