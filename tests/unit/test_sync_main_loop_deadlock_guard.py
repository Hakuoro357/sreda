"""R5 (M-R5-4): persist_envelope_sync deadlock guard on main loop.

Plan: plans/mellow-discovering-conway-final.md — Section 2.
Issue: #68.

persist_envelope_sync uses asyncio.run_coroutine_threadsafe(coro, _MAIN_LOOP).result()
to submit work. If caller is running ON _MAIN_LOOP — это blocks ON ITS OWN LOOP
= deadlock forever.

Guard: detect asyncio.get_running_loop() is _MAIN_LOOP → fail-fast log.error
и return без write. Footgun protection для mixed async/sync code в FastAPI app.
"""
from __future__ import annotations

import asyncio
import logging

import pytest


@pytest.fixture
def _enable_logging(monkeypatch):
    monkeypatch.setenv("SREDA_LLM_TRACE_LOGGING_ENABLED", "true")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _trace_root(monkeypatch, tmp_path):
    from sreda.services import llm_trace
    monkeypatch.setattr(llm_trace, "_TRACE_ROOT", tmp_path)
    yield tmp_path


def _envelope() -> dict:
    return {
        "schema_version": 1, "phase": "request", "attempt": "primary",
        "trace_id": "trace_sync_test", "run_id": "r", "iter": 0,
        "tenant_id": "t", "user_id": "u", "feature_key": "f",
        "request": {"messages": [], "tool_schemas": []},
    }


@pytest.mark.asyncio
async def test_sync_from_main_loop_logs_error_and_returns(
    _enable_logging, _trace_root, caplog,
):
    """call from main loop → log.error + no write (deadlock prevented)."""
    from sreda.services import llm_trace
    llm_trace._SHUTDOWN_FLAG = False
    llm_trace._WRITER_TASK = None
    llm_trace._WRITER_READY.clear()
    await llm_trace.startup_writer()
    try:
        with caplog.at_level(logging.ERROR, logger="sreda.services.llm_trace"):
            llm_trace.persist_envelope_sync(_envelope())
        # Должен log.error содержать "main loop"
        assert any("main loop" in r.message for r in caplog.records)
        # И НЕ должно ничего быть записано в файл
        files = list(_trace_root.rglob("trace_sync_test.jsonl"))
        assert files == []
    finally:
        await llm_trace.shutdown_drain(timeout_seconds=2.0)


def test_sync_from_no_running_loop_works(_enable_logging, _trace_root):
    """Call from sync context (no running loop) — submits через
    run_coroutine_threadsafe в _MAIN_LOOP from another thread."""
    import threading
    from sreda.services import llm_trace

    # Setup: запускаем _MAIN_LOOP в отдельном thread'е и startup_writer there
    loop = asyncio.new_event_loop()
    llm_trace._SHUTDOWN_FLAG = False
    llm_trace._WRITER_TASK = None
    llm_trace._WRITER_READY.clear()

    ready_event = threading.Event()

    def _loop_runner():
        asyncio.set_event_loop(loop)
        async def _bootstrap():
            await llm_trace.startup_writer()
            ready_event.set()
            # Keep loop running
            while not llm_trace._SHUTDOWN_FLAG:
                await asyncio.sleep(0.05)
            await llm_trace.shutdown_drain(timeout_seconds=2.0)
        loop.run_until_complete(_bootstrap())

    t = threading.Thread(target=_loop_runner, daemon=True)
    t.start()
    try:
        ready_event.wait(timeout=5.0)

        # Sync call from THIS thread — no running loop here → safe path
        llm_trace.persist_envelope_sync(_envelope())

        # Wait for write
        import time
        time.sleep(0.5)
        files = list(_trace_root.rglob("trace_sync_test.jsonl"))
        assert len(files) == 1
    finally:
        llm_trace._SHUTDOWN_FLAG = True
        t.join(timeout=5.0)
        loop.close()
