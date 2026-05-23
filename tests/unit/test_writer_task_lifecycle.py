"""R5 (M-R5-1): startup_writer + shutdown_drain lifecycle.

Plan: plans/mellow-discovering-conway-final.md — Section 6.
Issue: #68.

Writer task должен:
- Bootstrap при startup_writer() — create queue, spawn writer + GC tasks,
  set _WRITER_READY event
- Be idempotent — повторный startup_writer() = no-op
- Drain queue при shutdown_drain(timeout) — wait for queue.join() с timeout,
  cancel tasks, shutdown executor
"""
from __future__ import annotations

import asyncio

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


@pytest.mark.asyncio
async def test_startup_creates_queue_and_tasks(_enable_logging, _trace_root):
    from sreda.services import llm_trace
    # State до startup
    llm_trace._SHUTDOWN_FLAG = False
    llm_trace._WRITE_QUEUE = None
    llm_trace._WRITER_TASK = None
    llm_trace._GC_TASK = None
    llm_trace._WRITER_READY.clear()

    await llm_trace.startup_writer()
    try:
        assert llm_trace._WRITE_QUEUE is not None
        assert llm_trace._WRITER_TASK is not None
        assert not llm_trace._WRITER_TASK.done()
        assert llm_trace._GC_TASK is not None
        assert not llm_trace._GC_TASK.done()
        assert llm_trace._WRITER_READY.is_set()
    finally:
        await llm_trace.shutdown_drain(timeout_seconds=2.0)


@pytest.mark.asyncio
async def test_startup_idempotent(_enable_logging, _trace_root):
    """Повторный startup_writer без shutdown — no-op (не создаёт второго writer task)."""
    from sreda.services import llm_trace
    llm_trace._SHUTDOWN_FLAG = False
    llm_trace._WRITER_TASK = None
    llm_trace._WRITER_READY.clear()

    await llm_trace.startup_writer()
    task_id_first = id(llm_trace._WRITER_TASK)

    await llm_trace.startup_writer()  # second call
    task_id_second = id(llm_trace._WRITER_TASK)

    try:
        assert task_id_first == task_id_second
        assert llm_trace._WRITER_READY.is_set()
    finally:
        await llm_trace.shutdown_drain(timeout_seconds=2.0)


@pytest.mark.asyncio
async def test_shutdown_drain_completes_queued_envelopes(_enable_logging, _trace_root):
    """shutdown_drain ждёт queue.join() — все envelopes на диске."""
    from sreda.services import llm_trace
    llm_trace._SHUTDOWN_FLAG = False
    llm_trace._WRITER_TASK = None
    llm_trace._WRITER_READY.clear()

    await llm_trace.startup_writer()

    # submit несколько envelopes
    env = {
        "schema_version": 1, "phase": "response", "attempt": "primary",
        "trace_id": "trace_drain_test", "run_id": "run", "iter": 0,
        "tenant_id": "t", "user_id": "u", "feature_key": "f",
        "response": {"content": "ok"},
    }
    for i in range(5):
        env_copy = dict(env); env_copy["iter"] = i
        await llm_trace.persist_response_envelope(env_copy)

    await llm_trace.shutdown_drain(timeout_seconds=5.0)

    # все 5 envelopes должны быть в файле
    import json
    files = list(_trace_root.rglob("trace_drain_test.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    iters = sorted(json.loads(line)["iter"] for line in lines)
    assert iters == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_shutdown_cancels_writer_task(_enable_logging, _trace_root):
    from sreda.services import llm_trace
    llm_trace._SHUTDOWN_FLAG = False
    llm_trace._WRITER_TASK = None
    llm_trace._WRITER_READY.clear()

    await llm_trace.startup_writer()
    writer_task = llm_trace._WRITER_TASK
    gc_task = llm_trace._GC_TASK

    await llm_trace.shutdown_drain(timeout_seconds=2.0)

    assert writer_task.done()
    assert gc_task.done()
    assert llm_trace._SHUTDOWN_FLAG is True


@pytest.mark.asyncio
async def test_writer_ready_event_set_after_startup(_enable_logging, _trace_root):
    """_WRITER_READY event — sentinel что bootstrap complete."""
    from sreda.services import llm_trace
    llm_trace._SHUTDOWN_FLAG = False
    llm_trace._WRITER_TASK = None
    llm_trace._WRITER_READY.clear()

    assert not llm_trace._WRITER_READY.is_set()
    await llm_trace.startup_writer()
    try:
        assert llm_trace._WRITER_READY.is_set()
    finally:
        await llm_trace.shutdown_drain(timeout_seconds=2.0)
